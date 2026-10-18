#!/usr/bin/env bash
# run_tests.sh: the supported way to run the memguard suite. `unittest discover -s tests`
# does not necessarily run tests/__init__.py, so the sandbox variable is exported here
# explicitly rather than relied upon implicitly.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MEMGUARD_STATE_DIR="${MEMGUARD_STATE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/memguard-tests-state.XXXXXX")}"
trap 'rm -rf "$MEMGUARD_STATE_DIR"' EXIT
cd "$ROOT_DIR"
python3 -m unittest discover -s tests -p "${1:-test_*.py}" -v
