#!/usr/bin/env python3
"""Keep the Google Cloud Spot runner pool above its configured floor."""

from lib.spot_runner_autoscaler import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        import sys

        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
