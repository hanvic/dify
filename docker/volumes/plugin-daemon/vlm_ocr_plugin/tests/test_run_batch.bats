#!/usr/bin/env bats
# shellcheck disable=SC2164

# Lightweight BATS tests for pipeline/batch helper scripts.
# These tests only run static analysis (shellcheck) and do not execute real
# pipelines or connect to Ollama/Dify.

SCRIPTS_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../scripts" && pwd)"

@test "run_pipeline.sh passes shellcheck" {
    if ! command -v shellcheck >/dev/null 2>&1; then
        skip "shellcheck is not installed"
    fi
    run shellcheck "${SCRIPTS_DIR}/run_pipeline.sh"
    [ "$status" -eq 0 ]
}

@test "run_batch.sh placeholder" {
    if [[ ! -f "${SCRIPTS_DIR}/run_batch.sh" ]]; then
        skip "run_batch.sh does not exist yet (placeholder for batch wrapper)"
    fi

    if command -v shellcheck >/dev/null 2>&1; then
        run shellcheck "${SCRIPTS_DIR}/run_batch.sh"
        [ "$status" -eq 0 ]
    fi
}
