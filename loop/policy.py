"""Trusted policy and small, exact wire formats."""

import hashlib
import json
import re
from datetime import datetime, timezone

CENTRAL = "trask/copilot-workflows"
AUTHOR_ID = 218610
BOT_ID = 175728472
BOT_NODE = "BOT_kgDOCnlnWA"
BOT_IDENTITY_PATH = "users/copilot-pull-request-reviewer%5Bbot%5D"
STATE_BRANCH = "review-loop-state"
WORKER = "copilot-worker.lock.yml"
WORKER_PATH = ".github/workflows/" + WORKER
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUEST = re.compile(r"[0-9a-f]{32}\Z")
TERMINAL = {"preview_complete", "shadow_complete", "blocked", "failed", "cancelled", "exhausted", "clean", "complete"}
DEFAULTS = {
    "max_iterations": 5,
    "deadline_seconds": 7200,
    "worker_timeout_minutes": 30,
    "retention_days": 14,
    "propagation_seconds": 120,
}
MAX_ACTIVATION_DISPATCHES = 2
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
ACCOUNT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
PROFILE = "reviewable-v1"
LOOP_KINDS = {"copilot_review", "self_review", "pr_conflict_resolver", "ci_fix",
              "pr_description", "pr_simplify", "pr_review", "pr_consistency"}
REPORT_KINDS = {"pr_description", "pr_review"}
SINGLE_PASS = LOOP_KINDS - {"copilot_review", "self_review", "ci_fix"}


def diff_scope(request):
    return loop_kind(request) not in {"copilot_review", "pr_description"}


def source_effect(request):
    return loop_kind(request) not in REPORT_KINDS


def effect_repository(request, outcome=None):
    if loop_kind(request) == "ci_fix" and outcome == "rerun":
        return request["repo"]
    return request["head_repo"] if source_effect(request) else request["repo"]


class Rejected(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


def commit_author(request):
    author = request.get("commit_author")
    require(isinstance(author, dict) and set(author) == {"id", "login"}
            and type(author["id"]) is int and author["id"] > 0
            and type(request.get("authorized_actor_id")) is int
            and author["id"] == request["authorized_actor_id"]
            and isinstance(author["login"], str) and ACCOUNT.fullmatch(author["login"]),
            "Missing or invalid frozen GitHub commit author")
    return "Trask Stalnaker", f"{author['id']}+{author['login']}@users.noreply.github.com"


def loop_kind(request):
    kind = request.get("loop_kind", "copilot_review")
    require(isinstance(kind, str) and kind in LOOP_KINDS, "Unknown frozen loop kind")
    return kind


def staged_source(request):
    public_request(request)
    return diff_scope(request) and not direct_inputs(request)


def direct_inputs(request):
    return isinstance(request, dict) and request.get("input_mode") == "direct"


def public_request(request):
    require(request.get("source_private") is False and request.get("target_private") is False,
            "Only public base and head repositories are supported")
    require(all(isinstance(request.get(key), str) and request[key].casefold() != CENTRAL.casefold()
                for key in ("repo", "head_repo")),
            "Central automation repository cannot be a target or head repository")


def pipeline_limit(request):
    from loop.revisions import workflow_ref
    workflow_ref(request)
    if request.get("freeze_status") == "not_frozen":
        require(request.get("repo_id") is None and request.get("frozen_sha") is None,
                "An unfrozen gate cannot contain source identity")
    else:
        public_request(request)
    kind = loop_kind(request)
    if not direct_inputs(request) and kind == "pr_description" and request.get("freeze_status") != "not_frozen":
        from loop.recommendations import description_diff, diff_anchors
        evidence = request.get("pr_diff")
        if isinstance(evidence, dict) and "anchors" in evidence:
            exact(evidence, {"text", "sha256", "anchors"})
            require(hashlib.sha256(evidence["text"].encode("utf-8")).hexdigest() == evidence["sha256"]
                    and diff_anchors(evidence["text"])[0] == evidence["anchors"],
                    "Incomplete authoritative PR diff binding")
        else:
            exact(evidence, {"text", "sha256"})
            require(evidence == description_diff(evidence["text"]),
                    "Description diff binding differs")
    if diff_scope(request) and request.get("freeze_status") != "not_frozen":
        require(request.get("schema") == 2
                and isinstance(request.get("base_ref"), str) and safe_ref(request["base_ref"])
                and all(isinstance(request.get(key), str) and SHA.fullmatch(request[key])
                        for key in ("base_sha", "merge_base_sha")),
                "Incomplete frozen self-review scope")
        if not direct_inputs(request) and loop_kind(request) != "self_review":
            from loop.recommendations import diff_anchors
            evidence = request.get("pr_diff")
            exact(evidence, {"text", "sha256", "anchors"})
            require(hashlib.sha256(evidence["text"].encode("utf-8")).hexdigest() == evidence["sha256"]
                    and diff_anchors(evidence["text"])[0] == evidence["anchors"],
                    "Incomplete authoritative PR diff binding")
    budgets = request.get("budgets")
    require(isinstance(budgets, dict) and set(budgets) == set(DEFAULTS),
            "Missing or malformed frozen request budgets")
    maximum = budgets["max_iterations"]
    require(type(maximum) is int and maximum == DEFAULTS["max_iterations"]
            and all(type(budgets[key]) is int and budgets[key] == value
                    for key, value in DEFAULTS.items() if key != "max_iterations"),
            "Unsupported frozen request budgets")
    require(type(request.get("frozen_at")) is int and type(request.get("deadline")) is int
            and request["frozen_at"] < request["deadline"]
            <= request["frozen_at"] + budgets["deadline_seconds"],
            "Invalid frozen request deadline")
    if request.get("mode") == "publish":
        publication = request.get("publication")
        require(isinstance(publication, dict)
                and "reply_bot_threads" not in publication
                and "reviewable_retry" not in publication,
                "Retired publication features are read-only")
        require(type(publication.get("max_pipelines")) is int
                and publication["max_pipelines"] == maximum,
                "Inconsistent frozen publication pipeline budget")
        require(type(publication.get("authorized_at")) is int
                and type(publication.get("continuation_deadline")) is int
                and request["deadline"] <= publication["continuation_deadline"]
                <= publication["authorized_at"] + budgets["deadline_seconds"],
                "Invalid frozen publication deadline")
    return maximum


def supported_checkpoint(state):
    from loop.candidates import current_request
    current_request(state["request"])
    if state["request"].get("freeze_status") == "not_frozen":
        require(state["request"].get("repo_id") is None
                and state["request"].get("frozen_sha") is None,
                "An unfrozen gate cannot contain source identity")
    else:
        public_request(state["request"])
    publication = state["request"].get("publication")
    require(state.get("stage") not in {"auth_pending", "capability_intent", "waiting_capability",
                                      "capability_recheck", "root_effects"}
            and not state.get("capability") and not state.get("capability_probe")
            and (state["request"].get("mode") != "publish"
                 or isinstance(publication, dict)
                 and publication.get("profile") == PROFILE
                 and "reply_bot_threads" not in publication
                 and "reviewable_retry" not in publication),
            "Retired publication checkpoints are read-only")
    effects = state.get("effects", [])
    require(isinstance(effects, list)
            and all(isinstance(effect, dict) and effect.get("status") in {
                "pending", "confirmed", "skipped", "failed", "uncertain"}
                and isinstance(effect.get("key"), str) and type(effect.get("root")) is int
                and effect["root"] > 0 and isinstance(effect.get("thread"), str)
                for effect in effects), "Malformed current thread effects")
    require(not effects or loop_kind(state["request"]) == "copilot_review",
            "Self-review cannot contain thread effects")
    require(len({effect["key"] for effect in effects}) == len(effects)
            and len({effect["root"] for effect in effects}) == len(effects),
            "Duplicate current thread effects")


def pipeline_budget(state):
    supported_checkpoint(state)
    maximum = pipeline_limit(state["request"])
    if loop_kind(state["request"]) in SINGLE_PASS:
        maximum = 1
    count = state.get("iteration")
    require(type(count) is int and 0 <= count <= maximum,
            "Invalid consumed model pipeline count")
    require(count > 0 or not state.get("intent") and not state.get("run"),
            "Worker evidence without a consumed pipeline")
    if state["request"].get("mode") == "publish":
        publications = state.get("publications")
        require(isinstance(publications, list) and len(publications) <= count,
                "Publication evidence exceeds consumed pipelines")
    return maximum


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    if direct_inputs(value):
        value = {key: item for key, item in value.items() if key != "inputs"}
    return hashlib.sha256(canonical(value)).hexdigest()


def exact(obj, fields):
    require(isinstance(obj, dict) and set(obj) == set(fields), "Unexpected schema keys")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "Timestamp must include a timezone")
    return int(result.timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_target(value):
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]{0,7})", value)
    if match is None:
        match = re.fullmatch(r"([^/]+/[^/#]+)#([1-9][0-9]{0,7})", value)
    require(match is not None and REPO.fullmatch(match[1])
            and all(part not in {".", ".."} for part in match[1].split("/")),
            "Use an explicit owner/repo#number or github.com PR URL")
    return match[1], int(match[2])


def checkpoint_name(repo, number, repo_id=None):
    require(REPO.fullmatch(repo) and type(number) is int and 0 < number < 100000000,
            "Invalid checkpoint target")
    if repo_id is not None:
        require(type(repo_id) is int and 0 < repo_id < 10 ** 20, "Invalid frozen repository ID")
        return f"pr-v2-{repo_id}-{number}.json"
    return f"pr-v2-{hashlib.sha256(repo.casefold().encode()).hexdigest()[:32]}-{number}.json"


def bot(user):
    return (
        isinstance(user, dict)
        and user.get("id") == BOT_ID
        and user.get("node_id") == BOT_NODE
        and user.get("type") == "Bot"
    )


def safe_ref(ref):
    return (isinstance(ref, str) and 0 < len(ref) <= 200
            and re.fullmatch(r"[A-Za-z0-9_./-]+", ref)
            and not any(x in ref for x in ("..", "//", "@{"))
            and not ref.startswith(("-", "/", "."))
            and not ref.endswith(("/", ".", ".lock")))


def attributed_owner(api, pr, repo, actor_id, owner=None):
    if pr["user"]["type"] != "Bot":
        return None
    owner = api.call(f"user/{actor_id}") if owner is None else owner
    require(type(owner.get("id")) is int and owner["id"] == actor_id
            and isinstance(owner.get("login"), str) and ACCOUNT.fullmatch(owner["login"]),
            "Invalid GitHub PR owner identity")
    result = api.graphql("""
query($owner:String!, $name:String!, $searchQuery:String!) {
  repository(owner:$owner, name:$name) { nameWithOwner }
  search(query:$searchQuery, type:ISSUE, first:100) {
    pageInfo { hasNextPage }
    nodes { ... on PullRequest { number repository { nameWithOwner } } }
  }
}
""", {"owner": repo.split("/")[0], "name": repo.split("/")[1],
      "searchQuery": f"repo:{repo} is:pr is:open author:{owner['login']} {pr['number']}"})
    search = result.get("search", {})
    require(isinstance(result.get("repository"), dict)
            and result["repository"].get("nameWithOwner", "").casefold() == repo.casefold()
            and isinstance(search, dict)
            and isinstance(search.get("nodes"), list)
            and search.get("pageInfo", {}).get("hasNextPage") is False
            and all(isinstance(item, dict) and type(item.get("number")) is int
                    and isinstance(item.get("repository"), dict)
                    for item in search["nodes"]),
            "GitHub PR ownership search is incomplete")
    if any(item.get("number") == pr["number"]
           and item.get("repository", {}).get("nameWithOwner", "").casefold() == repo.casefold()
           for item in search["nodes"]):
        return {"id": owner["id"], "login": owner["login"]}
    return None


def eligible(pr, repo=None, actor_id=AUTHOR_ID, kind="copilot_review", owner=None):
    repo = pr["base"]["repo"]["full_name"] if repo is None else repo
    require(REPO.fullmatch(repo), "Invalid target repository")
    require(pr["state"] == "open" and not pr.get("merged", False), "PR is not open")
    require(kind in LOOP_KINDS and type(actor_id) is int and actor_id > 0
            and pr["user"]["type"] in {"User", "Bot"}
            and type(pr["user"]["id"]) is int and pr["user"]["id"] > 0
            and (kind == "pr_review" or pr["user"]["type"] == "User"
                 and pr["user"]["id"] == actor_id
                 or pr["user"]["type"] == "Bot" and isinstance(owner, dict)
                 and type(owner.get("id")) is int and owner["id"] == actor_id),
            "Wrong author")
    head, base = pr["head"], pr["base"]
    require(
        head["repo"] is not None
        and type(head["repo"]["id"]) is int and head["repo"]["id"] > 0
        and type(base["repo"]["id"]) is int and base["repo"]["id"] > 0
        and REPO.fullmatch(head["repo"]["full_name"])
        and base["repo"]["full_name"] == repo,
        "Missing or invalid base/head repository identity",
    )
    require(type(head["repo"].get("private")) is bool
            and type(base["repo"].get("private")) is bool,
            "Missing or inconsistent repository visibility")
    require(all(repository["private"] is False
                and repository.get("visibility", "public") == "public"
                for repository in (head["repo"], base["repo"])),
            "Only public base and head repositories are supported")
    require((head["repo"]["full_name"] == repo) == (head["repo"]["id"] == base["repo"]["id"])
            and (head["repo"]["id"] != base["repo"]["id"]
                 or head["repo"]["private"] == base["repo"]["private"]),
            "Inconsistent base/head repository identity")
    require(SHA.fullmatch(head["sha"]), "Invalid source SHA")
    ref = head["ref"]
    require(
        safe_ref(ref)
        and (head["repo"]["id"] != base["repo"]["id"] or ref != base["ref"]),
        "Unsafe head branch",
    )
    result = {"repo": repo, "repo_id": base["repo"]["id"], "pr": pr["number"],
            "head_repo": head["repo"]["full_name"], "head_repo_id": head["repo"]["id"],
            "head_ref": ref, "frozen_sha": head["sha"], "source_private": head["repo"]["private"],
            "target_private": base["repo"]["private"], "authorized_actor_id": actor_id}
    if kind == "pr_review" or owner is not None:
        result["pr_author_id"] = pr["user"]["id"]
    public_request(result)
    return result


def unchanged(request, pr):
    public_request(request)
    kind = loop_kind(request)
    owner = request.get("commit_author") if kind != "pr_review" and "pr_author_id" in request else None
    live = eligible(pr, request["repo"], request["authorized_actor_id"], kind, owner)
    require(all(request.get(k) == live[k]
                for k in live), "Target changed after freeze")
    if diff_scope(request):
        require(pr["base"]["ref"] == request["base_ref"],
                "PR base branch changed after freeze")


def check_target(api, request):
    public_request(request)
    pr = api.call(f"repos/{request['repo']}/pulls/{request['pr']}")
    unchanged(request, pr)
    if loop_kind(request) != "pr_review" and "pr_author_id" in request:
        require(attributed_owner(api, pr, request["repo"], request["authorized_actor_id"],
                                 request["commit_author"]) == request["commit_author"],
                "GitHub PR ownership changed after freeze")
    return pr


def private_source(request):
    public_request(request)
    return request["source_private"]


def dispositions(value, request):
    from loop.candidates import semantic
    semantic(value, request)


def worker_result(value, request):
    public_request(request)
    dispositions(value, request)


def candidate_outcome(value, request, candidate):
    worker_result(value, request)
    kind = loop_kind(request)
    if kind == "pr_conflict_resolver":
        require(value["outcome"] == "blocked" or
                (value["outcome"] == "merge") == candidate["changed"],
                "Merge outcome contradicts candidate graph")
    elif kind in REPORT_KINDS:
        require(not candidate["changed"] and not candidate["commits"]
                and candidate["patch_sha256"] == hashlib.sha256(b"").hexdigest(),
                "Report-only task contains source changes")
        if kind == "pr_description":
            require(candidate["tree"] is None and not candidate["changed_paths"]
                    and candidate["commit"] == candidate["parent"] == request["frozen_sha"],
                    "Description task contains a source candidate")
    elif kind == "self_review":
        require(value["outcome"] != "blocked", "Self-review is blocked or incomplete")
        require((value["outcome"] == "fixes") == candidate["changed"],
                "Self-review outcome contradicts candidate tree")
        if value["outcome"] == "clean":
            require(candidate["patch_sha256"] == hashlib.sha256(b"").hexdigest(),
                    "Clean self-review requires an empty patch")
    else:
        require((value["outcome"] == "fixes") == candidate["changed"]
                or value["outcome"] == "blocked", "Outcome contradicts candidate tree")
        if kind == "pr_consistency" and value["outcome"] == "fixes":
            avoidable = {item["path"] for item in value["consistency"]
                         if item["classification"] == "avoidable"}
            require(set(candidate["changed_paths"]) <= avoidable,
                    "Consistency candidate changes a path without an avoidable difference")


def publication_gate(*_args, **_kwargs):
    raise Rejected(
        "Use explicit launch with target/head repository access, "
        "existing Copilot findings and trusted candidate verification. "
        "No credential fallback."
    )
