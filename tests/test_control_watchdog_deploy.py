"""Watchdog deploy script: dry-run output, exact IAM roles, secrets as references, idempotent execution.

The watchdog is a Cloud Run service invoked by Cloud Scheduler with an OIDC token.

The runner is always a fake; nothing touches GCP or GitHub.
"""
from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from lib import control_watchdog_deploy as deploy

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "homelab-424523"
CONTROL_SA = f"spot-control-vm@{PROJECT}.iam.gserviceaccount.com"
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/spot-control/control-watchdog@sha256:" + "b" * 64
SECRET_VALUES = ("kv-token-VALUE", "access-secret-VALUE", "control-secret-VALUE", "access-id-VALUE", "pat-VALUE")
ENV = {
    "SPOT_CONTROL_PROVISIONING": "standard", "SPOT_CONTROL_REGIONS": "us-central1",
    "SPOT_DASHBOARD_IMAGE": "ghcr.io/swack-tools/spot-dashboard@sha256:" + "a" * 64,
    "SPOT_CONTROL_SERVICE_ACCOUNT": CONTROL_SA, "CLOUDFLARE_ACCOUNT_ID": "acct123",
    "CLOUDFLARE_KV_NAMESPACE_ID": "ns456", "DASHBOARD_CONTROLLED_TARGETS": "swack-tools,swack-tools/oxidex",
    # Secrets in the operator's environment must never reach the service spec as values.
    "CLOUDFLARE_KV_API_TOKEN": SECRET_VALUES[0], "CF_ACCESS_CLIENT_SECRET": SECRET_VALUES[1],
    "DASHBOARD_CONTROL_SECRET": SECRET_VALUES[2], "CF_ACCESS_CLIENT_ID": SECRET_VALUES[3],
    "SPOT_DASHBOARD_IMAGE_TRACK_TOKEN": SECRET_VALUES[4],
}


def never(argv):
    raise AssertionError(f"dry run ran {argv}")


def dry_run(*extra, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = deploy.main(["--dry-run", f"--project={PROJECT}", *extra], runner=never, env=env)
    return code, out.getvalue(), err.getvalue()


def commands(script):
    """Every command line of the dry-run script (comments and shell syntax lines dropped), as argv lists."""
    result = []
    for line in script.splitlines():
        line = line.strip()
        if line.startswith("gcloud "):
            result.append(shlex.split(line.replace("${PROJECT_NUMBER}", "N")))
    return result


def flag(argv, name):
    return next((a.split("=", 1)[1] for a in argv if a.startswith(f"--{name}=")), None)


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.code, self.script, self.err = dry_run(f"--image={IMAGE}")
        self.assertEqual(self.code, 0, self.err)
        self.commands = commands(self.script)

    def binding(self, *words):
        return [c for c in self.commands if "add-iam-policy-binding" in c and all(w in c for w in words)]

    def test_runs_nothing_and_is_valid_bash(self):
        result = subprocess.run(["bash", "-n"], input=self.script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing below has run", self.script)

    def test_exact_roles_and_where_each_is_bound(self):
        runtime = f"--member=serviceAccount:spot-control-watchdog@{PROJECT}.iam.gserviceaccount.com"
        roles = sorted({flag(c, "role") for c in self.commands if "add-iam-policy-binding" in c})
        self.assertEqual(roles, sorted([
            "roles/compute.instanceAdmin.v1", "roles/iam.serviceAccountUser", "roles/storage.objectAdmin",
            "roles/secretmanager.secretAccessor", "roles/run.invoker", "roles/artifactregistry.writer",
            "roles/iam.workloadIdentityUser"]))
        compute = self.binding("projects", runtime)
        self.assertEqual(len(compute), 1)
        self.assertEqual(flag(compute[0], "role"), "roles/compute.instanceAdmin.v1")
        self.assertEqual(flag(compute[0], "condition-from-file"), "@FILE:compute-condition.json")
        act_as = self.binding("service-accounts", runtime)
        self.assertEqual([c[4] for c in act_as], [CONTROL_SA])  # actAs on the control VM account only
        self.assertEqual(flag(act_as[0], "role"), "roles/iam.serviceAccountUser")
        storage = self.binding("buckets", runtime)
        self.assertEqual([(c[4], flag(c, "role")) for c in storage],
                         [(f"gs://{PROJECT}-spot-control-state", "roles/storage.objectAdmin")])
        secrets = sorted(c[3] for c in self.binding("secrets", runtime))
        self.assertEqual(secrets, ["spot-control-access-client-id", "spot-control-access-client-secret",
                                   "spot-dashboard-cloudflare-kv-token", "spot-dashboard-control-secret"])
        self.assertTrue(all(flag(c, "role") == "roles/secretmanager.secretAccessor"
                            for c in self.binding("secrets", runtime)))
        # The runtime account gets nothing at project level except the conditioned compute role.
        project_level = [c for c in self.commands if c[1:3] == ["projects", "add-iam-policy-binding"]]
        self.assertEqual(project_level, compute)
        invoker = self.binding("run", "services")
        self.assertEqual([(c[4], flag(c, "member"), flag(c, "role")) for c in invoker],
                         [("spot-control-watchdog", f"serviceAccount:spot-watchdog-invoker@{PROJECT}.iam."
                                                    "gserviceaccount.com", "roles/run.invoker")])
        writer = self.binding("artifacts")
        self.assertEqual([(c[4], flag(c, "role")) for c in writer], [("spot-control", "roles/artifactregistry.writer")])
        wif = [c for c in self.commands if flag(c, "role") == "roles/iam.workloadIdentityUser"]
        self.assertEqual(flag(wif[0], "member"), "principalSet://iam.googleapis.com/projects/N/locations/global/"
                         "workloadIdentityPools/github-actions/attribute.repository/swack-tools/spot-github-runners")
        # The documented binding list matches the commands.
        self.assertEqual(sorted({b[2] for b in deploy.iam_bindings(deploy.config_from_args(
            deploy.args_parser().parse_args([f"--project={PROJECT}"]), ENV))}), roles)

    def test_compute_condition_limits_instances_and_disks(self):
        self.assertIn("spot-control-vms-only", self.script)
        self.assertIn('startsWith(\\"spot-control-\\")', self.script)
        _, unconditioned, _ = dry_run(f"--image={IMAGE}", "--no-compute-condition")
        compute = [c for c in commands(unconditioned) if c[1:3] == ["projects", "add-iam-policy-binding"]]
        self.assertEqual(flag(compute[0], "condition"), "None")

    def test_secrets_are_references_never_values(self):
        for value in SECRET_VALUES:
            self.assertNotIn(value, self.script)
        job = next(c for c in self.commands if c[1:3] == ["run", "deploy"])
        refs = dict(item.split("=", 1) for item in flag(job, "set-secrets").split(","))
        self.assertEqual(refs, {
            "CF_ACCESS_CLIENT_ID": "spot-control-access-client-id:latest",
            "CF_ACCESS_CLIENT_SECRET": "spot-control-access-client-secret:latest",
            "CLOUDFLARE_KV_API_TOKEN": "spot-dashboard-cloudflare-kv-token:latest",
            "DASHBOARD_CONTROL_SECRET": "spot-dashboard-control-secret:latest"})
        env = dict(item.split("=", 1) for item in flag(job, "set-env-vars")[len("^|^"):].split("|"))
        self.assertFalse(set(env) & set(refs))
        self.assertFalse(set(env) & {"SPOT_DASHBOARD_IMAGE_TRACK_TOKEN", "CLOUDFLARE_KV_API_TOKEN"})
        self.assertEqual(env["SPOT_CONTROL_STATE_URI"], f"gs://{PROJECT}-spot-control-state/control-vm")
        self.assertEqual(env["SPOT_GCP_TOKEN_SOURCE"], "metadata")
        self.assertEqual(env["DASHBOARD_CONTROLLED_TARGETS"], "swack-tools,swack-tools/oxidex")  # commas survive
        self.assertEqual(env["WATCHDOG_PROJECT"], PROJECT)
        self.assertEqual(env["WATCHDOG_AUDIENCE"], "https://spot-control-watchdog-N.us-central1.run.app")
        self.assertEqual(env["WATCHDOG_INVOKER"], f"spot-watchdog-invoker@{PROJECT}.iam.gserviceaccount.com")
        self.assertEqual(env["WATCHDOG_VERIFY_SIGNATURE"], "1")
        self.assertEqual(env["SPOT_CONTROL_SERVICE_ACCOUNT"], CONTROL_SA)

    def test_service_is_iam_only_one_tick_at_a_time_and_request_billed(self):
        service = next(c for c in self.commands if c[1:3] == ["run", "deploy"])
        for setting in ("--no-allow-unauthenticated", "--concurrency=1", "--max-instances=1", "--min-instances=0",
                        "--cpu-throttling", "--no-cpu-boost", "--cpu=1", "--memory=512Mi", "--timeout=900",
                        "--ingress=all", f"--image={IMAGE}"):
            self.assertIn(setting, service)
        self.assertNotIn("--allow-unauthenticated", service)
        self.assertNotIn("--no-cpu-throttling", service)
        self.assertNotIn("allUsers", self.script)
        self.assertNotIn("allAuthenticatedUsers", self.script)
        self.assertNotIn("run jobs", self.script)  # no Cloud Run job: jobs bill a minute per execution
        self.assertGreater(deploy.REQUEST_TIMEOUT_SECONDS, __import__("lib.control_watchdog_server",
                                                                       fromlist=["x"]).TICK_TIMEOUT_SECONDS)

    def test_tracking_adds_the_github_token_reference(self):
        _, script, _ = dry_run(f"--image={IMAGE}", env=dict(ENV, SPOT_DASHBOARD_IMAGE_TRACK="main"))
        job = next(c for c in commands(script) if c[1:3] == ["run", "deploy"])
        self.assertIn("SPOT_DASHBOARD_IMAGE_TRACK_TOKEN=spot-control-watchdog-github-token:latest",
                      flag(job, "set-secrets"))
        self.assertIn("SPOT_DASHBOARD_IMAGE_TRACK=main", flag(job, "set-env-vars"))
        self.assertNotIn(SECRET_VALUES[4], script)

    def test_bucket_scheduler_and_wif_settings(self):
        create = next(c for c in self.commands if c[1:4] == ["storage", "buckets", "create"])
        for setting in ("--uniform-bucket-level-access", "--public-access-prevention", "--location=us-central1"):
            self.assertIn(setting, create)
        scheduler = next(c for c in self.commands if c[1:5] == ["scheduler", "jobs", "create", "http"])
        self.assertEqual(flag(scheduler, "schedule"), "*/3 * * * *")
        self.assertEqual(flag(scheduler, "oidc-service-account-email"),
                         f"spot-watchdog-invoker@{PROJECT}.iam.gserviceaccount.com")
        url = "https://spot-control-watchdog-N.us-central1.run.app"
        self.assertEqual(flag(scheduler, "oidc-token-audience"), url)
        self.assertEqual(flag(scheduler, "uri"), f"{url}/tick")
        self.assertEqual(flag(scheduler, "http-method"), "POST")
        self.assertIsNone(flag(scheduler, "oauth-service-account-email"))
        provider = next(c for c in self.commands if "create-oidc" in c)
        condition = flag(provider, "attribute-condition")
        for part in ("assertion.repository == 'swack-tools/spot-github-runners'", "assertion.ref == 'refs/heads/main'",
                     "workflows/control-watchdog-image.yml@refs/heads/main"):
            self.assertIn(part, condition)
        self.assertTrue((ROOT / deploy.IMAGE_WORKFLOW).exists())

    def test_without_an_image_only_infrastructure(self):
        code, script, _ = dry_run()
        self.assertEqual(code, 0)
        self.assertNotIn("gcloud run deploy", script)
        self.assertNotIn("scheduler jobs", script)
        self.assertIn("need --image", script)
        _, skipped, _ = dry_run(f"--image={IMAGE}", "--skip-scheduler")
        self.assertIn("gcloud run deploy", skipped)
        self.assertNotIn("scheduler jobs", skipped)


class ValidationTests(unittest.TestCase):
    def test_refusals(self):
        cases = [
            (["--image=us-central1-docker.pkg.dev/homelab-424523/spot-control/control-watchdog:latest"], ENV,
             "pinned by digest"),
            ([f"--image={IMAGE.replace('homelab-424523', 'other')}"], ENV, "pinned by digest"),
            ([], {k: v for k, v in ENV.items() if k != "SPOT_CONTROL_SERVICE_ACCOUNT"}, "service account e-mail"),
            ([], dict(ENV, SPOT_DASHBOARD_IMAGE=""), "never builds the dashboard image"),
            ([], dict(ENV, SPOT_DASHBOARD_IMAGE_TRACK="latest"), "must be empty or main"),
            ([], dict(ENV, SPOT_CONTROL_REGIONS="europe-west1"), "free only in"),
            ([], dict(ENV, DASHBOARD_GLOBAL_ADMINS="a|b"), "can't carry"),
            (["--schedule=every 2 minutes"], ENV, "cron"),
            (["--region=US"], ENV, "invalid region"),
            (["--secret=SPOT_CONTROL_REGIONS=x"], ENV, "--secret must be"),
        ]
        for extra, env, words in cases:
            code, out, err = dry_run(*extra, env=env)
            self.assertEqual(code, 1, (extra, out))
            self.assertIn(words, err, extra)
            self.assertEqual(out, "")

    def test_secret_names_can_never_become_plain_env_vars(self):
        # Even a config that carries secret values in its env (config_from_args already drops them) never puts
        # them in the service's plain env vars, and validate() refuses an overlap with the secret references.
        config = deploy.DeployConfig(project=PROJECT, control_vm_service_account=CONTROL_SA, env=dict(ENV))
        env = config.service_env()
        for name in [*deploy.SECRET_ENV, deploy.TRACK_SECRET_ENV[0]]:
            self.assertNotIn(name, env)
        for value in SECRET_VALUES:
            self.assertNotIn(value, json.dumps(env))
        leaky = deploy.DeployConfig(project=PROJECT, control_vm_service_account=CONTROL_SA, env=dict(ENV),
                                    secrets={"SPOT_CONTROL_REGIONS": "x"})
        self.assertIn("must come from Secret Manager", " ".join(deploy.validate(leaky)))

    def test_service_points_gcloud_at_a_writable_config_dir(self):
        # An unwritable gcloud config directory makes gcloud warn on stderr, which fails the inventory read.
        config = deploy.DeployConfig(project=PROJECT, control_vm_service_account=CONTROL_SA, env=dict(ENV))
        self.assertEqual(config.service_env()["CLOUDSDK_CONFIG"], "/tmp/gcloud-config")
        dockerfile = (Path(__file__).resolve().parent.parent / "watchdog" / "Dockerfile").read_text()
        self.assertIn("ENV CLOUDSDK_CONFIG=/tmp/gcloud-config", dockerfile)

    def test_flag_supplied_control_vm_account_reaches_the_service(self):
        env = {k: v for k, v in ENV.items() if k != "SPOT_CONTROL_SERVICE_ACCOUNT"}
        code, script, err = dry_run(f"--image={IMAGE}", f"--control-vm-service-account={CONTROL_SA}", env=env)
        self.assertEqual(code, 0, err)
        service = next(c for c in commands(script) if c[1:3] == ["run", "deploy"])
        self.assertIn(f"SPOT_CONTROL_SERVICE_ACCOUNT={CONTROL_SA}", flag(service, "set-env-vars"))

    def test_tracking_mode_is_normalized_like_the_manager(self):
        code, script, err = dry_run(f"--image={IMAGE}", env=dict(ENV, SPOT_DASHBOARD_IMAGE_TRACK=" Main "))
        self.assertEqual(code, 0, err)
        service = next(c for c in commands(script) if c[1:3] == ["run", "deploy"])
        self.assertIn("SPOT_DASHBOARD_IMAGE_TRACK_TOKEN=spot-control-watchdog-github-token:latest",
                      flag(service, "set-secrets"))
        self.assertIn("SPOT_DASHBOARD_IMAGE_TRACK=main|", flag(service, "set-env-vars") + "|")
        self.assertIn("gcloud secrets add-iam-policy-binding spot-control-watchdog-github-token", script)

    def test_signature_check_can_be_relaxed_but_claims_stay(self):
        _, script, _ = dry_run(f"--image={IMAGE}", "--no-verify-signature")
        service = next(c for c in commands(script) if c[1:3] == ["run", "deploy"])
        self.assertIn("WATCHDOG_VERIFY_SIGNATURE=0", flag(service, "set-env-vars"))
        self.assertIn("WATCHDOG_INVOKER=", flag(service, "set-env-vars"))

    def test_secret_override(self):
        code, script, _ = dry_run(f"--image={IMAGE}", "--secret=CLOUDFLARE_KV_API_TOKEN=my-kv-token")
        self.assertEqual(code, 0)
        self.assertIn("CLOUDFLARE_KV_API_TOKEN=my-kv-token:latest", script)
        self.assertIn("gcloud secrets add-iam-policy-binding my-kv-token", script)
        self.assertNotIn("add-iam-policy-binding spot-dashboard-cloudflare-kv-token", script)


class ExecuteTests(unittest.TestCase):
    def runner(self, existing=(), secrets_ok=True, fail=None):
        calls, files = [], {}

        def run(argv):
            argv = list(argv)
            calls.append(argv)
            for arg in argv:
                if arg.startswith("--condition-from-file="):
                    files["condition"] = json.loads(Path(arg.split("=", 1)[1]).read_text())
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return SimpleNamespace(returncode=0, stdout="123456789\n", stderr="")
            if "describe" in argv and "versions" in argv:
                return SimpleNamespace(returncode=0 if secrets_ok else 1, stdout="ENABLED\n" if secrets_ok else "",
                                       stderr="" if secrets_ok else "NOT_FOUND")
            if "describe" in argv:
                found = any(e in argv for e in existing)
                return SimpleNamespace(returncode=0 if found else 1, stdout="", stderr="")
            if fail and fail in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run, calls, files

    def deploy(self, runner, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy.main([f"--project={PROJECT}", f"--image={IMAGE}", *extra], runner=runner, env=ENV)
        return code, out.getvalue(), err.getvalue()

    def test_fresh_project_creates_everything(self):
        run, calls, files = self.runner()
        code, out, err = self.deploy(run)
        self.assertEqual(code, 0, err)
        verbs = [" ".join(c[1:4]) for c in calls]
        for created in ("storage buckets create", "iam service-accounts create", "artifacts repositories create",
                        "iam workload-identity-pools create", "run deploy spot-control-watchdog",
                        "scheduler jobs create"):
            self.assertIn(created, verbs)
        self.assertNotIn("scheduler jobs update", verbs)
        self.assertEqual(files["condition"]["expression"], deploy.COMPUTE_CONDITION_EXPRESSION)
        joined = "\n".join(" ".join(c) for c in calls)
        self.assertIn("projects/123456789/locations/global", joined)
        self.assertNotIn("${PROJECT_NUMBER}", joined)
        for value in SECRET_VALUES:
            self.assertNotIn(value, joined)
            self.assertNotIn(value, out)

    def test_existing_resources_are_updated_not_created(self):
        run, calls, _ = self.runner(existing=("gs://homelab-424523-spot-control-state", "describe"))
        code, _, err = self.deploy(run)
        self.assertEqual(code, 0, err)
        verbs = [" ".join(c[1:5]) for c in calls]
        self.assertFalse([v for v in verbs if v.startswith(("storage buckets create", "iam service-accounts create",
                                                             "artifacts repositories create"))])
        self.assertIn("scheduler jobs update http", verbs)
        self.assertIn("iam workload-identity-pools providers update-oidc", " | ".join(" ".join(c[1:5]) for c in calls))

    def test_missing_secret_stops_before_any_iam_or_job_change(self):
        run, calls, _ = self.runner(secrets_ok=False)
        code, _, err = self.deploy(run)
        self.assertEqual(code, 1)
        self.assertIn("has no enabled version", err)
        self.assertIn("--data-file=-", err)  # the hint reads the value from stdin, never from argv
        self.assertFalse([c for c in calls if "add-iam-policy-binding" in c or "deploy" in c or "jobs" in c])

    def test_failure_stops_at_that_step(self):
        run, calls, _ = self.runner(fail="deploy")
        code, _, err = self.deploy(run)
        self.assertEqual(code, 1)
        self.assertIn("PERMISSION_DENIED", err)
        self.assertFalse([c for c in calls if "scheduler" in c])


class ArtifactTests(unittest.TestCase):
    def test_dockerfile_pins_its_base_by_build_arg_and_carries_the_startup_script(self):
        text = (ROOT / "watchdog" / "Dockerfile").read_text()
        self.assertRegex(text, re.compile(r"^ARG BASE_IMAGE$", re.M))  # no default: the build must pin it
        self.assertRegex(text, re.compile(r"^FROM \$\{BASE_IMAGE\}$", re.M))
        self.assertIn("COPY cloud-init/control-vm-startup.sh /app/cloud-init/control-vm-startup.sh", text)
        self.assertIn('ENTRYPOINT ["python3", "-B", "/app/src/control_watchdog_server.py"]', text)
        self.assertRegex(text, re.compile(r"^USER 10001:10001$", re.M))

    def test_workflow_uses_federation_and_checks_the_base_pin(self):
        text = (ROOT / deploy.IMAGE_WORKFLOW).read_text()
        self.assertIn("id-token: write", text)
        self.assertIn("contents: read", text)
        self.assertNotIn("packages: write", text)
        self.assertIn("sts.googleapis.com/v1/token", text)
        self.assertIn(":generateAccessToken", text)
        self.assertIn("-slim@sha256:[0-9a-f]{64}$", text)
        self.assertIn("persist-credentials: false", text)
        for uses in re.findall(r"uses:\s*(\S+)", text):
            self.assertRegex(uses, r"@[0-9a-f]{40}$", "third-party actions are pinned by commit")
        self.assertIn('--label "org.opencontainers.image.revision=${GITHUB_SHA}"', text)


if __name__ == "__main__":
    unittest.main()
