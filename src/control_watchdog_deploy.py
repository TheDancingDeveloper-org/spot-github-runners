#!/usr/bin/env python3
"""Create or update the serverless control VM watchdog (Cloud Run job + Cloud Scheduler); --dry-run prints only."""

from lib.control_watchdog_deploy import main

if __name__ == "__main__":
    raise SystemExit(main())
