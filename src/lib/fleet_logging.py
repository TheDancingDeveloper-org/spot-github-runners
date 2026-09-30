"""Append controller lifecycle events and VM observations to a local log."""

from __future__ import annotations

import logging
import os
import signal
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger("spot_runner_autoscaler")


@contextmanager
def lifecycle_log(path: Path):
    """Append timestamped events and record normal or signal-driven shutdowns."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous_level = LOG.level
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    previous_signals = {}

    def stop(signum, frame):
        LOG.info("Controller received %s; shutting down", signal.Signals(signum).name)
        raise SystemExit(0)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signals[signum] = signal.signal(signum, stop)
        LOG.info("Controller started: PID %d; append log %s", os.getpid(), path)
        yield
    except Exception as exc:
        LOG.error("Controller stopped after an error of type %s; see service diagnostics",
                  type(exc).__name__)
        raise
    finally:
        LOG.info("Controller shutdown: PID %d", os.getpid())
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        LOG.removeHandler(handler)
        handler.close()
        LOG.setLevel(previous_level)


def observe_vms(vms, state: dict) -> None:
    """Log inventory changes without inferring why a VM disappeared."""
    previous = state.get("observed_vms", {})
    current = {}
    for vm in vms:
        if not vm.spot or not vm.name.startswith("oxidex-runners-"):
            continue
        key = f"{vm.zone}/{vm.name}"
        current[key] = vm.status
        if key not in previous:
            LOG.info("Spot VM observed: %s; status %s; machine %s", key, vm.status, vm.machine_type)
        elif previous[key] != vm.status:
            LOG.warning("Spot VM status changed: %s; %s -> %s", key, previous[key], vm.status)
        if vm.status.upper() == "TERMINATED" and previous.get(key) != vm.status:
            LOG.warning("Spot VM is terminated: %s; termination cause is unconfirmed", key)
    for key in previous.keys() - current.keys():
        LOG.warning("Spot VM no longer present: %s; consult deletion events for controller actions; "
                    "otherwise the cause is unconfirmed", key)
    state["observed_vms"] = current
