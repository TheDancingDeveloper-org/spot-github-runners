#!/usr/bin/env python3
"""HTTP entrypoint of the control VM watchdog (Cloud Run service): POST /tick runs one control VM step."""

from lib.control_watchdog_server import main

if __name__ == "__main__":
    raise SystemExit(main())
