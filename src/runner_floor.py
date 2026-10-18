#!/usr/bin/env python3
"""Keep at least one minimal Spot runner host per controlled GitHub target (the watchdog runs `tick --apply`)."""

from lib.runner_floor import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import logging
        from lib.log_format import configure_logging, error_detail

        configure_logging()
        logging.getLogger("spot_runner_autoscaler").error("%s: %s", type(exc).__name__, error_detail(exc))
        raise SystemExit(2)
