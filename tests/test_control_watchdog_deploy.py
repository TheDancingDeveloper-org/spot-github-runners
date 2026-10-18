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
                if arg.startswith("--condition-from-file=") and "add-iam-policy-binding" in argv:
                    files.setdefault("condition", json.loads(Path(arg.split("=", 1)[1]).read_text()))
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


FLOOR_TARGETS = ("swack-tools:secret=spot-floor-github-swackhamer,account=swackhamer,group=10;"
                 "Appz4Fun:secret=spot-floor-github-xbmc4lyfe,account=xbmc4lyfe;"
                 "xbmc4lyfe/newznab-tmux:secret=spot-floor-github-xbmc4lyfe,account=xbmc4lyfe,labels=ubuntu-latest")
FLOOR_ENV = dict(ENV, SPOT_RUNNER_FLOOR_TARGETS=FLOOR_TARGETS)


def cel_allows(expression: str, resource_type: str, name: str) -> bool:
    """Evaluate the subset of CEL the compute conditions use, the way IAM does for one resource."""
    def extract(template_prefix: str) -> str:
        index = name.find(template_prefix)
        return name[index + len(template_prefix):].split("/")[0] if index >= 0 else ""
    python = (expression.replace("&&", " and ").replace("||", " or ")
              .replace("resource.type", "rtype").replace(".startsWith(", ".startswith(")
              .replace('resource.name.extract("/instances/{name}")', 'extract("/instances/")')
              .replace('resource.name.extract("/disks/{name}")', 'extract("/disks/")'))
    return bool(eval(python, {"__builtins__": {}}, {"rtype": resource_type, "extract": extract}))  # noqa: S307


INSTANCE, DISK = "compute.googleapis.com/Instance", "compute.googleapis.com/Disk"


class FloorDeployTests(unittest.TestCase):
    def setUp(self):
        self.code, self.script, self.err = dry_run(f"--image={IMAGE}", env=FLOOR_ENV)
        self.assertEqual(self.code, 0, self.err)
        self.commands = commands(self.script)
        self.service = next(c for c in self.commands if c[1:3] == ["run", "deploy"])

    def test_a_second_narrow_compute_binding_and_the_control_binding_unchanged(self):
        project_level = [c for c in self.commands if c[1:3] == ["projects", "add-iam-policy-binding"]]
        self.assertEqual([flag(c, "role") for c in project_level], ["roles/compute.instanceAdmin.v1"] * 2)
        self.assertEqual([flag(c, "condition-from-file") for c in project_level],
                         ["@FILE:compute-condition.json", "@FILE:floor-condition.json"])
        self.assertIn(deploy.FLOOR_CONDITION_TITLE, self.script)
        # No actAs beyond the control VM account: floor hosts run with no service account.
        act_as = [c for c in self.commands if flag(c, "role") == "roles/iam.serviceAccountUser"]
        self.assertEqual([c[4] for c in act_as], [CONTROL_SA])
        _, unconditioned, _ = dry_run(f"--image={IMAGE}", "--no-compute-condition", env=FLOOR_ENV)
        compute = [c for c in commands(unconditioned) if c[1:3] == ["projects", "add-iam-policy-binding"]]
        self.assertEqual([flag(c, "condition") for c in compute], ["None"])

    def test_the_floor_condition_admits_runner_hosts_and_their_disks_only(self):
        floor, control = deploy.FLOOR_CONDITION_EXPRESSION, deploy.COMPUTE_CONDITION_EXPRESSION
        zone = "projects/p/zones/us-central1-a"
        allowed = [(INSTANCE, f"{zone}/instances/oxidex-runners-uscentral1-a-grks-tn31sq"),
                   (DISK, f"{zone}/disks/oxidex-runners-uscentral1-a-grks-tn31sq"),
                   (DISK, f"{zone}/disks/runner-data-uscentral1-a-grks-tn31sq"),
                   (DISK, f"{zone}/disks/runner-data"),
                   ("compute.googleapis.com/Subnetwork", "projects/p/regions/us-central1/subnetworks/default"),
                   ("cloudresourcemanager.googleapis.com/Project", "projects/p")]
        denied = [(INSTANCE, f"{zone}/instances/newznab-spot-runners-asia"),
                  (INSTANCE, f"{zone}/instances/builder-a"), (DISK, f"{zone}/disks/builder-cache-a"),
                  (INSTANCE, f"{zone}/instances/spot-control-a"), (DISK, f"{zone}/disks/spot-control-a"),
                  (INSTANCE, f"{zone}/instances/runner-data-x"), (DISK, f"{zone}/disks/xoxidex-runners-a"),
                  (DISK, f"{zone}/disks/registry-cache"), (DISK, f"{zone}/disks/oxidex-builder-cache")]
        for rtype, name in allowed:
            with self.subTest(name=name):
                self.assertTrue(cel_allows(floor, rtype, name))
        for rtype, name in denied:
            with self.subTest(name=name):
                self.assertFalse(cel_allows(floor, rtype, name))
        # The control VM condition still admits only spot-control-* instances and disks.
        self.assertTrue(cel_allows(control, INSTANCE, f"{zone}/instances/spot-control-a"))
        self.assertFalse(cel_allows(control, INSTANCE, f"{zone}/instances/oxidex-runners-a"))

    def test_one_mounted_secret_per_account_and_never_an_env_var(self):
        refs = dict(item.split("=", 1) for item in flag(self.service, "set-secrets").split(","))
        self.assertEqual(refs["/secrets/runner-floor/spot-floor-github-swackhamer/token"],
                         "spot-floor-github-swackhamer:latest")
        self.assertEqual(refs["/secrets/runner-floor/spot-floor-github-xbmc4lyfe/token"],
                         "spot-floor-github-xbmc4lyfe:latest")
        self.assertEqual(len([k for k in refs if k.startswith("/secrets/runner-floor/")]), 2)
        env = dict(item.split("=", 1) for item in flag(self.service, "set-env-vars")[len("^|^"):].split("|"))
        self.assertEqual(env["SPOT_RUNNER_FLOOR_TARGETS"], FLOOR_TARGETS)  # secret names only
        self.assertEqual(env["SPOT_RUNNER_FLOOR_STATE_URI"], f"gs://{PROJECT}-spot-control-state/runner-floor")
        self.assertEqual(env["SPOT_RUNNER_FLOOR_TOKEN_SOURCE"], "secret-files")
        self.assertFalse([k for k in env if "TOKEN" in k and not k.endswith("_TOKEN_SOURCE")])
        accessor = sorted(c[3] for c in self.commands if flag(c, "role") == "roles/secretmanager.secretAccessor")
        self.assertIn("spot-floor-github-swackhamer", accessor)
        self.assertIn("spot-floor-github-xbmc4lyfe", accessor)
        for secret in ("spot-floor-github-swackhamer", "spot-floor-github-xbmc4lyfe"):
            self.assertIn(f"--secret={secret}", self.script)  # the enabled-version check
        self.assertEqual(env["SPOT_GCP_TOKEN_SOURCE"], "metadata")

    def test_refusals(self):
        token = "ghp_" + "A" * 36
        for env, message in ((dict(ENV, SPOT_RUNNER_FLOOR_TARGETS=f"swack-tools:secret={token}"), "never hold a token"),
                             (dict(ENV, SPOT_RUNNER_FLOOR_TARGETS="o/r:secret=spot-control-access-client-id"),
                              "also back other service variables"),
                             (dict(FLOOR_ENV, SPOT_CONFIG_REPO=f"{token}/x"), "looks like a GitHub token"),
                             (dict(ENV, SPOT_RUNNER_FLOOR_TARGETS="swack-tools"), "runner floor"),
                             (dict(FLOOR_ENV, SPOT_RUNNER_FLOOR_MAX_DAILY_USD="50"), "runner floor")):
            with self.subTest(message=message):
                code, _, err = dry_run(f"--image={IMAGE}", env=env)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertNotIn(token, err)

    def test_floor_services_get_the_billing_api_and_more_memory(self):
        self.assertIn("--memory=1Gi", self.service)
        enable = next(c for c in self.commands if c[1:3] == ["services", "enable"])
        self.assertIn("cloudbilling.googleapis.com", enable)
        _, plain, _ = dry_run(f"--image={IMAGE}")
        service = next(c for c in commands(plain) if c[1:3] == ["run", "deploy"])
        self.assertIn("--memory=512Mi", service)
        self.assertNotIn("cloudbilling.googleapis.com", next(c for c in commands(plain) if c[1:3] == ["services", "enable"]))

    def test_the_documented_bindings_list_the_floor(self):
        config = deploy.config_from_args(deploy.args_parser().parse_args([f"--project={PROJECT}"]), FLOOR_ENV)
        rows = deploy.iam_bindings(config)
        self.assertTrue(any("oxidex-runners-*" in row[0] for row in rows))
        self.assertTrue(any("spot-floor-github-xbmc4lyfe" in row[0] for row in rows))

    def test_execute_passes_both_condition_files(self):
        calls, files = [], []

        def run(argv):
            argv = list(argv)
            calls.append(argv)
            for arg in argv:
                if arg.startswith("--condition-from-file="):
                    files.append(json.loads(Path(arg.split("=", 1)[1]).read_text()))
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return SimpleNamespace(returncode=0, stdout="123456789\n", stderr="")
            if "describe" in argv and "versions" in argv:
                return SimpleNamespace(returncode=0, stdout="ENABLED\n", stderr="")
            return SimpleNamespace(returncode=1 if "describe" in argv else 0, stdout="", stderr="")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy.main([f"--project={PROJECT}", f"--image={IMAGE}"], runner=run, env=FLOOR_ENV)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual([f["title"] for f in files], ["spot-control-vms-only", deploy.FLOOR_CONDITION_TITLE])
        self.assertEqual(files[1]["expression"], deploy.FLOOR_CONDITION_EXPRESSION)

    def test_the_image_carries_what_runner_manager_reads_from_disk(self):
        text = (ROOT / "watchdog" / "Dockerfile").read_text()
        self.assertIn("COPY runner/job-started.sh /app/runner/job-started.sh", text)
        self.assertIn("COPY builder/docker-admission.py /app/builder/docker-admission.py", text)
        self.assertIn("lib.runner_floor", text)
        workflow = (ROOT / deploy.IMAGE_WORKFLOW).read_text()
        self.assertIn('"runner/job-started.sh"', workflow)
        self.assertIn('"builder/docker-admission.py"', workflow)
        from lib import runner_manager
        source = Path(runner_manager.__file__).read_text()
        self.assertEqual(sorted(set(re.findall(r"ROOT\s*/\s*'([^']+)'", source))),
                         ["builder/docker-admission.py", "runner/job-started.sh"])


class FloorRemovalTests(unittest.TestCase):
    """Turning the floor off (or dropping a target's secret) removes the grants an earlier deploy made."""

    def runner(self, labeled=("spot-floor-github-swackhamer", "spot-floor-github-old"), absent=()):
        calls = []

        def run(argv):
            argv = list(argv)
            calls.append(argv)
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return SimpleNamespace(returncode=0, stdout="123456789\n", stderr="")
            if "describe" in argv and "versions" in argv:
                return SimpleNamespace(returncode=0, stdout="ENABLED\n", stderr="")
            if argv[:3] == ["gcloud", "secrets", "list"]:
                return SimpleNamespace(returncode=0, stdout="".join(f"{n}\n" for n in labeled), stderr="")
            if "remove-iam-policy-binding" in argv and any(a in argv for a in absent):
                return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Policy binding with the specified "
                                       "principal, role, and condition not found!")
            return SimpleNamespace(returncode=1 if "describe" in argv else 0, stdout="", stderr="")
        return run, calls

    def deploy(self, run, env):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy.main([f"--project={PROJECT}", f"--image={IMAGE}"], runner=run, env=env)
        return code, err.getvalue()

    def removed(self, calls):
        return [c for c in calls if "remove-iam-policy-binding" in c or "--remove-labels=spot-runner-floor" in c]

    def test_floor_off_removes_the_compute_binding_and_every_labeled_secret_grant(self):
        # A labeled secret that also backs a service variable keeps its access.
        run, calls = self.runner(labeled=("spot-floor-github-swackhamer", "spot-floor-github-old",
                                          "spot-dashboard-control-secret"))
        code, err = self.deploy(run, ENV)
        self.assertEqual(code, 0, err)
        compute = [c for c in calls if c[1:3] == ["projects", "remove-iam-policy-binding"]]
        self.assertEqual(len(compute), 1)
        self.assertEqual(flag(compute[0], "role"), "roles/compute.instanceAdmin.v1")
        removed = sorted(c[3] for c in self.removed(calls) if c[1:3] == ["secrets", "remove-iam-policy-binding"])
        self.assertEqual(removed, ["spot-floor-github-old", "spot-floor-github-swackhamer"])
        self.assertTrue(all(flag(c, "role") == "roles/secretmanager.secretAccessor"
                            for c in self.removed(calls) if c[1:3] == ["secrets", "remove-iam-policy-binding"]))

    def test_floor_on_keeps_current_secrets_and_removes_a_dropped_one(self):
        run, calls = self.runner()
        code, err = self.deploy(run, FLOOR_ENV)
        self.assertEqual(code, 0, err)
        removed = [c[3] for c in self.removed(calls) if c[1:3] == ["secrets", "remove-iam-policy-binding"]]
        self.assertEqual(removed, ["spot-floor-github-old"])  # swackhamer's secret is still configured
        self.assertFalse([c for c in calls if c[1:3] == ["projects", "remove-iam-policy-binding"]])
        labels = sorted(c[3] for c in calls if any(a.startswith("--update-labels=spot-runner-floor") for a in c))
        self.assertEqual(labels, ["spot-floor-github-swackhamer", "spot-floor-github-xbmc4lyfe"])

    def test_removal_is_idempotent(self):
        run, calls = self.runner(absent=("spot-floor-github-old", "projects"))
        self.assertEqual(self.deploy(run, ENV)[0], 0)
        run, calls = self.runner(labeled=())
        self.assertEqual(self.deploy(run, ENV)[0], 0)
        self.assertFalse([c for c in calls if c[1:3] == ["secrets", "remove-iam-policy-binding"]])

    def test_other_removal_failures_stop_the_deploy(self):
        def run(argv):
            if "remove-iam-policy-binding" in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED")
            return self.runner()[0](argv)
        code, err = self.deploy(run, ENV)
        self.assertEqual(code, 1)
        self.assertIn("PERMISSION_DENIED", err)

    def test_the_dry_run_shows_the_removals_and_stays_valid_bash(self):
        for env in (ENV, FLOOR_ENV):
            with self.subTest(floor=env is FLOOR_ENV):
                code, script, err = dry_run(f"--image={IMAGE}", env=env)
                self.assertEqual(code, 0, err)
                self.assertIn("labels.spot-runner-floor=token", script)
                self.assertIn('"$name"', script)
                self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0)
                self.assertEqual("projects remove-iam-policy-binding" in script, env is ENV)
