"""Replace an unmanaged runner host with the target's managed floor host: make before break, operator-triggered only.

``src/runner_floor.py replace-unmanaged`` without ``--apply`` is a read-only preview. With ``--apply`` it holds the
target's floor lease (so the watchdog's keeper skips the target) and runs these phases, saving each one in the
target's floor record before it acts, so a rerun resumes where it stopped:

1. ``ensure_managed``: the floor keeper's own steps provision the managed floor host when the target has none.
2. ``verified``: a fresh GitHub read shows every runner of the managed host online, with every scheduling label the
   old runners carry (except the old machine-type and region labels, which the preview names and
   ``--accept-dropped-labels`` accepts). The old runners' labels are recorded so an abort can restore them.
3. ``draining``: the old host gets the host job-start cordon (``runner_drain``, over ``gcloud compute ssh``), then
   ``drain_runners`` strips its custom labels, waits for every old runner to be idle, and deregisters each one after
   a fresh idle read. A host that can't run the cordon-aware hook holds here, unless the operator passed
   ``--label-strip-drain`` (see :func:`label_strip_step`). Before every drain step the managed host is checked
   again; if it lost a runner before any old runner was deregistered, the old labels and the cordon are restored and
   the replacement aborts.
4. ``deleting``: a fresh read must show every old registration gone; then the old VM (the same instance ID) is
   deleted with its attached disks, and a fresh read must show it gone.

Only repository targets are supported: an organization drain moves runners into an isolated runner group, which an
abort could not undo here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import runner_floor as rf
from .gcs_control_state import GcsClient, GcsLease, LeaseLost
from .runner_fleet import GceVm, GitHubRunner

LOG = logging.getLogger("spot_runner_autoscaler.floor")

IDLE_CONFIRM_SECONDS = 120  # label-strip drain: old runners stay idle this long before deregistration
EXIT_RESUMABLE = 75


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "replace-unmanaged",
        help="Replace an unmanaged runner host with the target's managed floor host (preview unless --apply)")
    parser.add_argument("--target", required=True, help="A repository floor target (owner/repo)")
    parser.add_argument("--instance", required=True, help="The unmanaged VM to retire")
    parser.add_argument("--zone", required=True, help="Its zone")
    parser.add_argument("--old-runner", action="append", default=[], metavar="NAME",
                        help="A runner registration of the old host (repeat for each); the preview lists candidates")
    parser.add_argument("--accept-dropped-labels", action="store_true",
                        help="Accept that the old machine-type and region labels don't move to the managed host")
    parser.add_argument("--label-strip-drain", action="store_true",
                        help="If the old host can't run the job-start cordon, drain by removing its custom labels "
                             "and waiting for idle runners instead (a job that selects only built-in labels can "
                             "still land on it until it is deregistered)")
    parser.add_argument("--github-user", default="", help="gh account (default: the target's account=)")
    parser.add_argument("--apply", action="store_true", help="Run the replacement")
    parser.add_argument("--max-wait-seconds", type=int, default=4 * 3600,
                        help="Stop (resumable) after this long (default: %(default)s)")
    parser.add_argument("--poll-seconds", type=int, default=30)


@dataclass
class Preflight:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    old_runners: list[GitHubRunner] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    required_labels: list[str] = field(default_factory=list)
    dropped_labels: list[str] = field(default_factory=list)
    managed: rf.Host | None = None
    steps: list[str] = field(default_factory=list)


def associated_runner_names(vm_name: str, machine_type: str, region: str,
                            runners: Sequence[GitHubRunner]) -> list[str]:
    """Every registration attributed to the old VM, independent of what the operator named: a name that contains the
    VM name, or custom labels that name the VM's machine type and region. Managed floor runners never count.

    The attribution is deliberately broad: a runner it wrongly includes only blocks the deletion, while a runner it
    missed could still be running a job when the VM is deleted.
    """
    shape = {machine_type, region} - {""}
    return sorted(r.name for r in runners if not r.name.startswith(f"{rf.RUNNER_PREFIX}-{rf.HOST_PREFIX}")
                  and ((vm_name and vm_name in r.name) or (len(shape) == 2 and rf.folded(shape) <=
                                                            rf.folded(rf.custom_labels(r)))))


def preflight(target: rf.FloorTarget, old: rf.Host | None, old_names: Sequence[str], runners: Sequence[GitHubRunner],
              hosts: Sequence[rf.Host], record: dict, *, accept_dropped: bool, instance: str = "") -> Preflight:
    """Every check that must pass before the replacement changes anything (pure; the preview prints it)."""
    out = Preflight()
    name = old.name if old else instance
    if not target.is_repo:
        out.problems.append(f"{target.name} is an organization; replace-unmanaged supports repository targets only")
    if old is None:
        out.problems.append(f"VM {instance} was not found in that zone")
    else:
        if old.name.startswith((rf.HOST_PREFIX, "spot-control-")):
            out.problems.append(f"{old.name} is a managed host; the autoscaler retires managed hosts")
        if old.target_label and old.target_label != target.key:
            out.problems.append(f"{old.name} carries the spot-target label of another target")
    by_name = {r.name: r for r in runners}
    out.candidates = associated_runner_names(name, old.machine_type if old else "", old.region if old else "", runners)
    if not old_names:
        out.problems.append("name the old host's runners with --old-runner (candidates: "
                            + (", ".join(out.candidates) or "none found") + ")")
    unnamed = sorted(set(out.candidates) - set(old_names))
    if old_names and unnamed:
        # Every runner of the VM is drained and must be gone before the VM is deleted, never a subset.
        out.problems.append(f"--old-runner must name every runner attributed to {name}; also name "
                            f"{', '.join(unnamed)} (or remove those registrations first if they aren't its runners)")
    for runner_name in dict.fromkeys(old_names):
        runner = by_name.get(runner_name)
        if runner is None:
            out.problems.append(f"runner {runner_name} is not registered on {target.name}")
        elif runner_name.startswith(f"{rf.RUNNER_PREFIX}-{rf.HOST_PREFIX}"):
            out.problems.append(f"runner {runner_name} belongs to a managed host")
        elif runner_name not in out.candidates:
            out.problems.append(f"runner {runner_name} doesn't look like a runner of {name}: its name doesn't "
                                "contain the VM name, and its labels lack the VM's machine type and region")
        else:
            out.old_runners.append(runner)
    # Labels compare without case, as GitHub matches them.
    old_custom = rf.folded(set().union(*(rf.custom_labels(r) for r in out.old_runners))) if out.old_runners else set()
    floor_static = rf.folded({rf.BASE_LABEL, *target.labels})
    shape = rf.folded({old.machine_type, old.region}) if old else set()
    missing = sorted(old_custom - floor_static - shape)
    if missing:
        out.problems.append(f"the managed floor host would not carry {', '.join(missing)}; add "
                            f"labels={'+'.join(missing)} to {target.name} in {rf.ENV_TARGETS} first")
    out.required_labels = sorted(old_custom - shape)
    out.dropped_labels = sorted((old_custom & shape) - floor_static)
    if out.dropped_labels and not accept_dropped:
        out.problems.append(f"labels {', '.join(out.dropped_labels)} name the old shape and region and don't move "
                            "to the managed host; check that no workflow selects them, then pass "
                            "--accept-dropped-labels")
    live = [h for h in rf.managed_hosts(target, hosts) if h.running]
    out.managed = live[0] if live else None
    if old is not None:
        name_of = lambda d: d.get("source", "").rsplit("/", 1)[-1] or d.get("deviceName", "?")  # noqa: E731
        deleted = ", ".join(name_of(d) for d in old.disks if d.get("autoDelete")) or "none"
        kept = ", ".join(name_of(d) for d in old.disks if not d.get("autoDelete")) or "none"
        out.steps = [
            f"1. ensure the managed floor host: "
            + (f"{out.managed.describe()} exists" if out.managed else "provision one (the keeper's launch steps)"),
            "2. verify every managed runner is online and carries " + (", ".join(out.required_labels) or "spot"),
            f"3. cordon {old.name} (host job-start cordon over gcloud compute ssh); if it can't refuse jobs, hold, "
            "or with --label-strip-drain drain it by label removal",
            f"4. remove the old runners' custom labels, wait until {', '.join(r.name for r in out.old_runners)} "
            "are idle, deregister each after a fresh idle read",
            "5. verify the old registrations are gone",
            f"6. delete {old.name} in {old.zone} with its auto-delete disks ({deleted}); other disks are kept "
            f"({kept})",
            "7. verify the VM is gone",
        ]
    recorded = record.get("replacement") or {}
    if recorded and (recorded.get("old") or {}).get("name") != name and \
            recorded.get("phase") not in ("done", "aborted", "held"):
        out.problems.append("another replacement is recorded for this target: "
                            f"{(recorded.get('old') or {}).get('name')} ({recorded.get('phase')})")
    return out


def managed_identity(record: dict, plan: rf.Plan, runners: Sequence[GitHubRunner]) -> dict:
    """The managed host the replacement relies on: the keeper's recorded floor host, or the target's live managed
    host (for example one the autoscaler launched) with the ``gha-<host>-<n>`` runners registered for it."""
    own = record.get("host") or {}
    if plan.host is not None and plan.action == "noop" and own.get("instance_id") != plan.host.instance_id:
        pattern = f"{rf.RUNNER_PREFIX}-{plan.host.name}-"
        names = sorted(r.name for r in runners if r.name.startswith(pattern) and r.name[len(pattern):].isdigit())
        return {"name": plan.host.name, "zone": plan.host.zone, "instance_id": plan.host.instance_id,
                "runner_names": names}
    return dict(own)


def managed_ready(own: dict, hosts: Sequence[rf.Host], runners: Sequence[GitHubRunner],
                  required: Sequence[str]) -> tuple[bool, str]:
    """Whether the managed host is running with every one of its runners online and carrying every required label."""
    own = own or {}
    host = next((h for h in hosts if h.name == own.get("name") and h.zone == own.get("zone")
                 and h.instance_id == own.get("instance_id")), None)
    if host is None or not host.running:
        return False, "the managed floor host is not running"
    by_name = {r.name: r for r in runners}
    for name in own.get("runner_names") or []:
        runner = by_name.get(name)
        if runner is None or not runner.online:
            return False, f"managed runner {name} is not online"
        lacking = rf.folded(required) - rf.folded(rf.custom_labels(runner))
        if lacking:
            return False, f"managed runner {name} lacks {', '.join(sorted(lacking))}"
    if not own.get("runner_names"):
        return False, "the managed floor host records no runners"
    return True, f"{host.describe()} is ready"


def label_strip_step(rep: dict, old_runners: Sequence[GitHubRunner], github: rf.GitHubClient,
                     target: rf.FloorTarget, save: Callable[[], None], now: float) -> bool:
    """Drain without a host cordon (only with --label-strip-drain): strip the custom labels, wait until every old
    runner has been idle for IDLE_CONFIRM_SECONDS, then deregister each one after a fresh idle read. Returns True
    once no old runner is registered."""
    present = list(old_runners)
    if not present:
        return True
    if not rep.get("labels_stripped"):
        for runner in present:
            if rf.custom_labels(runner):
                github._request("DELETE", f"{target.target.runners_url}/{runner.runner_id}/labels")
        rep["labels_stripped"] = True
        rep["idle_since"] = None
        save()
        return False
    if any(r.busy for r in present):
        rep["idle_since"] = None
        save()
        return False
    if not rep.get("idle_since"):
        rep["idle_since"] = now
        save()
        return False
    if now - float(rep["idle_since"]) < IDLE_CONFIRM_SECONDS:
        return False
    for runner in present:
        fresh = github.get_runner(target.target, runner.runner_id)
        if fresh is None:
            continue
        if fresh.get("name") != runner.name or fresh.get("busy") is not False:
            rep["idle_since"] = None
            save()
            return False
        # Pending until the delete returns: a crash in between leaves it to reconcile_pending, never to a rollback.
        rep["deregistering"] = runner.name
        save()
        github.delete_runner(target.target, runner.runner_id)
        rep.setdefault("deregistered", []).append(runner.name)
        rep.pop("deregistering", None)
        save()
    return False  # a fresh inventory must prove them gone


def irreversible(rep: dict) -> bool:
    """Whether any old runner was (or may have been) deregistered: then the old host can't be put back."""
    drain = rep.get("drain") or {}
    return bool(rep.get("deregistered") or rep.get("deregistering") or drain.get("deregistered_runner_ids")
                or drain.get("deregistering_runner_id") is not None)


def reconcile_pending(rep: dict, runners: Sequence[GitHubRunner]) -> str | None:
    """Settle a deregistration whose outcome was never saved, from a fresh GitHub inventory.

    Gone: it happened, so it is recorded as deregistered (irreversible). Still registered: the delete never took
    effect, so the pending mark is dropped and the runner counts as present. Returns what changed, or None.
    """
    drain = rep.get("drain") or {}
    pending_id = drain.get("deregistering_runner_id")
    if pending_id is not None:
        drain.pop("deregistering_runner_id")
        if pending_id in {r.runner_id for r in runners}:
            return f"runner {pending_id} is still registered; its interrupted deregistration never took effect"
        drain["deregistered_runner_ids"] = sorted(set(drain.get("deregistered_runner_ids") or []) | {pending_id})
        return f"runner {pending_id} is gone; its interrupted deregistration is recorded"
    pending_name = rep.get("deregistering")
    if pending_name:
        rep.pop("deregistering")
        if pending_name in {r.name for r in runners}:
            return f"runner {pending_name} is still registered; its interrupted deregistration never took effect"
        rep["deregistered"] = sorted(set(rep.get("deregistered") or []) | {pending_name})
        return f"runner {pending_name} is gone; its interrupted deregistration is recorded"
    return None


class Replacement:
    """The apply loop. Every provider and GitHub mutation runs behind ``lease.ensure``."""

    def __init__(self, args: argparse.Namespace, config: rf.FloorConfig, target: rf.FloorTarget, *,
                 store: rf.TargetStore, lease: GcsLease, inventory: Any, github: rf.GitHubClient, provider: Any,
                 drain: Callable[..., bool] | None = None, cordon: Callable[..., tuple[str, dict]] | None = None,
                 release_cordon: Callable[..., bool] | None = None,
                 delete_old: Callable[[rf.Host], None] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 out: Callable[[str], None] = print) -> None:
        from . import runner_drain

        self.args, self.config, self.target = args, config, target
        self.store, self.lease, self.inventory, self.github, self.provider = store, lease, inventory, github, provider
        self.drain = drain or runner_drain.drain_runners
        self.cordon = cordon or runner_drain.cordon_runner_host
        self.release_cordon = release_cordon or runner_drain.release_runner_host_cordon
        # An unmanaged host's disks: only the auto-delete ones go with it; the preview lists both kinds.
        self.delete_old = delete_old or (lambda host: provider.delete_instance(host, all_disks=False))
        self.clock, self.sleep, self.out = clock, sleep, out

    @property
    def rep(self) -> dict:
        return self.store.record["replacement"]

    def save(self) -> None:
        self.store.record["updated_at"] = self.clock()
        self.store.save(self.lease)

    def say(self, text: str) -> None:
        LOG.info("Replace %s: %s", self.args.instance, text)
        self.out(text)

    def _old_vm(self, host: rf.Host) -> GceVm:
        return GceVm(host.name, host.zone, host.status, host.machine_type, host.spot, instance_id=host.instance_id)

    def _old_runners(self, runners: Sequence[GitHubRunner]) -> list[GitHubRunner]:
        names = set(self.rep["old_runner_names"])
        return [r for r in runners if r.name in names]

    def _remove_stale_registrations(self, leftover: Sequence[GitHubRunner]) -> int | None:
        """The old VM vanished (for example during its drain) and left registrations behind: nothing deregisters
        them any more, so they would hold the deletion phase forever. A fresh describe must confirm the VM absent;
        then each offline, idle one is removed after a fresh read. An online or busy one holds instead."""
        if self.inventory.describe(self.rep["old"]["name"], self.rep["old"]["zone"]) is not None:
            self.say(f"{self.rep['old']['name']} is visible again; waiting for its runners to disappear")
            return None
        in_use = [r.name for r in leftover if r.online or r.busy]
        if in_use:
            self.say(f"holding: {', '.join(in_use)} still report online or busy although "
                     f"{self.rep['old']['name']} is gone; no registration is removed until they're offline and idle")
            return None
        for runner in leftover:
            fresh = self.github.get_runner(self.target.target, runner.runner_id)
            if fresh is None:
                continue
            if fresh.get("name") != runner.name or fresh.get("busy") is not False or fresh.get("status") != "offline":
                self.say(f"holding: {runner.name} is not offline and idle on a fresh read")
                return None
            self.rep["deregistering"] = runner.name  # settled by reconcile_pending if the delete's result is lost
            self.save()
            self.lease.ensure()
            self.github.delete_runner(self.target.target, runner.runner_id)
            self.rep["deregistered"] = sorted(set(self.rep.get("deregistered") or []) | {runner.name})
            self.rep.pop("deregistering", None)
            self.save()
        self.say(f"removed the stale registrations of the vanished {self.rep['old']['name']}: "
                 f"{', '.join(r.name for r in leftover)}")
        return None

    def _hold_for_managed(self, phase: str, why: str) -> None:
        """The old runners are (partly) gone, so the old host can't be restored, and the managed host isn't ready:
        keep the old VM, return to ensure_managed (which waits for, or re-provisions, the managed floor host), then
        continue the interrupted phase."""
        self.rep.update(phase="ensure_managed", return_phase=phase)
        self.save()
        self.say(f"holding: {why}. The old host stays, because it may be the only recoverable host and its "
                 f"drained runners can't be restored; the managed floor host is checked or provisioned again, then "
                 f"{phase} continues")
        return None

    def _unaccounted(self, runners: Sequence[GitHubRunner]) -> list[str]:
        """Runners attributed to the old VM that this replacement never drained (they registered later, or the
        record predates the complete-set check)."""
        old = self.rep["old"]
        associated = associated_runner_names(old["name"], old.get("machine_type", ""),
                                             old.get("zone", "").rsplit("-", 1)[0] if old.get("machine_type") else "",
                                             runners)
        return sorted(set(associated) - set(self.rep["old_runner_names"]))

    def _refuse_delete(self, names: Sequence[str]) -> int:
        self.say(f"refusing to delete {self.rep['old']['name']}: runner(s) {', '.join(names)} attributed to it are "
                 "still registered and this replacement didn't drain them; drain or remove them, then rerun")
        return 1

    def _abort(self, why: str, runners: Sequence[GitHubRunner], old: rf.Host | None) -> int | None:
        """Release the cordon, then restore the old runners' labels; only before any old runner was deregistered.

        The cordon goes first: labels restored on a still-cordoned host would route jobs to a host whose job-started
        hook refuses them. Until the release is confirmed the phase stays ``aborting`` and every loop retries it.
        """
        rep = self.rep
        if rep.get("phase") != "aborting":
            rep.update(phase="aborting", abort_reason=why)
            self.save()
        token = rep.get("cordon_intent")
        if token and old is not None and not rep.get("cordon_released") and not rep.get("cordon_absent"):
            if not self.release_cordon(self._old_vm(old), self.args, token, self.lease.ensure):
                self.say(f"aborting ({why}): the cordon release on {old.name} is unconfirmed; the old runners stay "
                         "without their labels until it is")
                return None
            rep["cordon_released"] = True
            self.save()
        original = rep.get("original_labels") or {}
        for runner in self._old_runners(runners):
            labels = original.get(runner.name)
            if labels and rf.folded(rf.custom_labels(runner)) != rf.folded(labels):
                self.github.set_labels(self.target.target, runner.runner_id, labels)
        rep.update(phase="aborted", aborted_at=self.clock())
        self.save()
        self.say(f"aborted: {why}; the cordon was released and the old runners' labels were restored")
        return 1

    def step(self) -> int | None:
        """One observation and at most one action. Returns an exit code when finished, else None."""
        self.lease.ensure()
        rep = self.rep
        hosts = self.inventory.list_hosts()
        runners = self.github.list_runners(self.target.target)
        old = self.inventory.describe(rep["old"]["name"], rep["old"]["zone"])
        if old is not None and old.instance_id != rep["old"]["instance_id"]:
            self.say(f"{old.name} is now another instance ({old.instance_id}); stopping without changes")
            return 1
        phase = rep["phase"]
        if phase in ("verified", "draining", "aborting", "deleting"):
            settled = reconcile_pending(rep, runners)
            if settled:
                self.save()
                self.say(settled)
        if phase == "aborting" and irreversible(rep):
            # An old runner is already gone, so a rollback can't restore the old host: go forward instead, which
            # waits for the managed host to be ready again and then finishes the drain and the deletion.
            rep.update(phase="draining", abort_reason=None, irreversible_since=rep.get("irreversible_since")
                       or self.clock())
            self.save()
            self.say("not rolling back: an old runner was already deregistered, so the old host can't be restored; "
                     "the replacement continues once the managed host is ready again")
            return None
        if phase == "aborting":
            return self._abort(rep.get("abort_reason") or "aborting", runners, old)
        if phase == "ensure_managed":
            plan = rf.plan_target(self.target, hosts, runners, self.store.record, self.clock())
            identity = managed_identity(self.store.record, plan, runners)
            ready, why = managed_ready(identity, hosts, runners, rep["required_labels"])
            if ready:
                # A hold during the drain or the deletion returns to where it stopped.
                rep.update(phase=rep.pop("return_phase", None) or "verified", managed=identity)
                self.save()
                self.say(f"managed floor host verified: {why}; continuing with {rep['phase']}")
                return None
            if plan.idle:
                self.say(f"waiting for the managed floor host: {plan.describe()} ({why})")
                return None
            keeper = rf.FloorKeeper(self.config, self.target, self.store.record, self.save, provider=self.provider,
                                    github=self.github, clock=self.clock)
            self.say(keeper.execute(plan, hosts))
            return None
        ready, why = managed_ready(rep.get("managed") or {}, hosts, runners, rep["required_labels"])
        if phase in ("verified", "draining", "deleting") and not ready:
            if phase != "deleting" and not irreversible(rep):
                return self._abort(f"the managed host is no longer ready ({why})", runners, old)
            return self._hold_for_managed(phase, why)
        if phase == "verified":
            rep["original_labels"] = {r.name: sorted(rf.custom_labels(r)) for r in self._old_runners(runners)}
            rep["phase"] = "draining"
            self.save()
            return None
        if phase == "draining":
            if old is None:
                self.say(f"{rep['old']['name']} disappeared during its drain; checking its registrations")
                rep["phase"] = "deleting"
                self.save()
                return None
            vm = self._old_vm(old)
            old_runners = self._old_runners(runners)
            if not rep.get("mode"):
                token = f"replace-{old.instance_id}"
                if rep.get("cordon_intent") != token:
                    # Durable before the call: an SSH timeout or a crash may leave a marker that only this token
                    # lets an abort release.
                    rep["cordon_intent"] = token
                    rep["drain"] = {"target": self.target.name, "runner_ids": [], "host_cordon_token": token}
                    self.save()
                state, detail = self.cordon(vm, self.args, token, [r.name for r in old_runners], self.lease.ensure)
                if state == "unsupported":
                    # The cordon program refuses before it writes any marker: none exists to release.
                    rep["cordon_absent"] = True
                    if not self.args.label_strip_drain:
                        rep.update(phase="held", hold_reason=detail.get("reason") or state)
                        self.save()
                        self.say(f"held: {old.name} can't refuse jobs at job start ({detail.get('reason') or state}); "
                                 "nothing was changed on it. Review the risk, then rerun with --label-strip-drain")
                        return 1
                    rep["mode"] = "label_strip"
                elif state in ("placed", "busy", "idle"):
                    rep["mode"] = "cordon"
                else:
                    self.say(f"cordon of {old.name} is unverified ({state}); retrying")
                    return None
                self.save()
                return None
            if rep["mode"] == "cordon":
                done = self.drain(rep["drain"], vm, self.args, {}, self.save, old_runners, self.lease.ensure)
            else:
                done = label_strip_step(rep, old_runners, self.github, self.target, self.save, self.clock())
            if done:
                rep["phase"] = "deleting"
                self.save()
                self.say("old runners are deregistered")
            return None
        if phase == "deleting":
            leftover = self._old_runners(runners)
            if leftover and old is None:
                return self._remove_stale_registrations(leftover)
            if leftover:
                self.say(f"waiting for {', '.join(r.name for r in leftover)} to disappear from GitHub")
                return None
            unaccounted = self._unaccounted(runners)
            if unaccounted:
                return self._refuse_delete(unaccounted)
            if old is None:
                rep.update(phase="done", done_at=self.clock())
                self.save()
                self.say(f"done: {rep['old']['name']} is deleted and {why}")
                return 0
            rep["delete_requested_at"] = self.clock()
            self.save()
            self.lease.ensure()
            # Fresh reads right before the delete. No runner attributed to this VM may still be registered.
            fresh = self.github.list_runners(self.target.target)
            remaining = self._old_runners(fresh) + [r for r in fresh if r.name in self._unaccounted(fresh)]
            if remaining:
                return self._refuse_delete([r.name for r in remaining])
            # The managed host must still be ready: the old VM is never deleted while it is the only recoverable
            # host, for example after the managed host was preempted once the drain had finished.
            ready_now, why_now = managed_ready(rep.get("managed") or {}, self.inventory.list_hosts(), fresh,
                                               rep["required_labels"])
            if not ready_now:
                return self._hold_for_managed("deleting", why_now)
            # And the VM must still be the recorded instance.
            current = self.inventory.describe(rep["old"]["name"], rep["old"]["zone"])
            if current is None:
                self.say(f"{rep['old']['name']} is already gone; the next check confirms it")
                return None
            if current.instance_id != rep["old"]["instance_id"]:
                self.say(f"refusing to delete {current.name}: it is now instance {current.instance_id}, not the "
                         f"recorded {rep['old']['instance_id']}")
                return 1
            self.delete_old(current)
            return None
        if phase == "done":
            self.say("already done")
            return 0
        self.say(f"the recorded replacement is {phase}; nothing to do")
        return 1

    def run(self) -> int:
        started = self.clock()
        while True:
            try:
                code = self.step()
            except LeaseLost as exc:
                self.say(f"stopped: the target lease was lost ({exc}); rerun to resume")
                return EXIT_RESUMABLE
            if code is not None:
                return code
            if self.clock() - started >= self.args.max_wait_seconds:
                self.say(f"stopped after {self.args.max_wait_seconds} s in phase {self.rep['phase']}; rerun to resume")
                return EXIT_RESUMABLE
            self.sleep(self.args.poll_seconds)


def resumable(recorded: dict, instance: str) -> bool:
    """A recorded replacement of this VM that a rerun continues instead of starting over: one in progress, or one
    marked aborted although an old runner was already deregistered (so it was never reversible)."""
    if (recorded.get("old") or {}).get("name") != instance:
        return False
    phase = recorded.get("phase")
    return phase not in ("done", "aborted", "held") or (phase == "aborted" and irreversible(recorded))


def run_replace(args: argparse.Namespace, config: rf.FloorConfig) -> int:
    target = config.target(args.target)
    if not config.state_uri:
        raise rf.FloorError(f"{rf.ENV_STATE_URI} is not set")
    tokens = rf.TokenSource(rf.TOKEN_SOURCE_GH, account_override=args.github_user)
    token = tokens(target)
    github = rf.GitHubClient(lambda: token)
    inventory = rf.ComputeInventory(config.project)
    location = rf.state_location(config.state_uri)
    client = GcsClient(location.bucket)
    store = rf.TargetStore(client, location, config.project, target)
    record = store.load()
    old = inventory.describe(args.instance, args.zone)
    runners = github.list_runners(target.target)
    hosts = inventory.list_hosts()
    recorded = record.get("replacement") or {}
    if resumable(recorded, args.instance):
        # A replacement in progress resumes from its record: its old runners may already be deregistered and its VM
        # deleted, which a fresh preflight would refuse.
        print(json.dumps({"target": target.name, "resume": {k: v for k, v in recorded.items()
                                                             if k != "original_labels"}}, indent=2, sort_keys=True))
        # The configuration may have changed since the replacement started: a managed host can only ever carry the
        # target's current labels, so a required label that left labels= would make the resume wait forever.
        lost = sorted(rf.folded(recorded.get("required_labels") or []) - rf.folded({rf.BASE_LABEL, *target.labels}))
        if lost and recorded.get("phase") in ("ensure_managed", "verified", "draining"):
            print(f"Refused: {target.name} no longer has labels {', '.join(lost)} in {rf.ENV_TARGETS}, which the "
                  "old runners carry and the managed host must too; restore them, then rerun")
            return 1
        if not args.apply:
            print(f"Preview only; nothing changed. Rerun with --apply to resume phase {recorded.get('phase')}.")
            return 0
        return _apply(args, config, target, token, client, location, store, inventory, check=None, old=old)
    check = preflight(target, old, args.old_runner, runners, hosts, record,
                      accept_dropped=args.accept_dropped_labels, instance=args.instance)
    print(json.dumps({"target": target.name, "old_vm": old.describe() if old else None,
                      "old_runners": [{"name": r.name, "status": r.status, "busy": r.busy, "labels": list(r.labels)}
                                      for r in check.old_runners],
                      "candidates": check.candidates, "required_labels": check.required_labels,
                      "dropped_labels": check.dropped_labels,
                      "managed": check.managed.describe() if check.managed else None,
                      "steps": check.steps, "problems": check.problems,
                      "recorded": record.get("replacement")}, indent=2, sort_keys=True))
    if check.problems:
        print("Refused: " + "; ".join(check.problems))
        return 1
    if not args.apply:
        print("Preview only; nothing changed. Rerun with --apply to run these steps.")
        return 0
    return _apply(args, config, target, token, client, location, store, inventory, check=check, old=old)


def _apply(args: argparse.Namespace, config: rf.FloorConfig, target: rf.FloorTarget, token: str, client: GcsClient,
           location: Any, store: rf.TargetStore, inventory: Any, *, check: Preflight | None,
           old: rf.Host | None) -> int:
    # runner_drain and the cordon use gh and gcloud as child processes: hand gh this account's token only.
    os.environ["GH_TOKEN"] = token
    lease = rf.target_lease(client, location, config.project, target,
                            owner=f"replace:{socket.gethostname()}:{os.getpid()}")
    if not lease.acquire():
        print(f"The target lease is held by {lease.holder.describe() if lease.holder else 'another controller'}; "
              "pause the watchdog or wait, then rerun")
        return EXIT_RESUMABLE
    try:
        record = store.load()
        current = record.get("replacement") or {}
        if current and (current.get("old") or {}).get("name") != args.instance and \
                current.get("phase") not in ("aborted", "held", "done"):
            print(f"Another replacement ({(current.get('old') or {}).get('name')}) is in progress for this target")
            return 1
        if resumable(current, args.instance) and current.get("phase") == "aborted":
            # Recorded as aborted although an old runner is gone: the rollback was never possible, so it goes on.
            current.update(phase="draining", abort_reason=None)
            store.save(lease)
        elif not current or current.get("phase") in ("aborted", "held", "done"):
            if record.get("replacement", {}).get("phase") == "done" and \
                    record["replacement"]["old"]["name"] == args.instance:
                print("Already done.")
                return 0
            if check is None or old is None:
                print("The recorded replacement changed while this command started; rerun it")
                return EXIT_RESUMABLE
            record["replacement"] = {"old": {"name": old.name, "zone": old.zone, "instance_id": old.instance_id,
                                             "machine_type": old.machine_type},
                                     "old_runner_names": [r.name for r in check.old_runners],
                                     "required_labels": check.required_labels, "phase": "ensure_managed",
                                     "started_at": time.time()}
            store.save(lease)
        provider = rf.GcloudProvider(config.project, guard=lease.ensure, deadline=rf.Deadline(), inventory=inventory)
        guarded = rf.GitHubClient(lambda: token, guard=lease.ensure)  # every GitHub mutation proves the lease
        return Replacement(args, config, target, store=store, lease=lease, inventory=inventory, github=guarded,
                           provider=provider).run()
    finally:
        lease.release()
