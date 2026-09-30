"""Labels for standard and Docker-enabled self-hosted runners."""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_RUNNER_LABELS = {"self-hosted", "Linux", "X64", "Windows", "macOS", "ARM64", "ARM"}


def custom_labels_for_runner(index: int, docker_runner_count: int, standard_labels: Iterable[str]) -> list[str]:
    """Return custom labels for the Spot pool or the separate Docker pool."""
    if index < 1 or docker_runner_count < 0:
        raise ValueError("runner index must be positive and Docker runner count nonnegative")
    if index <= docker_runner_count:
        source = list(standard_labels)
        optional = source[1:] if source and source[0] == "spot" else source
        return list(dict.fromkeys(["docker", *optional]))
    return list(dict.fromkeys(standard_labels))


def counts_toward_capacity(labels: Iterable[str], capacity_label: str = "spot") -> bool:
    """Count runners assigned to the requested capacity pool."""
    return capacity_label in labels
