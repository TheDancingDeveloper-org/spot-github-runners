"""Target configuration and GitHub Actions API routing for repositories and organizations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TargetConfig:
    name: str                           # e.g., "swack-tools" or "xbmc4lyfe/newznab-tmux"
    target_type: str = ""               # "org" or "repo"; auto-inferred if empty
    min_runners: int = 1                # Always-on floor
    runner_prefix: str = ""             # Host prefix
    runner_group_id: int | None = None  # None for repos, integer for orgs
    runner_patch: Any | None = None     # Optional RunnerPatch instance

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Target name cannot be empty")
        self.name = self.name.strip()

        if "/" in self.name or self.target_type == "repo":
            if self.name.startswith("/") or self.name.endswith("/"):
                raise ValueError(f"Repository target '{self.name}' cannot have leading or trailing slashes")
            parts = self.name.split("/")
            if len(parts) != 2:
                raise ValueError(f"Repository target '{self.name}' must have format 'owner/repo' with exactly one slash")
            owner, repo = parts[0].strip(), parts[1].strip()
            if not owner or not repo:
                raise ValueError(f"Repository target '{self.name}' must have non-empty owner and repo parts")
            self.name = f"{owner}/{repo}"
            if not self.target_type:
                self.target_type = "repo"
            elif self.target_type != "repo":
                raise ValueError(f"Target '{self.name}' contains '/' but target_type is '{self.target_type}'")
            if self.runner_group_id is not None:
                raise ValueError(f"Personal repository target '{self.name}' cannot have a runner_group_id")
        else:
            if not self.target_type:
                self.target_type = "org"
            elif self.target_type != "org":
                raise ValueError(f"Target '{self.name}' has no '/' but target_type is '{self.target_type}'")

    @property
    def owner(self) -> str:
        return self.name.split("/")[0] if "/" in self.name else self.name

    @property
    def repo(self) -> str | None:
        return self.name.split("/")[1] if "/" in self.name else None

    @property
    def is_repo(self) -> bool:
        return self.target_type == "repo"

    @property
    def is_org(self) -> bool:
        return self.target_type == "org"

    @property
    def api_path(self) -> str:
        return f"repos/{self.name}" if self.is_repo else f"orgs/{self.name}"

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.name}"

    @property
    def registration_token_url(self) -> str:
        return f"{self.api_path}/actions/runners/registration-token"

    @property
    def runners_url(self) -> str:
        return f"{self.api_path}/actions/runners"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target_type": self.target_type,
            "min_runners": self.min_runners,
            "runner_prefix": self.runner_prefix,
            "runner_group_id": self.runner_group_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetConfig:
        return cls(
            name=data["name"],
            target_type=data.get("target_type", ""),
            min_runners=data.get("min_runners", 1),
            runner_prefix=data.get("runner_prefix", ""),
            runner_group_id=data.get("runner_group_id"),
            runner_patch=data.get("runner_patch"),
        )


def resolve_target(target: str | TargetConfig | None, default_org: str = "swack-tools") -> TargetConfig:
    if isinstance(target, TargetConfig):
        return target
    if target is None:
        return TargetConfig(name=default_org)
    name = target.strip()
    if not name:
        raise ValueError("Target name cannot be empty")
    return TargetConfig(name=name)
