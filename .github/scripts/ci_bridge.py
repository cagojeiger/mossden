#!/usr/bin/env python3
"""Single-SHA GitHub CI bridge. No private application checkout on Home."""

import argparse
import base64
import hashlib
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
CLAIM_FIELDS = {"version", "request", "request_digest", "nonce", "claimed_at", "deadline", "public_run_id"}


def digest(request):
    return hashlib.sha256(compact({key: request[key] for key in REQUEST_FIELDS})).hexdigest()


def title(claim):
    return f"bridge:{claim['request_digest']}:{claim['nonce']}"


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
            except (URLError, TimeoutError, OSError):
                if method != "GET" or attempt == 2:
                    raise ApiFailure() from None
            self.pause(min(1 + attempt, max(0, self.deadline - self.clock())))
        raise ApiFailure()


class Bridge:
    def __init__(self, config, core, public, revision, clock=time.time, pause=time.sleep):
        self.config, self.core, self.public = config, core, public
        self.revision, self.clock, self.pause = revision, clock, pause
        require(config.get("version") == 1 and sha(revision), "Unconfigured trusted public revision")
        require(positive(config.get("dispatch_actor_id")), "Dispatch App actor ID is not configured")
        require(config.get("required_jobs") == ["Rust"] and config.get("deadline_seconds") == 3600,
                "Only the single Rust job and one-hour source deadline are supported")
        self.source = "repos/" + config["source_repository"]
        self.target = "repos/" + config["public_repository"]

    def workflow(self, public=False):
        api, repository = (self.public, self.target) if public else (self.core, self.source)
        filename = self.config["public_workflow" if public else "source_workflow"]
        result = api.request(f"{repository}/actions/workflows/{quote(filename, safe='')}")
        require(positive(result.get("id")) and result.get("path") == ".github/workflows/" + filename
                and result.get("state") == "active", "Unexpected workflow definition")
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

    def source_request(self, run_id, attempt, active=True, revision=None):
        workflow_id = self.workflow()
        latest = self.core.request(f"{self.source}/actions/runs/{run_id}")
        run = self.core.request(f"{self.source}/actions/runs/{run_id}/attempts/{attempt}")
        require(positive(run_id) and positive(attempt) and run.get("id") == run_id
                and run.get("run_attempt") == attempt and run.get("workflow_id") == workflow_id
                and run.get("path") == ".github/workflows/" + self.config["source_workflow"]
                and run.get("repository", {}).get("id") == self.config["source_repository_id"]
                and run.get("head_repository", {}).get("id") == self.config["source_repository_id"]
                and run.get("head_branch") == "main" and run.get("event") in ("push", "workflow_dispatch")
                and sha(run.get("head_sha")), "Unapproved source run")
        if active:
            require(latest.get("id") == run_id and latest.get("run_attempt") == attempt
                    and latest.get("status") == "in_progress" and run.get("status") == "in_progress",
                    "Source run or attempt is no longer active")
        created = timestamp(run.get("created_at"))
        require(created <= self.clock(), "Source creation time is in the future")
        request = dict(zip(REQUEST_FIELDS, (
            1, self.config["source_repository_id"], workflow_id, run_id, attempt, run["head_sha"],
            self.config["public_repository_id"], self.config["public_workflow"],
            revision or self.revision, created + self.config["deadline_seconds"],
        )))
        if active:
            self.before_deadline(request)
        return request

    def active(self, request):
        actual = self.source_request(request["source_run_id"], request["source_attempt"], revision=request["public_revision"])
        require(actual == request, "Source request metadata changed")
        self.before_deadline(request)

    def path(self, request):
        return (f"claims/{request['source_repository_id']}/{request['source_workflow_id']}/"
                f"{request['source_run_id']}.{request['source_attempt']}.json")

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
        return decode(raw), entry["sha"]

    def validate_claim(self, claim, expected=None):
        require(isinstance(claim, dict) and set(claim) == CLAIM_FIELDS and claim["version"] == 1,
                "Invalid claim schema")
        request = claim["request"]
        require(isinstance(request, dict) and set(request) == set(REQUEST_FIELDS)
                and request["version"] == 1 and all(positive(request[key]) for key in (
                    "source_repository_id", "source_workflow_id", "source_run_id", "source_attempt",
                    "public_repository_id", "deadline"))
                and sha(request["tested_sha"]) and sha(request["public_revision"])
                and request["source_repository_id"] == self.config["source_repository_id"]
                and request["public_repository_id"] == self.config["public_repository_id"]
                and request["public_workflow"] == self.config["public_workflow"], "Invalid claim request")
        require(expected is None or request == expected, "Claim belongs to a different request")
        require(claim["request_digest"] == digest(request)
                and isinstance(claim["nonce"], str) and re.fullmatch(r"[0-9a-f]{32}", claim["nonce"])
                and type(claim["claimed_at"]) is int
                and request["deadline"] - 3600 <= claim["claimed_at"] < request["deadline"]
                and claim["deadline"] == request["deadline"]
                and (claim["public_run_id"] is None or positive(claim["public_run_id"])), "Invalid claim evidence")
        return claim

    def claim(self, request):
        entry = self.read_file(self.path(request))
        if entry is not None:
            self.validate_claim(entry[0], request)
        return entry

    def put_file(self, path, value, blob_sha=None):
        data = {"message": "ci bridge state", "branch": self.config["state_branch"],
                "content": base64.b64encode(compact(value)).decode()}
        if blob_sha is not None:
            data["sha"] = blob_sha
        result = self.core.request(self.contents(path), data, method="PUT")
        require(isinstance(result, dict) and sha(result.get("content", {}).get("sha")), "State write was not acknowledged")
        return result["content"]["sha"]

    def acquire(self, request):
        self.active(request)
        existing = self.claim(request)
        if existing is not None:
            return existing, False
        value = {"version": 1, "request": request, "request_digest": digest(request),
                 "nonce": secrets.token_hex(16), "claimed_at": int(self.clock()),
                 "deadline": request["deadline"], "public_run_id": None}
        # Only this acknowledged create grants one ephemeral POST permission.
        # An uncertain write, existing file, restart, or rerun never recreates it.
        try:
            blob_sha = self.put_file(self.path(request), value)
        except Stopped:
            raise Stopped("Claim create was not acknowledged; no dispatch is permitted") from None
        require(self.claim(request) == (value, blob_sha), "Claim changed before dispatch")
        return (value, blob_sha), True

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
                and run.get("triggering_actor", {}).get("id") == self.config["dispatch_actor_id"],
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
        value, blob_sha = entry
        self.validate_claim(value, request)
        self.active(request)
        self.public_run(value, run_id)
        if value["public_run_id"] is not None:
            require(value["public_run_id"] == run_id, "Binding is immutable")
            return entry
        updated = {**value, "public_run_id": run_id}
        try:
            new_sha = self.put_file(self.path(request), updated, blob_sha)
            return updated, new_sha
        except Stopped:
            observed = self.claim(request)
            require(observed is not None and observed[0] == updated, "Binding update is uncertain or conflicting")
            return observed

    def observe_binding(self, request, seconds=150):
        end = min(request["deadline"], self.clock() + seconds)
        while self.clock() < end:
            self.active(request)
            entry = self.claim(request)
            require(entry is not None, "Claim disappeared")
            claim = entry[0]
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
            self.before_deadline(request)
            claim = entry[0]
            data = {"ref": "main", "inputs": {
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
            require(isinstance(request_digest, str) and isinstance(nonce, str)
                    and secrets.compare_digest(claim["request_digest"], request_digest)
                    and secrets.compare_digest(claim["nonce"], nonce), "Dispatch input does not match the claim")
            self.public_run(claim, public_run_id)
            if claim["public_run_id"] is not None:
                require(claim["public_run_id"] == public_run_id, "This public run is not bound")
                self.active(request)
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
        current_claim = self.claim(claim["request"])
        require(current_claim is not None and current_claim[0] == claim, "Binding changed before result acceptance")
        self.active(claim["request"])
        self.before_deadline(claim["request"])

    def wait(self, run_id, attempt):
        request = self.source_request(run_id, attempt)
        self.core.deadline = self.public.deadline = request["deadline"]
        while self.clock() < request["deadline"]:
            self.active(request)
            entry = self.observe_binding(request)
            claim = entry[0]
            run = self.public_run(claim, claim["public_run_id"])
            if run.get("status") == "completed":
                self.verify_results(claim)
                return claim
            self.pause(min(10, max(0, request["deadline"] - self.clock())))
        raise Stopped("Original source deadline elapsed")

    def terminal_receipt(self, claim, run):
        require(run.get("status") == "completed", "Cancellation acknowledgement is not termination")
        path = "settled/" + self.path(claim["request"]).removeprefix("claims/").removesuffix(".json") + f"/{run['id']}.json"
        value = {"version": 1, "request_digest": claim["request_digest"], "public_run_id": run["id"],
                 "conclusion": run.get("conclusion"), "observed_at": int(self.clock())}
        if self.read_file(path) is None:
            self.put_file(path, value)

    def reconcile_claim(self, path):
        entry = self.read_file(path)
        if entry is None:
            return
        claim = self.validate_claim(entry[0])
        request = claim["request"]
        require(path == self.path(request), "Claim path mismatch")
        active = True
        try:
            self.active(request)
        except Stopped:
            active = False
        # Cleanup observes delivery after the acceptance deadline too. Its
        # provenance must match, but expired delivery can never be admitted.
        runs = self.candidates(claim, admission=False)
        if active and claim["public_run_id"] is None and len(runs) == 1:
            claim = self.bind(request, entry, runs[0]["id"])[0]
        if claim["public_run_id"] is not None and all(run["id"] != claim["public_run_id"] for run in runs):
            runs.append(self.public_run(claim, claim["public_run_id"], admission=False))
        for run in runs:
            duplicate = claim["public_run_id"] is not None and run["id"] != claim["public_run_id"]
            if run.get("status") == "completed":
                # A receipt is observed terminal evidence for THIS run only.
                # It never suppresses future discovery of late/duplicate delivery.
                self.terminal_receipt(claim, run)
            elif not active or duplicate or len(runs) > 1:
                try:
                    self.public.request(f"{self.target}/actions/runs/{run['id']}/cancel", {}, method="POST")
                except ApiFailure as error:
                    if error.status != 409:
                        raise

    def reconcile(self):
        self.repository()
        self.repository(public=True)
        branch = self.core.request(self.source + "/branches/" + quote(self.config["state_branch"], safe=""))
        tree_sha = branch.get("commit", {}).get("commit", {}).get("tree", {}).get("sha")
        require(sha(tree_sha), "Invalid state branch tree")
        tree = self.core.request(f"{self.source}/git/trees/{tree_sha}?recursive=1")
        require(tree.get("truncated") is False and isinstance(tree.get("tree"), list), "Incomplete claim tree")
        paths = [entry["path"] for entry in tree["tree"] if entry.get("type") == "blob"
                 and entry.get("path", "").startswith("claims/") and entry["path"].endswith(".json")]
        require(len(paths) <= 1000, "Claim history requires an explicit retention design")
        failures = 0
        for path in paths:
            try:
                self.reconcile_claim(path)
            except Stopped:
                failures += 1
        require(failures == 0, f"Reconciliation requires attention for {failures} claim(s)")


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
        bridge.reconcile()
    elif gate:
        tested_sha = bridge.gate(integer(environment("INPUT_SOURCE_RUN_ID")),
                                 integer(environment("INPUT_SOURCE_ATTEMPT")),
                                 environment("INPUT_REQUEST_DIGEST"), environment("INPUT_CLAIM_NONCE"),
                                 integer(environment("GITHUB_RUN_ID")), integer(environment("GITHUB_RUN_ATTEMPT")),
                                 environment("GITHUB_SHA"))
        with open(environment("GITHUB_OUTPUT"), "a", encoding="utf-8") as output:
            output.write(f"tested_sha={tested_sha}\n")
    else:
        claim = getattr(bridge, args.command)(integer(environment("CORE_RUN_ID")), integer(environment("CORE_ATTEMPT")))
        print(json.dumps({"request_digest": claim["request_digest"], "public_run_id": claim["public_run_id"]}))


if __name__ == "__main__":
    try:
        main()
    except (Stopped, OSError, KeyError, TypeError, ValueError):
        # No API payload, private command output, token, or injected annotation.
        print("CI bridge stopped; inspect trusted control metadata.", file=sys.stderr)
        sys.exit(1)
