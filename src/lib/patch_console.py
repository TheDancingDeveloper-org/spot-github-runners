"""Runner patch console for the dashboard: config-repo manifests, validation, rollout preview, hot-patching.

Standard library only. Everything that talks to GitHub goes through :class:`ConfigRepoClient`, whose HTTP
transport is injectable (``requester(method, url, headers, body, timeout) -> (status, bytes)``), so tests
never reach GitHub. Saving a manifest never writes to the default branch: it creates a new branch from the
base commit the editor loaded, commits the file there (GitHub enforces the file's blob SHA), and opens a
pull request. A base commit that is no longer the default-branch HEAD is a conflict.

Hot-patching runs through an executor. The control VM image has no ``gcloud`` and no SSH path to runner
hosts, so the default executor refuses with a clear reason; :class:`GcloudHotPatchExecutor` (opt-in, where
``gcloud`` and operator credentials exist) wraps ``runner_manager.apply_live_patch``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from typing import Any, Callable, Iterable, Mapping, Protocol
import urllib.parse

from .runner_patch import (
    PATCH_RECEIPT_READ_COMMAND,
    SCHEMA_VERSION,
    HostPatchReceipt,
    RunnerPatch,
    classify_patch_diff,
    destructive_patch_changes,
    host_capacity_problems,
    merge_patch_dicts,
    parse_manifest_text,
    parse_patch_receipt,
    validate_patch_dict,
)

GITHUB_API_BASE = "https://api.github.com"
DEFAULTS_PATH = "defaults.yaml"
MAX_MANIFEST_BYTES = 256 * 1024
BRANCH_PREFIX = "spot-dashboard/"
ENV_HOT_PATCH_EXECUTOR = "DASHBOARD_HOT_PATCH_EXECUTOR"
HOT_PATCH_UNAVAILABLE_REASON = (
    "Hot-patching is not available from the control VM: its dashboard image has no gcloud and no SSH path "
    "to runner hosts. Run `src/spot_runner_manager.py --patch-repo OWNER/REPO --patch-live` from an operator "
    "workstation instead."
)
_REPO_MANIFEST_RE = re.compile(r"^repos/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})\.yaml$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FIELD_RE = re.compile(r"\b((?:hardware|host|container)\.[a-z_]+)\b")
SECTION_KEYS = {
    "hardware": {"arch", "isa", "extra_labels", "min_vcpu", "min_ram_gib", "qualify_command"},
    "host": {"apt_packages", "sysctl", "files", "boot_mkdirs", "boot_script"},
    "container": {"apt_packages", "sysctl", "mounts", "files", "dockerfile_run"},
}
TOP_LEVEL_KEYS = {"schema", "repository", *SECTION_KEYS}
CLASSIFICATIONS = ("noop", "hot_patch", "recreate_containers", "replace_vm")

Requester = Callable[..., "tuple[int, bytes]"]


class PatchConsoleError(RuntimeError):
    """Base error; ``status`` is the HTTP status the dashboard answers with."""

    status = 502

    def __init__(self, message: str, *, body: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.body = dict(body or {})


class GitHubRestError(PatchConsoleError):
    # 424 Failed Dependency, not 502: GitHub (or Compute Engine) failed, not the control VM. The edge Worker
    # treats a gateway 5xx as an outage and would retry it forever.
    status = 424

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message, body={"code": "upstream_error", "upstream_status": http_status})
        self.http_status = http_status


class ConflictError(PatchConsoleError):
    status = 409


class InvalidRequestError(PatchConsoleError):
    status = 400


class IdempotencyMismatchError(PatchConsoleError):
    status = 422


class HostRefusedError(PatchConsoleError):
    """One host cannot be hot-patched safely (its deployment details are not recorded)."""

    status = 409


class HotPatchUnavailable(PatchConsoleError):
    # 501 rather than 503: the edge Worker would turn a 503 into an endless control-VM wait.
    status = 501


def _ambiguous(exc: GitHubRestError) -> bool:
    """True when GitHub's answer was lost (transport failure, timeout) or was a server error: the write may or
    may not have happened."""
    return exc.http_status == 0 or exc.http_status >= 500


def _retry_error(step: str, exc: Exception) -> GitHubRestError:
    error = GitHubRestError(f"GitHub did not confirm whether it could {step} ({exc}); nothing was deleted. "
                            "Retry with the same Idempotency-Key to finish.", http_status=getattr(exc, "http_status", 0))
    error.body["retry_same_key"] = True
    return error


def config_repo_token(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return str(env.get("SPOT_CONFIG_GH_TOKEN") or env.get("GH_TOKEN") or "").strip()


def manifest_path_for(repository: str) -> str:
    if not isinstance(repository, str) or not _REPO_NAME_RE.match(repository) or repository.split("/")[1] in (".", ".."):
        raise InvalidRequestError(f"repository must be owner/repo, got {repository!r}")
    return f"repos/{repository}.yaml"


def repository_for_path(path: str) -> str | None:
    """``owner/repo`` for a repository manifest path, None for ``defaults.yaml``; raises for anything else."""
    if path == DEFAULTS_PATH:
        return None
    match = _REPO_MANIFEST_RE.match(path or "")
    if match is None or match.group(2) in (".", ".."):
        raise InvalidRequestError("path must be defaults.yaml or repos/<owner>/<repo>.yaml")
    return f"{match.group(1)}/{match.group(2)}"


# --------------------------------------------------------------------------------------------------
# GitHub REST client
# --------------------------------------------------------------------------------------------------


class ConfigRepoClient:
    """Minimal GitHub REST client for the runner config repository (injectable transport)."""

    def __init__(self, repo: str, token: str, requester: Requester | None = None, timeout: float = 20.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise ValueError(f"config repository must be owner/repo, got {repo!r}")
        self.repo = repo
        self.token = token
        self.timeout = timeout
        if requester is None:
            from .dashboard_auth import default_http_request

            requester = default_http_request
        self._requester = requester

    def _call(self, method: str, path: str, body: Any = None, *, ok: Iterable[int] = (200,)) -> tuple[int, Any]:
        if not self.token:
            raise GitHubRestError("No GitHub token for the config repository (set SPOT_CONFIG_GH_TOKEN or GH_TOKEN)")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "spot-dashboard-patch-console/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._requester(method, f"{GITHUB_API_BASE}/{path}", headers, data, self.timeout)
        except PatchConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures become a clear 502
            raise GitHubRestError(f"GitHub request failed: {type(exc).__name__}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if status not in tuple(ok):
            message = parsed.get("message") if isinstance(parsed, dict) else None
            raise GitHubRestError(f"GitHub {method} {path.split('?', 1)[0]} returned HTTP {status}"
                                  + (f": {message}" if message else ""), http_status=status)
        return status, parsed

    def default_branch(self) -> str:
        _, meta = self._call("GET", f"repos/{self.repo}")
        branch = meta.get("default_branch") if isinstance(meta, dict) else None
        if not isinstance(branch, str) or not branch.strip():
            raise GitHubRestError(f"{self.repo} does not report a default branch")
        return branch

    def branch_head(self, branch: str) -> str:
        _, ref = self._call("GET", f"repos/{self.repo}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}")
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise GitHubRestError(f"Could not resolve the HEAD commit of {self.repo}@{branch}")
        return sha

    def resolve(self) -> tuple[str, str]:
        branch = self.default_branch()
        return branch, self.branch_head(branch)

    def tree_paths(self, sha: str) -> list[str]:
        _, tree = self._call("GET", f"repos/{self.repo}/git/trees/{sha}?recursive=1")
        if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
            raise GitHubRestError(f"Unexpected git tree payload for {self.repo}@{sha}")
        if tree.get("truncated"):
            raise GitHubRestError(f"Git tree listing for {self.repo}@{sha} was truncated")
        return [e["path"] for e in tree["tree"] if isinstance(e, dict) and e.get("type") == "blob"
                and isinstance(e.get("path"), str)]

    def read_file(self, path: str, ref: str) -> tuple[str, str] | None:
        """(text, blob sha) of ``path`` at ``ref``, or None when it does not exist."""
        try:
            _, payload = self._call("GET", f"repos/{self.repo}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(ref, safe='')}")
        except GitHubRestError as exc:
            if exc.http_status == 404:
                return None
            raise
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not isinstance(payload.get("sha"), str):
            raise GitHubRestError(f"Unexpected content payload for {self.repo}:{path}@{ref}")
        try:
            text = base64.b64decode(payload.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubRestError(f"{self.repo}:{path}@{ref} is not UTF-8 text") from exc
        return text, payload["sha"]

    def branch_head_or_none(self, branch: str) -> str | None:
        try:
            return self.branch_head(branch)
        except GitHubRestError as exc:
            if exc.http_status == 404:
                return None
            raise

    def find_pull_request(self, branch: str) -> dict[str, Any] | None:
        """The pull request (open or closed) whose head is ``branch`` in this repository, or None."""
        owner = self.repo.split("/", 1)[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        _, prs = self._call("GET", f"repos/{self.repo}/pulls?head={head}&state=all")
        if not isinstance(prs, list) or not prs:
            return None
        pr = prs[0] if isinstance(prs[0], dict) else {}
        return {"number": pr.get("number"), "url": pr.get("html_url"), "state": pr.get("state")}

    def create_branch(self, branch: str, sha: str) -> None:
        self._call("POST", f"repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}, ok=(201,))

    def delete_branch(self, branch: str) -> None:
        self._call("DELETE", f"repos/{self.repo}/git/refs/heads/{urllib.parse.quote(branch, safe='/')}", ok=(204,))

    def put_file(self, path: str, branch: str, text: str, message: str, blob_sha: str | None) -> str:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if blob_sha:
            body["sha"] = blob_sha
        try:
            _, result = self._call("PUT", f"repos/{self.repo}/contents/{path}", body, ok=(200, 201))
        except GitHubRestError as exc:
            if exc.http_status in (409, 422):
                raise ConflictError(f"{path} changed on GitHub since it was loaded; reload and edit again") from exc
            raise
        commit = result.get("commit", {}).get("sha") if isinstance(result, dict) else None
        return commit if isinstance(commit, str) else ""

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        _, pr = self._call("POST", f"repos/{self.repo}/pulls",
                           {"title": title, "head": head, "base": base, "body": body}, ok=(201,))
        if not isinstance(pr, dict):
            raise GitHubRestError("GitHub returned an unexpected pull request payload")
        return {"number": pr.get("number"), "url": pr.get("html_url"), "state": pr.get("state")}


# --------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------


_MESSAGE_FIELDS = (
    ("Instruction set", "hardware.isa"),
    ("Unsupported instruction set", "hardware.isa"),
    ("No GCE CPU platform", "hardware.isa"),
    ("Mount source", "container.mounts"),
    ("Duplicate mount target", "container.mounts"),
)


def _field_of(message: str, section: str) -> str:
    for prefix, field_name in _MESSAGE_FIELDS:
        if message.startswith(prefix):
            return field_name
    match = _FIELD_RE.search(message)
    return match.group(1) if match else section


def validate_manifest(
    text: Any,
    *,
    path: str,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[RunnerPatch | None, list[dict[str, str]], dict[str, Any] | None]:
    """Validate manifest text for ``path``. Returns (effective patch or None, field errors, parsed dict).

    Errors are ``{"field": ..., "message": ...}``. Each section is checked on its own so one request reports
    every broken section, and unknown keys (typos validate_patch_dict would ignore) are errors.

    A repository manifest is an override: anything that depends on inherited settings (``hardware.isa``
    against the architecture, host and container checks that depend on it) uses the hardware it gets after
    merging with ``defaults``, and the result is validated as merged (what hosts actually get). Only
    structural checks apply to the override on its own.
    """
    errors: list[dict[str, str]] = []
    repository = repository_for_path(path)
    if not isinstance(text, str) or not text.strip():
        return None, [{"field": "content", "message": "Manifest content is empty"}], None
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        return None, [{"field": "content", "message": f"Manifest is larger than {MAX_MANIFEST_BYTES} bytes"}], None
    try:
        data = parse_manifest_text(text)
    except (ValueError, TypeError) as exc:
        return None, [{"field": "content", "message": f"Could not parse the manifest: {exc}"}], None
    if not isinstance(data, dict):
        return None, [{"field": "content", "message": "Manifest root must be a mapping"}], None
    for key in sorted(set(data) - TOP_LEVEL_KEYS):
        errors.append({"field": str(key), "message": f"Unknown top-level key {key!r}"})
    schema = data.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        errors.append({"field": "schema", "message": f"schema must be {SCHEMA_VERSION!r}"})
    declared = data.get("repository")
    if declared not in (None, ""):
        if repository is None:
            errors.append({"field": "repository", "message": "defaults.yaml must not name a repository"})
        elif not isinstance(declared, str) or declared.lower() != repository.lower():
            errors.append({"field": "repository", "message": f"repository must be {repository!r} for {path}"})
    context_arch = _context_arch(data, defaults if repository is not None else None)
    for section, allowed in SECTION_KEYS.items():
        value = data.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append({"field": section, "message": f"{section} section must be a mapping"})
            continue
        for key in sorted(set(value) - allowed):
            errors.append({"field": f"{section}.{key}", "message": f"Unknown key {key!r} in {section}"})
        # Checked in the architecture the host will actually have (inherited from defaults.yaml when the
        # override doesn't set one), so an override adding isa: [neon] under an arm64 default is valid.
        if section == "hardware":
            probe: dict[str, Any] = {"hardware": {**value, "arch": value.get("arch", context_arch)}}
        else:
            probe = {section: value, "hardware": {"arch": context_arch}}
        try:
            validate_patch_dict(probe)
        except (ValueError, TypeError) as exc:
            errors.append({"field": _field_of(str(exc), section), "message": str(exc)})
    if errors:
        return None, errors, data
    if repository is None:
        try:
            return validate_patch_dict(data), [], data
        except (ValueError, TypeError) as exc:
            return None, [{"field": _field_of(str(exc), "content"), "message": str(exc)}], data
    merged = merge_patch_dicts(dict(defaults or {}), data)
    if not merged.get("repository"):
        merged["repository"] = repository
    try:
        effective = validate_patch_dict(merged)
    except (ValueError, TypeError) as exc:
        return None, [{"field": "effective", "message": f"Merged with defaults.yaml: {exc}"}], data
    return effective, [], data


_ARCH_NAMES = {"x86_64": "x86_64", "x64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}


def _context_arch(data: Mapping[str, Any], defaults: Mapping[str, Any] | None) -> str:
    """The architecture the merged patch will have: the override's, else the inherited default's, else x86_64.

    An unrecognized inherited value falls back to x86_64 here; the merged validation reports it."""
    for source in (data, defaults or {}):
        hardware = source.get("hardware") if isinstance(source, Mapping) else None
        if isinstance(hardware, Mapping) and "arch" in hardware:
            return _ARCH_NAMES.get(str(hardware.get("arch")).strip().lower(), "x86_64")
    return "x86_64"


def patch_summary(patch: RunnerPatch) -> dict[str, Any]:
    return {"patch_sha256": patch.patch_sha256, "effective": patch.to_canonical_dict()}


# --------------------------------------------------------------------------------------------------
# Hosts, executors, classification
# --------------------------------------------------------------------------------------------------


UNRECORDED_REASON = (
    "no spot-runner-patch metadata: the host was provisioned before patch recording. It stays unknown until it is "
    "reprovisioned or hot-patched once from a workstation (src/spot_runner_manager.py --patch-live records it)"
)


UNRECORDED_HOST_REASON = (
    "provisioned before patch recording; reprovision or run --patch-live. The host has no spot-target label and no "
    "spot-runner-patch metadata, so neither its repository nor its patch is known and it is listed for every "
    "repository. It stays unknown, and blocks hot-patching, until it is reprovisioned or hot-patched once from a "
    "workstation (src/spot_runner_manager.py --patch-live records it)"
)


def pending_patch_reason(pending: str) -> str:
    """Why a host whose provisioning patch is still pending classifies as ``unknown``."""
    requested = ("no patch" if pending == "none" else "an unreadable patch record" if len(pending) != 64
                 else f"patch {pending[:12]}")
    return (f"pending, unconfirmed: provisioning requested {requested} (spot-runner-patch-pending-sha256), but "
            "the bootstrap has not confirmed it, so the patch this host runs is unknown. It may still be "
            "bootstrapping, or its package install or patch step failed. It stays unknown until "
            "src/spot_runner_manager.py confirms a completed bootstrap, or until --patch-live applies "
            + ("a patch" if pending == "none" else
               "the pending patch (any other patch needs a new VM, because the attempt's partial changes are "
               "unknown)"))


def runner_hosts(instances: Iterable[Mapping[str, Any]], repository: str) -> list[dict[str, Any]]:
    """Running CI runner hosts of ``repository``: recorded ``patch_target``, or the host's ``spot-target`` label.

    A host marked ``unrecorded_host`` (found by name, provisioned before target labels and patch recording) could
    belong to any repository, so it is included for every repository; ``classify_hosts`` reports it ``unknown``.
    """
    from .target_config import target_label_value

    wanted = repository.lower()
    label = target_label_value(repository)
    hosts = []
    for inst in instances:
        if not isinstance(inst, Mapping):
            continue
        if inst.get("role") != "cicd_runner" or inst.get("status") != "RUNNING":
            continue
        if (inst.get("unrecorded_host") is not True and str(inst.get("patch_target") or "").lower() != wanted
                and inst.get("target_label") != label):
            continue
        hosts.append(dict(inst))
    return hosts


def _boot_problem(host: Mapping[str, Any]) -> str:
    """Why the applied record may not describe the current boot ('' when it was confirmed in this boot)."""
    recorded = str(host.get("patch_boot") or "")
    current = str(host.get("last_start_timestamp") or "")
    if not recorded:
        return ("the applied patch record has no boot binding (spot-runner-patch-boot), so a reboot since it was "
                "confirmed can't be ruled out; reprovision or re-run --patch-live")
    if not current:
        return "the host's lastStartTimestamp is unknown, so the applied patch record can't be tied to this boot"
    if recorded != current:
        return (f"rebooted since the patch was confirmed (confirmed in the boot started {recorded}, now {current}), "
                "and a reboot can bring the runners back without it; reprovision or re-run --patch-live")
    return ""


def _recorded_manifest(host: Mapping[str, Any], target: RunnerPatch) -> tuple[bool, dict[str, Any] | None, str]:
    """(known, current manifest or None, detail) from the ``spot-runner-patch`` metadata recorded on a host."""
    sha = str(host.get("patch_sha256") or "")
    if not sha:
        return False, None, UNRECORDED_REASON
    stale = _boot_problem(host)
    if stale:
        return False, None, stale
    if sha == "none":
        return True, None, "no patch recorded at provisioning (instance metadata)"
    raw = host.get("patch_manifest_json") or ""
    if not raw:
        if sha == target.patch_sha256:
            return True, target.to_canonical_dict(), "sha256 recorded in instance metadata (manifest too large to record)"
        return False, None, ("only the sha256 is recorded (the manifest was larger than the metadata limit), so the "
                             "change cannot be classified")
    try:
        manifest = json.loads(raw)
        recorded_sha = validate_patch_dict(manifest).patch_sha256
    except (ValueError, TypeError, AttributeError) as exc:
        return False, None, f"the recorded spot-runner-patch metadata is invalid ({type(exc).__name__})"
    if recorded_sha != sha:
        return False, None, "the recorded spot-runner-patch metadata does not match its recorded sha256"
    return True, manifest, "recorded in instance metadata"


class HotPatchExecutor(Protocol):
    available: bool
    reason: str

    def read_manifest(self, host: Mapping[str, Any]) -> HostPatchReceipt | dict[str, Any] | None:
        """Return the host's patch receipt (or recorded manifest), or None when it has none."""

    def apply(self, host: Mapping[str, Any], patch: RunnerPatch) -> dict[str, Any]:
        """Live-apply ``patch`` to ``host`` and return the per-host result."""


class UnavailableHotPatchExecutor:
    """Default on the control VM: it cannot reach runner hosts, so it never pretends to."""

    available = False

    def __init__(self, reason: str = HOT_PATCH_UNAVAILABLE_REASON) -> None:
        self.reason = reason

    def read_manifest(self, host: Mapping[str, Any]) -> dict[str, Any] | None:
        raise HotPatchUnavailable(self.reason)

    def apply(self, host: Mapping[str, Any], patch: RunnerPatch) -> dict[str, Any]:
        raise HotPatchUnavailable(self.reason)


_RUNNER_MANAGER_LOCK = threading.Lock()


class GcloudHotPatchExecutor:
    """Runs where ``gcloud`` and operator credentials exist; wraps ``runner_manager.apply_live_patch``.

    Every deployment value comes from the host record (its VM metadata and labels, recorded at provisioning):
    ``name``, ``zone``, ``machine_type``, ``runner_prefix``, ``runners_count``, ``runner_label``,
    ``runner_region``, and ``patch_target``. A host missing any of them is refused, never patched with
    runner_manager defaults, because ``sync_runner_labels`` would otherwise rewrite its scheduling labels.
    """

    def __init__(self, project: str, *, which: Callable[[str], str | None] | None = None,
                 run: Callable[..., Any] | None = None) -> None:
        self.project = project
        self._run = run or subprocess.run
        self.available = (which or shutil.which)("gcloud") is not None
        self.reason = "" if self.available else "gcloud is not installed where the dashboard runs"

    def read_manifest(self, host: Mapping[str, Any]) -> HostPatchReceipt:
        """The host's patch state: its last completed manifest and whether that receipt proves the current state."""
        name, zone = str(host.get("name") or ""), str(host.get("zone") or "")
        if not name or not zone:
            raise PatchConsoleError("host record has no name or zone")
        result = self._run(
            ["gcloud", "compute", "ssh", name, f"--zone={zone}", f"--project={self.project}", "--quiet",
             f"--command={PATCH_RECEIPT_READ_COMMAND}"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise PatchConsoleError(f"could not read the patch state of {name} (exit {result.returncode})")
        try:
            return parse_patch_receipt(result.stdout or "")
        except (ValueError, TypeError, AttributeError) as exc:
            raise PatchConsoleError(f"{name} has an unreadable patch state: {exc}") from exc

    def apply(self, host: Mapping[str, Any], patch: RunnerPatch) -> dict[str, Any]:
        from . import runner_manager

        outcome = self._with_runner_manager(lambda args: runner_manager.apply_live_patch(args, str(host["zone"])),
                                            host, patch)
        return {"result": "applied" if outcome == "hot_patch" else str(outcome)}

    def reconcile_labels(self, host: Mapping[str, Any], patch: RunnerPatch) -> None:
        """Sync the GitHub labels of a host that is already at ``patch`` (a noop never runs the live script)."""
        from . import runner_manager

        self._with_runner_manager(lambda args: runner_manager.reconcile_runner_labels(args, str(host["zone"])),
                                  host, patch)

    def _with_runner_manager(self, action: Callable[[Any], Any], host: Mapping[str, Any], patch: RunnerPatch) -> Any:
        """Run ``action`` with runner_manager arguments taken only from the host record; refuse incomplete records."""
        from . import runner_manager

        required = ("name", "zone", "machine_type", "runner_prefix", "runners_count", "runner_label",
                    "runner_region", "patch_target", "registration_target")
        missing = [k for k in required if not host.get(k)]
        registration = str(host.get("registration_target") or "")
        if registration and "/" not in registration and not host.get("runner_group_id"):
            missing.append("runner_group_id")  # org-registered runners: label sync needs the group
        if missing:
            raise HostRefusedError(f"refused: the host record does not carry {', '.join(missing)} "
                                   "(provisioned before deployment details were recorded)")
        violations, unknowns = _capacity(host, patch)
        if violations or unknowns:
            raise HostRefusedError("refused: " + "; ".join(violations + unknowns))
        zone, region = str(host["zone"]), str(host["runner_region"])
        if not zone.startswith(region + "-"):
            raise HostRefusedError(f"refused: recorded region {region} does not contain zone {zone}")
        if str(host["patch_target"]).lower() != str(patch.repository).lower():
            raise HostRefusedError(f"refused: the host belongs to {host['patch_target']}, not {patch.repository}")
        heavy = host.get("heavy_runner_pool")
        args = runner_manager.args_parser().parse_args([
            # --target is the registration scope (for example the org swack-tools), not the patch repository.
            "--max-cost", "0", "--project", self.project, "--target", registration,
            *(["--runner-group-id", str(int(host["runner_group_id"]))] if "/" not in registration else []),
            "--instance-name", str(host["name"]), "--region", region, "--zone", zone,
            "--instance-type", str(host["machine_type"]), "--runner-prefix", str(host["runner_prefix"]),
            "--runner-count", str(int(host["runners_count"])), "--runner-label", str(host["runner_label"]),
            *(["--heavy-runner-pool", str(int(heavy))] if heavy in (2, 3) else []),
        ])
        args.runner_patch = patch
        # A runner floor host's extra scheduling labels (recorded at provisioning) survive the label sync.
        args.extra_runner_labels = tuple(host.get("runner_extra_labels") or ())
        # runner_manager reads its project from a module global (set by its own main()); scope the override.
        with _RUNNER_MANAGER_LOCK:
            previous = runner_manager.PROJECT
            runner_manager.PROJECT = self.project
            try:
                return action(args)
            finally:
                runner_manager.PROJECT = previous


ENV_FLEET_INVENTORY = "DASHBOARD_FLEET_INVENTORY"


def gce_host_source(project: str) -> Callable[[], list[dict[str, Any]]]:
    """Runner hosts from a live Compute Engine listing (gcloud credentials or the GCE metadata server)."""

    def source() -> list[dict[str, Any]]:
        import asyncio

        from . import compute_api

        async def listing() -> list[dict[str, Any]]:
            auth = compute_api.get_auth(project)
            api = compute_api.AsyncGoogleApi(auth.token, 4)
            try:
                items, unreachables = await compute_api.list_instances(api, auth.project)
                hosts = compute_api.runner_host_records(items)
                # Observed capacity for the hardware-constraint check (one lookup per zone and machine type).
                shapes = {(h["zone"], h["machine_type"]) for h in hosts if h.get("zone") and h.get("machine_type")}
                specs = {}
                for zone, machine_type in shapes:
                    try:
                        specs[(zone, machine_type)] = await compute_api.machine_type_capacity(
                            api, auth.project, zone, machine_type)
                    except Exception:  # noqa: BLE001 - unknown capacity is reported as such, never guessed
                        specs[(zone, machine_type)] = {}
                for h in hosts:
                    h.update(specs.get((h.get("zone"), h.get("machine_type")), {}))
            finally:
                while not api.connections.empty():
                    _reader, writer = api.connections.get_nowait()
                    writer.close()
            if unreachables:
                raise RuntimeError(f"zones unreachable: {', '.join(unreachables[:5])}")
            return hosts

        return asyncio.run(listing())

    return source


def host_source_from_env(project: str, env: Mapping[str, str] | None = None) -> Callable[[], list[dict[str, Any]]] | None:
    env = os.environ if env is None else env
    return gce_host_source(project) if str(env.get(ENV_FLEET_INVENTORY, "") or "").strip().lower() == "gce" else None


def executor_from_env(project: str, env: Mapping[str, str] | None = None) -> HotPatchExecutor:
    env = os.environ if env is None else env
    choice = str(env.get(ENV_HOT_PATCH_EXECUTOR, "") or "").strip().lower()
    if choice == "gcloud":
        executor = GcloudHotPatchExecutor(project)
        if executor.available:
            return executor
        return UnavailableHotPatchExecutor(f"{ENV_HOT_PATCH_EXECUTOR}=gcloud but gcloud is not installed")
    return UnavailableHotPatchExecutor()


@dataclass
class HostClassification:
    host: dict[str, Any]
    classification: str
    current_patch_sha256: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.host.get("name"),
            "zone": self.host.get("zone"),
            "classification": self.classification,
            "current_patch_sha256": self.current_patch_sha256,
            "detail": self.detail,
        }


def classify_hosts(hosts: Iterable[Mapping[str, Any]], target: RunnerPatch,
                   executor: HotPatchExecutor) -> list[HostClassification]:
    """Classify each host against ``target``: live state via the executor, else the recorded manifest."""
    out = []
    for host in hosts:
        host = dict(host)
        if host.get("unrecorded_host") is True:
            # Never read or patched live: the host's repository is unknown, and the executor would refuse it anyway
            # for its missing deployment record, which would report a partial success instead of a refusal.
            out.append(HostClassification(host, "unknown", None, UNRECORDED_HOST_REASON))
            continue
        violations, unknowns = _capacity(host, target)
        if violations:
            # The host can never satisfy the patch's hardware constraints: it needs a different VM.
            out.append(HostClassification(host, "replace_vm", _recorded_sha(host),
                                          "undersized or incompatible for this patch: " + "; ".join(violations)))
            continue
        if unknowns:
            out.append(HostClassification(host, "unknown", _recorded_sha(host),
                                          "cannot verify the patch's hardware constraints: " + "; ".join(unknowns)))
            continue
        pending = str(host.get("patch_pending_sha256") or "")
        if pending:
            # Never noop: neither the applied record nor a host receipt proves that a pending bootstrap finished.
            out.append(HostClassification(host, "unknown", None, pending_patch_reason(pending)))
            continue
        proof, receipt_detail, receipt_pending = True, "", ""
        try:
            if executor.available:
                current = executor.read_manifest(host)
                source = "read from the host"
                if isinstance(current, HostPatchReceipt):
                    proof, receipt_detail, receipt_pending = current.proof, current.detail, current.pending_sha256
                    current = current.manifest
            elif "patch_manifest" in host:
                current = host.get("patch_manifest")
                source = "recorded by the fleet controller"
            else:
                known, current, source = _recorded_manifest(host, target)
                if not known:
                    out.append(HostClassification(host, "unknown", None, source))
                    continue
            if current is not None and not isinstance(current, dict):
                raise ValueError("recorded patch manifest is not a mapping")
            current_sha = validate_patch_dict(current).patch_sha256 if current is not None else None
            classification = classify_patch_diff(current, target, pending_sha256=receipt_pending)
            detail = source if source.startswith(("recorded", "no patch", "sha256")) else f"current patch {source}"
            dropped = (destructive_patch_changes(validate_patch_dict(current), target)
                       if current is not None and classification == "replace_vm" else [])
            if receipt_pending and receipt_pending != target.patch_sha256 and classification == "replace_vm":
                detail = (f"{detail}; an attempt to apply patch {receipt_pending[:12]} never completed, so its partial "
                          "changes are unknown and only a new VM removes them")
            elif dropped:
                detail = (f"{detail}; the live script cannot undo applied changes ({'; '.join(dropped)}), so a new VM "
                          "is needed")
            if not proof and classification == "noop":
                # Only a completion receipt from this boot proves the patch; a live re-apply writes one.
                classification = "hot_patch"
                detail = (f"the host names this patch, but {receipt_detail}; a hot patch re-applies it and writes a "
                          "completion receipt")
            elif not proof:
                detail = f"{detail} ({receipt_detail})"
            out.append(HostClassification(host, classification, current_sha, detail))
        except Exception as exc:  # noqa: BLE001 - an unreadable host is "unknown", never a guess
            out.append(HostClassification(host, "unknown", None, f"could not determine the current patch: {exc}"))
    return out


def _capacity(host: Mapping[str, Any], target: RunnerPatch) -> tuple[list[str], list[str]]:
    return host_capacity_problems(target, machine_type=str(host.get("machine_type") or ""),
                                  vcpu=host.get("vcpu"), ram_gib=host.get("ram_gib"),
                                  cpu_platform=str(host.get("cpu_platform") or ""))


def _recorded_sha(host: Mapping[str, Any]) -> str | None:
    sha = str(host.get("patch_sha256") or "")
    return sha if len(sha) == 64 else None


# --------------------------------------------------------------------------------------------------
# Console operations
# --------------------------------------------------------------------------------------------------


class PatchConsole:
    """Operations behind the dashboard's ``/api/patches`` endpoints."""

    def __init__(self, config_repo: str, *, token: str | None = None, requester: Requester | None = None,
                 executor: HotPatchExecutor | None = None,
                 host_source: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.config_repo = config_repo
        self._token = token
        self._requester = requester
        self.executor: HotPatchExecutor = executor or UnavailableHotPatchExecutor()
        # Live runner-host inventory (GCE instances with their recorded patch); None uses dashboard state records.
        self.host_source = host_source

    def _hosts(self, instances: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if self.host_source is None:
            return [dict(i) for i in instances if isinstance(i, Mapping)]
        try:
            return list(self.host_source())
        except PatchConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001 - inventory failures are a clear 502, never "no hosts"
            raise GitHubRestError(f"Could not list runner hosts from Compute Engine: {type(exc).__name__}: {exc}") from exc

    def client(self) -> ConfigRepoClient:
        token = self._token if self._token is not None else config_repo_token()
        return ConfigRepoClient(self.config_repo, token, self._requester)

    def _defaults(self, client: ConfigRepoClient, sha: str) -> dict[str, Any]:
        found = client.read_file(DEFAULTS_PATH, sha)
        if found is None:
            return {}
        try:
            parsed = parse_manifest_text(found[0])
        except (ValueError, TypeError) as exc:
            raise ConflictError(f"defaults.yaml at {sha[:12]} does not parse: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}

    def _override_errors(self, client: ConfigRepoClient, sha: str, defaults: Mapping[str, Any]) -> list[dict[str, str]]:
        """Field errors for every ``repos/<owner>/<repo>.yaml`` at ``sha`` that is invalid merged with ``defaults``.

        Each repository manifest is an override of defaults.yaml, so a defaults change can break every override
        after it merges. A failed listing or read is never treated as "no overrides": it raises a
        same-key-retryable error, because the check did not happen.
        """
        try:
            paths = client.tree_paths(sha)
        except PatchConsoleError as exc:
            raise _retry_error("list the repository manifests to check them against these defaults", exc) from exc
        errors: list[dict[str, str]] = []
        for path in sorted((p for p in paths if _REPO_MANIFEST_RE.match(p)), key=str.lower):
            try:
                found = client.read_file(path, sha)
            except PatchConsoleError as exc:
                raise _retry_error(f"read {path} to check it against these defaults", exc) from exc
            if found is None:
                raise _retry_error(f"read {path} to check it against these defaults",
                                   GitHubRestError(f"{path} is listed at {sha[:12]} but could not be read"))
            repository = repository_for_path(path)
            _, problems, _ = validate_manifest(found[0], path=path, defaults=defaults)
            for problem in problems:
                errors.append({"field": f"{path}:{problem['field']}", "repository": str(repository), "path": path,
                               "message": f"{repository} would be invalid with these defaults: {problem['message']}"})
        return errors

    def _validated(self, client: ConfigRepoClient, path: str, content: Any, sha: str) -> RunnerPatch:
        """Validate ``content`` for ``path`` at commit ``sha``, as it merges with everything it affects.

        A repository manifest merges with defaults.yaml at ``sha``; new defaults merge with every repository
        manifest at ``sha``, and a defaults change that breaks any of them is refused with field errors naming
        those repositories.
        """
        repository = repository_for_path(path)
        defaults = self._defaults(client, sha) if repository else None
        patch, errors, data = validate_manifest(content, path=path, defaults=defaults)
        if patch is not None and repository is None:
            errors = self._override_errors(client, sha, data or {})
            if errors:
                raise InvalidRequestError("These defaults would make repository manifests invalid",
                                          body={"errors": errors,
                                                "repositories": sorted({e["repository"] for e in errors})})
        if patch is None:
            raise InvalidRequestError("The manifest is invalid", body={"errors": errors})
        return patch

    def list_manifests(self) -> dict[str, Any]:
        client = self.client()
        branch, sha = client.resolve()
        paths = client.tree_paths(sha)
        repos = sorted({f"{m.group(1)}/{m.group(2)}" for m in map(_REPO_MANIFEST_RE.match, paths) if m},
                       key=str.lower)
        return {
            "config_repo": self.config_repo,
            "default_branch": branch,
            "head_sha": sha,
            "defaults": {"path": DEFAULTS_PATH, "present": DEFAULTS_PATH in paths},
            "repositories": [{"repository": r, "path": manifest_path_for(r)} for r in repos],
            "executor": {"available": self.executor.available, "reason": self.executor.reason},
        }

    def load_manifest(self, path: str) -> dict[str, Any]:
        repository_for_path(path)
        client = self.client()
        branch, sha = client.resolve()
        found = client.read_file(path, sha)
        content, blob = (found if found is not None else ("", None))
        body: dict[str, Any] = {
            "config_repo": self.config_repo, "path": path, "repository": repository_for_path(path),
            "default_branch": branch, "base_sha": sha, "blob_sha": blob, "exists": found is not None,
            "content": content,
        }
        if found is not None:
            defaults = self._defaults(client, sha) if path != DEFAULTS_PATH else None
            patch, errors, _ = validate_manifest(content, path=path, defaults=defaults)
            body["errors"] = errors
            if patch is not None:
                body.update(patch_summary(patch))
        return body

    def validate(self, path: str, content: Any) -> dict[str, Any]:
        defaults = None
        warning = ""
        if path != DEFAULTS_PATH:
            repository_for_path(path)
            try:
                client = self.client()
                defaults = self._defaults(client, client.resolve()[1])
            except PatchConsoleError as exc:
                warning = f"Checked without defaults.yaml (could not read it: {exc})"
        patch, errors, data = validate_manifest(content, path=path, defaults=defaults)
        if patch is not None and path == DEFAULTS_PATH:
            # New defaults must also keep every repository override valid once they merge.
            try:
                client = self.client()
                errors = self._override_errors(client, client.resolve()[1], data or {})
            except PatchConsoleError as exc:
                warning = f"Checked without the repository manifests (could not read them: {exc})"
            if errors:
                patch = None
        body: dict[str, Any] = {"valid": patch is not None, "errors": errors, "path": path}
        if warning:
            body["warning"] = warning
        if patch is not None:
            body.update(patch_summary(patch))
        return body

    def effective_patch(self, repository: str, content: Any = None) -> tuple[RunnerPatch, dict[str, Any]]:
        """The effective patch for ``repository``: a draft (merged with current defaults) or the repo HEAD."""
        path = manifest_path_for(repository)
        client = self.client()
        branch, sha = client.resolve()
        defaults = self._defaults(client, sha)
        source: dict[str, Any] = {"default_branch": branch, "head_sha": sha}
        if content is None:
            found = client.read_file(path, sha)
            if found is None:
                raise ConflictError(f"{self.config_repo} has no {path} at {sha[:12]}")
            content = found[0]
            source["draft"] = False
        else:
            source["draft"] = True
        patch, errors, _ = validate_manifest(content, path=path, defaults=defaults)
        if patch is None:
            raise InvalidRequestError("The manifest is invalid", body={"errors": errors})
        return validate_patch_dict(patch.to_canonical_dict(), source_commit=sha, source_branch=branch), source

    def preview(self, repository: str, instances: Iterable[Mapping[str, Any]], content: Any = None) -> dict[str, Any]:
        patch, source = self.effective_patch(repository, content)
        hosts = classify_hosts(runner_hosts(self._hosts(instances), repository), patch, self.executor)
        return {
            "repository": repository,
            "patch_sha256": patch.patch_sha256,
            "source": source,
            "hosts": [h.to_dict() for h in hosts],
            "hot_patch_allowed": bool(hosts) and self.executor.available and not source["draft"]
            and all(h.classification in ("hot_patch", "noop") for h in hosts)
            and any(h.classification == "hot_patch" for h in hosts),
            "executor": {"available": self.executor.available, "reason": self.executor.reason},
        }

    @staticmethod
    def branch_for(path: str, idempotency_key: str | None) -> str:
        """``spot-dashboard/<slug>-<digest>``: derived from the Idempotency-Key and path, so a retry after a lost
        response (even on a replacement VM) finds the branch and pull request it already created."""
        repository = repository_for_path(path)
        slug = re.sub(r"[^a-z0-9-]+", "-", (repository or "defaults").lower()).strip("-")[:60]
        suffix = (hashlib.sha256(f"{idempotency_key}\0{path}".encode("utf-8")).hexdigest()[:16]
                  if idempotency_key else secrets.token_hex(8))
        return f"{BRANCH_PREFIX}{slug}-{suffix}"

    def save(self, path: str, content: Any, base_sha: Any, message: Any = None,
             idempotency_key: str | None = None) -> dict[str, Any]:
        """Open a pull request for ``content`` (see :meth:`_save`).

        Every outcome that is not settled is retryable with the same key (``retry_same_key``, never stored as
        final): any failed reconciliation lookup, and any lost or 5xx GitHub answer anywhere in the save. Only
        definite answers are final: validation and conflicts, and GitHub 4xx rejections of a read or a write.
        """
        try:
            return self._save(path, content, base_sha, message, idempotency_key)
        except GitHubRestError as exc:
            if exc.body.get("retry_same_key") is True or not _ambiguous(exc):
                raise
            raise _retry_error("finish the save", exc) from exc

    @staticmethod
    def _lookup(step: str, fn: Callable[..., Any], *args: Any) -> Any:
        """A reconciliation lookup: the earlier outcome stays unknown when it fails, so any failure is retryable."""
        try:
            return fn(*args)
        except PatchConsoleError as exc:
            raise _retry_error(step, exc) from exc

    def _save(self, path: str, content: Any, base_sha: Any, message: Any,
              idempotency_key: str | None) -> dict[str, Any]:
        repository_for_path(path)  # Refuses any path that is not defaults.yaml or a repository manifest.
        if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
            raise InvalidRequestError("base_sha must be the 40-hex commit the manifest was loaded from")
        client = self.client()
        branch, head = client.resolve()
        new_branch = self.branch_for(path, idempotency_key)
        if new_branch == branch or not new_branch.startswith(BRANCH_PREFIX):
            raise InvalidRequestError("Refusing to write to the default branch")
        title = str(message or "").strip()[:200] or f"Update {path} from the Spot dashboard"
        # Reconcile first: an earlier attempt with this key may have created the branch, the commit, or the PR.
        existing_head = (self._lookup("check for an earlier attempt", client.branch_head_or_none, new_branch)
                         if idempotency_key else None)
        if existing_head is not None and existing_head != base_sha:
            on_branch = self._lookup("read the earlier attempt's commit", client.read_file, path, new_branch)
            if on_branch is None or on_branch[0] != content:
                raise IdempotencyMismatchError(
                    "This Idempotency-Key was already used for a different change to this file")
            patch = self._validated(client, path, content, base_sha)
            pr = self._lookup("find the earlier attempt's pull request", client.find_pull_request,
                              new_branch) or client.open_pull_request(
                new_branch, branch, title,
                f"Edited in the Spot dashboard from {branch}@{base_sha[:12]}.\n\n"
                f"Effective patch sha256: `{patch.patch_sha256}`")
            return {"status": "pull_request_opened", "reconciled": True, "path": path, "branch": new_branch,
                    "base_branch": branch, "base_sha": base_sha, "commit_sha": existing_head, "pull_request": pr,
                    **patch_summary(patch)}
        if head != base_sha:
            raise ConflictError(
                f"{branch} moved from {base_sha[:12]} to {head[:12]} since this manifest was loaded; reload and edit again",
                body={"status": "conflict", "head_sha": head, "base_sha": base_sha},
            )
        patch = self._validated(client, path, content, base_sha)
        found = client.read_file(path, base_sha)
        blob = found[1] if found is not None else None
        if found is not None and found[0] == content:
            raise InvalidRequestError("The manifest is unchanged; nothing to save")
        pr_body = (f"Edited in the Spot dashboard from {branch}@{base_sha[:12]}.\n\n"
                   f"Effective patch sha256: `{patch.patch_sha256}`")
        if existing_head is None:
            try:
                client.create_branch(new_branch, base_sha)
            except GitHubRestError as exc:
                # A lost response may hide a branch that was created; only GitHub's own answer settles it.
                if not _ambiguous(exc) and exc.http_status != 422:
                    raise
                if self._lookup("check whether the branch was created", client.branch_head_or_none, new_branch) is None:
                    raise _retry_error("create the branch", exc) from exc
        try:
            commit = client.put_file(path, new_branch, content, title, blob)
        except ConflictError:
            # GitHub rejected the commit outright (the file's blob changed): nothing was committed, so the
            # branch this request created, still at base_sha, can go.
            self._delete_untouched_branch(client, new_branch, base_sha)
            raise
        except GitHubRestError as exc:
            if not _ambiguous(exc):
                self._delete_untouched_branch(client, new_branch, base_sha)
                raise
            head_now = self._lookup("check whether the commit landed", client.branch_head_or_none, new_branch)
            on_branch = (self._lookup("check whether the commit landed", client.read_file, path, new_branch)
                         if head_now not in (None, base_sha) else None)
            if on_branch is None or on_branch[0] != content:
                raise _retry_error("commit the manifest", exc) from exc
            commit = head_now or ""
        try:
            pr = client.open_pull_request(new_branch, branch, title, pr_body)
        except GitHubRestError as exc:
            # Created, or never created? Ask GitHub before deciding anything; the branch always stays, so a retry
            # with the same key can finish the job.
            pr = self._lookup("check whether the pull request was opened", client.find_pull_request, new_branch)
            if pr is None:
                raise _retry_error("open the pull request", exc) if _ambiguous(exc) else exc
        return {
            "status": "pull_request_opened",
            "path": path,
            "branch": new_branch,
            "base_branch": branch,
            "base_sha": base_sha,
            "commit_sha": commit,
            "pull_request": pr,
            **patch_summary(patch),
        }

    @staticmethod
    def _delete_untouched_branch(client: ConfigRepoClient, branch: str, base_sha: str) -> None:
        """Delete a branch this request created only while it provably holds no commit of ours."""
        try:
            if client.branch_head_or_none(branch) == base_sha:
                client.delete_branch(branch)
        except PatchConsoleError:
            pass

    def hot_patch(self, repository: str, patch_sha256: Any, instances: Iterable[Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
        """Apply the repository's current effective patch to every recorded host, only when all are hot_patch/noop."""
        if not self.executor.available:
            raise HotPatchUnavailable(self.executor.reason, body={"code": "hot_patch_unavailable"})
        patch, source = self.effective_patch(repository)
        if patch_sha256 != patch.patch_sha256:
            raise ConflictError(
                "The effective patch changed since the preview; preview again before hot-patching",
                body={"status": "stale_patch", "patch_sha256": patch.patch_sha256},
            )
        hosts = classify_hosts(runner_hosts(self._hosts(instances), repository), patch, self.executor)
        if not hosts:
            raise ConflictError(f"No running runner hosts are recorded for {repository}")
        blocked = [h for h in hosts if h.classification not in ("hot_patch", "noop")]
        base = {"repository": repository, "patch_sha256": patch.patch_sha256, "source": source}
        if blocked:
            raise ConflictError(
                "Hot-patching is allowed only when every host classifies as hot_patch or noop",
                body={**base, "status": "refused", "hosts": [h.to_dict() for h in hosts]},
            )
        results = []
        for h in hosts:
            entry = h.to_dict()
            if h.classification == "noop":
                # Re-derived from the host, so a retry after a lost response never re-runs a host that already
                # applied this patch (its receipt and recorded sha already match). Its GitHub labels are still
                # reconciled when the executor can: a noop is the only step that reaches a host with stale labels.
                entry["result"] = "noop"
                entry["detail"] = f"already at this patch ({h.detail})"
                reconcile = getattr(self.executor, "reconcile_labels", None)
                if reconcile is not None:
                    try:
                        reconcile(h.host, patch)
                        entry["detail"] += "; GitHub labels reconciled"
                    except HostRefusedError as exc:
                        entry["result"] = "refused"
                        entry["detail"] = f"already at this patch, but its GitHub labels were not reconciled: {exc}"
                    except Exception as exc:  # noqa: BLE001 - report per host, keep going
                        entry["result"] = "failed"
                        entry["detail"] = (f"already at this patch, but reconciling its GitHub labels failed: "
                                           f"{type(exc).__name__}: {exc}")
            else:
                try:
                    entry.update(self.executor.apply(h.host, patch))
                except HostRefusedError as exc:
                    entry["result"] = "refused"
                    entry["detail"] = str(exc)
                except Exception as exc:  # noqa: BLE001 - report per host, keep going
                    entry["result"] = "failed"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
        applied = [r for r in results if r.get("result") == "applied"]
        failed = [r for r in results if r.get("result") in ("failed", "refused")]
        status = "noop" if not applied and not failed else ("applied" if not failed else ("partial" if applied else "failed"))
        return 200, {**base, "status": status, "hosts": results}
