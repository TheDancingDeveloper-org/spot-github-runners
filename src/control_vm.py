#!/usr/bin/env python3
"""Inspect and manage the Spot control VM that hosts the web dashboard."""

from lib.control_vm import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import logging
        from lib.log_format import configure_logging, error_detail

        configure_logging()
        logging.getLogger("spot_runner_autoscaler").error("%s: %s", type(exc).__name__, error_detail(exc))
        raise SystemExit(2)
