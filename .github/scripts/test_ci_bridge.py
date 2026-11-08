import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote

import ci_bridge as bridge


CONFIG = json.loads((Path(__file__).parents[1] / "ci-bridge.json").read_text())
REVISION = "2" * 40
SOURCE_SHA = "1" * 40


class Clock:
    def __init__(self):
        self.value = 1700000050

    def __call__(self):
        return self.value

    def pause(self, seconds):
        self.value += seconds


class Provider:
    """A tiny provider fake: writes can take effect before losing their response."""
    def __init__(self, clock):
        self.clock, self.files, self.runs = clock, {}, {}
        self.mutations = []
        self.lose_write = None
        self.lost_post = self.lost_cancel = False
        self.source = {
            "id": 11, "run_attempt": 1, "workflow_id": 101, "path": ".github/workflows/ci.yml",
            "repository": {"id": CONFIG["source_repository_id"]},
            "head_repository": {"id": CONFIG["source_repository_id"]},
            "head_branch": "main", "event": "push", "head_sha": SOURCE_SHA,
            "status": "in_progress", "created_at": bridge.utc(1700000000),
        }

    def run(self, run_id, digest, nonce):
        return {
            "id": run_id, "run_attempt": 1, "workflow_id": 201, "path": ".github/workflows/ci.yml",
            "repository": {"id": CONFIG["public_repository_id"]},
            "head_repository": {"id": CONFIG["public_repository_id"]},
            "actor": {"id": CONFIG["dispatch_actor_id"]},
            "triggering_actor": {"id": CONFIG["dispatch_actor_id"]},
            "head_branch": "main", "event": "workflow_dispatch", "head_sha": REVISION,
            "display_title": f"bridge:v2:11:1:{digest}:{nonce}", "status": "in_progress", "conclusion": None,
            "created_at": bridge.utc(int(self.clock())),
        }

    def request(self, public, path, data=None, method="GET", missing=False):
        bare = path.split("?")[0]
        repository = "repos/" + CONFIG["public_repository" if public else "source_repository"]
        relative = bare.removeprefix(repository)
        if method != "GET":
            self.mutations.append((public, method, relative))
        if relative.startswith("/contents/"):
            filename = unquote(relative.removeprefix("/contents/"))
            if method == "GET":
                if filename not in self.files:
                    if missing:
                        return None
                    raise bridge.ApiFailure(404)
                value, blob_sha = self.files[filename]
                raw = bridge.compact(value)
                return {"sha": blob_sha, "size": len(raw), "encoding": "base64",
                        "content": base64.b64encode(raw).decode()}
            existing = self.files.get(filename)
            if (existing and data.get("sha") != existing[1]) or (not existing and "sha" in data):
                raise bridge.ApiFailure(409)
            value = json.loads(base64.b64decode(data["content"]))
            blob_sha = bridge.Bridge.blob_sha(bridge.compact(value))
            self.files[filename] = (value, blob_sha)
            if self.lose_write and self.lose_write(value):
                self.lose_write = None
                raise bridge.ApiFailure()
            return {"content": {"path": filename, "sha": blob_sha}}
        if relative == "":
            return {"id": CONFIG["public_repository_id" if public else "source_repository_id"],
                    "full_name": repository.removeprefix("repos/"), "private": not public}
        if relative == "/branches/main":
            return {"commit": {"sha": REVISION}}
        if relative == "/branches/ci-state":
            return {"commit": {"commit": {"tree": {"sha": "3" * 40}}}}
        if relative.startswith("/git/trees/"):
            return {"truncated": False, "tree": [{"path": filename, "type": "blob", "sha": sha} for filename, (_, sha) in self.files.items()]}
        if relative == "/actions/workflows/ci.yml":
            return {"id": 201 if public else 101, "path": ".github/workflows/ci.yml", "state": "active"}
        if relative == "/actions/workflows/ci.yml/dispatches":
            inputs = data["inputs"]
            assert inputs["request_version"] == "2"
            self.runs[21] = self.run(21, inputs["request_digest"], inputs["claim_nonce"])
            if self.lost_post:
                self.lost_post = False
                raise bridge.ApiFailure()
            return None
        if relative == "/actions/workflows/201/runs":
            filters = parse_qs(path.partition("?")[2])
            runs = [run for run in copy.deepcopy(self.runs).values()
                    if "status" not in filters or run["status"] in filters["status"]]
            return {"total_count": len(runs), "workflow_runs": runs}
        if not public and relative in ("/actions/runs/11", "/actions/runs/11/attempts/1"):
            return copy.deepcopy(self.source)
        if public and relative.startswith("/actions/runs/"):
            pieces = relative.split("/")
            run_id = int(pieces[3])
            if relative.endswith("/cancel"):
                # HTTP acceptance does not change actual execution state.
                if self.lost_cancel:
                    raise bridge.ApiFailure()
                return None
            if relative.endswith("/jobs"):
                return {"total_count": 1, "jobs": [{
                    "id": 31, "run_id": run_id, "run_attempt": 1, "head_sha": REVISION,
                    "name": "Rust", "status": "completed", "conclusion": "success",
                }]}
            return copy.deepcopy(self.runs[run_id])
        raise AssertionError((public, path, method))


class Api:
    def __init__(self, provider, public):
        self.provider, self.public = provider, public
        self.deadline = provider.clock() + 300

    def request(self, path, data=None, method="GET", missing=False):
        return self.provider.request(self.public, path, data, method, missing)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.provider = Provider(self.clock)
        self.control = bridge.Bridge(CONFIG, Api(self.provider, False), Api(self.provider, True), REVISION,
                                     self.clock, self.clock.pause)

    def dispatch(self):
        self.control.prepare(self.control.source_request(11, 1))
        return self.control.dispatch(11, 1)

    def posts(self):
        return [call for call in self.provider.mutations if call[2].endswith("/dispatches")]

    def cancellations(self):
        return [int(path.split("/")[-2]) for _, method, path in self.provider.mutations
                if method == "POST" and path.endswith("/cancel")]

    def markers(self):
        return [path for path in self.provider.files if path.startswith("v2/settled/")]

    def complete(self):
        self.provider.runs[21].update(status="completed", conclusion="success")

    def test_v2_request_digest_cannot_match_v1_wire(self):
        request = self.control.source_request(11, 1)
        self.assertEqual(request["version"], 2)
        self.assertNotEqual(bridge.digest(request), bridge.digest({**request, "version": 1}))

    def test_normal_gate_and_result_acceptance(self):
        claim = self.dispatch()
        self.assertEqual(self.control.gate(11, 1, claim["request_digest"], claim["nonce"], 21, 1, REVISION), SOURCE_SHA)
        self.complete()
        result = self.control.wait(11, 1)
        self.assertEqual((result["public_run_id"], result["decision"]), (21, "success"))
        self.assertEqual(self.control.claim(result["request"])[0], result)
        self.assertEqual(len(self.posts()), 1)

    def test_lost_dispatch_response_never_reposts_even_after_restart(self):
        self.provider.lost_post = True
        self.assertEqual(self.dispatch()["public_run_id"], 21)
        self.assertEqual(self.dispatch()["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_uncertain_reservation_never_grants_permission(self):
        self.provider.lose_write = lambda value: value['nonce'] is not None
        with self.assertRaises(bridge.Stopped):
            self.dispatch()
        self.assertEqual(len(self.provider.files), 1)  # Write really took effect.
        with self.assertRaises(bridge.Stopped):
            self.dispatch()
        self.assertEqual(self.posts(), [])

    def test_lost_binding_response_requires_exact_intended_readback(self):
        self.provider.lose_write = lambda value: value["public_run_id"] is not None
        claim = self.dispatch()
        self.assertEqual(claim["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_binding_is_immutable(self):
        claim = self.dispatch()
        self.provider.runs[22] = self.provider.run(22, claim["request_digest"], claim["nonce"])
        with self.assertRaisesRegex(bridge.Stopped, "immutable"):
            self.control.bind(claim["request"], self.control.claim(claim["request"]), 22)

    def test_wrong_public_provenance_is_rejected(self):
        claim = self.dispatch()
        self.complete()
        for key, value in (("head_sha", "f" * 40), ("run_attempt", 2), ("actor", {"id": 999})):
            with self.subTest(key=key):
                original = self.provider.runs[21][key]
                self.provider.runs[21][key] = value
                with self.assertRaises(bridge.Stopped):
                    self.control.verify_results(claim)
                self.provider.runs[21][key] = original

    def test_success_after_original_deadline_is_rejected(self):
        claim = self.dispatch()
        self.complete()
        self.clock.value = claim["request"]["deadline"]
        with self.assertRaisesRegex(bridge.Stopped, "deadline"):
            self.control.verify_results(claim)

    def test_wrong_job_run_identity_is_rejected(self):
        claim = self.dispatch()
        self.complete()
        original = self.control.public.request

        def altered(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if "jobs?" in path:
                result["jobs"][0]["run_id"] = 999
            return result

        self.control.public.request = altered
        with self.assertRaisesRegex(bridge.Stopped, "evidence"):
            self.control.verify_results(claim)

    def test_source_attempt_change_revokes_gate(self):
        claim = self.dispatch()
        self.provider.source["run_attempt"] = 2
        with self.assertRaises(bridge.Stopped):
            self.control.gate(11, 1, claim["request_digest"], claim["nonce"], 21, 1, REVISION)

    def test_cancel_acknowledgement_is_not_terminal_receipt(self):
        claim = self.dispatch()
        self.provider.source["status"] = "completed"
        self.control.reconcile_claim(self.control.path(claim["request"]))
        self.assertTrue(any(call[2].endswith("/cancel") for call in self.provider.mutations))
        self.assertFalse(any(path.startswith("v2/settled/") for path in self.provider.files))
        self.complete()
        self.control.reconcile_claim(self.control.path(claim["request"]))
        self.assertTrue(any(path.startswith("v2/settled/") for path in self.provider.files))

    def test_delivery_created_after_deadline_is_cancelled_then_observed(self):
        request = self.control.source_request(11, 1)
        self.control.prepare(request)
        (claim, _), _ = self.control.acquire(request)
        self.clock.value = request["deadline"] + 10
        self.provider.runs[21] = self.provider.run(21, claim["request_digest"], claim["nonce"])
        with self.assertRaisesRegex(bridge.Stopped, "window"):
            self.control.public_run(claim, 21)
        self.control.reconcile_claim(self.control.path(request))
        self.assertTrue(any(call[2].endswith("/cancel") for call in self.provider.mutations))
        self.assertFalse(any(path.startswith("v2/settled/") for path in self.provider.files))
        self.complete()
        self.control.reconcile_claim(self.control.path(request))
        self.assertEqual(self.control.claim(request)[0]["workers"], {"21": "completed"})
        self.assertFalse(self.markers())
        self.assertEqual(self.posts(), [])

    def test_prepared_request_can_resume_without_reusing_a_reservation(self):
        request = self.control.source_request(11, 1)
        claim, _ = self.control.prepare(request)
        self.assertIsNone(claim["nonce"])
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.dispatch()["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_historical_v1_attempt_cannot_dispatch(self):
        request = self.control.source_request(11, 1)
        legacy_path = self.control.path(request).removeprefix("v2/")
        self.control.put_file(legacy_path, {"version": 1})
        with self.assertRaisesRegex(bridge.Stopped, "Historical V1"):
            self.dispatch()
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.control.reconcile(11, 1), {"state": "no_v2_claim"})

    def test_unknown_core_status_cannot_cancel(self):
        claim = self.dispatch()
        request = self.control.core.request

        def unavailable(path, *args, **kwargs):
            if path.endswith("/actions/runs/11"):
                raise bridge.ApiFailure(503)
            return request(path, *args, **kwargs)

        self.control.core.request = unavailable
        with self.assertRaises(bridge.ApiFailure):
            self.control.reconcile_claim(self.control.path(claim["request"]))
        self.assertEqual(self.cancellations(), [])
        self.assertEqual(self.control.claim(claim["request"])[0], claim)

    def test_only_duplicate_is_cancelled_when_bound_worker_is_active(self):
        claim = self.dispatch()
        self.provider.runs[22] = self.provider.run(22, claim["request_digest"], claim["nonce"])
        self.control.reconcile(11, 1)
        self.control.reconcile(11, 1)
        self.assertEqual(self.cancellations(), [22])
        self.complete()
        self.assertEqual(self.control.wait(11, 1)["decision"], "success")

    def test_ambiguous_unbound_workers_close_before_cancellation(self):
        request = self.control.source_request(11, 1)
        self.control.prepare(request)
        (claim, _), _ = self.control.acquire(request)
        for run_id in (21, 22):
            self.provider.runs[run_id] = self.provider.run(run_id, claim["request_digest"], claim["nonce"])
        self.assertEqual(self.control.reconcile(11, 1)["decision"], "ambiguous")
        self.assertCountEqual(self.cancellations(), [21, 22])
        with self.assertRaises(bridge.Stopped):
            self.control.gate(11, 1, claim["request_digest"], claim["nonce"], 21, 1, REVISION)

    def test_lost_result_ack_uses_canonical_success_without_second_write(self):
        claim = self.dispatch()
        self.complete()
        self.provider.lose_write = lambda value: value["decision"] == "success"
        result = self.control.wait(11, 1)
        self.assertEqual(result["decision"], "success")
        mutations = list(self.provider.mutations)
        self.assertEqual(self.control.wait(11, 1), result)
        self.assertEqual(self.provider.mutations, mutations)
        self.assertEqual(result["request"], claim["request"])

    def test_closing_decision_wins_over_stale_success_cas(self):
        claim = self.dispatch()
        self.complete()
        update = self.control.update

        def close_first(entry, **changes):
            if changes == {"decision": "success"}:
                update(entry, decision="source_closed")
            return update(entry, **changes)

        self.control.update = close_first
        with self.assertRaisesRegex(bridge.Stopped, "Result cannot be accepted"):
            self.control.wait(11, 1)
        self.assertEqual(self.control.claim(claim["request"])[0]["decision"], "source_closed")

    def test_success_survives_source_completion_and_watchdog_settles(self):
        claim = self.dispatch()
        self.complete()
        self.control.wait(11, 1)
        self.provider.source["status"] = "completed"
        self.assertEqual(self.control.reconcile(11, 1),
                         {"state": "settled", "decision": "success", "public_run_id": 21})
        self.assertEqual(self.control.claim(claim["request"])[0]["workers"], {"21": "completed"})
        self.assertEqual(len(self.markers()), 1)
        self.assertEqual(self.cancellations(), [])

    def test_lost_cancel_post_is_observed_and_never_repeated(self):
        self.dispatch()
        self.provider.source["status"] = "completed"
        self.provider.lost_cancel = True
        with self.assertRaises(bridge.ApiFailure):
            self.control.reconcile(11, 1)
        self.control.reconcile(11, 1)
        self.assertEqual(self.cancellations(), [21])
        self.assertFalse(self.markers())
        self.complete()
        self.assertEqual(self.control.reconcile(11, 1)["state"], "settled")

    def test_lost_cancel_reservation_ack_does_not_authorize_post(self):
        self.dispatch()
        self.provider.source["status"] = "completed"
        self.provider.lose_write = lambda value: value["workers"].get("21") == "cancel_reserved"
        with self.assertRaises(bridge.ApiFailure):
            self.control.reconcile(11, 1)
        self.control.reconcile(11, 1)
        self.assertEqual(self.cancellations(), [])
        self.assertFalse(self.markers())

    def test_expired_unbound_claim_is_fenced_and_late_worker_is_still_found(self):
        request = self.control.source_request(11, 1)
        self.control.prepare(request)
        (claim, _), _ = self.control.acquire(request)
        self.clock.value = request["deadline"]
        self.assertEqual(self.control.reconcile(11, 1)["state"], "pending")
        self.clock.value += bridge.FENCE_GRACE
        self.assertEqual(self.control.reconcile(11, 1)["state"], "settled")
        self.provider.runs[21] = self.provider.run(21, claim["request_digest"], claim["nonce"])
        self.control.core.deadline = self.control.public.deadline = self.clock() + 300
        with self.assertRaisesRegex(bridge.Stopped, "requires attention"):
            self.control.reconcile()
        self.assertEqual(self.cancellations(), [21])
        self.complete()
        self.assertEqual(self.control.reconcile(11, 1)["state"], "settled")
        self.assertEqual(self.posts(), [])

    def test_sweep_continues_after_batch_limit_without_replaying_old_requests(self):
        paths = [f"v2/claims/{CONFIG['source_repository_id']}/101/{run}.1.json"
                 for run in range(100, 100 + bridge.SWEEP_BATCH + 2)]
        for path in paths:
            self.control.put_file(path, {})
        self.control.put_file(f"claims/{CONFIG['source_repository_id']}/101/1.1.json", {"version": 1})
        visited = []
        self.control.reconcile_claim = visited.append
        self.control.reconcile()
        self.assertEqual(visited, paths[:bridge.SWEEP_BATCH])
        self.control.reconcile()
        self.assertEqual(visited[bridge.SWEEP_BATCH:bridge.SWEEP_BATCH + 2], paths[-2:])

    def test_dispatch_observes_request_published_by_source_ci(self):
        request = self.control.source_request(11, 1)

        def publish_on_first_poll(seconds):
            self.clock.pause(seconds)
            self.control.prepare(request)

        self.control.pause = publish_on_first_poll
        self.assertEqual(self.control.dispatch(11, 1)["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_dispatch_without_publication_expires_without_granting_authority(self):
        with self.assertRaisesRegex(bridge.Stopped, "publication deadline"):
            self.control.dispatch(11, 1)
        self.assertFalse(self.provider.files)
        self.assertEqual(self.posts(), [])

    def test_wait_publishes_and_observes_separate_dispatch(self):
        def dispatch_on_first_poll(seconds):
            self.clock.pause(seconds)
            self.control.dispatch(11, 1)
            self.complete()

        self.control.pause = dispatch_on_first_poll
        self.assertEqual(self.control.wait(11, 1)["decision"], "success")
        self.assertEqual(len(self.posts()), 1)

    def test_wait_without_dispatch_expires_without_granting_authority(self):
        request = self.control.source_request(11, 1)
        self.control.pause = lambda _: setattr(self.clock, "value", request["deadline"])
        with self.assertRaisesRegex(bridge.Stopped, "deadline"):
            self.control.wait(11, 1)
        self.assertIsNone(self.control.claim(request)[0]["nonce"])
        self.assertEqual(self.posts(), [])

    def test_reconcile_routes_source_completion_to_exact_attempt(self):
        env = {"GITHUB_EVENT_NAME": "workflow_run", "COMPLETED_WORKFLOW": "CI",
               "COMPLETED_RUN_ID": "11", "COMPLETED_RUN_ATTEMPT": "2"}
        with patch.dict(os.environ, env, clear=True), patch.object(self.control, "reconcile") as reconcile:
            bridge.reconcile_event(self.control)
            reconcile.assert_called_once_with(11, 2)

    def test_reconcile_routes_dispatch_completion_schedule_and_manual_to_sweep(self):
        for event in ("workflow_run", "schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                env = {"GITHUB_EVENT_NAME": event, "COMPLETED_WORKFLOW": "Dispatch public CI",
                       "COMPLETED_RUN_ID": "99", "COMPLETED_RUN_ATTEMPT": "2"}
                with patch.dict(os.environ, env, clear=True), patch.object(self.control, "reconcile") as reconcile:
                    bridge.reconcile_event(self.control)
                    reconcile.assert_called_once_with()

    def test_reconcile_rejects_unknown_workflow(self):
        env = {"GITHUB_EVENT_NAME": "workflow_run", "COMPLETED_WORKFLOW": "Release image"}
        with patch.dict(os.environ, env, clear=True), patch.object(self.control, "reconcile") as reconcile:
            with self.assertRaisesRegex(bridge.Stopped, "Unexpected completed workflow"):
                bridge.reconcile_event(self.control)
            reconcile.assert_not_called()

    def test_private_wrapper_suppresses_output_and_command_files(self):
        with tempfile.TemporaryDirectory() as directory:
            command_file = Path(directory) / "public-output"
            command_file.write_text("sentinel")
            wrapper = Path(__file__).with_name("private_run.sh")
            script = 'printf "::error::private-source\\n"; echo private-stderr >&2; echo secret > "$GITHUB_OUTPUT"; exit 17'
            result = subprocess.run(["bash", str(wrapper), "bash", "-c", script],
                                    capture_output=True, text=True, env={"PATH": __import__("os").environ["PATH"],
                                                                       "GITHUB_OUTPUT": str(command_file)})
            self.assertEqual(result.returncode, 17)
            self.assertNotIn("private-source", result.stdout + result.stderr)
            self.assertNotIn("private-stderr", result.stdout + result.stderr)
            self.assertEqual(command_file.read_text(), "sentinel")


if __name__ == "__main__":
    unittest.main()
