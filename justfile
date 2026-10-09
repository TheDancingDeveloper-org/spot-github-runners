# Python validation for the infrastructure repository. The OxiDex control
# checkout owns remote transport, source verification, admission, and evidence.
default:
    @just --list

# Submit this checkout to the reviewed OxiDex remote route.
test:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -z "${OXIDEX_CONTROL_REPO:-}" || ! -f "$OXIDEX_CONTROL_REPO/justfile" ]]; then
      echo 'Set OXIDEX_CONTROL_REPO to a reviewed OxiDex control checkout with test-infra-remote.' >&2
      exit 2
    fi
    if ! just --justfile "$OXIDEX_CONTROL_REPO/justfile" --show test-infra-remote >/dev/null 2>&1; then
      echo 'OxiDex control checkout lacks the reviewed test-infra-remote recipe.' >&2
      exit 2
    fi
    just --justfile "$OXIDEX_CONTROL_REPO/justfile" test-infra-remote "$(pwd -P)"

# Run only inside the admitted dedicated-builder image; no arguments accepted.
infra-python-tests:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$(uname -s)" != Linux || ! -f /run/oxidex-build-container || "$(id -u)" == 0 ]]; then
      echo 'infra-python-tests requires the admitted non-root Linux builder container.' >&2
      exit 2
    fi
    PYTHONPATH=src python3 -B tools/infra_python_test_proof.py

# Complete the signed CI-only source suite inside the admitted builder.
infra-python-complete-tests:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$(uname -s)" != Linux || ! -f /run/oxidex-build-container || "$(id -u)" == 0 ]]; then
      echo 'infra-python-complete-tests requires the admitted non-root Linux builder container.' >&2
      exit 2
    fi
    PYTHONPATH=src python3 -B tools/infra_python_complete_proof.py
