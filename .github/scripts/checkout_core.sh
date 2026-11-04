#!/usr/bin/env bash
set -euo pipefail
set +x

[[ "${TESTED_SHA:?}" =~ ^[a-f0-9]{40}$ ]]
: "${CORE_READ_TOKEN:?}"
ci_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
checkout_tmp="$(mktemp -d)"
checkout_status=0
trap '
    checkout_status=$?
    trap - EXIT
    bash "$ci_dir/revoke_core_token.sh" || checkout_status=1
    rm -rf "$checkout_tmp"
    exit "$checkout_status"
' EXIT

cat >"$checkout_tmp/askpass" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
    *Username*) printf '%s\n' x-access-token ;;
    *) printf '%s\n' "$CORE_READ_TOKEN" ;;
esac
ASKPASS
chmod 700 "$checkout_tmp/askpass"
export GIT_ASKPASS="$checkout_tmp/askpass"
export GIT_TERMINAL_PROMPT=0

git init --quiet source
git -C source remote add origin https://github.com/cagojeiger/mossden-core.git
git -C source -c credential.helper= fetch --quiet --depth=1 --no-tags origin "$TESTED_SHA"
git -C source checkout --quiet --detach FETCH_HEAD
[[ "$(git -C source rev-parse HEAD)" == "$TESTED_SHA" ]]
git -C source remote remove origin
