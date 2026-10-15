"""Configurable scaling policy engine, hourly CI/CD run counters, CPU scope evaluators
(all/majority/any), VM journal OOM attribution, and GitHub Actions step/line deep-link
generator for the Web UI Dashboard.
"""

from __future__ import annotations

import math
import re
from typing import Any


GITHUB_STEP_LINE_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/"
    r"(?P<run_id>\d+)/job/(?P<job_id>\d+)#step:(?P<step>\d+):(?P<line>\d+)$"
)


def build_github_oom_deep_link(
    repository: str,
    run_id: int | str,
    job_id: int | str,
    step_number: int,
    line_number: int,
) -> str:
    """Build an exact GitHub Actions job step + line deep link.

    Example:
    https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50
    """
    repo = str(repository).strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Invalid GitHub repository '{repository}'")
    run_str = str(run_id).strip()
    job_str = str(job_id).strip()
    if not run_str.isdigit() or not job_str.isdigit():
        raise ValueError("run_id and job_id must be numeric")
    step = int(step_number)
    line = int(line_number)
    if step < 1 or line < 1:
        raise ValueError("step_number and line_number must be positive integers")
    return f"https://github.com/{repo}/actions/runs/{run_str}/job/{job_str}#step:{step}:{line}"


def parse_github_oom_deep_link(url: str) -> dict[str, Any]:
    """Parse and validate a GitHub Actions job step + line deep link."""
    match = GITHUB_STEP_LINE_URL_RE.match((url or "").strip())
    if not match:
        raise ValueError(f"Invalid GitHub Actions step/line URL: {url}")
    return {
        "repository": match.group("repo"),
        "run_id": int(match.group("run_id")),
        "job_id": int(match.group("job_id")),
        "step_number": int(match.group("step")),
        "line_number": int(match.group("line")),
        "url": match.group(0),
    }


def default_scaling_config() -> dict[str, Any]:
    """Return the default scaling policy configuration for CI/CD runners and Rust builders."""
    return {
        "cicd_runner": {
            "min_runners": 4,
            "max_runners": 24,
            "runners_per_instance": 2,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "scale_up_step_runners": 2,
            # Scale-up triggers
            "enable_queued_jobs_trigger": True,
            "queued_jobs_threshold": 2,
            "enable_provisioned_jobs_trigger": True,
            "provisioned_jobs_threshold": 6,
            "enable_hourly_runs_scale_up": True,
            "hourly_runs_scale_up_threshold": 20,
            "hourly_runs_scale_up_target_runners": 8,
            "enable_cpu_utilization_trigger": True,
            "cpu_threshold_percent": 50.0,
            "cpu_scope": "majority",  # "all", "majority", or "any"
            "enable_journal_oom_trigger": True,
            "enable_github_ci_oom_trigger": True,
            # Scale-down triggers
            "enable_hourly_runs_scale_down": True,
            "scale_down_max_jobs_past_hour": 5,
            "scale_down_target_runners": 2,
            "enable_idle_cpu_scale_down": True,
            "scale_down_cpu_percent": 25.0,
            # Max scale-up budget (including disk costs)
            "max_scale_up_hourly_usd": 5.00,
            "max_scale_up_daily_usd": 10.00,
            "max_scale_up_monthly_usd": 50.00,
            "include_disk_in_scale_budget": True,
        },
        "rust_builder": {
            "min_instances": 1,
            "max_instances": 4,
            "builder_vcpu": 16,
            "builder_ram_gib": 32,
            "enable_queue_trigger": True,
            "queued_builds_threshold": 2,
            "enable_cpu_utilization_trigger": True,
            "cpu_threshold_percent": 65.0,
            "cpu_scope": "majority",
            "enable_journal_oom_trigger": True,
            "enable_low_builds_scale_down": True,
            "scale_down_max_builds_past_hour": 2,
            "scale_down_target_instances": 1,
            "max_scale_up_hourly_usd": 3.50,
            "max_scale_up_daily_usd": 12.00,
            "max_scale_up_monthly_usd": 80.00,
            "include_disk_in_scale_budget": True,
        },
    }


def evaluate_cpu_scope(
    cpu_percentages: list[float],
    threshold_percent: float,
    scope: str,
) -> tuple[bool, str]:
    """Evaluate whether CPU utilization across instances meets the 'all', 'majority', or 'any' rule."""
    if not cpu_percentages:
        return False, "No active instances reporting CPU utilization"
    hot_count = sum(1 for val in cpu_percentages if val > threshold_percent)
    total = len(cpu_percentages)
    norm_scope = (scope or "majority").lower()
    if norm_scope == "all":
        triggered = hot_count == total
        detail = f"{hot_count}/{total} instances > {threshold_percent:g}% CPU (requires ALL)"
    elif norm_scope == "any":
        triggered = hot_count >= 1
        detail = f"{hot_count}/{total} instances > {threshold_percent:g}% CPU (requires ANY)"
    else:
        # Majority means strictly more than 50% of instances
        triggered = hot_count * 2 > total
        detail = f"{hot_count}/{total} instances > {threshold_percent:g}% CPU (requires MAJORITY)"
    return triggered, detail


def evaluate_scaling_decision(
    policy: dict[str, Any],
    telemetry: dict[str, Any],
    per_instance_hourly_usd: float = 0.14,
) -> dict[str, Any]:
    """Evaluate live telemetry against scaling triggers and max scale-up budgets.

    Returns the recommended runner count, instance count, active triggers, OOM links,
    and budget compliance details.
    """
    cfg = {**default_scaling_config()["cicd_runner"], **(policy or {})}

    current_runners = max(0, int(telemetry.get("current_runners", 4)))
    runners_per_vm = max(1, int(cfg.get("runners_per_instance", 2)))
    min_runners = max(1, int(cfg.get("min_runners", 4)))
    max_runners = max(min_runners, int(cfg.get("max_runners", 24)))
    step_runners = max(1, int(cfg.get("scale_up_step_runners", 2)))

    jobs_ran_past_hour = max(0, int(telemetry.get("jobs_ran_past_hour", 0)))
    provisioned_jobs = max(0, int(telemetry.get("provisioned_jobs", 0)))
    queued_jobs = max(0, int(telemetry.get("queued_jobs", 0)))
    cpu_percentages = [float(x) for x in telemetry.get("instance_cpu_percentages", [])]
    journal_oom_events = list(telemetry.get("journal_oom_events", []))
    github_oom_runs = list(telemetry.get("github_oom_runs", []))

    scale_up_reasons: list[dict[str, Any]] = []
    scale_down_reasons: list[dict[str, Any]] = []
    desired_runners = current_runners

    # 1. GitHub Actions CI Run OOM Trigger
    if cfg.get("enable_github_ci_oom_trigger", True) and github_oom_runs:
        for run in github_oom_runs:
            link = run.get("deep_link") or build_github_oom_deep_link(
                run.get("repository", "Appz4Fun/nzbget"),
                run.get("run_id", 38596411473),
                run.get("job_id", 115964282184),
                run.get("step_number", 2),
                run.get("line_number", 50),
            )
            scale_up_reasons.append(
                {
                    "trigger": "github_ci_oom",
                    "summary": (
                        f"GitHub Actions CI run #{run.get('run_id')} job {run.get('job_id')} "
                        f"died with OOM at step {run.get('step_number')}:{run.get('line_number')}"
                    ),
                    "deep_link": link,
                    "repository": run.get("repository"),
                    "run_id": run.get("run_id"),
                    "job_id": run.get("job_id"),
                    "step_number": run.get("step_number"),
                    "line_number": run.get("line_number"),
                    "error_excerpt": run.get(
                        "error_excerpt",
                        "cargo: rustc killed by signal 9 (SIGKILL) — cgroup memory.oom.group=1 (exit code 137)",
                    ),
                }
            )
        desired_runners = max(desired_runners, current_runners + step_runners)

    # 2. VM Journal OOM Trigger
    if cfg.get("enable_journal_oom_trigger", True) and journal_oom_events:
        for evt in journal_oom_events:
            scale_up_reasons.append(
                {
                    "trigger": "vm_journal_oom",
                    "summary": (
                        f"VM journalctl on {evt.get('instance_name', 'unknown')} recorded OOM kill: "
                        f"{evt.get('process', 'rustc')} (PID {evt.get('pid', 0)})"
                    ),
                    "instance_name": evt.get("instance_name"),
                    "journal_line": evt.get(
                        "journal_line",
                        "kernel: Out of memory: Killed process 18492 (rustc) total-vm:34128900kB, anon-rss:31840120kB",
                    ),
                }
            )
        desired_runners = max(desired_runners, current_runners + step_runners)

    # 3. Queued jobs waiting for a runner
    if cfg.get("enable_queued_jobs_trigger", True) and queued_jobs >= int(cfg.get("queued_jobs_threshold", 2)):
        scale_up_reasons.append(
            {
                "trigger": "queued_jobs",
                "summary": (
                    f"{queued_jobs} queued jobs waiting for a runner "
                    f"(threshold >= {cfg.get('queued_jobs_threshold', 2)})"
                ),
            }
        )
        desired_runners = max(desired_runners, current_runners + max(step_runners, queued_jobs))

    # 4. Current provisioned / active jobs
    if cfg.get("enable_provisioned_jobs_trigger", True) and provisioned_jobs >= int(cfg.get("provisioned_jobs_threshold", 6)):
        scale_up_reasons.append(
            {
                "trigger": "provisioned_jobs",
                "summary": (
                    f"{provisioned_jobs} provisioned active jobs "
                    f"(threshold >= {cfg.get('provisioned_jobs_threshold', 6)})"
                ),
            }
        )
        desired_runners = max(desired_runners, current_runners + step_runners)

    # 5. Hourly CI/CD runs volume scale-up
    if cfg.get("enable_hourly_runs_scale_up", True) and jobs_ran_past_hour >= int(cfg.get("hourly_runs_scale_up_threshold", 20)):
        target_from_hourly = int(cfg.get("hourly_runs_scale_up_target_runners", current_runners + step_runners))
        scale_up_reasons.append(
            {
                "trigger": "hourly_runs_high",
                "summary": (
                    f"{jobs_ran_past_hour} CI/CD runs in past hour "
                    f"(threshold >= {cfg.get('hourly_runs_scale_up_threshold', 20)})"
                ),
            }
        )
        desired_runners = max(desired_runners, target_from_hourly)

    # 6. CPU utilization threshold & scope ('all', 'majority', 'any')
    if cfg.get("enable_cpu_utilization_trigger", True):
        cpu_triggered, cpu_detail = evaluate_cpu_scope(
            cpu_percentages,
            float(cfg.get("cpu_threshold_percent", 50.0)),
            str(cfg.get("cpu_scope", "majority")),
        )
        if cpu_triggered:
            scale_up_reasons.append(
                {
                    "trigger": f"cpu_{cfg.get('cpu_scope', 'majority')}",
                    "summary": f"CPU threshold exceeded: {cpu_detail}",
                }
            )
            desired_runners = max(desired_runners, current_runners + step_runners)

    # Scale-down evaluation (only when no scale-up triggers fired and no jobs are queued)
    if not scale_up_reasons:
        if (
            cfg.get("enable_hourly_runs_scale_down", True)
            and jobs_ran_past_hour <= int(cfg.get("scale_down_max_jobs_past_hour", 5))
            and queued_jobs == 0
        ):
            low_target = max(1, int(cfg.get("scale_down_target_runners", 2)))
            scale_down_reasons.append(
                {
                    "trigger": "hourly_runs_low",
                    "summary": (
                        f"Only {jobs_ran_past_hour} CI/CD jobs ran in the last hour "
                        f"(<= {cfg.get('scale_down_max_jobs_past_hour', 5)} threshold) -> "
                        f"scaling down to {low_target} CI/CD runners"
                    ),
                }
            )
            desired_runners = low_target
        elif (
            cfg.get("enable_idle_cpu_scale_down", True)
            and queued_jobs == 0
            and cpu_percentages
            and all(c < float(cfg.get("scale_down_cpu_percent", 25.0)) for c in cpu_percentages)
        ):
            idle_target = max(min_runners, current_runners - runners_per_vm)
            if idle_target < current_runners:
                scale_down_reasons.append(
                    {
                        "trigger": "idle_cpu",
                        "summary": (
                            f"All instances below {cfg.get('scale_down_cpu_percent', 25.0):g}% CPU "
                            f"with 0 queued jobs -> scaling down to {idle_target} runners"
                        ),
                    }
                )
                desired_runners = idle_target
        else:
            desired_runners = max(min_runners, current_runners)
    else:
        desired_runners = min(max_runners, max(min_runners, desired_runners))

    # Enforce max scale-up budget caps (hourly, daily, monthly)
    desired_instances = math.ceil(desired_runners / runners_per_vm) if desired_runners > 0 else 0
    unit_hourly = max(0.0001, float(per_instance_hourly_usd))
    max_hourly_cap = float(cfg.get("max_scale_up_hourly_usd", 5.00))
    max_daily_cap = float(cfg.get("max_scale_up_daily_usd", 10.00))
    max_monthly_cap = float(cfg.get("max_scale_up_monthly_usd", 50.00))

    effective_hourly_cap = min(
        max_hourly_cap,
        max_daily_cap / 24.0,
        max_monthly_cap / (24.0 * 365.0 / 12.0),
    )
    max_affordable_instances = max(1, int(math.floor(effective_hourly_cap / unit_hourly)))
    budget_capped = False
    budget_cap_note = ""

    if scale_up_reasons and desired_instances > max_affordable_instances:
        budget_capped = True
        desired_instances = max_affordable_instances
        desired_runners = desired_instances * runners_per_vm
        budget_cap_note = (
            f"Scale-up capped at {desired_instances} instance(s) ({desired_runners} runners) "
            f"by max scale-up budget (${max_hourly_cap:.2f}/hr, ${max_daily_cap:.2f}/day, "
            f"${max_monthly_cap:.2f}/mo incl. disks)"
        )

    projected_hourly = round(desired_instances * unit_hourly, 4)
    projected_daily = round(projected_hourly * 24.0, 2)
    projected_monthly = round(projected_daily * (365.0 / 12.0), 2)

    direction = (
        "scale_up"
        if desired_runners > current_runners
        else ("scale_down" if desired_runners < current_runners else "hold")
    )

    return {
        "direction": direction,
        "current_runners": current_runners,
        "desired_runners": desired_runners,
        "current_instances": math.ceil(current_runners / runners_per_vm) if current_runners > 0 else 0,
        "desired_instances": desired_instances,
        "runners_per_instance": runners_per_vm,
        "instance_required_vcpu": runners_per_vm * int(cfg.get("runner_vcpu", 4)),
        "instance_required_ram_gib": runners_per_vm * int(cfg.get("runner_ram_gib", 16)),
        "scale_up_reasons": scale_up_reasons,
        "scale_down_reasons": scale_down_reasons,
        "budget_capped": budget_capped,
        "budget_cap_note": budget_cap_note,
        "projected_hourly_usd": projected_hourly,
        "projected_daily_usd": projected_daily,
        "projected_monthly_usd": projected_monthly,
        "effective_hourly_cap_usd": round(effective_hourly_cap, 4),
    }
