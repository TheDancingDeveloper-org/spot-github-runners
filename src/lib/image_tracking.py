"""Opt-in dashboard image rollout: follow the published ``:main`` image of the dashboard.

``SPOT_DASHBOARD_IMAGE_TRACK=main`` makes the control VM manager resolve the digest that
``ghcr.io/swack-tools/spot-dashboard:main`` points at on each check. It starts the normal make-before-break
rotation (create, health, freeze, import, version floor, fence, cutover, delete) to that digest only when all of
these hold:

* no transition is in progress, the active control VM is running and healthy, and the other slot is empty;
* the rotation cooldown (``ROTATION_COOLDOWN_SECONDS`` after the last completed or aborted transition) has passed;
* the digest differs from the active VM's image and is not backed off (an earlier rollout to it aborted);
* the image's ``org.opencontainers.image.revision`` label names a commit whose Dashboard image workflow run on
  ``main`` concluded ``success``;
* that commit is strictly newer than the active image's commit: GitHub's compare API reports it ``ahead``.
  ``identical``, ``behind`` and ``diverged`` never roll, so the manager never moves to an older image.

Every network answer that is missing or malformed is a reason not to roll. Nothing here changes provider state;
the manager turns a positive decision into a ``rotate`` plan.

Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .https_transport import HttpResponse, Transport, TransportError, https_request

ENV_IMAGE_TRACK = "SPOT_DASHBOARD_IMAGE_TRACK"
ENV_IMAGE_TRACK_TOKEN = "SPOT_DASHBOARD_IMAGE_TRACK_TOKEN"
TRACK_MAIN = "main"
TRACK_VALUES = ("", TRACK_MAIN)
IMAGE_WORKFLOW_FILE = "dashboard-image.yml"
REVISION_LABEL = "org.opencontainers.image.revision"
REGISTRY_HOST = "ghcr.io"
GITHUB_API = "https://api.github.com"
FAILED_DIGESTS_KEPT = 20
VERDICT_RECHECK_SECONDS = 600   # pending or failed workflow verdicts are checked again after this long
HTTP_TIMEOUT_SECONDS = 15.0

MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_RE = re.compile(r"^ghcr\.io/([a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*)(?:(@sha256:[0-9a-f]{64})|:([0-9a-f]{40}))$")


class TrackingError(RuntimeError):
    """A registry or GitHub answer was missing or malformed; never roll on it."""


def parse_image(ref: str, repository: str) -> tuple[str | None, str | None]:
    """(digest, commit) of ``ref`` when it is an image of ``repository`` (``ghcr.io/<owner>/<name>``) pinned by
    digest (``@sha256:...``, commit unknown) or by 40-hex commit tag (``:<sha>``, digest unknown); (None, None)
    for anything else."""
    match = _IMAGE_RE.match(ref or "")
    if not match or f"{REGISTRY_HOST}/{match.group(1)}" != repository:
        return None, None
    if match.group(2):
        return match.group(2)[1:], None
    return None, match.group(3)


def digest_ref(repository: str, digest: str) -> str:
    return f"{repository}@{digest}"


# --------------------------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------------------------

class RegistryClient:
    """Read-only GHCR (OCI distribution API) client for one repository, authenticated with a PAT."""

    def __init__(self, repository: str, token: str, *, transport: Transport | None = None,
                 timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        if not repository.startswith(f"{REGISTRY_HOST}/"):
            raise ValueError(f"image tracking supports {REGISTRY_HOST} images only, not {repository!r}")
        self.name = repository[len(REGISTRY_HOST) + 1:]
        self.token = token
        self.transport = transport or https_request
        self.timeout = timeout
        self._bearer: str | None = None

    def _call(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        try:
            return self.transport("GET", url, headers, None, self.timeout)
        except TransportError as exc:
            raise TrackingError(str(exc)) from exc

    def _registry_token(self) -> str:
        if self._bearer:
            return self._bearer
        basic = base64.b64encode(f"x-access-token:{self.token}".encode("utf-8")).decode("ascii")
        scope = urllib.parse.quote(f"repository:{self.name}:pull", safe="")
        response = self._call(f"https://{REGISTRY_HOST}/token?service={REGISTRY_HOST}&scope={scope}",
                              {"Authorization": f"Basic {basic}", "User-Agent": "spot-control-plane/1.0"})
        if response.status != 200:
            raise TrackingError(f"GHCR token request returned HTTP {response.status}")
        try:
            token = json.loads(response.body.decode("utf-8")).get("token")
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            raise TrackingError("GHCR token response is not JSON") from exc
        if not isinstance(token, str) or not token:
            raise TrackingError("GHCR token response has no token")
        self._bearer = token
        return token

    def _get(self, path: str, accept: str) -> HttpResponse:
        headers = {"Authorization": f"Bearer {self._registry_token()}", "Accept": accept,
                   "User-Agent": "spot-control-plane/1.0"}
        return self._call(f"https://{REGISTRY_HOST}/v2/{self.name}/{path}", headers)

    def manifest(self, reference: str) -> tuple[str, dict[str, Any]]:
        """(digest, manifest) for a tag or digest. The digest is computed from the bytes, never trusted."""
        response = self._get(f"manifests/{reference}", MANIFEST_ACCEPT)
        if response.status != 200:
            raise TrackingError(f"GHCR manifest {reference} returned HTTP {response.status}")
        digest = "sha256:" + hashlib.sha256(response.body).hexdigest()
        if _DIGEST_RE.match(reference) and digest != reference:
            raise TrackingError(f"GHCR manifest {reference} does not hash to its digest")
        claimed = response.header("docker-content-digest")
        if claimed and claimed != digest:
            raise TrackingError(f"GHCR manifest {reference} digest header {claimed} differs from its bytes")
        try:
            manifest = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TrackingError(f"GHCR manifest {reference} is not JSON") from exc
        if not isinstance(manifest, dict):
            raise TrackingError(f"GHCR manifest {reference} is not an object")
        return digest, manifest

    def _blob(self, digest: str) -> bytes:
        response = self._get(f"blobs/{digest}", "application/octet-stream")
        if response.status in (301, 302, 303, 307, 308):
            location = response.header("location") or ""
            if not location.startswith("https://"):
                raise TrackingError("GHCR blob redirect is not HTTPS")
            # The signed redirect URL authorizes itself: never forward the registry token to another host.
            response = self._call(location, {"User-Agent": "spot-control-plane/1.0"})
        if response.status != 200:
            raise TrackingError(f"GHCR blob {digest} returned HTTP {response.status}")
        if "sha256:" + hashlib.sha256(response.body).hexdigest() != digest:
            raise TrackingError(f"GHCR blob {digest} does not hash to its digest")
        return response.body

    def resolve(self, tag: str) -> str:
        return self.manifest(tag)[0]

    def revision(self, digest: str) -> str | None:
        """The 40-hex ``org.opencontainers.image.revision`` label of the image (linux/amd64 of an index)."""
        _, manifest = self.manifest(digest)
        if manifest.get("mediaType") in _INDEX_TYPES or "manifests" in manifest:
            entries = [m for m in manifest.get("manifests") or [] if isinstance(m, dict)]
            chosen = next((m for m in entries if (m.get("platform") or {}).get("os") == "linux"
                           and (m.get("platform") or {}).get("architecture") == "amd64"), None)
            if chosen is None or not _DIGEST_RE.match(str(chosen.get("digest") or "")):
                raise TrackingError(f"image index {digest} has no linux/amd64 manifest")
            _, manifest = self.manifest(chosen["digest"])
        config = manifest.get("config") or {}
        config_digest = str(config.get("digest") or "") if isinstance(config, dict) else ""
        if not _DIGEST_RE.match(config_digest):
            raise TrackingError(f"image {digest} has no config digest")
        try:
            image_config = json.loads(self._blob(config_digest).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TrackingError(f"image config of {digest} is not JSON") from exc
        labels = ((image_config.get("config") or {}).get("Labels") or {}) if isinstance(image_config, dict) else {}
        revision = labels.get(REVISION_LABEL) if isinstance(labels, dict) else None
        return revision if isinstance(revision, str) and _SHA_RE.match(revision) else None


class GitHubClient:
    """Read-only GitHub REST client for the workflow verdict and commit ordering."""

    def __init__(self, repository: str, token: str, *, transport: Transport | None = None,
                 timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        self.repository = repository
        self.token = token
        self.transport = transport or https_request
        self.timeout = timeout

    def _json(self, path: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "spot-control-plane/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.transport("GET", f"{GITHUB_API}{path}", headers, None, self.timeout)
        except TransportError as exc:
            raise TrackingError(str(exc)) from exc
        if response.status != 200:
            raise TrackingError(f"GitHub {path.split('?', 1)[0]} returned HTTP {response.status}")
        try:
            return json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TrackingError(f"GitHub {path.split('?', 1)[0]} returned invalid JSON") from exc

    def workflow_verdict(self, sha: str, workflow: str = IMAGE_WORKFLOW_FILE, branch: str = TRACK_MAIN) -> str:
        """"success" when a run of ``workflow`` for ``sha`` on ``branch`` concluded success; "pending" while one is
        queued or running; "failure" when every completed run failed; "missing" when there is no run."""
        if not _SHA_RE.match(sha):
            raise TrackingError(f"not a commit: {sha!r}")
        query = urllib.parse.urlencode({"head_sha": sha, "branch": branch, "per_page": 20})
        data = self._json(f"/repos/{self.repository}/actions/workflows/{workflow}/runs?{query}")
        runs = [r for r in (data.get("workflow_runs") if isinstance(data, dict) else None) or []
                if isinstance(r, dict) and r.get("head_sha") == sha and r.get("head_branch") == branch]
        if any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs):
            return "success"
        if any(r.get("status") != "completed" for r in runs):
            return "pending"
        return "failure" if runs else "missing"

    def compare(self, base: str, head: str) -> str:
        """GitHub's ``status`` of head relative to base: ahead, behind, identical, or diverged."""
        if not (_SHA_RE.match(base) and _SHA_RE.match(head)):
            raise TrackingError("compare needs two commits")
        data = self._json(f"/repos/{self.repository}/compare/{base}...{head}?per_page=1")
        status = data.get("status") if isinstance(data, dict) else None
        if status not in ("ahead", "behind", "identical", "diverged"):
            raise TrackingError(f"GitHub compare returned status {status!r}")
        return status


# --------------------------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RolloutDecision:
    roll: bool
    reason: str
    image: str | None = None       # digest-pinned image to roll to
    digest: str | None = None
    revision: str | None = None
    from_revision: str | None = None


def _no(reason: str, **kw: Any) -> RolloutDecision:
    return RolloutDecision(False, reason, **kw)


def decide(*, transition_active: bool, active_ready: bool, cooling: bool, active_digest: str | None,
           active_revision: str | None, candidate_digest: str | None, candidate_revision: str | None,
           failed_digests: Any, workflow: str | None, ancestry: str | None, repository: str) -> RolloutDecision:
    """Pure rollout decision from gathered facts, checked in a fixed order (the first failing guard wins)."""
    if transition_active:
        return _no("a transition is in progress")
    if not active_ready:
        return _no("the active control VM is not healthy, or the other slot is occupied")
    if cooling:
        return _no("rotation cooldown active")
    if not candidate_digest:
        return _no(f"{repository}:{TRACK_MAIN} could not be resolved")
    if candidate_digest == active_digest:
        return _no(f"active image is {TRACK_MAIN} ({candidate_digest[:19]})")
    if candidate_digest in (failed_digests or ()):
        return _no(f"a rollout to {candidate_digest[:19]} aborted earlier; waiting for a new {TRACK_MAIN} digest")
    if not active_revision:
        return _no("the active image's commit is unknown, so a newer image can't be proven newer")
    if not candidate_revision:
        return _no(f"{TRACK_MAIN} image {candidate_digest[:19]} has no {REVISION_LABEL} label")
    if workflow != "success":
        return _no(f"Dashboard image workflow for {candidate_revision[:12]} on {TRACK_MAIN} is {workflow or 'unknown'}")
    if ancestry != "ahead":
        what = {"identical": "the same commit", "behind": "older than the active image",
                "diverged": "not a descendant of the active image's commit"}.get(ancestry or "", "of unknown order")
        return _no(f"{TRACK_MAIN} image commit {candidate_revision[:12]} is {what}")
    return RolloutDecision(True, f"{TRACK_MAIN} moved to {candidate_revision[:12]} ({candidate_digest[:19]}), "
                                 f"ahead of the active {active_revision[:12]}",
                           image=digest_ref(repository, candidate_digest), digest=candidate_digest,
                           revision=candidate_revision, from_revision=active_revision)


@dataclass
class ImageTracker:
    """Gathers the facts lazily (stopping at the first failing guard, so idle checks cost one registry lookup) and
    caches per-digest answers in ``cache`` (the ``image`` section of the control VM state)."""

    repository: str
    registry: RegistryClient | None
    github: GitHubClient | None
    clock: Callable[[], float]
    notes: list[str] = field(default_factory=list)

    def _revision(self, cache: dict, digest: str) -> str | None:
        revisions = cache.setdefault("revisions", {})
        if digest in revisions:
            return revisions[digest] or None
        revision = self.registry.revision(digest) if self.registry else None
        revisions[digest] = revision or ""
        # Bounded: keep the newest entries only.
        while len(revisions) > FAILED_DIGESTS_KEPT:
            revisions.pop(next(iter(revisions)))
        return revision

    def active_facts(self, cache: dict, active_image: str) -> tuple[str | None, str | None]:
        """(digest, commit) of the active image; resolves a commit tag's digest or a digest's commit once."""
        digest, revision = parse_image(active_image, self.repository)
        if digest is None and revision is None:
            return None, None
        if digest is None and self.registry is not None:
            digest = self.registry.resolve(revision)
        if revision is None and digest is not None:
            revision = self._revision(cache, digest)
        return digest, revision

    def evaluate(self, cache: dict, *, active_image: str | None, transition_active: bool, active_ready: bool,
                 cooling: bool, now: float | None = None) -> RolloutDecision:
        now = self.clock() if now is None else now
        gates = dict(transition_active=transition_active, active_ready=active_ready, cooling=cooling,
                     repository=self.repository)
        facts: dict[str, Any] = dict(active_digest=None, active_revision=None, candidate_digest=None,
                                     candidate_revision=None, failed_digests=(), workflow=None, ancestry=None)
        if transition_active or not active_ready or cooling:
            return decide(**gates, **facts)  # no network call while any gate is closed
        if self.registry is None or self.github is None:
            return _no(f"{ENV_IMAGE_TRACK_TOKEN} is not set, so image tracking can't read the registry")
        try:
            facts["candidate_digest"] = candidate = self.registry.resolve(TRACK_MAIN)
            if (cache.get("main") or {}).get("digest") != candidate:
                cache["main"] = {"digest": candidate, "seen_at": now}
            facts["active_digest"], facts["active_revision"] = self.active_facts(cache, active_image or "")
            facts["failed_digests"] = list(cache.get("failed") or ())
            if candidate == facts["active_digest"] or candidate in facts["failed_digests"] \
                    or not facts["active_revision"]:
                return decide(**gates, **facts)
            facts["candidate_revision"] = revision = self._revision(cache, candidate)
            if not revision:
                return decide(**gates, **facts)
            # The workflow verdict belongs to the candidate; the order belongs to the (active, candidate) pair.
            key = f"{candidate}|{facts['active_revision']}"
            verdicts = cache.setdefault("verdicts", {})
            known = verdicts.get(key)
            if isinstance(known, dict) and (known.get("final")
                                            or now - float(known.get("at") or 0) < VERDICT_RECHECK_SECONDS):
                facts["workflow"], facts["ancestry"] = known.get("workflow"), known.get("ancestry")
            else:
                facts["workflow"] = self.github.workflow_verdict(revision)
                if facts["workflow"] == "success":
                    facts["ancestry"] = self.github.compare(facts["active_revision"], revision)
                # A concluded success and the order of two commits never change; anything else is checked again.
                verdicts[key] = {"workflow": facts["workflow"], "ancestry": facts["ancestry"], "at": now,
                                 "final": facts["workflow"] == "success"}
                while len(verdicts) > FAILED_DIGESTS_KEPT:
                    verdicts.pop(next(iter(verdicts)))
            return decide(**gates, **facts)
        except TrackingError as exc:
            return _no(f"image tracking failed this check: {exc}")


def record_failed(cache: dict, digest: str) -> None:
    """Back off ``digest`` until ``:main`` names another one (bounded list, newest last)."""
    failed = [d for d in cache.get("failed") or [] if d != digest]
    failed.append(digest)
    cache["failed"] = failed[-FAILED_DIGESTS_KEPT:]
