"""Explicit synthetic current-scan receipts for offline controller fixtures."""
def complete_job_tracking(repository,observed_at):
    return dict(last_poll_by_repository={repository:observed_at},capacity_jobs={},
        active_coverage_by_repository={repository:dict(schema=1,observed_at=observed_at,complete=True,
            enumerated_statuses=['in_progress','queued','waiting','pending','requested'],unavailable_run_ids=[])})
