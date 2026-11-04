#!/usr/bin/env bash
set -euo pipefail
set +x

code="$(curl --silent --show-error --max-time 15 \
    --request DELETE --output /dev/null --write-out '%{http_code}' \
    --header "Authorization: Bearer ${CORE_READ_TOKEN:?}" \
    --header 'Accept: application/vnd.github+json' \
    --header 'X-GitHub-Api-Version: 2022-11-28' \
    https://api.github.com/installation/token)"
[[ "$code" == 204 || "$code" == 401 ]]
