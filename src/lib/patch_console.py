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
    SCHEMA_VERSION,
    RunnerPatch,
    classify_patch_diff,
    merge_patch_dicts,
    parse_manifest_text,
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
    status = 502

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class ConflictError(PatchConsoleError):
    status = 409


class InvalidRequestError(PatchConsoleError):
    status = 400


class HotPatchUnavailable(PatchConsoleError):
    # 501 rather than 503: the edge Worker would turn a 503 into an endless control-VM wait.
    status = 501


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
            _, payload = self._call("GET", f"repos/{self.repo}/contents/{path}?ref={ref}")
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
    every broken section, unknown keys (typos validate_patch_dict would ignore) are errors, and a repository
    manifest is also checked merged with ``defaults`` (the patch hosts would actually get).
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
    for section, allowed in SECTION_KEYS.items():
        value = data.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append({"field": section, "message": f"{section} section must be a mapping"})
            continue
        for key in sorted(set(value) - allowed):
            errors.append({"field": f"{section}.{key}", "message": f"Unknown key {key!r} in {section}"})
        probe: dict[str, Any] = {section: value}
        if section != "hardware" and isinstance(data.get("hardware"), dict):
            probe["hardware"] = {"arch": data["hardware"].get("arch", "x86_64")}
        try:
            validate_patch_dict(probe)
        except (ValueError, TypeError) as exc:
            errors.append({"field": _field_of(str(exc), section), "message": str(exc)})
    if errors:
        return None, errors, data
    try:
        own = validate_patch_dict(data)
    except (ValueError, TypeError) as exc:
        return None, [{"field": _field_of(str(exc), "content"), "message": str(exc)}], data
    if repository is None:
        return own, [], data
    merged = merge_patch_dicts(dict(defaults or {}), data)
    if not merged.get("repository"):
        merged["repository"] = repository
    try:
        effective = validate_patch_dict(merged)
    except (ValueError, TypeError) as exc:
        return None, [{"field": "effective", "message": f"Merged with defaults.yaml: {exc}"}], data
    return effective, [], data


def patch_summary(patch: RunnerPatch) -> dict[str, Any]:
    return {"patch_sha256": patch.patch_sha256, "effective": patch.to_canonical_dict()}


# --------------------------------------------------------------------------------------------------
# Hosts, executors, classification
# --------------------------------------------------------------------------------------------------


def runner_hosts(instances: Iterable[Mapping[str, Any]], repository: str) -> list[dict[str, Any]]:
    """Running CI runner hosts recorded for ``repository`` (instance field ``patch_target``)."""
    wanted = repository.lower()
    hosts = []
    for inst in instances:
        if not isinstance(inst, Mapping):
            continue
        if inst.get("role") != "cicd_runner" or inst.get("status") != "RUNNING":
            continue
        if str(inst.get("patch_target") or "").lower() != wanted:
            continue
        hosts.append(dict(inst))
    return hosts


class HotPatchExecutor(Protocol):
    available: bool
    reason: str

    def read_manifest(self, host: Mapping[str, Any]) -> dict[str, Any] | None: ...

    def apply(self, host: Mapping[str, Any], patch: RunnerPatch) -> dict[str, Any]: ...


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

    Host records must carry ``name``, ``zone``, ``machine_type``, ``runner_prefix``, and ``runners_count``.
    """

    def __init__(self, project: str, *, which: Callable[[str], str | None] | None = None,
                 run: Callable[..., Any] | None = None) -> None:
        self.project = project
        self._run = run or subprocess.run
        self.available = (which or shutil.which)("gcloud") is not None
        self.reason = "" if self.available else "gcloud is not installed where the dashboard runs"

    def read_manifest(self, host: Mapping[str, Any]) -> dict[str, Any] | None:
        name, zone = str(host.get("name") or ""), str(host.get("zone") or "")
        if not name or not zone:
            raise PatchConsoleError("host record has no name or zone")
        result = self._run(
            ["gcloud", "compute", "ssh", name, f"--zone={zone}", f"--project={self.project}", "--quiet",
             "--command=sudo -n cat /var/lib/gha-runner-patch/state.json 2>/dev/null || true"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise PatchConsoleError(f"could not read the patch state of {name} (exit {result.returncode})")
        raw = (result.stdout or "").strip()
        if not raw:
            return None
        try:
            manifest = json.loads(raw).get("manifest")
        except (ValueError, AttributeError) as exc:
            raise PatchConsoleError(f"{name} has an unreadable patch state file") from exc
        return manifest if isinstance(manifest, dict) else None

    def apply(self, host: Mapping[str, Any], patch: RunnerPatch) -> dict[str, Any]:
        from . import runner_manager

        missing = [k for k in ("name", "zone", "machine_type", "runner_prefix", "runners_count") if not host.get(k)]
        if missing:
            raise PatchConsoleError(f"host record is missing {', '.join(missing)}")
        args = runner_manager.args_parser().parse_args([
            "--max-cost", "0", "--project", self.project, "--target", patch.repository,
            "--instance-name", str(host["name"]), "--zone", str(host["zone"]),
            "--instance-type", str(host["machine_type"]), "--runner-prefix", str(host["runner_prefix"]),
            "--runner-count", str(int(host["runners_count"])),
        ])
        args.runner_patch = patch
        # runner_manager reads its project from a module global (set by its own main()); scope the override.
        with _RUNNER_MANAGER_LOCK:
            previous = runner_manager.PROJECT
            runner_manager.PROJECT = self.project
            try:
                outcome = runner_manager.apply_live_patch(args, str(host["zone"]))
            finally:
                runner_manager.PROJECT = previous
        return {"result": "applied" if outcome == "hot_patch" else str(outcome)}


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
        try:
            if executor.available:
                current = executor.read_manifest(host)
                source = "read from the host"
            elif "patch_manifest" in host:
                current = host.get("patch_manifest")
                source = "recorded by the fleet controller"
            else:
                out.append(HostClassification(host, "unknown", None,
                                              "the host's current patch is not recorded and the host is unreachable"))
                continue
            if current is not None and not isinstance(current, dict):
                raise ValueError("recorded patch manifest is not a mapping")
            current_sha = validate_patch_dict(current).patch_sha256 if current is not None else None
            classification = classify_patch_diff(current, target)
            out.append(HostClassification(host, classification, current_sha, f"current patch {source}"))
        except Exception as exc:  # noqa: BLE001 - an unreadable host is "unknown", never a guess
            out.append(HostClassification(host, "unknown", None, f"could not determine the current patch: {exc}"))
    return out


# --------------------------------------------------------------------------------------------------
# Console operations
# --------------------------------------------------------------------------------------------------


class PatchConsole:
    """Operations behind the dashboard's ``/api/patches`` endpoints."""

    def __init__(self, config_repo: str, *, token: str | None = None, requester: Requester | None = None,
                 executor: HotPatchExecutor | None = None) -> None:
        self.config_repo = config_repo
        self._token = token
        self._requester = requester
        self.executor: HotPatchExecutor = executor or UnavailableHotPatchExecutor()

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
        patch, errors, _ = validate_manifest(content, path=path, defaults=defaults)
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
        hosts = classify_hosts(runner_hosts(instances, repository), patch, self.executor)
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

    def save(self, path: str, content: Any, base_sha: Any, message: Any = None) -> dict[str, Any]:
        repository = repository_for_path(path)
        if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
            raise InvalidRequestError("base_sha must be the 40-hex commit the manifest was loaded from")
        client = self.client()
        branch, head = client.resolve()
        if head != base_sha:
            raise ConflictError(
                f"{branch} moved from {base_sha[:12]} to {head[:12]} since this manifest was loaded; reload and edit again",
                body={"status": "conflict", "head_sha": head, "base_sha": base_sha},
            )
        defaults = self._defaults(client, base_sha) if repository else None
        patch, errors, _ = validate_manifest(content, path=path, defaults=defaults)
        if patch is None:
            raise InvalidRequestError("The manifest is invalid", body={"errors": errors})
        found = client.read_file(path, base_sha)
        blob = found[1] if found is not None else None
        if found is not None and found[0] == content:
            raise InvalidRequestError("The manifest is unchanged; nothing to save")
        slug = re.sub(r"[^a-z0-9-]+", "-", (repository or "defaults").lower()).strip("-")[:60]
        new_branch = f"{BRANCH_PREFIX}{slug}-{base_sha[:7]}-{secrets.token_hex(3)}"
        if new_branch == branch or not new_branch.startswith(BRANCH_PREFIX):
            raise InvalidRequestError("Refusing to write to the default branch")
        title = str(message or "").strip()[:200] or f"Update {path} from the Spot dashboard"
        client.create_branch(new_branch, base_sha)
        try:
            commit = client.put_file(path, new_branch, content, title, blob)
            pr = client.open_pull_request(
                new_branch, branch, title,
                f"Edited in the Spot dashboard from {branch}@{base_sha[:12]}.\n\n"
                f"Effective patch sha256: `{patch.patch_sha256}`",
            )
        except PatchConsoleError:
            try:
                client.delete_branch(new_branch)
            except PatchConsoleError:
                pass
            raise
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
        hosts = classify_hosts(runner_hosts(instances, repository), patch, self.executor)
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
                entry["result"] = "noop"
            else:
                try:
                    entry.update(self.executor.apply(h.host, patch))
                except Exception as exc:  # noqa: BLE001 - report per host, keep going
                    entry["result"] = "failed"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
        applied = [r for r in results if r.get("result") == "applied"]
        failed = [r for r in results if r.get("result") == "failed"]
        status = "noop" if not applied and not failed else ("applied" if not failed else ("partial" if applied else "failed"))
        return 200, {**base, "status": status, "hosts": results}
