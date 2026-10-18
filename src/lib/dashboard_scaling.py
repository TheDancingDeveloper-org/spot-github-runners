"""Configurable scaling policy engine, hourly CI/CD run counters, CPU scope evaluators
(all/majority/any), VM journal OOM attribution, and GitHub Actions step/line deep-link
generator for the Web UI Dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import time
from typing import Any


GITHUB_STEP_LINE_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/"
    r"(?P<run_id>\d+)/job/(?P<job_id>\d+)#step:(?P<step>\d+):(?P<line>\d+)$"
)

FLEET_RUNNER_NAME_PREFIXES: tuple[str, ...] = (
    "gha-oxidex-runners-",
    "oxidex-runners-",
    "newznab-spot-runners-",
    "newznab-newznab-spot-runners-",
    "spot-runner-",
)


def build_github_oom_deep_link(
    repository: str,
    run_id: int | str,
    job_id: int | str,
    step_number: int | str | None,
    line_number: int | str | None,
) -> str:
    """Build an exact GitHub Actions job step + line deep link.

    Example:
    https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50
    """
    repo = str(repository or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Invalid GitHub repository '{repository}'")
    run_str = str(run_id or "").strip()
    job_str = str(job_id or "").strip()
    if not run_str.isdigit() or not job_str.isdigit():
        raise ValueError("run_id and job_id must be numeric")
    if step_number is None or line_number is None:
        raise ValueError("step_number and line_number cannot be None")
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


GITHUB_OOM_RUN_LOCATION_FIELDS = ("repository", "run_id", "job_id", "step_number", "line_number")


def _positive_int_field(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"add_github_oom_run.{field} must be a positive integer")
    text = str(value).strip()
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"add_github_oom_run.{field} must be a positive integer")
    return int(text)


def validate_github_oom_run(raw: Any) -> dict[str, Any]:
    """Validate a caller-reported GitHub CI failure run; never fills in missing facts.

    The caller supplies the failing job's location, either as a ``deep_link`` or as ``repository``,
    ``run_id``, ``job_id``, ``step_number``, and ``line_number``, plus the failing log line as
    ``error_excerpt``. ``runner_name`` is optional: without it the run counts as an external runner's,
    so it never drives fleet scaling. Raises ValueError when a required fact is missing or malformed.
    """
    if not isinstance(raw, dict):
        raise ValueError("add_github_oom_run must be a JSON object")
    item = dict(raw)
    deep_link = item.get("deep_link")
    if deep_link not in (None, ""):
        if not isinstance(deep_link, str):
            raise ValueError("add_github_oom_run.deep_link must be a string")
        parsed = parse_github_oom_deep_link(deep_link)
        for field in GITHUB_OOM_RUN_LOCATION_FIELDS[1:]:
            parsed[field] = _positive_int_field(parsed[field], field)
        for field in GITHUB_OOM_RUN_LOCATION_FIELDS:
            if item.get(field) in (None, ""):
                continue
            given = (str(item[field]).strip().strip("/") if field == "repository"
                     else _positive_int_field(item[field], field))
            if given != parsed[field]:
                raise ValueError(f"add_github_oom_run.{field} does not match deep_link")
        location = {field: parsed[field] for field in GITHUB_OOM_RUN_LOCATION_FIELDS}
        location["deep_link"] = parsed["url"]
    else:
        missing = [field for field in GITHUB_OOM_RUN_LOCATION_FIELDS if item.get(field) in (None, "")]
        if missing:
            raise ValueError(
                "add_github_oom_run requires a deep_link or " + ", ".join(GITHUB_OOM_RUN_LOCATION_FIELDS)
                + f" (missing: {', '.join(missing)})"
            )
        if not isinstance(item["repository"], str):
            raise ValueError("add_github_oom_run.repository must be a string")
        location = {"repository": item["repository"].strip().strip("/")}
        for field in GITHUB_OOM_RUN_LOCATION_FIELDS[1:]:
            location[field] = _positive_int_field(item[field], field)
        location["deep_link"] = build_github_oom_deep_link(
            location["repository"], location["run_id"], location["job_id"],
            location["step_number"], location["line_number"],
        )
    item.update(location)
    excerpt = item.get("error_excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise ValueError("add_github_oom_run requires error_excerpt, the failing log line")
    item["error_excerpt"] = excerpt.strip()
    runner_name = item.pop("runner_name", None)
    if runner_name is not None and not isinstance(runner_name, str):
        raise ValueError("add_github_oom_run.runner_name must be a string")
    if runner_name and runner_name.strip():
        item["runner_name"] = runner_name.strip()
    return item


def is_self_hosted_spot_runner(runner_name: str | None) -> bool:
    """Return True only if runner_name affirmatively matches our self-hosted GCP Spot runner
    fleet naming convention (e.g. 'gha-oxidex-runners-...'), or False if missing or external.
    """
    if not runner_name:
        return False
    norm = str(runner_name).strip().lower()
    if not norm:
        return False
    return norm.startswith(FLEET_RUNNER_NAME_PREFIXES)


def classify_github_ci_failure(
    error_excerpt: str,
    runner_name: str | None = None,
) -> dict[str, Any]:
    """Classify a GitHub Actions failure log line and runner ownership.

    Prevents:
    1. Default GitHub-hosted or unrelated runners from triggering self-hosted GCP Spot fleet scaling.
    2. Deployment/step timeouts (such as `##[error]Timeout reached, aborting!`)
       from being misclassified as out-of-memory (OOM) events.
    """
    our_runner = is_self_hosted_spot_runner(runner_name)
    text = (error_excerpt or "").strip()
    lower = text.lower()
    if any(
        token in lower
        for token in (
            "timeout reached",
            "timed out",
            "the operation was canceled",
            "canceling pages deployment",
            "etimedout",
        )
    ):
        badge = (
            "Timeout · External/GitHub-Hosted Runner (Not Ours — Ignored)"
            if not our_runner
            else "Timeout (Not OOM — Ignored)"
        )
        return {
            "failure_classification": "TIMEOUT",
            "is_oom": False,
            "is_self_hosted_runner": our_runner,
            "badge_text": badge,
        }
    if any(
        token in lower
        for token in (
            "out of memory",
            "oom-kill",
            "oom_kill",
            "memory.oom",
            "memory.max",
            "exit code 137",
            "exit 137",
            "signal: 9",
            "signal 9",
            "sigkill",
            "killed process",
            "cannot allocate memory",
            "enomem",
        )
    ):
        if not our_runner:
            return {
                "failure_classification": "EXTERNAL_RUNNER_OOM",
                "is_oom": False,
                "is_self_hosted_runner": False,
                "badge_text": "OOM on External/GitHub-Hosted Runner (Not Ours — Ignored)",
            }
        return {
            "failure_classification": "OOM",
            "is_oom": True,
            "is_self_hosted_runner": True,
            "badge_text": "OOM Killed (Exit 137 · Self-Hosted Spot Runner)",
        }
    return {
        "failure_classification": "OTHER_FAILURE",
        "is_oom": False,
        "is_self_hosted_runner": our_runner,
        "badge_text": (
            "External/GitHub-Hosted Runner (Not Ours — Ignored)"
            if not our_runner
            else "Non-OOM Failure (Ignored)"
        ),
    }


def _github_oom_excerpt(run: dict[str, Any]) -> str:
    # Raw evidence wins over stale cached classification flags, matching the scaling evaluator's contract.
    if "error_excerpt" in run and run.get("error_excerpt") is not None:
        return str(run["error_excerpt"])
    if run.get("is_oom") is False or str(run.get("failure_classification", "")).strip().upper() == "TIMEOUT":
        return "##[error]Timeout reached, aborting!"
    return "cargo: rustc killed by signal 9 (SIGKILL) — cgroup memory.oom.group=1 (exit code 137)"


def _is_eligible_github_oom(run: dict[str, Any]) -> bool:
    """Only classified OOMs on our self-hosted GCP runners can affect scaling or incident disposition."""
    classification = classify_github_ci_failure(_github_oom_excerpt(run), runner_name=run.get("runner_name"))
    return bool(classification["is_self_hosted_runner"] and classification["is_oom"])


def _is_recent_unhandled_oom(
    evt: dict[str, Any],
    now_ts: float,
    lookback_seconds: float,
) -> bool:
    """Return True if an OOM event is unhandled and occurred within the active lookback window."""
    if (
        evt.get("acknowledged")
        or evt.get("resolved")
        or evt.get("handled")
        or evt.get("active") is False
    ):
        return False
    ts_raw = evt.get("failed_at") or evt.get("observed_at") or evt.get("timestamp")
    if not ts_raw:
        return False
    try:
        dt = datetime.fromisoformat(str(ts_raw).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_seconds = now_ts - dt.timestamp()
        return 0.0 <= age_seconds <= lookback_seconds
    except ValueError:
        return False


def default_scaling_config() -> dict[str, Any]:
    """Return the default scaling policy configuration for CI/CD runners and Rust builders."""
    return {
        "cicd_runner": {
            "min_runners": 4,
            "max_runners": 24,
            "runners_per_instance": 4,
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
            "oom_lookback_minutes": 60,
            # Scale-down triggers
            "enable_hourly_runs_scale_down": True,
            "scale_down_max_jobs_past_hour": 5,
            "scale_down_target_runners": 4,
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
            "builder_ram_gib": 28,
            "container_vcpu": 16,
            "container_ram_gib": 28,
            "enable_queue_trigger": True,
            "queued_builds_threshold": 2,
            "enable_cpu_utilization_trigger": True,
            "cpu_threshold_percent": 65.0,
            "cpu_scope": "majority",
            "enable_journal_oom_trigger": True,
            "oom_lookback_minutes": 60,
            "enable_low_builds_scale_down": True,
            "scale_down_max_builds_past_hour": 2,
            "scale_down_target_instances": 1,
            "max_scale_up_hourly_usd": 3.50,
            "max_scale_up_daily_usd": 12.00,
            "max_scale_up_monthly_usd": 80.00,
            "include_disk_in_scale_budget": True,
        },
    }


def _finite_float_val(val: Any, name: str = "value") -> float:
    if isinstance(val, bool):
        raise ValueError(f"{name} must be a finite number")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number (got {val!r})")
    return f


def _finite_int_val(val: Any, name: str = "value") -> int:
    if isinstance(val, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f"{name} must be a finite integer (got {val!r})")
    return int(val)


def _assert_finite_json_tree(val: Any, path: str = "value") -> None:
    """Recursively reject NaN or Infinity anywhere in a nested policy or telemetry payload."""
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f"{path} must be a finite number (got {val!r})")
    if isinstance(val, dict):
        for k, v in val.items():
            _assert_finite_json_tree(v, f"{path}.{k}")
    elif isinstance(val, (list, tuple)):
        for idx, item in enumerate(val):
            _assert_finite_json_tree(item, f"{path}[{idx}]")


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
    norm_scope = (scope or "majority").strip().lower()
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
    now_epoch: float | None = None,
    max_instances: int | None = None,
    running_instance_hourly_rates: list[float] | None = None,
    other_role_hourly_usd: float | None = None,
) -> dict[str, Any]:
    """Evaluate live telemetry against scaling triggers and max scale-up budgets.

    Returns the recommended runner count, instance count, active triggers, OOM links,
    and budget compliance details.
    """
    _assert_finite_json_tree(policy, "policy")
    _assert_finite_json_tree(telemetry, "telemetry")
    cfg = {**default_scaling_config()["cicd_runner"], **(policy or {})}
    now_ts = _finite_float_val(now_epoch, "now_epoch") if now_epoch is not None else time.time()
    lookback_seconds = max(
        60.0, _finite_float_val(cfg.get("oom_lookback_minutes", 60), "oom_lookback_minutes") * 60.0
    )

    current_runners = max(0, _finite_int_val(telemetry.get("current_runners", 4), "current_runners"))
    explicit_policy_density = (policy or {}).get("runners_per_instance")
    template_density = telemetry.get("template_runners_per_instance") or cfg.get("template_runners_per_instance")
    known_current_instances = telemetry.get("current_instances")
    if explicit_policy_density is not None:
        runners_per_vm = max(1, _finite_int_val(explicit_policy_density, "runners_per_instance"))
    elif template_density is not None:
        runners_per_vm = max(1, _finite_int_val(template_density, "template_runners_per_instance"))
    elif (
        known_current_instances is not None
        and _finite_int_val(known_current_instances, "current_instances") > 0
        and current_runners > 0
    ):
        runners_per_vm = max(
            1, math.ceil(current_runners / _finite_int_val(known_current_instances, "current_instances"))
        )
    else:
        runners_per_vm = max(1, _finite_int_val(cfg.get("runners_per_instance", 4), "runners_per_instance"))

    current_instances_count = (
        max(0, _finite_int_val(known_current_instances, "current_instances"))
        if known_current_instances is not None
        else (math.ceil(current_runners / runners_per_vm) if current_runners > 0 else 0)
    )

    min_runners = max(1, _finite_int_val(cfg.get("min_runners", 4), "min_runners"))
    max_runners = max(min_runners, _finite_int_val(cfg.get("max_runners", 24), "max_runners"))
    step_runners = max(1, _finite_int_val(cfg.get("scale_up_step_runners", 2), "scale_up_step_runners"))

    jobs_ran_past_hour = max(0, _finite_int_val(telemetry.get("jobs_ran_past_hour", 0), "jobs_ran_past_hour"))
    raw_prov = (
        telemetry.get("provisioned_jobs")
        if telemetry.get("provisioned_jobs") is not None
        else telemetry.get("active_jobs", 0)
    )
    provisioned_jobs = max(0, _finite_int_val(raw_prov, "provisioned_jobs"))
    raw_queued = (
        telemetry.get("queued_jobs")
        if telemetry.get("queued_jobs") is not None
        else telemetry.get("queued_runner_jobs", 0)
    )
    queued_jobs = max(0, _finite_int_val(raw_queued, "queued_jobs"))
    cpu_percentages = [
        _finite_float_val(x, "instance_cpu_percentages")
        for x in (telemetry.get("instance_cpu_percentages") or [])
    ]
    journal_oom_events = list(telemetry.get("journal_oom_events") or [])
    github_oom_runs = list(telemetry.get("github_oom_runs") or [])

    scale_up_reasons: list[dict[str, Any]] = []
    scale_down_reasons: list[dict[str, Any]] = []
    desired_runners = current_runners

    # 1. GitHub Actions CI Run OOM Trigger (ignores TIMEOUT, external runners, or expired events)
    if cfg.get("enable_github_ci_oom_trigger", True) and github_oom_runs:
        oom_target_runners = current_runners
        for run in github_oom_runs:
            if not isinstance(run, dict) or not _is_eligible_github_oom(run):
                continue
            if not _is_recent_unhandled_oom(run, now_ts, lookback_seconds):
                continue
            if run.get("baseline_runners") is not None:
                baseline_val = _finite_int_val(run["baseline_runners"], "baseline_runners")
                required_target = min(max_runners, baseline_val + step_runners)
                if current_runners >= required_target and (current_runners > baseline_val or baseline_val >= max_runners):
                    run["handled"] = True
                    continue
            excerpt = _github_oom_excerpt(run)
            baseline_val = _finite_int_val(
                run.setdefault("baseline_runners", current_runners),
                "baseline_runners",
            )
            oom_target_runners = max(oom_target_runners, baseline_val + step_runners)
            try:
                link = run.get("deep_link") or build_github_oom_deep_link(
                    run.get("repository") or "swack-tools/oxidex",
                    run.get("run_id") or 37192272062,
                    run.get("job_id") or 111407847874,
                    run.get("step_number") or 8,
                    run.get("line_number") or 106,
                )
            except (ValueError, TypeError):
                link = ""
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
                    "error_excerpt": excerpt,
                }
            )
        if oom_target_runners > current_runners:
            desired_runners = max(desired_runners, oom_target_runners)

    # 2. VM Journal OOM Trigger (only recent unhandled CI runner events; exclude Rust builder OOMs)
    if cfg.get("enable_journal_oom_trigger", True) and journal_oom_events:
        active_journal_ooms: list[dict[str, Any]] = []
        journal_oom_target_runners = current_runners
        for evt in journal_oom_events:
            if not isinstance(evt, dict):
                continue
            if (
                str(evt.get("role", "")).strip().lower() == "rust_builder"
                or str(evt.get("instance_name", "")).strip().lower().startswith("builder-")
            ):
                continue
            if not _is_recent_unhandled_oom(evt, now_ts, lookback_seconds):
                continue
            if evt.get("baseline_runners") is not None:
                baseline_val = _finite_int_val(evt["baseline_runners"], "baseline_runners")
                required_target = min(max_runners, baseline_val + step_runners)
                if current_runners >= required_target and (current_runners > baseline_val or baseline_val >= max_runners):
                    evt["handled"] = True
                    continue
            baseline_val = _finite_int_val(
                evt.setdefault("baseline_runners", current_runners),
                "baseline_runners",
            )
            journal_oom_target_runners = max(journal_oom_target_runners, baseline_val + step_runners)
            active_journal_ooms.append(evt)
        for evt in active_journal_ooms:
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
        if active_journal_ooms and journal_oom_target_runners > current_runners:
            desired_runners = max(desired_runners, journal_oom_target_runners)

    # 3. Queued jobs waiting for a runner
    if cfg.get("enable_queued_jobs_trigger", True) and queued_jobs >= _finite_int_val(cfg.get("queued_jobs_threshold", 2), "queued_jobs_threshold"):
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
    if cfg.get("enable_provisioned_jobs_trigger", True) and provisioned_jobs >= _finite_int_val(cfg.get("provisioned_jobs_threshold", 6), "provisioned_jobs_threshold"):
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
    if cfg.get("enable_hourly_runs_scale_up", True) and jobs_ran_past_hour >= _finite_int_val(cfg.get("hourly_runs_scale_up_threshold", 20), "hourly_runs_scale_up_threshold"):
        target_from_hourly = _finite_int_val(cfg.get("hourly_runs_scale_up_target_runners", current_runners + step_runners), "hourly_runs_scale_up_target_runners")
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
            _finite_float_val(cfg.get("cpu_threshold_percent", 50.0), "cpu_threshold_percent"),
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

    # Scale-down evaluation (only when no scale-up triggers fired, no jobs are queued,
    # and target preserves capacity for both min_runners and active provisioned_jobs)
    capacity_floor = max(
        min(max_runners, max(min_runners, provisioned_jobs)),
        min(current_runners, provisioned_jobs),
    )
    if not scale_up_reasons:
        if (
            cfg.get("enable_hourly_runs_scale_down", True)
            and jobs_ran_past_hour <= _finite_int_val(cfg.get("scale_down_max_jobs_past_hour", 5), "scale_down_max_jobs_past_hour")
            and queued_jobs == 0
        ):
            low_target = max(
                capacity_floor,
                min(
                    max_runners,
                    _finite_int_val(cfg.get("scale_down_target_runners", min_runners), "scale_down_target_runners"),
                ),
            )
            if low_target < current_runners:
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
            else:
                desired_runners = max(capacity_floor, current_runners)
        elif (
            cfg.get("enable_idle_cpu_scale_down", True)
            and queued_jobs == 0
            and cpu_percentages
            and all(c < _finite_float_val(cfg.get("scale_down_cpu_percent", 25.0), "scale_down_cpu_percent") for c in cpu_percentages)
        ):
            idle_target = max(capacity_floor, current_runners - runners_per_vm)
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
                desired_runners = max(capacity_floor, current_runners)
        else:
            desired_runners = max(capacity_floor, current_runners)
    else:
        desired_runners = max(capacity_floor, current_runners, min(max_runners, desired_runners))

    # Compute desired instances from fleet state and runner density, quantizing scale-up to whole VM capacity
    if desired_runners == current_runners and known_current_instances is not None:
        desired_instances = current_instances_count
    elif desired_runners > current_runners:
        if known_current_instances is not None:
            added_instances = math.ceil((desired_runners - current_runners) / runners_per_vm)
            max_add_by_runners = max(0, (max_runners - current_runners) // runners_per_vm)
            added_instances = min(added_instances, max_add_by_runners)
            desired_instances = current_instances_count + added_instances
            desired_runners = current_runners + added_instances * runners_per_vm
        else:
            desired_instances = math.ceil(desired_runners / runners_per_vm)
            max_instances_by_runners = max(current_instances_count, max_runners // runners_per_vm)
            if desired_instances > max_instances_by_runners:
                desired_instances = max_instances_by_runners
            if desired_instances == current_instances_count:
                desired_runners = current_runners
            else:
                desired_runners = desired_instances * runners_per_vm
    else:
        desired_instances = math.ceil(desired_runners / runners_per_vm) if desired_runners > 0 else 0
        desired_instances = min(current_instances_count, desired_instances)
        if desired_instances == current_instances_count:
            desired_runners = current_runners
            scale_down_reasons.clear()
        elif desired_instances == 0:
            desired_runners = 0
        elif known_current_instances is not None:
            removed_instances = current_instances_count - desired_instances
            desired_runners = max(capacity_floor, current_runners - removed_instances * runners_per_vm)
        else:
            desired_runners = desired_instances * runners_per_vm
        if desired_runners < current_runners and not scale_down_reasons:
            scale_down_reasons.append(
                {
                    "trigger": "max_runners_cap",
                    "summary": (
                        f"Current fleet ({current_runners} runners across {current_instances_count} VMs) "
                        f"exceeds max_runners={max_runners} -> scaling down to {desired_runners} runners "
                        f"({desired_instances} VMs)"
                    ),
                }
            )

    raw_max_instances = (
        max_instances
        if max_instances is not None
        else (
            (policy or {}).get("max_instances")
            if (policy or {}).get("max_instances") is not None
            else telemetry.get("max_instances")
        )
    )
    if raw_max_instances is not None:
        max_instances_cap = max(1, _finite_int_val(raw_max_instances, "max_instances"))
        if desired_instances > max_instances_cap:
            if desired_instances < current_instances_count or bool(scale_down_reasons):
                desired_instances = max_instances_cap
                if known_current_instances is not None:
                    removed_instances = current_instances_count - desired_instances
                    desired_runners = max(capacity_floor, current_runners - removed_instances * runners_per_vm)
                else:
                    desired_runners = desired_instances * runners_per_vm
            else:
                desired_instances = max(current_instances_count, max_instances_cap)
                if desired_instances == current_instances_count:
                    desired_runners = current_runners
                elif known_current_instances is not None:
                    desired_runners = current_runners + (desired_instances - current_instances_count) * runners_per_vm
                else:
                    desired_runners = desired_instances * runners_per_vm

    # Enforce max scale-up budget caps (hourly, daily, monthly) without turning a blocked scale-up into a scale-down
    unit_hourly = max(0.0001, _finite_float_val(per_instance_hourly_usd, "per_instance_hourly_usd"))
    max_hourly_cap = _finite_float_val(cfg.get("max_scale_up_hourly_usd", 5.00), "max_scale_up_hourly_usd")
    max_daily_cap = _finite_float_val(cfg.get("max_scale_up_daily_usd", 10.00), "max_scale_up_daily_usd")
    max_monthly_cap = _finite_float_val(cfg.get("max_scale_up_monthly_usd", 50.00), "max_scale_up_monthly_usd")

    effective_hourly_cap = min(
        max_hourly_cap,
        max_daily_cap / 24.0,
        max_monthly_cap / (24.0 * 365.0 / 12.0),
    )
    other_burn = 0.0
    if other_role_hourly_usd is not None:
        other_burn = max(0.0, _finite_float_val(other_role_hourly_usd, "other_role_hourly_usd"))
    elif cfg.get("shared_global_budget") or telemetry.get("shared_global_budget"):
        raw_other = telemetry.get("builder_instance_hourly_rates")
        if isinstance(raw_other, (list, tuple)):
            other_burn = sum(max(0.0, _finite_float_val(r, "builder_instance_hourly_rates")) for r in raw_other)
    if other_burn > 0.0:
        effective_hourly_cap = max(0.0, effective_hourly_cap - other_burn)

    raw_fleet_rates = (
        running_instance_hourly_rates
        if running_instance_hourly_rates is not None
        else telemetry.get("running_instance_hourly_rates")
    )
    existing_rates: list[float] | None = None
    if isinstance(raw_fleet_rates, (list, tuple)) and len(raw_fleet_rates) == current_instances_count:
        existing_rates = [
            max(0.0, _finite_float_val(r, "running_instance_hourly_rates")) for r in raw_fleet_rates
        ]

    if existing_rates is not None:
        existing_fleet_hourly = sum(existing_rates)
        remaining_hourly = effective_hourly_cap - existing_fleet_hourly
        if remaining_hourly < -1e-9:
            max_affordable_instances = 0
        else:
            max_affordable_instances = current_instances_count + int(
                math.floor(max(0.0, remaining_hourly + 1e-9) / unit_hourly)
            )
    else:
        max_affordable_instances = max(0, int(math.floor(effective_hourly_cap / unit_hourly)))

    budget_capped = False
    budget_cap_note = ""

    if (scale_up_reasons or desired_instances > current_instances_count) and (
        desired_instances > max_affordable_instances
    ):
        budget_capped = True
        desired_instances = max(current_instances_count, max_affordable_instances)
        if desired_instances == current_instances_count:
            desired_runners = current_runners
        elif known_current_instances is not None:
            desired_runners = current_runners + (desired_instances - current_instances_count) * runners_per_vm
        else:
            desired_runners = desired_instances * runners_per_vm
        budget_cap_note = (
            f"Scale-up capped at {desired_instances} instance(s) ({desired_runners} runners) "
            f"by max scale-up budget (${max_hourly_cap:.2f}/hr, ${max_daily_cap:.2f}/day, "
            f"${max_monthly_cap:.2f}/mo incl. disks)"
        )

    if existing_rates is not None:
        if desired_instances >= current_instances_count:
            projected_hourly = round(
                sum(existing_rates) + (desired_instances - current_instances_count) * unit_hourly, 4
            )
        else:
            projected_hourly = round(sum(sorted(existing_rates)[:desired_instances]), 4)
    else:
        projected_hourly = round(desired_instances * unit_hourly, 4)
    projected_daily = round(projected_hourly * 24.0, 2)
    projected_monthly = round(projected_daily * (365.0 / 12.0), 2)

    direction = (
        "scale_up"
        if desired_runners > current_runners
        else ("scale_down" if desired_runners < current_runners else "hold")
    )

    runner_ram_val = _finite_float_val(cfg.get("runner_ram_gib", 16), "runner_ram_gib")
    total_req_ram = runners_per_vm * runner_ram_val

    return {
        "role": "cicd_runner",
        "direction": direction,
        "current_runners": current_runners,
        "desired_runners": desired_runners,
        "current_instances": current_instances_count,
        "desired_instances": desired_instances,
        "runners_per_instance": runners_per_vm,
        "instance_required_vcpu": runners_per_vm * _finite_int_val(cfg.get("runner_vcpu", 4), "runner_vcpu"),
        "instance_required_ram_gib": int(total_req_ram) if total_req_ram.is_integer() else round(total_req_ram, 2),
        "scale_up_reasons": scale_up_reasons,
        "scale_down_reasons": scale_down_reasons,
        "budget_capped": budget_capped,
        "budget_cap_note": budget_cap_note,
        "projected_hourly_usd": projected_hourly,
        "projected_daily_usd": projected_daily,
        "projected_monthly_usd": projected_monthly,
        "effective_hourly_cap_usd": round(effective_hourly_cap, 4),
    }


def evaluate_builder_scaling_decision(
    policy: dict[str, Any],
    telemetry: dict[str, Any],
    per_instance_hourly_usd: float = 0.25,
    now_epoch: float | None = None,
    running_instance_hourly_rates: list[float] | None = None,
    other_role_hourly_usd: float | None = None,
) -> dict[str, Any]:
    """Evaluate dedicated Rust builder telemetry against builder scaling triggers and budgets."""
    _assert_finite_json_tree(policy, "policy")
    _assert_finite_json_tree(telemetry, "telemetry")
    raw_policy = policy or {}
    cfg = {**default_scaling_config()["rust_builder"], **raw_policy}
    now_ts = _finite_float_val(now_epoch, "now_epoch") if now_epoch is not None else time.time()
    lookback_seconds = max(
        60.0, _finite_float_val(cfg.get("oom_lookback_minutes", 60), "oom_lookback_minutes") * 60.0
    )

    current_instances = max(
        0,
        _finite_int_val(
            telemetry.get("current_builder_instances", telemetry.get("current_builders", telemetry.get("current_instances", 2))),
            "current_builder_instances",
        ),
    )
    min_instances = max(1, _finite_int_val(cfg.get("min_instances", 1), "min_instances"))
    max_instances = max(min_instances, _finite_int_val(cfg.get("max_instances", 4), "max_instances"))

    builds_ran_past_hour = max(
        0, _finite_int_val(telemetry.get("builds_ran_past_hour", 5), "builds_ran_past_hour")
    )
    raw_builds = (
        telemetry.get("active_builds")
        if telemetry.get("active_builds") is not None
        else telemetry.get("active_rust_builds", 0)
    )
    active_builds = max(0, _finite_int_val(raw_builds, "active_builds"))
    raw_q_builds = (
        telemetry.get("queued_builds")
        if telemetry.get("queued_builds") is not None
        else telemetry.get("queued_rust_builds", 0)
    )
    queued_builds = max(0, _finite_int_val(raw_q_builds, "queued_builds"))
    cpu_percentages = [
        _finite_float_val(x, "builder_cpu_percentages")
        for x in (telemetry.get("builder_cpu_percentages") or [])
    ]
    if "builder_journal_oom_events" in telemetry:
        journal_oom_events = list(telemetry.get("builder_journal_oom_events") or [])
    else:
        journal_oom_events = [
            evt
            for evt in (telemetry.get("journal_oom_events") or [])
            if str(evt.get("role", "")) == "rust_builder"
            or str(evt.get("instance_name", "")).lower().startswith("builder-")
        ]

    if "container_vcpu" in raw_policy:
        req_vcpu = _finite_int_val(raw_policy["container_vcpu"], "container_vcpu")
    elif "builder_vcpu" in raw_policy:
        req_vcpu = _finite_int_val(raw_policy["builder_vcpu"], "builder_vcpu")
    else:
        req_vcpu = _finite_int_val(cfg.get("container_vcpu", cfg.get("builder_vcpu", 16)), "container_vcpu")

    if "container_ram_gib" in raw_policy:
        req_ram = _finite_float_val(raw_policy["container_ram_gib"], "container_ram_gib")
    elif "builder_ram_gib" in raw_policy:
        req_ram = _finite_float_val(raw_policy["builder_ram_gib"], "builder_ram_gib")
    else:
        req_ram = _finite_float_val(
            cfg.get("container_ram_gib", cfg.get("builder_ram_gib", 28)), "container_ram_gib"
        )

    scale_up_reasons: list[dict[str, Any]] = []
    scale_down_reasons: list[dict[str, Any]] = []
    desired_instances = current_instances

    if cfg.get("enable_journal_oom_trigger", True) and journal_oom_events:
        active_ooms: list[dict[str, Any]] = []
        for evt in journal_oom_events:
            if not isinstance(evt, dict):
                continue
            if not _is_recent_unhandled_oom(evt, now_ts, lookback_seconds):
                continue
            if (
                evt.get("baseline_instances") is not None
                and (
                    current_instances > _finite_int_val(evt["baseline_instances"], "baseline_instances")
                    or (current_instances >= max_instances and _finite_int_val(evt["baseline_instances"], "baseline_instances") >= max_instances)
                )
            ):
                evt["handled"] = True
                continue
            evt.setdefault("baseline_instances", current_instances)
            active_ooms.append(evt)
        for evt in active_ooms:
            scale_up_reasons.append(
                {
                    "trigger": "vm_journal_oom",
                    "summary": (
                        f"Builder VM journalctl on {evt.get('instance_name', 'builder')} recorded OOM kill: "
                        f"{evt.get('process', 'rustc')} (PID {evt.get('pid', 0)})"
                    ),
                }
            )
        if active_ooms:
            desired_instances = max(desired_instances, current_instances + 1)

    if cfg.get("enable_queue_trigger", True) and queued_builds >= _finite_int_val(cfg.get("queued_builds_threshold", 2), "queued_builds_threshold"):
        scale_up_reasons.append(
            {
                "trigger": "queued_builds",
                "summary": (
                    f"{queued_builds} queued Rust builds waiting for a builder "
                    f"(threshold >= {cfg.get('queued_builds_threshold', 2)})"
                ),
            }
        )
        desired_instances = max(desired_instances, current_instances + 1)

    if cfg.get("enable_cpu_utilization_trigger", True):
        cpu_triggered, cpu_detail = evaluate_cpu_scope(
            cpu_percentages,
            _finite_float_val(cfg.get("cpu_threshold_percent", 65.0), "cpu_threshold_percent"),
            str(cfg.get("cpu_scope", "majority")),
        )
        if cpu_triggered:
            scale_up_reasons.append(
                {
                    "trigger": f"cpu_{cfg.get('cpu_scope', 'majority')}",
                    "summary": f"Builder CPU threshold exceeded: {cpu_detail}",
                }
            )
            desired_instances = max(desired_instances, current_instances + 1)

    capacity_floor = max(
        min(max_instances, max(min_instances, active_builds)),
        min(current_instances, active_builds),
    )
    if not scale_up_reasons:
        if (
            cfg.get("enable_low_builds_scale_down", True)
            and builds_ran_past_hour <= _finite_int_val(cfg.get("scale_down_max_builds_past_hour", 2), "scale_down_max_builds_past_hour")
            and queued_builds == 0
        ):
            low_target = max(
                capacity_floor,
                min(
                    max_instances,
                    _finite_int_val(cfg.get("scale_down_target_instances", min_instances), "scale_down_target_instances"),
                ),
            )
            if low_target < current_instances:
                scale_down_reasons.append(
                    {
                        "trigger": "low_builds",
                        "summary": (
                            f"Only {builds_ran_past_hour} Rust builds ran in the last hour "
                            f"(<= {cfg.get('scale_down_max_builds_past_hour', 2)}) -> "
                            f"scaling down to {low_target} builder instance(s)"
                        ),
                    }
                )
                desired_instances = low_target
            else:
                desired_instances = max(capacity_floor, min(max_instances, current_instances))
        else:
            desired_instances = max(capacity_floor, min(max_instances, current_instances))
    else:
        desired_instances = max(capacity_floor, current_instances, min(max_instances, desired_instances))

    if desired_instances < current_instances and not scale_down_reasons:
        scale_down_reasons.append(
            {
                "trigger": "max_instances_cap",
                "summary": (
                    f"Current fleet ({current_instances} builder VMs) "
                    f"exceeds max_instances={max_instances} -> scaling down to {desired_instances} instance(s)"
                ),
            }
        )

    unit_hourly = max(0.0001, _finite_float_val(per_instance_hourly_usd, "per_instance_hourly_usd"))
    max_hourly_cap = _finite_float_val(cfg.get("max_scale_up_hourly_usd", 3.50), "max_scale_up_hourly_usd")
    max_daily_cap = _finite_float_val(cfg.get("max_scale_up_daily_usd", 12.00), "max_scale_up_daily_usd")
    max_monthly_cap = _finite_float_val(cfg.get("max_scale_up_monthly_usd", 80.00), "max_scale_up_monthly_usd")

    effective_hourly_cap = min(
        max_hourly_cap,
        max_daily_cap / 24.0,
        max_monthly_cap / (24.0 * 365.0 / 12.0),
    )
    other_burn = 0.0
    if other_role_hourly_usd is not None:
        other_burn = max(0.0, _finite_float_val(other_role_hourly_usd, "other_role_hourly_usd"))
    elif cfg.get("shared_global_budget") or telemetry.get("shared_global_budget"):
        raw_other = telemetry.get("running_instance_hourly_rates")
        if isinstance(raw_other, (list, tuple)):
            other_burn = sum(max(0.0, _finite_float_val(r, "running_instance_hourly_rates")) for r in raw_other)
    if other_burn > 0.0:
        effective_hourly_cap = max(0.0, effective_hourly_cap - other_burn)

    raw_builder_rates = (
        running_instance_hourly_rates
        if running_instance_hourly_rates is not None
        else telemetry.get("builder_instance_hourly_rates")
    )
    existing_builder_rates: list[float] | None = None
    if isinstance(raw_builder_rates, (list, tuple)) and len(raw_builder_rates) == current_instances:
        existing_builder_rates = [
            max(0.0, _finite_float_val(r, "builder_instance_hourly_rates")) for r in raw_builder_rates
        ]

    if existing_builder_rates is not None:
        existing_fleet_hourly = sum(existing_builder_rates)
        remaining_hourly = effective_hourly_cap - existing_fleet_hourly
        if remaining_hourly < -1e-9:
            max_affordable_instances = 0
        else:
            max_affordable_instances = current_instances + int(
                math.floor(max(0.0, remaining_hourly + 1e-9) / unit_hourly)
            )
    else:
        max_affordable_instances = max(0, int(math.floor(effective_hourly_cap / unit_hourly)))

    budget_capped = False
    budget_cap_note = ""

    if (scale_up_reasons or desired_instances > current_instances) and (
        desired_instances > max_affordable_instances
    ):
        budget_capped = True
        desired_instances = max(current_instances, max_affordable_instances)
        budget_cap_note = (
            f"Builder scale-up capped at {desired_instances} instance(s) "
            f"by max scale-up budget (${max_hourly_cap:.2f}/hr, ${max_daily_cap:.2f}/day, "
            f"${max_monthly_cap:.2f}/mo incl. disks)"
        )

    if existing_builder_rates is not None:
        if desired_instances >= current_instances:
            projected_hourly = round(
                sum(existing_builder_rates) + (desired_instances - current_instances) * unit_hourly, 4
            )
        else:
            projected_hourly = round(sum(sorted(existing_builder_rates)[:desired_instances]), 4)
    else:
        projected_hourly = round(desired_instances * unit_hourly, 4)
    projected_daily = round(projected_hourly * 24.0, 2)
    projected_monthly = round(projected_daily * (365.0 / 12.0), 2)

    direction = (
        "scale_up"
        if desired_instances > current_instances
        else ("scale_down" if desired_instances < current_instances else "hold")
    )

    return {
        "role": "rust_builder",
        "direction": direction,
        "current_runners": current_instances,
        "desired_runners": desired_instances,
        "current_instances": current_instances,
        "desired_instances": desired_instances,
        "runners_per_instance": 1,
        "instance_required_vcpu": req_vcpu,
        "instance_required_ram_gib": req_ram,
        "scale_up_reasons": scale_up_reasons,
        "scale_down_reasons": scale_down_reasons,
        "budget_capped": budget_capped,
        "budget_cap_note": budget_cap_note,
        "projected_hourly_usd": projected_hourly,
        "projected_daily_usd": projected_daily,
        "projected_monthly_usd": projected_monthly,
        "effective_hourly_cap_usd": round(effective_hourly_cap, 4),
    }
