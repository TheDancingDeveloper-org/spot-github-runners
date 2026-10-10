"""Stop and lock gha-spot VMs once actual spend reaches the budget."""
import base64
import json
import os

import functions_framework
from google.cloud import compute_v1

PROJECT = os.environ["GCP_PROJECT"]
ZONE = os.environ["GCP_ZONE"]


@functions_framework.cloud_event
def on_budget(event):
    msg = json.loads(base64.b64decode(event.data["message"]["data"]))
    cost, budget = float(msg["costAmount"]), float(msg["budgetAmount"])
    print(f"budget update: cost={cost} budget={budget}")
    if cost < budget:
        return
    client = compute_v1.InstancesClient()
    for inst in client.list(project=PROJECT, zone=ZONE, filter="labels.gha-spot=true"):
        labels = dict(inst.labels)
        if labels.get("gha-budget-locked") != "true":
            labels["gha-budget-locked"] = "true"
            client.set_labels(
                project=PROJECT, zone=ZONE, instance=inst.name,
                instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                    labels=labels, label_fingerprint=inst.label_fingerprint),
            ).result()
        if inst.status in ("RUNNING", "PROVISIONING", "STAGING"):
            client.stop(project=PROJECT, zone=ZONE, instance=inst.name).result()
            print(f"stopped {inst.name}")
