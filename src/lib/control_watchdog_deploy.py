"""Idempotent deployment of the serverless control VM watchdog (``src/control_watchdog_deploy.py``).

The watchdog is a Cloud Run service (``src/control_watchdog_server.py``) with request-based billing. Cloud
Scheduler sends it ``POST /tick`` with an OIDC token every 3 minutes, and each request runs one
``control_vm.py ensure --apply --skip-if-idle --skip-if-locked`` step against the Cloud Storage state
(``SPOT_CONTROL_STATE_URI``). A service, not a Cloud Run job: jobs use instance-based billing with a one-minute
minimum per execution, which at this cadence leaves the free tier. This module creates or updates, in order:

1. The APIs it needs.
2. The state bucket: regional, uniform bucket-level access, public access prevention enforced.
3. Three service accounts: the service's runtime account, the scheduler's invoker account, and the image publisher
   account that GitHub Actions impersonates through Workload Identity Federation.
4. The Artifact Registry Docker repository for the watchdog image.
5. A check that every Secret Manager secret the service references has an enabled version (it never writes one).
6. IAM, each binding on the narrowest resource that works (see :func:`iam_bindings`).
7. The Workload Identity pool and GitHub OIDC provider, limited to the image workflow on ``main``.
8. With ``--image``: the Cloud Run service (no unauthenticated access, concurrency 1, at most one instance, secrets
   as Secret Manager references only), its invoker binding, and the Cloud Scheduler job with an OIDC target.

``--dry-run`` prints every command as a reviewable shell script and runs nothing. Without it, every resource is
described first and created only when missing; bindings and updates are idempotent.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import control_vm as cv
from . import image_tracking

DEFAULT_REGION = "us-central1"
# Every 3 minutes: 14,400 requests a month at about 8 s is about 115,000 vCPU-seconds, inside the 180,000 free.
# Every 2 minutes would be about 173,000 at 8 s and over the free tier from about 8.3 s a tick.
DEFAULT_SCHEDULE = "*/3 * * * *"
DEFAULT_GITHUB_REPO = "swack-tools/spot-github-runners"
IMAGE_WORKFLOW = ".github/workflows/control-watchdog-image.yml"
STATE_PREFIX = "control-vm"
REQUEST_TIMEOUT_SECONDS = 900  # the server kills a tick after 840 s

# Every env var the service receives from Secret Manager, and the secret it references by default.
SECRET_ENV: dict[str, str] = {
    "CLOUDFLARE_KV_API_TOKEN": "spot-dashboard-cloudflare-kv-token",
    "DASHBOARD_CONTROL_SECRET": "spot-dashboard-control-secret",
    "CF_ACCESS_CLIENT_ID": "spot-control-access-client-id",
    "CF_ACCESS_CLIENT_SECRET": "spot-control-access-client-secret",
}
# Only with SPOT_DASHBOARD_IMAGE_TRACK=main: a classic token with read:packages (and repo if the repo is private).
TRACK_SECRET_ENV = (image_tracking.ENV_IMAGE_TRACK_TOKEN, "spot-control-watchdog-github-token")

# Non-secret settings copied from the operator's environment into the service, when set.
PASSTHROUGH_ENV = (
    "SPOT_CONTROL_PROVISIONING", "SPOT_CONTROL_REGIONS", "SPOT_CONTROL_MAX_DAILY_USD", "SPOT_CONTROL_EXTERNAL_IP",
    "SPOT_CONTROL_SERVICE_ACCOUNT", "SPOT_CONTROL_ORIGIN_DOMAIN", "SPOT_DASHBOARD_IMAGE", "SPOT_DASHBOARD_REPO",
    "SPOT_DASHBOARD_SOURCE_REF", "SPOT_DASHBOARD_PUBLIC_URL", "SPOT_CLOUDFLARED_IMAGE", "SPOT_CONFIG_REPO",
    "DASHBOARD_CONTROLLED_TARGETS", "DASHBOARD_GLOBAL_ADMINS", "DASHBOARD_SESSION_NOT_BEFORE",
    "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_KV_NAMESPACE_ID", image_tracking.ENV_IMAGE_TRACK,
)

APIS = ("run.googleapis.com", "cloudscheduler.googleapis.com", "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com", "storage.googleapis.com", "iam.googleapis.com",
        "iamcredentials.googleapis.com", "sts.googleapis.com", "compute.googleapis.com")

ROLE_COMPUTE = "roles/compute.instanceAdmin.v1"
ROLE_ACT_AS = "roles/iam.serviceAccountUser"
ROLE_SECRET = "roles/secretmanager.secretAccessor"
ROLE_OBJECTS = "roles/storage.objectAdmin"
ROLE_INVOKER = "roles/run.invoker"
ROLE_AR_WRITER = "roles/artifactregistry.writer"
ROLE_WIF_USER = "roles/iam.workloadIdentityUser"

# IAM condition on the compute binding: Compute Engine instances and disks only when named spot-control-*; every
# other resource the role covers (subnetworks, machine types, zone operations, the project for list calls) as is.
COMPUTE_CONDITION_EXPRESSION = (
    '(resource.type != "compute.googleapis.com/Instance" && resource.type != "compute.googleapis.com/Disk") || '
    'resource.name.extract("/instances/{name}").startsWith("spot-control-") || '
    'resource.name.extract("/disks/{name}").startsWith("spot-control-")'
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_JOB_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCHEDULE_RE = re.compile(r"^[0-9*/,\- ]+$")


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeployConfig:
    project: str
    control_vm_service_account: str
    region: str = DEFAULT_REGION
    bucket: str = ""
    runtime_account: str = "spot-control-watchdog"
    invoker_account: str = "spot-watchdog-invoker"
    publisher_account: str = "spot-watchdog-publisher"
    repository: str = "spot-control"
    image_name: str = "control-watchdog"
    service: str = "spot-control-watchdog"
    scheduler_job: str = "spot-control-watchdog"
    schedule: str = DEFAULT_SCHEDULE
    image: str = ""
    github_repo: str = DEFAULT_GITHUB_REPO
    wif_pool: str = "github-actions"
    wif_provider: str = "spot-github-runners"
    project_number: str = ""
    compute_condition: bool = True
    skip_scheduler: bool = False
    verify_signature: bool = True
    secrets: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)  # plain service env (validated, never a secret)

    @property
    def state_bucket(self) -> str:
        return self.bucket or f"{self.project.replace(':', '-').replace('.', '-')}-spot-control-state"

    @property
    def state_uri(self) -> str:
        return f"gs://{self.state_bucket}/{STATE_PREFIX}"

    def email(self, account: str) -> str:
        domain = self.project
        if ":" in domain:  # domain-scoped project example.com:proj -> proj.example.com.iam.gserviceaccount.com
            org, name = domain.split(":", 1)
            domain = f"{name}.{org}"
        return f"{account}@{domain}.iam.gserviceaccount.com"

    @property
    def runtime_email(self) -> str:
        return self.email(self.runtime_account)

    @property
    def invoker_email(self) -> str:
        return self.email(self.invoker_account)

    @property
    def publisher_email(self) -> str:
        return self.email(self.publisher_account)

    @property
    def image_repo(self) -> str:
        return f"{self.region}-docker.pkg.dev/{self.project.replace(':', '/')}/{self.repository}/{self.image_name}"

    @property
    def number(self) -> str:
        return self.project_number or "${PROJECT_NUMBER}"

    @property
    def service_url(self) -> str:
        """The service's deterministic URL, known before it exists; also the OIDC audience."""
        return f"https://{self.service}-{self.number}.{self.region}.run.app"

    @property
    def wif_provider_name(self) -> str:
        return (f"projects/{self.number}/locations/global/workloadIdentityPools/{self.wif_pool}/providers/"
                f"{self.wif_provider}")

    @property
    def track_mode(self) -> str:
        """SPOT_DASHBOARD_IMAGE_TRACK normalized exactly as ControlConfig.from_env normalizes it."""
        return str(self.env.get(image_tracking.ENV_IMAGE_TRACK, "") or "").strip().lower()

    @property
    def tracking(self) -> bool:
        return self.track_mode == image_tracking.TRACK_MAIN

    def secret_refs(self) -> dict[str, str]:
        """Job env var -> Secret Manager secret name."""
        refs = dict(SECRET_ENV)
        if self.tracking:
            refs[TRACK_SECRET_ENV[0]] = TRACK_SECRET_ENV[1]
        refs.update(self.secrets)
        return refs

    def service_env(self) -> dict[str, str]:
        """Plain env vars of the service: the operator's non-secret settings plus the fixed watchdog settings."""
        env = {key: str(self.env[key]) for key in PASSTHROUGH_ENV if str(self.env.get(key, "") or "").strip()}
        if self.track_mode:
            env[image_tracking.ENV_IMAGE_TRACK] = self.track_mode
        if self.control_vm_service_account:
            # The account the VMs run as, and the only one the runtime account may act as; without it gcloud would
            # create VMs as the default Compute Engine account, which the runtime account can't act as.
            env["SPOT_CONTROL_SERVICE_ACCOUNT"] = self.control_vm_service_account
        env.update({
            "WATCHDOG_PROJECT": self.project,
            "WATCHDOG_AUDIENCE": self.service_url,
            "WATCHDOG_INVOKER": self.invoker_email,
            "WATCHDOG_VERIFY_SIGNATURE": "1" if self.verify_signature else "0",
            cv.ENV_CONTROL_STATE_URI: self.state_uri,
            "SPOT_GCP_TOKEN_SOURCE": "metadata",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "true",
            # gcloud writes logs and credentials under its config directory; any warning about an unwritable
            # one reaches stderr, which the inventory parser treats as a failed read. /tmp is writable on Cloud Run.
            "CLOUDSDK_CONFIG": "/tmp/gcloud-config",
        })
        return env


def validate(config: DeployConfig) -> list[str]:
    """Every problem that must stop the deploy before any command runs."""
    problems = []
    try:
        cv.project_control_paths(config.project, ".")
    except ValueError as exc:
        problems.append(str(exc))
    if not _REGION_RE.match(config.region):
        problems.append(f"invalid region {config.region!r}")
    if not _BUCKET_RE.match(config.state_bucket):
        problems.append(f"invalid bucket name {config.state_bucket!r}")
    for account in (config.runtime_account, config.invoker_account, config.publisher_account):
        if not _NAME_RE.match(account):
            problems.append(f"service account name {account!r} must be 6-30 lower-case letters, digits or hyphens")
    for name in (config.service, config.scheduler_job, config.repository, config.image_name, config.wif_pool,
                 config.wif_provider):
        if not _JOB_RE.match(name):
            problems.append(f"invalid resource name {name!r}")
    if not re.match(r"^[^\s,@]+@[^\s,@]+\.iam\.gserviceaccount\.com$", config.control_vm_service_account or ""):
        problems.append("SPOT_CONTROL_SERVICE_ACCOUNT (--control-vm-service-account) must be the control VM's "
                        "service account e-mail; the watchdog needs iam.serviceAccountUser on exactly that account")
    if config.env.get("SPOT_CONTROL_SERVICE_ACCOUNT") and \
            config.env["SPOT_CONTROL_SERVICE_ACCOUNT"] != config.control_vm_service_account:
        problems.append("--control-vm-service-account differs from SPOT_CONTROL_SERVICE_ACCOUNT")
    if not _GH_REPO_RE.match(config.github_repo):
        problems.append("--github-repo must be owner/repo")
    if not _SCHEDULE_RE.match(config.schedule) or len(config.schedule.split()) != 5:
        problems.append("--schedule must be a five-field cron expression")
    if config.project_number and not re.match(r"^[0-9]{6,20}$", config.project_number):
        problems.append("--project-number must be numeric")
    if config.image and not re.match(rf"^{re.escape(config.image_repo)}@sha256:[0-9a-f]{{64}}$", config.image):
        problems.append(f"--image must be {config.image_repo}@sha256:<64 hex> (pinned by digest)")
    refs = config.secret_refs()
    for env_name, secret in refs.items():
        if not _SECRET_RE.match(secret):
            problems.append(f"invalid secret name {secret!r} for {env_name}")
    env = config.service_env()
    overlap = sorted(set(env) & set(refs))
    if overlap:
        problems.append(f"{', '.join(overlap)} must come from Secret Manager, never as plain env vars")
    for key, value in env.items():
        if any(c in value for c in "|\n\r"):
            problems.append(f"{key} contains a character the service spec can't carry ('|' or a newline)")
    # The service runs the same checks before it creates anything; refusing here keeps a broken one from deploying.
    dummy = {key: "x" for key in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "DASHBOARD_CONTROL_SECRET")}
    control = cv.ControlConfig.from_env(config.project, {**env, **dummy})
    problems.extend(f"control VM settings: {p}" for p in
                    control.provisioning_problems() + control.tracking_problems() + control.create_problems())
    return problems


# --------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """``check`` decides between ``create`` (check failed: resource missing) and ``update`` (it exists); ``run``
    commands always run. ``require`` steps only verify and stop the deploy with ``hint`` when they fail."""

    title: str
    run: tuple[tuple[str, ...], ...] = ()
    check: tuple[str, ...] | None = None
    create: tuple[tuple[str, ...], ...] = ()
    update: tuple[tuple[str, ...], ...] = ()
    require: tuple[tuple[tuple[str, ...], str], ...] = ()  # (command that must print ENABLED, what to do if not)
    files: Mapping[str, str] = field(default_factory=dict)  # name -> content, referenced as @FILE:<name>


def _c(*argv: str) -> tuple[str, ...]:
    return tuple(argv)


def iam_bindings(config: DeployConfig) -> list[tuple[str, str, str, str]]:
    """(resource, member, role, why) of every IAM binding the deploy adds; the documentation lists the same."""
    runtime = f"serviceAccount:{config.runtime_email}"
    bindings = [
        (f"project {config.project}" + (" (condition: instances and disks named spot-control-*)"
                                        if config.compute_condition else ""), runtime, ROLE_COMPUTE,
         "list, describe, create, start, stop, set-scheduling, and delete the spot-control-a/b VMs and their boot "
         "disks; list machine types and use the subnetwork and an external IP"),
        (f"service account {config.control_vm_service_account}", runtime, ROLE_ACT_AS,
         "create control VMs that run as that account (iam.serviceAccounts.actAs), and no other"),
        (f"bucket gs://{config.state_bucket}", runtime, ROLE_OBJECTS,
         "read and conditionally write the state and lease objects; no other bucket"),
    ]
    for env_name, secret in sorted(config.secret_refs().items()):
        bindings.append((f"secret {secret}", runtime, ROLE_SECRET, f"read {env_name} at instance start; no other secret"))
    bindings.append((f"Cloud Run service {config.service}", f"serviceAccount:{config.invoker_email}", ROLE_INVOKER,
                     "Cloud Scheduler calls this one service (run.routes.invoke); nobody else can"))
    bindings.append((f"repository {config.repository}", f"serviceAccount:{config.publisher_email}", ROLE_AR_WRITER,
                     "GitHub Actions pushes the watchdog image to this repository only"))
    bindings.append((f"service account {config.publisher_email}",
                     f"principalSet://iam.googleapis.com/projects/{config.number}/locations/global/"
                     f"workloadIdentityPools/{config.wif_pool}/attribute.repository/{config.github_repo}",
                     ROLE_WIF_USER, f"only {config.github_repo} workflows that the provider admits may impersonate it"))
    return bindings


def build_steps(config: DeployConfig) -> list[Step]:
    p = f"--project={config.project}"
    region = f"--region={config.region}"
    location = f"--location={config.region}"
    bucket = f"gs://{config.state_bucket}"
    runtime = f"serviceAccount:{config.runtime_email}"
    steps = [Step("Enable the APIs", run=(_c("gcloud", "services", "enable", *APIS, p),))]
    steps.append(Step(
        f"State bucket {bucket}: regional, uniform bucket-level access, public access prevention",
        check=_c("gcloud", "storage", "buckets", "describe", bucket, p, "--format=value(name)"),
        create=(_c("gcloud", "storage", "buckets", "create", bucket, p, location, "--default-storage-class=STANDARD",
                   "--uniform-bucket-level-access", "--public-access-prevention"),),
        run=(_c("gcloud", "storage", "buckets", "update", bucket, p, "--uniform-bucket-level-access",
                "--public-access-prevention"),)))
    for account, display in ((config.runtime_account, "Control VM watchdog (Cloud Run service)"),
                             (config.invoker_account, "Control VM watchdog scheduler invoker"),
                             (config.publisher_account, "Control VM watchdog image publisher (GitHub Actions)")):
        steps.append(Step(
            f"Service account {config.email(account)}",
            check=_c("gcloud", "iam", "service-accounts", "describe", config.email(account), p, "--format=value(email)"),
            create=(_c("gcloud", "iam", "service-accounts", "create", account, p, f"--display-name={display}"),)))
    steps.append(Step(
        f"Artifact Registry repository {config.repository}",
        check=_c("gcloud", "artifacts", "repositories", "describe", config.repository, location, p,
                 "--format=value(name)"),
        create=(_c("gcloud", "artifacts", "repositories", "create", config.repository, "--repository-format=docker",
                   location, p, "--description=Control VM watchdog images"),)))
    refs = config.secret_refs()
    by_secret: dict[str, list[str]] = {}
    for env_name, secret in refs.items():
        by_secret.setdefault(secret, []).append(env_name)
    steps.append(Step(
        "Secrets the service references (each needs an enabled version; this deploy never writes secret values)",
        require=tuple((_c("gcloud", "secrets", "versions", "describe", "latest", f"--secret={secret}", p,
                          "--format=value(state)"),
                       f"secret {secret} ({', '.join(sorted(by_secret[secret]))}) has no enabled version; create it "
                       f"from stdin: printf '%s' \"${sorted(by_secret[secret])[0]}\" | gcloud secrets create {secret} "
                       f"--data-file=- --replication-policy=automatic {p}")
                      for secret in sorted(by_secret))))

    condition = {"expression": COMPUTE_CONDITION_EXPRESSION, "title": "spot-control-vms-only",
                 "description": "Instances and disks named spot-control-*; other resource types unrestricted"}
    compute_args = (["--condition-from-file=@FILE:compute-condition.json"] if config.compute_condition
                    else ["--condition=None"])
    iam_runs = [
        _c("gcloud", "projects", "add-iam-policy-binding", config.project, f"--member={runtime}",
           f"--role={ROLE_COMPUTE}", *compute_args, "--format=none"),
        _c("gcloud", "iam", "service-accounts", "add-iam-policy-binding", config.control_vm_service_account, p,
           f"--member={runtime}", f"--role={ROLE_ACT_AS}", "--format=none"),
        _c("gcloud", "storage", "buckets", "add-iam-policy-binding", bucket, f"--member={runtime}",
           f"--role={ROLE_OBJECTS}", "--format=none"),
    ]
    iam_runs += [_c("gcloud", "secrets", "add-iam-policy-binding", secret, p, f"--member={runtime}",
                    f"--role={ROLE_SECRET}", "--format=none") for secret in sorted(set(refs.values()))]
    steps.append(Step("Runtime service account IAM (narrowest resource for each role)", run=tuple(iam_runs),
                      files={"compute-condition.json": json.dumps(condition, indent=2)}
                      if config.compute_condition else {}))

    owner = config.github_repo.split("/", 1)[0]
    workflow_ref = f"{config.github_repo}/{IMAGE_WORKFLOW}@refs/heads/main"
    attribute_condition = (f"assertion.repository == '{config.github_repo}' && assertion.repository_owner == "
                           f"'{owner}' && assertion.ref == 'refs/heads/main' && assertion.workflow_ref == "
                           f"'{workflow_ref}'")
    mapping = ("google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,"
               "attribute.workflow_ref=assertion.workflow_ref")
    pool = (f"--workload-identity-pool={config.wif_pool}", "--location=global", p)
    steps.append(Step(
        f"Workload Identity pool {config.wif_pool}",
        check=_c("gcloud", "iam", "workload-identity-pools", "describe", config.wif_pool, "--location=global", p,
                 "--format=value(name)"),
        create=(_c("gcloud", "iam", "workload-identity-pools", "create", config.wif_pool, "--location=global", p,
                   "--display-name=GitHub Actions"),)))
    oidc = (f"--attribute-mapping={mapping}", f"--attribute-condition={attribute_condition}")
    steps.append(Step(
        f"GitHub OIDC provider {config.wif_provider} (only {IMAGE_WORKFLOW} on main of {config.github_repo})",
        check=_c("gcloud", "iam", "workload-identity-pools", "providers", "describe", config.wif_provider, *pool,
                 "--format=value(name)"),
        create=(_c("gcloud", "iam", "workload-identity-pools", "providers", "create-oidc", config.wif_provider, *pool,
                   "--issuer-uri=https://token.actions.githubusercontent.com", *oidc),),
        update=(_c("gcloud", "iam", "workload-identity-pools", "providers", "update-oidc", config.wif_provider, *pool,
                   *oidc),)))
    principal = (f"principalSet://iam.googleapis.com/projects/{config.number}/locations/global/"
                 f"workloadIdentityPools/{config.wif_pool}/attribute.repository/{config.github_repo}")
    steps.append(Step("Image publisher IAM", run=(
        _c("gcloud", "iam", "service-accounts", "add-iam-policy-binding", config.publisher_email, p,
           f"--member={principal}", f"--role={ROLE_WIF_USER}", "--format=none"),
        _c("gcloud", "artifacts", "repositories", "add-iam-policy-binding", config.repository, location, p,
           f"--member=serviceAccount:{config.publisher_email}", f"--role={ROLE_AR_WRITER}", "--format=none"),
    )))
    if not config.image:
        return steps

    env = config.service_env()
    env_arg = "--set-env-vars=^|^" + "|".join(f"{k}={v}" for k, v in sorted(env.items()))
    secrets_arg = "--set-secrets=" + ",".join(f"{k}={v}:latest" for k, v in sorted(refs.items()))
    steps.append(Step(f"Cloud Run service {config.service} (one tick per POST /tick; IAM only)", run=(
        _c("gcloud", "run", "deploy", config.service, p, region, f"--image={config.image}",
           f"--service-account={config.runtime_email}", "--no-allow-unauthenticated", "--ingress=all",
           "--concurrency=1", "--min-instances=0", "--max-instances=1", "--cpu=1", "--memory=512Mi",
           "--cpu-throttling", "--no-cpu-boost", f"--timeout={REQUEST_TIMEOUT_SECONDS}", env_arg, secrets_arg,
           "--labels=spot-role=control-watchdog", "--quiet"),
        _c("gcloud", "run", "services", "add-iam-policy-binding", config.service, p, region,
           f"--member=serviceAccount:{config.invoker_email}", f"--role={ROLE_INVOKER}", "--format=none"),
    )))
    if config.skip_scheduler:
        return steps
    scheduler = (p, location, f"--schedule={config.schedule}", "--time-zone=Etc/UTC",
                 f"--uri={config.service_url}/tick", "--http-method=POST",
                 f"--oidc-service-account-email={config.invoker_email}", f"--oidc-token-audience={config.service_url}",
                 f"--attempt-deadline={REQUEST_TIMEOUT_SECONDS}s", "--max-retry-attempts=0")
    steps.append(Step(
        f"Cloud Scheduler job {config.scheduler_job} ({config.schedule}, OIDC as {config.invoker_email})",
        check=_c("gcloud", "scheduler", "jobs", "describe", config.scheduler_job, location, p, "--format=value(name)"),
        create=(_c("gcloud", "scheduler", "jobs", "create", "http", config.scheduler_job, *scheduler,
                   "--description=Runs the control VM watchdog"),),
        update=(_c("gcloud", "scheduler", "jobs", "update", "http", config.scheduler_job, *scheduler),)))
    return steps


# --------------------------------------------------------------------------------------------
# Rendering and execution
# --------------------------------------------------------------------------------------------

_PLACEHOLDER = "${PROJECT_NUMBER}"


def _quote(arg: str) -> str:
    """shlex.quote, except that the ${PROJECT_NUMBER} placeholder stays expandable in the dry-run script."""
    if _PLACEHOLDER not in arg:
        return shlex.quote(arg)
    parts = (part.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
             for part in arg.split(_PLACEHOLDER))
    return '"' + _PLACEHOLDER.join(parts) + '"'


def _shell(argv: Sequence[str]) -> str:
    return " ".join(_quote(arg) for arg in argv)


def render(config: DeployConfig, steps: Sequence[Step]) -> str:
    """The dry-run script: every command, in order, as it would run. Nothing runs."""
    lines = ["#!/usr/bin/env bash", "# Control VM watchdog deploy (dry run): nothing below has run.", "set -euo pipefail"]
    if not config.project_number:
        lines.append(f"PROJECT_NUMBER=\"$(gcloud projects describe {shlex.quote(config.project)} "
                     "--format='value(projectNumber)')\"")
    for index, step in enumerate(steps, 1):
        lines += ["", f"# {index}. {step.title}"]
        for name, content in step.files.items():
            lines.append(f"# @FILE:{name} is a temporary file holding:")
            lines += [f"#   {line}" for line in content.splitlines()]
        for command, hint in step.require:
            lines.append(f"test \"$({_shell(command)})\" = ENABLED || {{ echo {shlex.quote(hint)} >&2; exit 1; }}")
        if step.check:
            lines.append(f"if {_shell(step.check)} >/dev/null 2>&1; then")
            lines += [f"  {_shell(c)}" for c in step.update] or ["  :  # exists"]
            lines.append("else")
            lines += [f"  {_shell(c)}" for c in step.create]
            lines.append("fi")
        lines += [_shell(c) for c in step.run]
    if not config.image:
        lines += ["", "# The Cloud Run service and the scheduler need --image: build the watchdog image first (see the",
                  "# Control watchdog image workflow), then run this deploy again with --image=<digest>."]
    lines += ["", "# GitHub repository variables for the image workflow (set them yourself with gh):"]
    for name, value in workflow_variables(config).items():
        lines.append(f"#   gh variable set {name} --repo {config.github_repo} --body {_quote(value)}")
    return "\n".join(lines) + "\n"


def workflow_variables(config: DeployConfig) -> dict[str, str]:
    return {"WATCHDOG_IMAGE": config.image_repo, "WATCHDOG_PUBLISHER_SA": config.publisher_email,
            "WATCHDOG_WIF_PROVIDER": config.wif_provider_name,
            "WATCHDOG_BASE_IMAGE": "gcr.io/google.com/cloudsdktool/google-cloud-cli:<version>-slim@sha256:<digest>"}


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), check=False, text=True, capture_output=True, timeout=600)


def execute(config: DeployConfig, steps: Sequence[Step], runner: Runner = default_runner,
            out: Callable[[str], None] = print) -> None:
    """Run the steps. Raises DeployError at the first failure; everything before it stays in place (idempotent)."""
    number = config.project_number
    if not number:
        result = runner(["gcloud", "projects", "describe", config.project, "--format=value(projectNumber)"])
        number = (result.stdout or "").strip()
        if result.returncode != 0 or not re.match(r"^[0-9]{6,20}$", number):
            raise DeployError(f"could not read the project number: {(result.stderr or '').strip()[:300]}")
    with tempfile.TemporaryDirectory(prefix="spot-watchdog-deploy-") as tmp:
        def materialize(argv: Sequence[str], files: Mapping[str, str]) -> list[str]:
            resolved = []
            for arg in argv:
                for name, content in files.items():
                    marker = f"@FILE:{name}"
                    if marker in arg:
                        path = Path(tmp) / name
                        path.write_text(content, encoding="utf-8")
                        arg = arg.replace(marker, str(path))
                resolved.append(arg.replace("${PROJECT_NUMBER}", number))
            return resolved

        def run(argv: Sequence[str], files: Mapping[str, str], what: str) -> None:
            command = materialize(argv, files)
            out(f"+ {shlex.join(command)}")
            result = runner(command)
            if result.returncode != 0:
                raise DeployError(f"{what} failed: {(result.stderr or '').strip()[:500]}")

        for index, step in enumerate(steps, 1):
            out(f"# {index}. {step.title}")
            for command, hint in step.require:
                result = runner(materialize(command, step.files))
                if result.returncode != 0 or (result.stdout or "").strip() != "ENABLED":
                    raise DeployError(hint)
            if step.check:
                exists = runner(materialize(step.check, step.files)).returncode == 0
                for command in (step.update if exists else step.create):
                    run(command, step.files, step.title)
                if exists and not step.update:
                    out("  exists")
            for command in step.run:
                run(command, step.files, step.title)


# --------------------------------------------------------------------------------------------
# Command-line interface
# --------------------------------------------------------------------------------------------

def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update the serverless control VM watchdog (Cloud Run service + Cloud Scheduler). "
                    "Reads the non-secret control VM settings from the environment (source the same env file "
                    "control_vm.py uses). --dry-run prints the commands and runs nothing.")
    parser.add_argument("--project", default=cv.DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION, help="Region of the service, scheduler, repo, and bucket")
    parser.add_argument("--bucket", default="", help="State bucket (default: <project>-spot-control-state)")
    parser.add_argument("--image", default="",
                        help="Watchdog image pinned by digest (<region>-docker.pkg.dev/<project>/<repo>/"
                             "control-watchdog@sha256:...); without it only the infrastructure is deployed")
    parser.add_argument("--control-vm-service-account", default=None,
                        help="Control VM service account (default: SPOT_CONTROL_SERVICE_ACCOUNT)")
    parser.add_argument("--github-repo", default=DEFAULT_GITHUB_REPO)
    parser.add_argument("--project-number", default="", help="Skip the project-number lookup")
    parser.add_argument("--schedule", default=DEFAULT_SCHEDULE,
                        help="Cron schedule of the ticks (default: %(default)s; every 2 minutes leaves the free tier "
                             "once a tick averages more than about 8 s)")
    parser.add_argument("--secret", action="append", default=[], metavar="ENV=SECRET",
                        help="Reference another Secret Manager secret for a service env var (repeatable)")
    parser.add_argument("--no-compute-condition", dest="compute_condition", action="store_false",
                        help="Grant the compute role without the spot-control-* IAM condition")
    parser.add_argument("--skip-scheduler", action="store_true",
                        help="Deploy the service but not the scheduler (call it by hand first)")
    parser.add_argument("--no-verify-signature", dest="verify_signature", action="store_false",
                        help="Check the OIDC token's claims but not its signature in the server (Cloud Run IAM still "
                             "verifies it); only if requests arrive with the signature stripped")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands; run nothing")
    return parser


def config_from_args(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> DeployConfig:
    env = os.environ if env is None else env
    secrets = {}
    for item in args.secret:
        name, sep, secret = item.partition("=")
        allowed = set(SECRET_ENV) | {TRACK_SECRET_ENV[0]}
        if not sep or name not in allowed:
            raise DeployError(f"--secret must be ENV=SECRET with ENV one of {', '.join(sorted(allowed))}")
        secrets[name] = secret
    plain = {key: str(env.get(key, "") or "") for key in PASSTHROUGH_ENV}
    return DeployConfig(
        project=args.project, region=args.region, bucket=args.bucket, image=args.image,
        control_vm_service_account=args.control_vm_service_account or plain.get("SPOT_CONTROL_SERVICE_ACCOUNT", ""),
        github_repo=args.github_repo, project_number=args.project_number, schedule=args.schedule,
        compute_condition=args.compute_condition, skip_scheduler=args.skip_scheduler, secrets=secrets, env=plain,
        verify_signature=args.verify_signature)


def main(argv: Sequence[str] | None = None, *, runner: Runner = default_runner,
         env: Mapping[str, str] | None = None) -> int:
    args = args_parser().parse_args(argv)
    try:
        config = config_from_args(args, env)
        problems = validate(config)
        if problems:
            raise DeployError("refusing to deploy:\n  - " + "\n  - ".join(problems))
        steps = build_steps(config)
        if args.dry_run:
            sys.stdout.write(render(config, steps))
            return 0
        execute(config, steps, runner)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Deployed. Service: {config.service} ({config.service_url.replace('${PROJECT_NUMBER}', 'NUMBER')}); "
          f"state: {config.state_uri}.")
    if not config.image:
        print("Build the watchdog image, then run again with --image=<digest> to deploy the service and the "
              "scheduler.")
    return 0
