#!/usr/bin/env python3
"""Provision a Spot VM and register persistent GitHub Actions runners."""

from lib.runner_manager import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyError, ZeroDivisionError) as exc:
        import sys

        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
