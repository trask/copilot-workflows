"""Durable original-bot reply and resolution effects, never mutation retries."""

import copy
import hashlib
import time
import uuid

from loop.api import APIError
from loop.freeze import threads
from loop.policy import bot, digest, loop_kind, require

RESOLVE = """mutation($thread:ID!, $claim:String!) {
  resolveReviewThread(input:{threadId:$thread, clientMutationId:$claim}) {
    clientMutationId thread { id isResolved }
  }
}"""


def conversation(comments, root):
    selected = [comment for comment in comments
                if comment["id"] == root or comment.get("in_reply_to_id") == root]
    return [{"id": comment["id"], "body_hash": hashlib.sha256(
        comment["body"].encode("utf-8")).hexdigest(),
        "author": {key: comment["user"][key] for key in ("id", "node_id", "type")},
        "updated_at": comment.get("updated_at"),
        "parent": comment.get("in_reply_to_id")} for comment in sorted(selected, key=lambda c: c["id"])]


def pending(state):
    return any(effect.get(kind, {}).get("status") in {"uncertain", "acknowledged"}
               for effect in state.get("effects", []) for kind in ("reply", "resolution"))


def initialize(state):
    require(loop_kind(state["request"]) == "copilot_review"
            and state["publication_intent"]["status"] == "confirmed",
            "Thread effects require confirmed external publication")
    decisions = {item["key"]: item for item in state["report"]["dispositions"]["findings"]}
    effects = []
    for finding in state["request"]["findings"]:
        if finding.get("kind") != "inline":
            continue
        require(bot(finding.get("root_author")) and finding.get("thread_context") is not None,
                "Missing frozen original bot conversation")
        decision = decisions[finding["key"]]
        require(decision["disposition"] in {"fixed", "not_warranted"}, "Blocked thread decision")
        effects.append({"key": finding["key"], "root": finding["comment_id"],
                        "thread": finding["thread_id"], "status": "pending"})
    return effects


def reply_body(state, effect):
    decision = next(item for item in state["report"]["dispositions"]["findings"]
                    if item["key"] == effect["key"])
    mapping = state["report"]["candidate"]["finding_commits"]
    lead = ("Addressed in " + mapping[effect["key"]] + "."
            if decision["disposition"] == "fixed" else "No code change.")
    return (lead + "\n\n" + decision["analysis"] +
            f"\n\n<!-- copilot-loop:{digest(state['request'])}:{effect['root']}:"
            f"{effect['reply']['claim']} -->")


def live_context(api, state, effect):
    request = state["request"]
    comments = api.pages(f"repos/{request['repo']}/pulls/{request['pr']}/comments")
    roots = threads(api, request["pr"], request["repo"])
    original = next(item for item in request["findings"] if item["key"] == effect["key"])
    matching = [item for item in comments if item["id"] == effect["root"]]
    require(len(matching) == 1, "Original root disappeared or is ambiguous")
    root = matching[0]
    require(bot(root.get("user")) and not root.get("in_reply_to_id")
            and root["body"] == original["body"] and root["path"] == original["path"]
            and root["original_commit_id"] == original["original_commit_id"]
            and root["pull_request_review_id"] == original["review_id"]
            and root.get("pull_request_url") ==
            f"https://api.github.com/repos/{request['repo']}/pulls/{request['pr']}"
            and effect["root"] in roots and roots[effect["root"]]["id"] == effect["thread"],
            "Original thread/root identity changed")
    context = conversation(comments, effect["root"])
    confirmed = effect.get("reply", {}).get("reply_id")
    baseline = original["thread_context"]
    known = {item["id"]: item for item in baseline}
    if confirmed and observed_reply(api, state, effect) != {
            "reply_id": confirmed, "body_hash": effect["reply"]["body_hash"],
            "actor_id": request["authorized_actor_id"]}:
        return roots[effect["root"]], comments, "confirmed_thread_reply_changed_or_missing"
    for item in context:
        if item["id"] == confirmed:
            continue
        if item["id"] not in known or item != known[item["id"]] or not bot(item["author"]):
            return roots[effect["root"]], comments, "thread_conversation_changed_or_human_intervention"
    if {item["id"] for item in context if item["id"] != confirmed} != set(known):
        return roots[effect["root"]], comments, "thread_conversation_changed_or_human_intervention"
    return roots[effect["root"]], comments, None


def observed_reply(api, state, effect):
    request = state["request"]
    body = reply_body(state, effect)
    comments = api.pages(f"repos/{request['repo']}/pulls/{request['pr']}/comments")
    matches = [item for item in comments if item.get("in_reply_to_id") == effect["root"]
               and item["body"] == body and item["user"]["id"] == request["authorized_actor_id"]
               and item["user"]["type"] == "User" and item.get("pull_request_url") ==
               f"https://api.github.com/repos/{request['repo']}/pulls/{request['pr']}"]
    require(len(matches) <= 1, "Ambiguous matching thread replies")
    if not matches:
        return None
    return {"reply_id": matches[0]["id"], "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "actor_id": matches[0]["user"]["id"]}


def advance(store, name, state, central, read, publisher, now):
    from loop.control import owned
    from loop.live import cas, guard, owner
    owned(state)
    guard(store, name, state, read, now)
    effect_index = next((index for index, effect in enumerate(state["effects"])
                         if effect["status"] not in {"confirmed", "skipped"}), None)
    if effect_index is None:
        return cas(store, name, state, stage="threads_settled", next_check_at=now)
    effects = copy.deepcopy(state["effects"])
    effect = effects[effect_index]
    request = state["request"]
    for slot in ("reply", "resolution"):
        intent = effect.get(slot)
        if intent:
            require(intent["request_digest"] == digest(request)
                    and intent["generation"] == state["generation"]
                    and intent["head"] == state["expected_sha"]
                    and intent["root"] == effect["root"] and intent["thread"] == effect["thread"],
                    "Thread intent binding changed")
    publisher.identity(dict(request, frozen_sha=state["expected_sha"]))
    if effect.get("reply", {}).get("status") in {"uncertain", "acknowledged"}:
        confirmation = observed_reply(read, state, effect)
        if confirmation is None:
            return cas(store, name, state, stage="blocked", reason="thread_reply_uncertain_no_retry",
                       next_check_at=now)
        owned(state)
        guard(store, name, state, read, int(time.time()))
        effect["reply"].update(status="confirmed", confirmed_at=now, **confirmation)
        effect["status"] = "pending"
        return cas(store, name, state, effects=effects, next_check_at=now)
    thread, _, changed = live_context(read, state, effect)
    if effect.get("resolution", {}).get("status") in {"uncertain", "acknowledged"}:
        if changed or effect["resolution"]["status"] != "acknowledged" or not thread["resolved"]:
            return cas(store, name, state, stage="blocked", reason="thread_resolution_uncertain_no_retry",
                       next_check_at=now)
        owned(state)
        guard(store, name, state, read, int(time.time()))
        effect["resolution"].update(status="confirmed", confirmed_at=now)
        effect["status"] = "confirmed"
        return cas(store, name, state, effects=effects, next_check_at=now)
    if changed:
        effect.update(status="skipped", reason=changed)
        return cas(store, name, state, effects=effects, next_check_at=now)
    if thread["resolved"]:
        effect.update(status="skipped", reason="thread_already_settled")
        return cas(store, name, state, effects=effects, next_check_at=now)
    kind = "resolve" if effect.get("reply", {}).get("status") == "confirmed" else "reply"
    slot = "resolution" if kind == "resolve" else "reply"
    intent = {"kind": kind, "claim": uuid.uuid4().hex, "root": effect["root"],
              "thread": effect["thread"], "status": "uncertain", "owner": owner(),
              "request_digest": digest(request), "generation": state["generation"],
              "head": state["expected_sha"], "recorded_at": now}
    effect[slot] = intent
    effect["status"] = "uncertain"
    if kind == "reply":
        intent["body_hash"] = hashlib.sha256(reply_body(state, effect).encode()).hexdigest()
    state = cas(store, name, state, effects=effects, next_check_at=now)
    owned(state)
    guard(store, name, state, read, int(time.time()))
    publisher.identity(dict(request, frozen_sha=state["expected_sha"]))
    thread, _, changed = live_context(read, state, effect)
    require(not changed and not thread["resolved"],
            "Thread changed before mutation; preserve its intent")
    bound = dict(intent)
    if kind == "reply":
        bound["body"] = reply_body(state, effect)
        require(hashlib.sha256(bound["body"].encode()).hexdigest() == intent["body_hash"],
                "Reply body binding changed")
    publisher.bind_effect(bound)
    try:
        if kind == "reply":
            publisher.call(f"repos/{request['repo']}/pulls/{request['pr']}/comments/{effect['root']}/replies",
                           "POST", {"body": bound["body"]})
            confirmation = observed_reply(read, state, effect)
            require(confirmation is not None, "Reply response could not be independently confirmed")
            owned(state)
            guard(store, name, state, read, int(time.time()))
            intent.update(status="confirmed", confirmed_at=int(time.time()), **confirmation)
            effect["status"] = "pending"
        else:
            response = publisher.call("graphql", "POST", {
                "query": RESOLVE, "variables": {"thread": effect["thread"], "claim": intent["claim"]}})
            require(not response.get("errors") and response["data"]["resolveReviewThread"][
                "clientMutationId"] == intent["claim"] and response["data"]["resolveReviewThread"][
                "thread"] == {"id": effect["thread"], "isResolved": True},
                "Thread resolution response differs")
            intent.update(status="acknowledged", acknowledged_at=int(time.time()))
            state = cas(store, name, state, effects=effects, next_check_at=int(time.time()))
            thread, _, changed = live_context(read, state, effect)
            require(not changed and thread["resolved"], "Thread resolution is not confirmed")
            owned(state)
            guard(store, name, state, read, int(time.time()))
            intent.update(status="confirmed", confirmed_at=int(time.time()))
            effect["status"] = "confirmed"
    except APIError as error:
        if error.status in {401, 403, 404, 422}:
            intent.update(status="failed", http_status=error.status)
            effect.update(status="failed", reason="publisher_permission_rejected")
            cas(store, name, state, effects=effects, stage="blocked",
                reason="publisher_permission_rejected", error=str(error), next_check_at=int(time.time()))
        raise
    finally:
        publisher.bind_effect(None)
    return cas(store, name, state, effects=effects, next_check_at=int(time.time()))
