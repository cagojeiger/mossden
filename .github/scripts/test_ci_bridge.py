import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote

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
        self.lost_create = self.lost_update = self.lost_post = False
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
            "display_title": f"bridge:{digest}:{nonce}", "status": "in_progress", "conclusion": None,
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
            blob_sha = hashlib.sha1(bridge.compact(value)).hexdigest()
            self.files[filename] = (value, blob_sha)
            if (existing and self.lost_update) or (not existing and self.lost_create):
                self.lost_create = self.lost_update = False
                raise bridge.ApiFailure()
            return {"content": {"sha": blob_sha}}
        if relative == "":
            return {"id": CONFIG["public_repository_id" if public else "source_repository_id"],
                    "full_name": repository.removeprefix("repos/"), "private": not public}
        if relative == "/branches/main":
            return {"commit": {"sha": REVISION}}
        if relative == "/branches/ci-state":
            return {"commit": {"commit": {"tree": {"sha": "3" * 40}}}}
        if relative.startswith("/git/trees/"):
            return {"truncated": False, "tree": [{"path": filename, "type": "blob"} for filename in self.files]}
        if relative == "/actions/workflows/ci.yml":
            return {"id": 201 if public else 101, "path": ".github/workflows/ci.yml", "state": "active"}
        if relative == "/actions/workflows/ci.yml/dispatches":
            inputs = data["inputs"]
            self.runs[21] = self.run(21, inputs["request_digest"], inputs["claim_nonce"])
            if self.lost_post:
                self.lost_post = False
                raise bridge.ApiFailure()
            return None
        if relative == "/actions/workflows/201/runs":
            return {"total_count": len(self.runs), "workflow_runs": list(copy.deepcopy(self.runs).values())}
        if not public and relative in ("/actions/runs/11", "/actions/runs/11/attempts/1"):
            return copy.deepcopy(self.source)
        if public and relative.startswith("/actions/runs/"):
            pieces = relative.split("/")
            run_id = int(pieces[3])
            if relative.endswith("/cancel"):
                # HTTP acceptance does not change actual execution state.
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


def response(value, etag=None):
    result = io.BytesIO(bridge.compact(value))
    result.headers = {"ETag": etag} if etag else {}
    return result


class HttpProvider:
    """Exercise the real Api with online ETag validation, not a local cache hit."""
    def __init__(self, provider, core_budget=1000):
        self.provider, self.core_budget = provider, core_budget
        self.calls = []

    def open(self, request, timeout):
        path = request.full_url.removeprefix("https://api.github.com/")
        public = path.startswith("repos/" + CONFIG["public_repository"] + "/")
        method = request.get_method()
        try:
            value = self.provider.request(public, path,
                                          bridge.decode(request.data) if request.data else None, method)
        except bridge.ApiFailure as error:
            raise HTTPError(request.full_url, error.status or 503, "fixture", {}, io.BytesIO()) from None
        etag = '"' + hashlib.sha256(bridge.compact(value)).hexdigest() + '"'
        conditional = request.get_header("If-none-match")
        status = 304 if method == "GET" and conditional == etag else 200
        if not public and status != 304 and sum(not call["public"] and call["status"] != 304
                                               for call in self.calls) >= self.core_budget:
            status = 403
        self.calls.append({"public": public, "path": path, "status": status,
                           "conditional": conditional, "method": method})
        if status != 200:
            raise HTTPError(request.full_url, status, "fixture", {"ETag": etag}, io.BytesIO())
        return response(value, etag)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = bridge.Api("first-token", self.clock, self.clock.pause)
        self.api.poll_cache = {}

    def test_online_304_reuses_an_isolated_copy(self):
        unchanged = HTTPError("https://api.github.com/example", 304, "unchanged", {}, io.BytesIO())
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({"items": [1]}, '"v1"'), unchanged, unchanged]
            first = self.api.request("example")
            first["items"].append(2)
            second = self.api.request("example")
            second["items"].append(3)
            self.assertEqual(self.api.request("example"), {"items": [1]})
            self.assertEqual(opener.return_value.open.call_count, 3)
            self.assertEqual(opener.return_value.open.call_args.args[0].get_header("If-none-match"), '"v1"')

    def test_changed_200_replaces_the_cached_representation(self):
        unchanged = HTTPError("https://api.github.com/example", 304, "unchanged", {}, io.BytesIO())
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({"value": 1}, '"v1"'),
                                                       response({"value": 2}, '"v2"'), unchanged]
            self.api.request("example")
            self.assertEqual(self.api.request("example"), {"value": 2})
            self.assertEqual(self.api.request("example"), {"value": 2})
            self.assertEqual(opener.return_value.open.call_args.args[0].get_header("If-none-match"), '"v2"')

    def test_cache_is_scoped_to_exact_path_and_current_credentials(self):
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({}, '"v1"') for _ in range(3)]
            self.api.request("example?attempt=1")
            self.api.request("example?attempt=2")
            self.api.token = "replacement-token"
            self.api.request("example?attempt=1")
            requests = [call.args[0] for call in opener.return_value.open.call_args_list]
            self.assertTrue(all(request.get_header("If-none-match") is None for request in requests))
            self.assertEqual(requests[-1].get_header("Authorization"), "Bearer replacement-token")

    def test_errors_never_fall_back_to_cached_data(self):
        for status in (403, 503):
            with self.subTest(status=status), patch.object(bridge, "build_opener") as opener:
                self.api.poll_cache = {}
                opener.return_value.open.side_effect = [response({}, '"v1"')] + [
                    HTTPError("https://api.github.com/example", status, "failed", {}, io.BytesIO())
                    for _ in range(1 if status == 403 else 3)]
                self.api.request("example")
                with self.assertRaises(bridge.ApiFailure) as error:
                    self.api.request("example")
                self.assertEqual(error.exception.status, status)
                self.assertEqual(opener.return_value.open.call_count, 2 if status == 403 else 4)

    def test_unrequested_304_cannot_supply_a_representation(self):
        for token in (None, "token"):
            with self.subTest(token=token), patch.object(bridge, "build_opener") as opener:
                self.api.token = token
                opener.return_value.open.side_effect = HTTPError("https://api.github.com/example", 304,
                                                                  "unexpected", {}, io.BytesIO())
                with self.assertRaises(bridge.ApiFailure):
                    self.api.request("example")

    def test_missing_etag_and_unauthenticated_reads_are_not_cached(self):
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({}, '"v1"'), response({}), response({})]
            self.api.request("example")
            self.api.request("example")
            self.api.request("example")
            self.assertIsNone(opener.return_value.open.call_args.args[0].get_header("If-none-match"))
        self.api.token = None
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({}, '"v1"') for _ in range(2)]
            self.api.request("example")
            self.api.request("example")
            self.assertIsNone(opener.return_value.open.call_args.args[0].get_header("If-none-match"))

    def test_failed_mutation_is_never_retried(self):
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://api.github.com/example", 503,
                                                              "uncertain", {}, io.BytesIO())
            with self.assertRaises(bridge.ApiFailure):
                self.api.request("example", {}, method="POST")
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_disabled_cache_and_mutations_are_unconditional(self):
        with patch.object(bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = [response({}, '"v1"') for _ in range(4)]
            self.api.request("example")
            self.api.request("example", {}, method="POST")
            self.api.poll_cache = None
            self.api.request("example")
            self.api.request("example")
            requests = [call.args[0] for call in opener.return_value.open.call_args_list]
            self.assertTrue(all(request.get_header("If-none-match") is None for request in requests))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.provider = Provider(self.clock)
        self.control = bridge.Bridge(CONFIG, Api(self.provider, False), Api(self.provider, True), REVISION,
                                     self.clock, self.clock.pause)

    def posts(self):
        return [call for call in self.provider.mutations if call[2].endswith("/dispatches")]

    def complete(self):
        self.provider.runs[21].update(status="completed", conclusion="success")

    def test_request_digest_matches_the_existing_rust_v1_golden(self):
        request = self.control.source_request(11, 1)
        request["source_run_id"] = 1001
        # Independent Rust fixture from the offline ci-bridge skeleton.
        self.assertEqual(bridge.digest(request),
                         "a4232d2e139268763c7b4e897306cde6d017dca47f6888d0b4f1f9c514f71468")

    def test_normal_gate_and_result_acceptance(self):
        claim = self.control.dispatch(11, 1)
        self.assertEqual(self.control.gate(11, 1, claim["request_digest"], claim["nonce"], 21, 1, REVISION), SOURCE_SHA)
        self.complete()
        self.assertEqual(self.control.wait(11, 1)["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_bound_wait_cycle_retains_guards_with_four_core_and_two_public_gets(self):
        self.control.dispatch(11, 1)
        counts, cycles = [0, 0], []
        for index, api in enumerate((self.control.core, self.control.public)):
            original = api.request

            def counted(*args, index=index, original=original, **kwargs):
                counts[index] += 1
                return original(*args, **kwargs)

            api.request = counted

        def pause(seconds):
            cycles.append(tuple(counts))
            counts[:] = [0, 0]
            self.clock.pause(seconds)
            if len(cycles) == 2:
                raise bridge.Stopped("end sampled polling")

        self.control.pause = pause
        with self.assertRaisesRegex(bridge.Stopped, "sampled"):
            self.control.wait(11, 1)
        self.assertEqual(cycles, [(7, 2), (4, 2)])  # Initial source read costs three core GETs.
        self.assertIsNone(self.control.core.poll_cache)
        self.assertIsNone(self.control.public.poll_cache)

    def test_stable_long_wait_revalidates_online_and_final_acceptance_is_uncached(self):
        self.control.dispatch(11, 1)
        transport = HttpProvider(self.provider)
        finish = self.clock() + 2700

        def pause(seconds):
            self.clock.pause(seconds)
            if self.clock() >= finish:
                self.complete()

        self.control = bridge.Bridge(CONFIG, bridge.Api("core", self.clock, self.clock.pause),
                                     bridge.Api("public", self.clock, self.clock.pause), REVISION,
                                     self.clock, pause)
        with patch.object(bridge, "build_opener", return_value=transport):
            self.assertEqual(self.control.wait(11, 1)["public_run_id"], 21)
        self.assertTrue(any(call["status"] == 304 for call in transport.calls))
        self.assertLess(sum(not call["public"] and call["status"] == 200
                            for call in transport.calls), 1000)
        self.assertTrue(all(call["conditional"] is None for call in transport.calls[-9:]))
        self.assertIsNone(self.control.core.poll_cache)
        self.assertIsNone(self.control.public.poll_cache)

    def test_final_source_change_is_rejected_after_cached_polling(self):
        self.control.dispatch(11, 1)
        transport = HttpProvider(self.provider)

        def pause(seconds):
            self.clock.pause(seconds)
            self.complete()

        self.control = bridge.Bridge(CONFIG, bridge.Api("core", self.clock, self.clock.pause),
                                     bridge.Api("public", self.clock, self.clock.pause), REVISION,
                                     self.clock, pause)
        verify = self.control.verify_results

        def changed(claim):
            self.assertIsNone(self.control.core.poll_cache)
            self.assertIsNone(self.control.public.poll_cache)
            self.provider.source["run_attempt"] = 2
            verify(claim)

        self.control.verify_results = changed
        with patch.object(bridge, "build_opener", return_value=transport):
            with self.assertRaises(bridge.Stopped):
                self.control.wait(11, 1)
        self.assertTrue(any(call["status"] == 304 for call in transport.calls))
        self.assertIsNone(self.control.core.poll_cache)
        self.assertIsNone(self.control.public.poll_cache)

    def test_wait_still_stops_at_the_original_deadline(self):
        claim = self.control.dispatch(11, 1)
        with self.assertRaisesRegex(bridge.Stopped, "deadline"):
            self.control.wait(11, 1)
        self.assertEqual(self.clock(), claim["deadline"])
        self.assertIsNone(self.control.core.poll_cache)
        self.assertIsNone(self.control.public.poll_cache)

    def test_wait_still_stops_when_the_source_closes(self):
        self.control.dispatch(11, 1)

        def pause(seconds):
            self.clock.pause(seconds)
            self.provider.source["status"] = "completed"

        self.control.pause = pause
        with self.assertRaisesRegex(bridge.Stopped, "active"):
            self.control.wait(11, 1)
        self.assertIsNone(self.control.core.poll_cache)
        self.assertIsNone(self.control.public.poll_cache)

    def test_lost_dispatch_response_never_reposts_even_after_restart(self):
        self.provider.lost_post = True
        self.assertEqual(self.control.dispatch(11, 1)["public_run_id"], 21)
        self.assertEqual(self.control.dispatch(11, 1)["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_uncertain_claim_create_never_grants_permission(self):
        self.provider.lost_create = True
        with self.assertRaises(bridge.Stopped):
            self.control.dispatch(11, 1)
        self.assertEqual(len(self.provider.files), 1)  # Write really took effect.
        with self.assertRaises(bridge.Stopped):
            self.control.dispatch(11, 1)
        self.assertEqual(self.posts(), [])

    def test_lost_binding_response_requires_exact_intended_readback(self):
        self.provider.lost_update = True
        claim = self.control.dispatch(11, 1)
        self.assertEqual(claim["public_run_id"], 21)
        self.assertEqual(len(self.posts()), 1)

    def test_binding_is_immutable(self):
        claim = self.control.dispatch(11, 1)
        self.provider.runs[22] = self.provider.run(22, claim["request_digest"], claim["nonce"])
        with self.assertRaisesRegex(bridge.Stopped, "immutable"):
            self.control.bind(claim["request"], self.control.claim(claim["request"]), 22)

    def test_wrong_public_provenance_is_rejected(self):
        claim = self.control.dispatch(11, 1)
        self.complete()
        for key, value in (("head_sha", "f" * 40), ("run_attempt", 2), ("actor", {"id": 999})):
            with self.subTest(key=key):
                original = self.provider.runs[21][key]
                self.provider.runs[21][key] = value
                with self.assertRaises(bridge.Stopped):
                    self.control.verify_results(claim)
                self.provider.runs[21][key] = original

    def test_success_after_original_deadline_is_rejected(self):
        claim = self.control.dispatch(11, 1)
        self.complete()
        self.clock.value = claim["deadline"]
        with self.assertRaisesRegex(bridge.Stopped, "deadline"):
            self.control.verify_results(claim)

    def test_wrong_job_run_identity_is_rejected(self):
        claim = self.control.dispatch(11, 1)
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
        claim = self.control.dispatch(11, 1)
        self.provider.source["run_attempt"] = 2
        with self.assertRaises(bridge.Stopped):
            self.control.gate(11, 1, claim["request_digest"], claim["nonce"], 21, 1, REVISION)

    def test_cancel_acknowledgement_is_not_terminal_receipt(self):
        claim = self.control.dispatch(11, 1)
        self.provider.source["status"] = "completed"
        self.control.reconcile_claim(self.control.path(claim["request"]))
        self.assertTrue(any(call[2].endswith("/cancel") for call in self.provider.mutations))
        self.assertFalse(any(path.startswith("settled/") for path in self.provider.files))
        self.complete()
        self.control.reconcile_claim(self.control.path(claim["request"]))
        self.assertTrue(any(path.startswith("settled/") for path in self.provider.files))

    def test_delivery_created_after_deadline_is_cancelled_then_observed(self):
        request = self.control.source_request(11, 1)
        (claim, _), _ = self.control.acquire(request)
        self.clock.value = request["deadline"] + 10
        self.provider.runs[21] = self.provider.run(21, claim["request_digest"], claim["nonce"])
        with self.assertRaisesRegex(bridge.Stopped, "window"):
            self.control.public_run(claim, 21)
        self.control.reconcile_claim(self.control.path(request))
        self.assertTrue(any(call[2].endswith("/cancel") for call in self.provider.mutations))
        self.assertFalse(any(path.startswith("settled/") for path in self.provider.files))
        self.complete()
        self.control.reconcile_claim(self.control.path(request))
        self.assertTrue(any(path.startswith("settled/") for path in self.provider.files))
        self.assertEqual(self.posts(), [])

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
