#!/usr/bin/env bash
# Suppress all application output and GitHub command files in the public job.
set -euo pipefail
set +x

private_tmp="$(mktemp -d)"
trap 'rm -rf "$private_tmp"' EXIT
export GITHUB_OUTPUT="$private_tmp/output"
export GITHUB_ENV="$private_tmp/env"
export GITHUB_PATH="$private_tmp/path"
export GITHUB_STEP_SUMMARY="$private_tmp/summary"
export GITHUB_STATE="$private_tmp/state"
export CARGO_TERM_COLOR=never

private_commands="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
printf '::stop-commands::%s\n' "$private_commands"
set +e
"$@" >/dev/null 2>&1
private_status=$?
set -e
printf '::%s::\n' "$private_commands"
exit "$private_status"
