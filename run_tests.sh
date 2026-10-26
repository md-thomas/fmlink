#!/bin/bash
# Runs FMlink's regression test suite. Must be run from the project root
# (the tests rely on cwd for resolving fmlink_link.py imports - see
# tests/test_phy_loopback.py's module docstring for why).
#
# Usage: ./run_tests.sh                 # everything
#        ./run_tests.sh tests.test_fmlink_link   # just one module, fast

set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ $# -gt 0 ]; then
    python3 -m unittest -v "$@"
else
    python3 -m unittest discover -s tests -v
fi
