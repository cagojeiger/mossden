# Mossden CI control

The Python standard-library bridge supports one private `main` push or manual
attempt and one public `Rust` job. Home runs reviewed, pinned control code. The
GitHub-hosted public worker fetches the exact private source only after admission.

| Command | Additional inputs |
| --- | --- |
| `preflight` | Repository and pinned workflow metadata |
| `dispatch`, `wait` | `CORE_RUN_ID`, `CORE_ATTEMPT` |
| `gate` | V2 dispatch inputs and the public run identity |
| `reconcile` | Exact completed source ID and attempt for `workflow_run`; otherwise a bounded sweep |

The control jobs use `CORE_TOKEN` and a public-only `PUBLIC_TOKEN`. The gate
uses the private read App token `CORE_READ_TOKEN` and its public job token.
`PUBLIC_CI_REVISION` is the reviewed full public commit SHA. The public run title
binds its Core run and attempt to its V2 request digest and reservation nonce.

## State and recovery

The bridge stores the request, dispatch reservation, immutable public run ID,
decision and worker observations in one file under private `ci-state/v2/claims`.
The original one-hour deadline is based on the authenticated source run creation
time. Creating a request grants no dispatch authority. Only the exact
acknowledgement of a blob-SHA compare-and-set reservation permits one POST.
A lost acknowledgement, existing reservation or uncertain POST permits
observation only. Historical V1 claims and terminal records remain in place.
New requests cannot reuse a V1 attempt with a recorded claim.

The gate verifies the reservation, provenance, bound public attempt 1, active
source and deadline before outputting the source SHA. The waiter verifies that
exact run and its successful `Rust` job before committing success. Recovery
uses the same record to close a request. A source lookup failure grants no
cancellation authority. An active bound worker is preserved when duplicates
appear. Cancellation needs its own acknowledged reservation; a cancel response
does not count as termination.

A completed source event checks that attempt. Scheduled and manual sweeps check
at most 20 candidates per batch within the API budget and retain a cursor for
deferred work. A settlement marker is tied to the exact claim blob. A verified
closed, unbound reservation can be settled after the original deadline plus
24 hours if no observed worker remains active. Global sweeps still discover late
active workers for settled requests. Ambiguous reads fail safely and are reported
for follow-up. Preserve claim history and keep writes limited to trusted control
jobs. GitHub run observations and blob updates are separate operations.

## Output boundary

Run all private checkout and application commands through
`bash .github/scripts/private_run.sh COMMAND ...`. The wrapper suppresses
stdout, stderr, GitHub annotations and command files and preserves the exit code.
Revoke the reader token and remove checkout credentials before executing source.
Keep private artifacts, Actions caches and logs out of the public workflow.
The wrapper isolates output from reviewed source, while the worker itself has
network access.

Run the local provider-fault tests with:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_ci_bridge.py' -v
actionlint
shellcheck .github/scripts/*.sh
```
