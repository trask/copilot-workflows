"""Bounded GitHub API access with a read-only target policy."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from loop.policy import CENTRAL, Rejected, require

MAX_RESPONSE = 16 * 1024 * 1024


class APIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DeadlineReached(TimeoutError):
    pass


class API:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN")
        self.deadline = None

    def authorize(self, path, method, data):
        require(path.startswith(("repos/", "users/", "user/", "graphql"))
                or path.startswith("installation/repositories"), "Unexpected API path")
        if method != "GET" and path != "graphql":
            require(path.startswith(f"repos/{CENTRAL}/"), "Target mutation is forbidden")

    def call(self, path, method="GET", data=None, raw=False, limit=MAX_RESPONSE, accept=None):
        self.authorize(path, method, data)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "copilot-review-loop"}
        if accept is not None:
            require(method == "GET" and accept == "application/vnd.github.diff",
                    "Unsupported response format")
            headers["Accept"] = accept
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request("https://api.github.com/" + path, data=body,
                                     headers=headers, method=method)
        for attempt in range(3):
            timeout = 60
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadlineReached("API execution deadline reached")
                timeout = min(timeout, remaining)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = response.read(limit + 1)
            except urllib.error.HTTPError as error:
                error.close()
                if method != "GET" or attempt == 2 or error.code not in {500, 502, 503, 504}:
                    # Do not echo response bodies, which can include untrusted text.
                    raise APIError(error.code, f"GitHub API {method} failed with HTTP {error.code}") from error
                failure = f"HTTP {error.code}"
            except (TimeoutError, urllib.error.URLError) as error:
                if (method != "GET" or attempt == 2
                        or isinstance(error, urllib.error.URLError)
                        and not isinstance(error.reason, TimeoutError)):
                    raise
                failure = "timeout"
            else:
                break
            delay = attempt + 1
            if self.deadline is not None and self.deadline - time.monotonic() <= delay:
                raise DeadlineReached("API retry would exceed execution deadline")
            print(f"READ RETRY: GitHub API GET {failure}; attempt {attempt + 2}/3",
                  file=sys.stderr)
            time.sleep(delay)
        require(len(payload) <= limit, "API response exceeds limit")
        return payload if raw else (json.loads(payload) if payload else None)

    def pages(self, path, key=None):
        result = []
        for page in range(1, 101):
            sep = "&" if "?" in path else "?"
            response = self.call(f"{path}{sep}per_page=100&page={page}")
            items = response if key is None else response[key]
            require(isinstance(items, list), "Expected API array")
            result.extend(items)
            require(len(result) <= 10000, "Pagination limit exceeded")
            if len(items) < 100:
                return result
        raise Rejected("Pagination was not completed")

    def graphql(self, query, variables):
        require(query.lstrip().startswith("query"), "Only read-only GraphQL is supported")
        result = self.call("graphql", "POST", {"query": query, "variables": variables})
        require(not result.get("errors"), "GraphQL failed; do not accept partial data")
        return result["data"]

    def artifact_zip(self, artifact_id, limit):
        return self.signed_download(f"repos/{CENTRAL}/actions/artifacts/{artifact_id}/zip", limit)

    def signed_download(self, path, limit):
        self.authorize(path, "GET", None)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        url = "https://api.github.com/" + path
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (self.token or ""), "User-Agent": "copilot-review-loop",
        })
        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=60)
        except urllib.error.HTTPError as error:
            require(error.code == 302, "Artifact download did not return a signed redirect")
            destination = error.headers["Location"]
        else:
            raise Rejected("Unexpected artifact download response")
        parsed = urllib.parse.urlsplit(destination)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and parsed.port in {None, 443}, "Unsafe artifact redirect")
        # Signed destination came from the authenticated API. Never forward its bearer token.
        with urllib.request.urlopen(destination, timeout=60) as response:
            payload = response.read(limit + 1)
        require(len(payload) <= limit, "Artifact download exceeds limit")
        return payload
