#!/bin/sh
set -eu

if [ -n "${LOADTEST_RESULTS_DIR:-}" ] && [ -n "${LOADTEST_RUN_ID:-}" ]; then
  mkdir -p "${LOADTEST_RESULTS_DIR}/${LOADTEST_RUN_ID}"
fi

exec locust -f /workspace/loadtests/locustfile.py "$@"
