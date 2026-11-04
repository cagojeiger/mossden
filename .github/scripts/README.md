# Mossden CI control

The application remains Rust. This Python standard-library adapter executes only
reviewed, pinned public control code on Home; it never checks out application
source there. It supports one private `main` push/manual source SHA, one public
`Rust` job, and a deadline anchored at the source run's original creation time.

Run with `python3 .github/scripts/ci_bridge.py COMMAND` from the public control
checkout. Configuration defaults to `.github/ci-bridge.json`.

| Command | Environment |
| --- | --- |
| `preflight` | `CORE_TOKEN`, `PUBLIC_TOKEN`, `PUBLIC_CI_REVISION` |
| `dispatch`, `wait` | Above plus `CORE_RUN_ID`, `CORE_ATTEMPT` |
| `reconcile` | Same as preflight |
| `gate` | `CORE_READ_TOKEN`, public job's `PUBLIC_TOKEN`, `PUBLIC_CI_REVISION`, `INPUT_SOURCE_RUN_ID`, `INPUT_SOURCE_ATTEMPT`, `INPUT_REQUEST_DIGEST`, `INPUT_CLAIM_NONCE`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_SHA`, `GITHUB_OUTPUT` |

`CORE_TOKEN` is the private job's GitHub token with Contents write and Actions
read. `PUBLIC_TOKEN` is the public-only dispatch App token for control commands;
gate uses its public job token with Actions read. `CORE_READ_TOKEN` is the
private-only reader App token with Contents/Actions read. No token is written
to claim state or printed. `PUBLIC_CI_REVISION` is a reviewed full public commit
SHA, supplied by the pinned private workflow; gate additionally requires its
actual run SHA to match the revision stored in the claim. Public run-name must
be `bridge:${{ inputs.request_digest }}:${{ inputs.claim_nonce }}`.

`dispatch` writes a create-only claim on private `ci-state` before its one POST.
Only an acknowledged new claim gives that invocation permission to send. Any
existing claim, uncertain create, lost response, or restart permits observation
only. An unbound claim with no observed public run may remain until expiration;
retrying it never sends another POST. Public main moving from the pin fails
closed. Confirm the public workflow's main commit and source workflow ID in
preflight before enabling execution. No automatic mutation retry is performed.
The at-most-one POST guarantee assumes trusted state writers preserve claim
history. Administrators deleting/resetting `ci-state` claims can remove the
durable evidence of consumed permission and invalidate that guarantee.

Binding is immutable and uses blob-SHA CAS. A lost update response is accepted
only if rereading sees the exact intended binding. Gate validates the App actor,
repository/workflow, attempt 1, title, SHA, source attempt/active status, original
deadline and this run's binding before allowing source fetch. Gate outputs only
`tested_sha`. `wait` independently checks provider run and exact `Rust` job
evidence, rereads binding and source status, and rechecks the original deadline.
Only its exit 0 means accepted success. Separate API reads are not an atomic
transaction; the Quint model's atomic acceptance remains an abstraction.

`reconcile` observes all claims, adopts a unique eligible run, cancels expired,
inactive or duplicate executions, and writes per-run `settled/` evidence only
after observing `status=completed`. A cancel response is not termination.
Receipts do not suppress future discovery of late delivery or duplicates.
Cleanup searches through the current observation time and can cancel delivery
created after the acceptance deadline; gate/wait still reject that delivery.
Unbound ambiguous runs are cancelled, never arbitrarily selected. The initial
full scan stops at 1,000 claims/API entries and needs an explicit retention
design before scaling. It does not assert unconditional eventual termination.

All private source fetch, extraction and application commands must execute
inside `bash .github/scripts/private_run.sh COMMAND ...`. It suppresses stdout,
stderr, GitHub annotations and command files, preserving only the exit code.
Do not enable artifacts, Actions caches, upload logs, or expose application
output in an outer step. This is output isolation for reviewed main code, not
a network sandbox against malicious application code. Revoke the reader token
and remove source checkout credentials before executing application commands.

Local fault-injection checks:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_ci_bridge.py' -v
bash -n .github/scripts/private_run.sh
```

These fakes verify acknowledgement loss, no second POST, immutable binding,
wrong/late evidence rejection and cancellation-versus-termination. They do not
replace an actual GitHub Actions execution or prove implementation refinement.
