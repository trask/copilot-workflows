"""Data-only branch with non-force, parent-bound compare-and-swap updates."""

import base64
import copy
import hashlib
import json
import random
import re
import time

from loop.api import APIError, MAX_RESPONSE
from loop.policy import CENTRAL, STATE_BRANCH, Rejected, canonical, checkpoint_name, require

PREFIX = f"repos/{CENTRAL}/git/"


def blob_oid(payload):
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()


class Conflict(RuntimeError):
    pass


class State:
    def __init__(self, api):
        self.api = api
        self.blobs = {}
        self.cached = None
        self.written_head = None

    def snapshot(self):
        for attempt in range(5):
            try:
                ref = self.api.call(PREFIX + "ref/heads/" + STATE_BRANCH)
            except APIError as error:
                if error.status != 404:
                    raise
                if self.written_head is None:
                    return None, None, {}
            else:
                head = ref["object"]["sha"]
                if self.written_head is None or head == self.written_head:
                    break
                comparison = self.api.call(
                    f"repos/{CENTRAL}/compare/{self.written_head}...{head}?per_page=1")
                require(comparison["base_commit"]["sha"] == self.written_head,
                        "State comparison identity differs")
                if comparison["status"] == "ahead":
                    require(comparison["merge_base_commit"]["sha"] == self.written_head,
                            "State ref does not descend from the acknowledged write")
                    break
                require(comparison["status"] == "behind"
                        and comparison["merge_base_commit"]["sha"] == head,
                        "State ref diverged from the acknowledged write")
            require(attempt < 4, "Acknowledged state write is not yet visible")
            print("[state] Waiting for the acknowledged state write to become visible")
            time.sleep(attempt + 1)
        if self.cached is not None and self.cached[0] == head:
            return head, self.cached[1], copy.deepcopy(self.cached[2])
        commit = self.api.call(PREFIX + "commits/" + head)
        tree = self.api.call(PREFIX + "trees/" + commit["tree"]["sha"])
        require(not tree["truncated"], "State tree is incomplete")
        entries = {}
        blobs = {}
        missing = []
        for entry in tree["tree"]:
            require(entry["type"] == "blob" and entry["mode"] == "100644"
                    and entry["path"].endswith(".json") and "/" not in entry["path"],
                    "State branch must contain JSON data only")
            cached = self.blobs.get(entry["path"])
            if cached is None or cached[0] != entry["sha"]:
                missing.append(entry)
            else:
                blobs[entry["path"]] = cached
        batches = []
        size = 0
        for entry in missing:
            # Reserve response space for JSON escaping and GraphQL metadata.
            if not batches or len(batches[-1]) == 100 or size + entry.get("size", 0) > MAX_RESPONSE // 8:
                batches.append([])
                size = 0
            batches[-1].append(entry)
            size += entry.get("size", 0)
        for group in batches:
            variables = dict(zip(("owner", "repo"), CENTRAL.split("/")))
            variables.update({f"blob{i}": entry["sha"] for i, entry in enumerate(group)})
            arguments = ", ".join(f"$blob{i}: GitObjectID!" for i in range(len(group)))
            fields = " ".join(
                f"blob{i}: object(oid: $blob{i}) {{ ... on Blob {{ oid text isTruncated }} }}"
                for i in range(len(group)))
            batch = self.api.graphql(
                "query($owner: String!, $repo: String!, " + arguments + ") { "
                "repository(owner: $owner, name: $repo) { " + fields + " } }",
                variables)["repository"]
            for i, entry in enumerate(group):
                blob = batch[f"blob{i}"]
                require(isinstance(blob, dict) and blob.get("oid") == entry["sha"]
                        and type(blob.get("isTruncated")) is bool,
                        "Batched checkpoint blob identity or completeness differs")
                require(isinstance(blob.get("text"), str), "Checkpoint blob is not text")
                payload = blob["text"].encode("utf-8")
                if blob["isTruncated"] or blob_oid(payload) != entry["sha"]:
                    print("[state] Reading complete Git blob; GraphQL text is truncated or differs")
                    complete = self.api.call(PREFIX + "blobs/" + entry["sha"])
                    payload = base64.b64decode(complete["content"])
                require(blob_oid(payload) == entry["sha"], "Checkpoint blob bytes differ from Git identity")
                blobs[entry["path"]] = (entry["sha"], json.loads(payload))
        for entry in tree["tree"]:
            cached = blobs[entry["path"]]
            entries[entry["path"]] = copy.deepcopy(cached[1])
        self.blobs = blobs
        self.cached = (head, commit["tree"]["sha"], copy.deepcopy(entries))
        return head, commit["tree"]["sha"], entries

    def write(self, name, value, expected):
        require(re.fullmatch(r"pr-v2-(?:[1-9][0-9]{0,19}|[0-9a-f]{32})-[1-9][0-9]{0,7}\.json", name)
                and value["schema"] == value["request"]["schema"] == 2
                and name == checkpoint_name(value["request"]["repo"], value["request"]["pr"],
                                            value["request"].get("repo_id")),
                "Unsafe checkpoint namespace")
        head, tree, entries = self.snapshot()
        if entries.get(name) != expected:
            raise Conflict("Checkpoint changed concurrently")
        pending = {name: canonical(value)}
        if expected and expected["request"]["request_id"] != value["request"]["request_id"]:
            archive_name = "request-" + expected["request"]["request_id"] + ".json"
            require(re.fullmatch(r"request-[0-9a-f]{32}\.json", archive_name), "Unsafe archive name")
            require(archive_name not in entries or entries[archive_name] == expected,
                    "Existing archived provenance differs")
            require("request-" + value["request"]["request_id"] + ".json" not in entries,
                    "New request identity was previously archived")
            pending[archive_name] = canonical(expected)
        reconciliation = value.get("reconciliation")
        if expected and reconciliation and not expected.get("reconciliation"):
            archive_name = f"stopped-{expected['request']['request_id']}-{expected['generation']}.json"
            require(expected["stage"] == "blocked"
                    and expected["reason"] == "blocked_uncertain_publication_requires_operator"
                    and value["request"] == expected["request"]
                    and value["generation"] == expected["generation"]
                    and reconciliation["stopped_archive"] == archive_name,
                    "Invalid stopped publication evidence archive")
            require(archive_name not in entries or entries[archive_name] == expected,
                    "Existing stopped publication evidence differs")
            pending[archive_name] = canonical(expected)
        body = {"tree": []}
        for path, payload in pending.items():
            blob = self.api.call(PREFIX + "blobs", "POST",
                                 {"content": payload.decode(), "encoding": "utf-8"})
            body["tree"].append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if tree is not None:
            body["base_tree"] = tree
        new_tree = self.api.call(PREFIX + "trees", "POST", body)
        commit = self.api.call(PREFIX + "commits", "POST", {
            "message": "Update review loop checkpoint", "tree": new_tree["sha"],
            "parents": [] if head is None else [head],
        })
        try:
            if head is None:
                self.api.call(PREFIX + "refs", "POST",
                              {"ref": "refs/heads/" + STATE_BRANCH, "sha": commit["sha"]})
            else:
                # A competing child of head is not an ancestor of this commit. GitHub rejects
                # the non-fast-forward update, preserving every other PR checkpoint.
                self.api.call(PREFIX + "refs/heads/" + STATE_BRANCH, "PATCH",
                              {"sha": commit["sha"], "force": False})
        except APIError as error:
            if error.status in {409, 422}:
                raise Conflict("State ref transaction lost a race") from error
            raise
        self.written_head = commit["sha"]

    def update(self, name, operation):
        for attempt in range(20):
            _, _, entries = self.snapshot()
            before = entries.get(name)
            after = operation(copy.deepcopy(before))
            if after == before:
                return after
            try:
                self.write(name, after, before)
                return after
            except Conflict:
                if attempt < 19:
                    time.sleep(random.uniform(0, min(1, 0.05 * 2 ** attempt)))
                continue
        raise Conflict("State retry budget exhausted")
