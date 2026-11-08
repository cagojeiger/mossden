#!/usr/bin/env python3
"""Single-SHA GitHub CI bridge. No private application checkout on Home."""

import argparse
import base64
from bisect import bisect_right
import hashlib
from http.client import HTTPException
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class Stopped(Exception):
    pass


class ApiFailure(Stopped):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"GitHub API failed ({status or 'unknown outcome'})")


def require(condition, message):
    if not condition:
        raise Stopped(message)


def positive(value):
    return type(value) is int and value > 0


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def integer(value):
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value), "Invalid run identity")
    return int(value)


def timestamp(value):
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        raise Stopped("Invalid provider timestamp") from None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def decode(value):
    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except (ValueError, TypeError, UnicodeError):
        raise Stopped("Invalid JSON document") from None


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


REQUEST_FIELDS = (
    "version", "source_repository_id", "source_workflow_id", "source_run_id",
    "source_attempt", "tested_sha", "public_repository_id", "public_workflow",
    "public_revision", "deadline",
)
VERSION = 2
RUN_STATES = ("queued", "pending", "requested", "waiting", "in_progress", "completed")
CLOSED_DECISIONS = ("source_closed", "expired", "ambiguous")
DECISIONS = (None, "success", *CLOSED_DECISIONS)
FENCE_GRACE = 24 * 3600
SWEEP_BATCH = 20
CLAIM_FIELDS = {
    "version", "request", "request_digest", "nonce", "claimed_at", "public_run_id",
    "decision", "workers",
}


def digest(request):
    return hashlib.sha256(compact({key: request[key] for key in REQUEST_FIELDS})).hexdigest()


def title(claim):
    request = claim["request"]
    return (f"bridge:v2:{request['source_run_id']}:{request['source_attempt']}:"
            f"{claim['request_digest']}:{claim['nonce']}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, token=None, clock=time.time, pause=time.sleep):
        self.token, self.clock, self.pause = token, clock, pause
        self.deadline = self.clock() + 300

    def request(self, path, data=None, method="GET", missing=False):
        require(not path.startswith("/") and "://" not in path, "Invalid API path")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "mossden-ci-bridge"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = compact(data) if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        # A mutation is sent ONCE. An absent response is never evidence of no effect.
        for attempt in range(3 if method == "GET" else 1):
            remaining = self.deadline - self.clock()
            require(remaining > 0, "API observation deadline elapsed")
            try:
                request = Request("https://api.github.com/" + path, data=body, headers=headers, method=method)
                with build_opener(NoRedirect).open(request, timeout=min(15, remaining)) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                    require(len(raw) <= 2 * 1024 * 1024, "API response is too large")
                    return decode(raw) if raw else None
            except HTTPError as error:
                if method == "GET" and missing and error.code == 404:
                    return None
                if method != "GET" or error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ApiFailure(error.code) from None
            except (URLError, OSError, HTTPException):
                if method != "GET" or attempt == 2:
                    raise ApiFailure() from None
            self.pause(min(1 + attempt, max(0, self.deadline - self.clock())))
        raise ApiFailure()


class Bridge:
    def __init__(self, config, core, public, revision, clock=time.time, pause=time.sleep):
        self.config, self.core, self.public = config, core, public
        self.revision, self.clock, self.pause = revision, clock, pause
        require(config.get("version") == VERSION and sha(revision), "Unconfigured trusted public revision")
        require(positive(config.get("dispatch_actor_id")), "Dispatch App actor ID is not configured")
        require(config.get("required_jobs") == ["Rust"] and config.get("deadline_seconds") == 3600,
                "Only the single Rust job and one-hour source deadline are supported")
        self.source = "repos/" + config["source_repository"]
        self.target = "repos/" + config["public_repository"]
        self.workflow_ids = {}

    def workflow(self, public=False):
        if public in self.workflow_ids:
            return self.workflow_ids[public]
        api, repository = (self.public, self.target) if public else (self.core, self.source)
        filename = self.config["public_workflow" if public else "source_workflow"]
        result = api.request(f"{repository}/actions/workflows/{quote(filename, safe='')}")
        require(positive(result.get("id")) and result.get("path") == ".github/workflows/" + filename
                and result.get("state") == "active", "Unexpected workflow definition")
        self.workflow_ids[public] = result["id"]
        return result["id"]

    def repository(self, public=False):
        api, path = (self.public, self.target) if public else (self.core, self.source)
        result = api.request(path)
        expected = self.config["public_repository_id" if public else "source_repository_id"]
        require(result.get("id") == expected and result.get("full_name") == path.removeprefix("repos/")
                and result.get("private") is (not public), "Repository identity or visibility changed")

    def preflight(self):
        self.repository()
        self.repository(public=True)
        self.workflow()
        self.workflow(public=True)
        branch = self.public.request(self.target + "/branches/main")
        require(branch.get("commit", {}).get("sha") == self.revision, "Public main moved away from the trusted pin")
        self.core.request(self.source + "/branches/" + quote(self.config["state_branch"], safe=""))

    def before_deadline(self, request):
        require(self.clock() < request["deadline"], "Original source deadline elapsed")

    def source_snapshot(self, run_id, attempt, revision=None):
        workflow_id = self.workflow()
        latest = self.core.request(self.source + f"/actions/runs/{run_id}")
        run = self.core.request(self.source + f"/actions/runs/{run_id}/attempts/{attempt}")
        for item in (latest, run):
            require(isinstance(item, dict) and item.get("id") == run_id and item.get("workflow_id") == workflow_id
                    and item.get("path") == ".github/workflows/" + self.config["source_workflow"]
                    and item.get("repository", {}).get("id") == self.config["source_repository_id"]
                    and item.get("head_repository", {}).get("id") == self.config["source_repository_id"]
                    and item.get("head_branch") == "main" and item.get("event") in ("push", "workflow_dispatch")
                    and sha(item.get("head_sha")) and item.get("status") in RUN_STATES,
                    "Unverified source run")
        require(positive(run_id) and positive(attempt) and run.get("run_attempt") == attempt
                and positive(latest.get("run_attempt")) and latest["run_attempt"] >= attempt
                and latest["head_sha"] == run["head_sha"], "Unverified source attempt")
        created = timestamp(run.get("created_at"))
        require(created <= self.clock(), "Source creation time is in the future")
        request = dict(zip(REQUEST_FIELDS, (
            VERSION, self.config["source_repository_id"], workflow_id, run_id, attempt, run["head_sha"],
            self.config["public_repository_id"], self.config["public_workflow"],
            revision or self.revision, created + self.config["deadline_seconds"],
        )))
        if latest["run_attempt"] > attempt or "completed" in (latest["status"], run["status"]):
            return request, "source_closed"
        if latest["status"] == run["status"] == "in_progress":
            return request, "active"
        return request, "waiting"

    def source_request(self, run_id, attempt):
        request, state = self.source_snapshot(run_id, attempt)
        require(state == "active", "Source run or attempt is no longer active")
        self.before_deadline(request)
        return request

    def source_state(self, request):
        observed, state = self.source_snapshot(request["source_run_id"], request["source_attempt"],
                                               request["public_revision"])
        require(observed == request, "Source request metadata changed")
        return state

    def active(self, request):
        require(self.source_state(request) == "active", "Source run or attempt is no longer active")
        self.before_deadline(request)

    def claim_path(self, workflow_id, run_id, attempt):
        return f"v2/claims/{self.config['source_repository_id']}/{workflow_id}/{run_id}.{attempt}.json"

    def path(self, request):
        return self.claim_path(request["source_workflow_id"], request["source_run_id"], request["source_attempt"])

    def contents(self, path):
        return self.source + "/contents/" + quote(path, safe="/")

    def read_file(self, path):
        entry = self.core.request(self.contents(path) + "?" + urlencode({"ref": self.config["state_branch"]}), missing=True)
        if entry is None:
            return None
        require(entry.get("encoding") == "base64" and sha(entry.get("sha"))
                and type(entry.get("size")) is int and entry["size"] <= 16384, "Invalid state file")
        try:
            raw = base64.b64decode(entry["content"], validate=False)
        except (ValueError, TypeError):
            raise Stopped("Invalid state encoding") from None
        require(len(raw) <= 16384, "State file is too large")
        require(self.blob_sha(raw) == entry["sha"], "State bytes do not match the Git blob")
        return decode(raw), entry["sha"]

    @staticmethod
    def blob_sha(raw):
        return hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()

    def validate_claim(self, claim, expected=None):
        require(isinstance(claim, dict) and set(claim) == CLAIM_FIELDS and claim["version"] == VERSION,
                "Invalid claim schema")
        request = claim["request"]
        require(isinstance(request, dict) and set(request) == set(REQUEST_FIELDS)
                and request["version"] == VERSION and all(positive(request[key]) for key in (
                    "source_repository_id", "source_workflow_id", "source_run_id", "source_attempt",
                    "public_repository_id", "deadline"))
                and sha(request["tested_sha"]) and sha(request["public_revision"])
                and request["source_repository_id"] == self.config["source_repository_id"]
                and request["public_repository_id"] == self.config["public_repository_id"]
                and request["public_workflow"] == self.config["public_workflow"], "Invalid claim request")
        require(expected is None or request == expected, "Claim belongs to a different request")
        nonce, claimed_at = claim["nonce"], claim["claimed_at"]
        bound, decision, workers = claim["public_run_id"], claim["decision"], claim["workers"]
        reserved = (isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{32}", nonce)
                    and type(claimed_at) is int
                    and request["deadline"] - self.config["deadline_seconds"] <= claimed_at < request["deadline"])
        require(claim["request_digest"] == digest(request)
                and (reserved or (nonce is None and claimed_at is None and bound is None and workers == {}))
                and (bound is None or positive(bound)) and decision in DECISIONS
                and isinstance(workers, dict) and all(re.fullmatch(r"[1-9][0-9]*", key)
                    and state in ("cancel_reserved", "completed") for key, state in workers.items())
                and (decision != "success" or (positive(bound) and workers.get(str(bound)) != "cancel_reserved")),
                "Invalid claim evidence")
        return claim

    def claim(self, request):
        entry = self.read_file(self.path(request))
        if entry is not None:
            self.validate_claim(entry[0], request)
        return entry

    def put_file(self, path, value, blob_sha=None):
        raw = compact(value)
        data = {"message": "ci bridge V2 state", "branch": self.config["state_branch"],
                "content": base64.b64encode(raw).decode()}
        if blob_sha is not None:
            data["sha"] = blob_sha
        result = self.core.request(self.contents(path), data, method="PUT")
        content = result.get("content") if isinstance(result, dict) else None
        expected = self.blob_sha(raw)
        require(isinstance(content, dict) and content.get("path") == path and content.get("sha") == expected,
                "State write was not acknowledged")
        return expected

    def update(self, entry, **changes):
        claim, blob_sha = entry
        updated = self.validate_claim({**claim, **changes}, claim["request"])
        return updated, self.put_file(self.path(claim["request"]), updated, blob_sha)

    def prepare(self, request):
        path = self.path(request)
        entry = self.claim(request)
        if entry is not None:
            return entry
        require(self.read_file(path.removeprefix("v2/")) is None, "Historical V1 attempt cannot be replayed")
        self.active(request)
        claim = {"version": VERSION, "request": request, "request_digest": digest(request),
                 "nonce": None, "claimed_at": None, "public_run_id": None, "decision": None, "workers": {}}
        return claim, self.put_file(path, claim)

    def acquire(self, request):
        entry = self.prepare(request)
        claim = entry[0]
        require(claim["decision"] is None, "Request is already decided")
        if claim["nonce"] is not None:
            return entry, False
        self.active(request)
        # Only this exact reservation ACK grants local permission to send one POST.
        # A prepared request, existing reservation or uncertain write grants none.
        return self.update(entry, nonce=secrets.token_hex(16), claimed_at=int(self.clock())), True

    def rows(self, api, path, field):
        values = []
        for page in range(1, 11):
            result = api.request(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            rows, total = result.get(field), result.get("total_count")
            require(isinstance(rows, list) and type(total) is int and 0 <= total <= 1000,
                    "API listing exceeds the supported complete observation window")
            values.extend(rows)
            if len(rows) < 100 or len(values) >= total:
                require(len(values) == total, "API listing changed or is incomplete")
                ids = [row.get("id") for row in values]
                require(all(positive(value) for value in ids) and len(set(ids)) == len(ids), "Duplicate API listing entry")
                return values
        raise Stopped("Incomplete API listing")

    def public_run(self, claim, run_id, admission=True):
        request = claim["request"]
        run = self.public.request(f"{self.target}/actions/runs/{run_id}")
        require(run.get("id") == run_id and run.get("run_attempt") == 1
                and run.get("workflow_id") == self.workflow(public=True)
                and run.get("path") == ".github/workflows/" + self.config["public_workflow"]
                and run.get("repository", {}).get("id") == self.config["public_repository_id"]
                and run.get("head_repository", {}).get("id") == self.config["public_repository_id"]
                and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main"
                and run.get("head_sha") == request["public_revision"]
                and run.get("display_title") == title(claim)
                and run.get("actor", {}).get("id") == self.config["dispatch_actor_id"]
                and run.get("triggering_actor", {}).get("id") == self.config["dispatch_actor_id"]
                and run.get("status") in RUN_STATES,
                "Public run identity or provenance mismatch")
        created = timestamp(run.get("created_at"))
        require(claim["claimed_at"] <= created, "Public run predates the claim")
        if admission:
            require(created < request["deadline"], "Public run is outside the request window")
        return run

    def candidates(self, claim, admission=True):
        request = claim["request"]
        upper = request["deadline"] if admission else int(self.clock())
        query = urlencode({"event": "workflow_dispatch", "branch": "main",
                           "created": utc(request["deadline"] - 3600) + ".." + utc(upper)})
        runs = self.rows(self.public, f"{self.target}/actions/workflows/{self.workflow(public=True)}/runs?{query}", "workflow_runs")
        return [self.public_run(claim, run["id"], admission=admission)
                for run in runs if run.get("display_title") == title(claim)]

    def bind(self, request, entry, run_id):
        value = entry[0]
        self.validate_claim(value, request)
        require(value["nonce"] is not None and value["decision"] is None, "Request cannot bind a worker")
        self.active(request)
        self.public_run(value, run_id)
        if value["public_run_id"] is not None:
            require(value["public_run_id"] == run_id, "Binding is immutable")
            return entry
        try:
            return self.update(entry, public_run_id=run_id)
        except Stopped:
            observed = self.claim(request)
            require(observed is not None and observed[0] == {**value, "public_run_id": run_id}, "Binding update is uncertain or conflicting")
            return observed

    def observe_binding(self, request, seconds=150):
        end = min(request["deadline"], self.clock() + seconds)
        while self.clock() < end:
            self.active(request)
            entry = self.claim(request)
            require(entry is not None, "Claim disappeared")
            claim = entry[0]
            require(claim["decision"] is None and claim["nonce"] is not None, "Request cannot observe binding")
            if claim["public_run_id"] is not None:
                self.public_run(claim, claim["public_run_id"])
                return entry
            runs = self.candidates(claim)
            require(len(runs) <= 1, "Ambiguous public runs; no binding is permitted")
            if runs:
                return self.bind(request, entry, runs[0]["id"])
            self.pause(min(5, max(0, end - self.clock())))
        raise Stopped("Public execution was not bound; observe or reconcile, never redispatch")

    def dispatch(self, run_id, attempt):
        self.preflight()
        request = self.source_request(run_id, attempt)
        self.core.deadline = self.public.deadline = request["deadline"]
        entry, permission = self.acquire(request)
        if permission:
            self.active(request)
            branch = self.public.request(self.target + "/branches/main")
            require(branch.get("commit", {}).get("sha") == self.revision, "Public main moved before dispatch")
            require(self.claim(request) == entry, "Reservation changed before dispatch")
            self.active(request)
            claim = entry[0]
            data = {"ref": "main", "inputs": {
                "request_version": str(VERSION),
                "source_run_id": str(run_id), "source_attempt": str(attempt),
                "request_digest": claim["request_digest"], "claim_nonce": claim["nonce"],
            }}
            try:
                # Spend the permission regardless of whether GitHub's response arrives.
                self.public.request(f"{self.target}/actions/workflows/{quote(self.config['public_workflow'])}/dispatches",
                                    data, method="POST")
            except Stopped:
                pass
        return self.observe_binding(request)[0]

    def gate(self, run_id, attempt, request_digest, nonce, public_run_id, public_attempt, public_sha):
        require(public_attempt == 1 and public_sha == self.revision, "Untrusted public execution revision or attempt")
        self.repository()
        self.repository(public=True)
        request = self.source_request(run_id, attempt)
        self.core.deadline = self.public.deadline = request["deadline"]
        end = min(request["deadline"], self.clock() + 180)
        while self.clock() < end:
            entry = self.claim(request)
            require(entry is not None, "No dispatch claim exists")
            claim = entry[0]
            require(claim["decision"] is None and claim["nonce"] is not None, "Request cannot admit a worker")
            require(isinstance(request_digest, str) and isinstance(nonce, str)
                    and secrets.compare_digest(claim["request_digest"], request_digest)
                    and secrets.compare_digest(claim["nonce"], nonce), "Dispatch input does not match the claim")
            run = self.public_run(claim, public_run_id)
            require(run["status"] != "completed", "Public worker is no longer active")
            if claim["public_run_id"] is not None:
                require(claim["public_run_id"] == public_run_id, "This public run is not bound")
                self.active(request)
                current = self.claim(request)
                require(current is not None and current[0]["decision"] is None
                        and current[0]["nonce"] == nonce and current[0]["public_run_id"] == public_run_id
                        and str(public_run_id) not in current[0]["workers"],
                        "Claim changed before source admission")
                self.before_deadline(request)
                return request["tested_sha"]
            self.active(request)
            self.pause(min(5, max(0, end - self.clock())))
        raise Stopped("No immutable binding before the gate observation deadline")

    def verify_results(self, claim):
        run = self.public_run(claim, claim["public_run_id"])
        require(run.get("status") == "completed" and run.get("conclusion") == "success", "Public execution did not succeed")
        jobs = self.rows(self.public, f"{self.target}/actions/runs/{run['id']}/attempts/1/jobs", "jobs")
        for name in self.config["required_jobs"]:
            matching = [job for job in jobs if job.get("name") == name]
            require(len(matching) == 1, "Required job is missing or duplicated")
            job = matching[0]
            require(job.get("run_id") == run["id"] and job.get("run_attempt") == 1
                    and job.get("head_sha") == claim["request"]["public_revision"]
                    and job.get("status") == "completed" and job.get("conclusion") == "success",
                    "Required job evidence mismatch")
        latest = self.public_run(claim, run["id"])
        require(latest.get("status") == "completed" and latest.get("conclusion") == "success", "Public result changed")
        self.active(claim["request"])

    def accept(self, request):
        for _ in range(3):
            entry = self.claim(request)
            require(entry is not None, "Claim disappeared before result acceptance")
            claim = entry[0]
            require(claim["decision"] in (None, "success") and claim["public_run_id"] is not None
                    and claim["workers"].get(str(claim["public_run_id"])) != "cancel_reserved",
                    "Result cannot be accepted")
            self.verify_results(claim)
            if claim["decision"] is None:
                try:
                    self.update(entry, decision="success")
                except ApiFailure as error:
                    if error.status == 409:
                        continue
                except Stopped:
                    # An uncertain write can only be resolved by the canonical record.
                    pass
            final = self.claim(request)
            require(final is not None and final[0]["decision"] == "success"
                    and final[0]["nonce"] == claim["nonce"]
                    and final[0]["public_run_id"] == claim["public_run_id"],
                    "Canonical success was not observed")
            self.active(request)
            return final[0]
        raise Stopped("Result decision conflicted")

    def wait(self, run_id, attempt):
        request = self.source_request(run_id, attempt)
        self.core.deadline = self.public.deadline = request["deadline"]
        while self.clock() < request["deadline"]:
            entry = self.claim(request)
            require(entry is not None and entry[0]["decision"] in (None, "success"), "Request is closed")
            claim = entry[0]
            self.active(request)
            if claim["public_run_id"] is not None:
                run = self.public_run(claim, claim["public_run_id"])
                if run["status"] == "completed":
                    return self.accept(request)
            self.pause(min(15, max(0, request["deadline"] - self.clock())))
        raise Stopped("Original source deadline elapsed")

    def observe_terminal(self, entry, run_id):
        workers = entry[0]["workers"]
        if workers.get(str(run_id)) == "completed":
            return entry
        return self.update(entry, workers={**workers, str(run_id): "completed"})

    def cancel(self, entry, run_id):
        claim = entry[0]
        request = claim["request"]
        workers = claim["workers"]
        bound, decision = claim["public_run_id"], claim["decision"]
        require((bound is not None and run_id != bound) or decision in CLOSED_DECISIONS,
                "Cancellation is not authorized for this worker")
        if str(run_id) in workers:
            return entry
        # The same record excludes a success decision for a worker reserved for cancellation.
        entry = self.update(entry, workers={**workers, str(run_id): "cancel_reserved"})
        require(self.claim(request) == entry, "Cancellation reservation changed")
        if self.public_run(entry[0], run_id, admission=False)["status"] == "completed":
            return self.observe_terminal(entry, run_id)
        try:
            self.public.request(f"{self.target}/actions/runs/{run_id}/cancel", {}, method="POST")
        except ApiFailure as error:
            if error.status != 409:
                raise
        return entry

    def settled_path(self, path, blob_sha):
        return f"v2/settled/{path.removeprefix('v2/claims/').removesuffix('.json')}/{blob_sha}.json"

    def reconcile_claim(self, path):
        entry = self.read_file(path)
        if entry is None:
            return {"state": "no_v2_claim"}
        claim = self.validate_claim(entry[0])
        request = claim["request"]
        require(path == self.path(request), "Claim path mismatch")
        runs = {}
        if claim["nonce"] is not None:
            runs = {run["id"]: run for run in self.candidates(claim, admission=False)}
            known = {int(key) for key in claim["workers"]}
            if claim["public_run_id"] is not None:
                known.add(claim["public_run_id"])
            for run_id in known - runs.keys():
                runs[run_id] = self.public_run(claim, run_id, admission=False)

        # An API failure or unverified identity raises before a closing or cancel write.
        state = self.source_state(request)
        decision = claim["decision"]
        if decision is None:
            if self.clock() >= request["deadline"]:
                decision = "expired"
            elif state == "source_closed":
                decision = state
            elif state == "active" and claim["public_run_id"] is None:
                if len(runs) == 1:
                    entry = self.bind(request, entry, next(iter(runs)))
                elif len(runs) > 1:
                    decision = "ambiguous"
            if decision is not None:
                entry = self.update(entry, decision=decision)

        for run_id, run in runs.items():
            current = entry[0]
            bound = current["public_run_id"]
            if run["status"] == "completed":
                entry = self.observe_terminal(entry, run_id)
            elif current["decision"] in CLOSED_DECISIONS or (bound is not None and run_id != bound):
                entry = self.cancel(entry, run_id)

        claim, blob_sha = entry
        cleanup = all(claim["workers"].get(str(run_id)) == "completed" for run_id in runs)
        fenced = self.clock() >= request["deadline"] + FENCE_GRACE
        settled = claim["decision"] is not None and cleanup and (
            claim["nonce"] is None or claim["public_run_id"] is not None or fenced)
        if settled:
            marker = self.settled_path(path, blob_sha)
            if self.read_file(marker) is None:
                reason = "dispatch_fenced" if claim["nonce"] and claim["public_run_id"] is None else "observed"
                self.put_file(marker, {"claim_sha": blob_sha, "reason": reason})
        require(cleanup or not fenced, "Public worker cleanup is still unconfirmed")
        return {"state": "settled" if settled else "pending", "decision": claim["decision"],
                "public_run_id": claim["public_run_id"]}

    def active_claim_paths(self, source_workflow_id):
        public_workflow_id = self.workflow(public=True)
        paths = set()
        for state in RUN_STATES[:-1]:
            query = f"event=workflow_dispatch&branch=main&status={state}"
            for run in self.rows(self.public, f"{self.target}/actions/workflows/{public_workflow_id}/runs?{query}",
                                 "workflow_runs"):
                match = re.fullmatch(r"bridge:v2:([1-9][0-9]*):([1-9][0-9]*):[0-9a-f]{64}:[0-9a-f]{32}",
                                     run.get("display_title") or "")
                if match:
                    paths.add(self.claim_path(source_workflow_id, *match.groups()))
        return paths

    def reconcile(self, run_id=None, attempt=None):
        self.repository()
        self.repository(public=True)
        workflow_id = self.workflow()
        if run_id is not None:
            return self.reconcile_claim(self.claim_path(workflow_id, run_id, attempt))

        branch = self.core.request(self.source + "/branches/" + quote(self.config["state_branch"], safe=""))
        tree_sha = branch.get("commit", {}).get("commit", {}).get("tree", {}).get("sha")
        require(sha(tree_sha), "Invalid state branch tree")
        tree = self.core.request(f"{self.source}/git/trees/{tree_sha}?recursive=1")
        require(tree.get("truncated") is False and isinstance(tree.get("tree"), list), "Incomplete claim tree")
        files = {entry["path"]: entry["sha"] for entry in tree["tree"] if entry.get("type") == "blob"}
        active = self.active_claim_paths(workflow_id)
        prefix = f"v2/claims/{self.config['source_repository_id']}/{workflow_id}/"
        paths = sorted(path for path, blob_sha in files.items() if path.startswith(prefix)
                       and path.endswith(".json") and (path in active or self.settled_path(path, blob_sha) not in files))
        cursor_path = "v2/cursor.json"
        cursor = self.read_file(cursor_path)
        after = cursor[0]["after"] if cursor else ""
        start = bisect_right(paths, after)
        ordered = paths[start:] + paths[:start]
        failures, visited = 0, 0
        hard_end = min(self.core.deadline, self.public.deadline)
        for path in ordered[:SWEEP_BATCH]:
            if self.clock() + 30 >= hard_end:
                break
            self.core.deadline = self.public.deadline = min(hard_end - 15, self.clock() + 30)
            try:
                self.reconcile_claim(path)
            except Stopped:
                failures += 1
            visited += 1
        self.core.deadline = self.public.deadline = hard_end
        if visited and visited < len(paths):
            self.put_file(cursor_path, {"after": ordered[visited - 1]}, cursor[1] if cursor else None)
        print(json.dumps({"visited": visited, "remaining": len(paths) - visited, "needs_attention": failures}))
        require(failures == 0 and (visited or not paths), "Reconciliation requires attention")
        return None


def environment(name):
    value = os.environ.get(name)
    require(bool(value), f"Missing {name}")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "dispatch", "gate", "wait", "reconcile"))
    parser.add_argument("--config", default=".github/ci-bridge.json")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as file:
        config = decode(file.read())
    gate = args.command == "gate"
    core = Api(environment("CORE_READ_TOKEN" if gate else "CORE_TOKEN"))
    public = Api(environment("PUBLIC_TOKEN"))
    bridge = Bridge(config, core, public, environment("PUBLIC_CI_REVISION"))
    if args.command == "preflight":
        bridge.preflight()
    elif args.command == "reconcile":
        event = os.environ.get("GITHUB_EVENT_NAME")
        if event == "workflow_run":
            result = bridge.reconcile(integer(environment("COMPLETED_RUN_ID")),
                                      integer(environment("COMPLETED_RUN_ATTEMPT")))
            print(json.dumps(result))
        else:
            bridge.reconcile()
    elif gate:
        require(environment("INPUT_REQUEST_VERSION") == str(VERSION), "Unsupported request version")
        tested_sha = bridge.gate(integer(environment("INPUT_SOURCE_RUN_ID")),
                                 integer(environment("INPUT_SOURCE_ATTEMPT")),
                                 environment("INPUT_REQUEST_DIGEST"), environment("INPUT_CLAIM_NONCE"),
                                 integer(environment("GITHUB_RUN_ID")), integer(environment("GITHUB_RUN_ATTEMPT")),
                                 environment("GITHUB_SHA"))
        with open(environment("GITHUB_OUTPUT"), "a", encoding="utf-8") as output:
            output.write(f"tested_sha={tested_sha}\n")
    else:
        claim = getattr(bridge, args.command)(integer(environment("CORE_RUN_ID")), integer(environment("CORE_ATTEMPT")))
        print(json.dumps({"version": VERSION, "request_digest": claim["request_digest"],
                          "public_run_id": claim["public_run_id"], "decision": claim["decision"]}))


if __name__ == "__main__":
    try:
        main()
    except (Stopped, OSError, KeyError, TypeError, ValueError):
        # No API payload, private command output, token, or injected annotation.
        print("CI bridge stopped; inspect trusted control metadata.", file=sys.stderr)
        sys.exit(1)
