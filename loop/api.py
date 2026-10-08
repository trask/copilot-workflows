"""Bounded GitHub API access with a read-only target policy."""

import http.client
from contextlib import ExitStack
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from loop.policy import CENTRAL, Rejected, require

MAX_RESPONSE = 16 * 1024 * 1024


class APIError(RuntimeError):
    def __init__(self, status, message, *, rate_limited=False, retry_at=None):
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited or status == 429
        self.retry_at = retry_at


class DeadlineReached(TimeoutError):
    pass


def transport_failure(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, DeadlineReached):
        return None
    if isinstance(reason, (TimeoutError, ConnectionResetError, ssl.SSLEOFError,
                           http.client.IncompleteRead)):
        return type(reason).__name__
    return None


class API:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN")
        self.deadline = None

    def authorize(self, path, method, data):
        require(path.startswith(("repos/", "users/", "user/", "graphql"))
                or path.startswith("installation/repositories"), "Unexpected API path")
        if method != "GET" and path != "graphql":
            require(path.startswith(f"repos/{CENTRAL}/"), "Target mutation is forbidden")

    def read_timeout(self):
        if self.deadline is None:
            return 60
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineReached("API execution deadline reached")
        return min(60, remaining)

    def retry_read(self, attempt, failure):
        delay = attempt + 1
        if self.deadline is not None and self.deadline - time.monotonic() <= delay:
            raise DeadlineReached("API retry would exceed execution deadline")
        print(f"READ RETRY: {failure}; attempt {attempt + 2}/3", file=sys.stderr)
        time.sleep(delay)

    def call(self, path, method="GET", data=None, raw=False, limit=MAX_RESPONSE, accept=None,
             *, token=None):
        self.authorize(path, method, data)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "copilot-review-loop"}
        if accept is not None:
            require(method == "GET" and accept == "application/vnd.github.diff",
                    "Unsupported response format")
            headers["Accept"] = accept
        token = self.token if token is None else token
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request("https://api.github.com/" + path, data=body,
                                     headers=headers, method=method)
        for attempt in range(3):
            timeout = self.read_timeout()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = response.read() if limit is None else response.read(limit + 1)
            except urllib.error.HTTPError as error:
                error.close()
                if method != "GET" or attempt == 2 or error.code not in {500, 502, 503, 504}:
                    # Do not echo response bodies, which can include untrusted text.
                    limits = {}
                    for header in ("X-RateLimit-Limit", "X-RateLimit-Remaining",
                                   "X-RateLimit-Reset", "Retry-After"):
                        value = error.headers.get(header)
                        if value is not None and value.isascii() and value.isdecimal():
                            limits[header] = int(value)
                    rate_limited = (error.code == 429 or error.code == 403
                                    and (limits.get("X-RateLimit-Remaining") == 0
                                         or "Retry-After" in limits))
                    retry_at = (limits.get("X-RateLimit-Reset")
                                if limits.get("X-RateLimit-Remaining") == 0 else None)
                    if retry_at is None and "Retry-After" in limits:
                        retry_at = int(time.time()) + limits["Retry-After"]
                    message = f"GitHub API {method} failed with HTTP {error.code}; endpoint={path}"
                    if rate_limited:
                        message += "; rate limited"
                    for header, value in limits.items():
                        message += f"; {header}={value}"
                    raise APIError(error.code, message, rate_limited=rate_limited,
                                   retry_at=retry_at) from error
                failure = f"HTTP {error.code}"
            except (TimeoutError, urllib.error.URLError, ConnectionResetError,
                    ssl.SSLEOFError, http.client.IncompleteRead) as error:
                failure = transport_failure(error)
                if method != "GET" or attempt == 2 or failure is None:
                    raise
            else:
                break
            self.retry_read(attempt, f"GitHub API GET {failure}; endpoint={path}")
        require(limit is None or len(payload) <= limit, "API response exceeds limit")
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

    def signed_download(self, path, limit=None, *, log_windows=(), token=None, destination=None):
        for attempt in range(3):
            try:
                return self._signed_download(path, limit, log_windows=log_windows, token=token,
                                             destination=destination)
            except APIError as error:
                if attempt == 2 or error.status not in {500, 502, 503, 504}:
                    raise
                failure = f"HTTP {error.status}"
            except (TimeoutError, urllib.error.URLError, ConnectionResetError,
                    ssl.SSLEOFError, http.client.IncompleteRead) as error:
                failure = transport_failure(error)
                if attempt == 2 or failure is None:
                    raise
            self.retry_read(attempt, f"GitHub signed download GET {failure}; endpoint={path}")

    def _signed_download(self, path, limit=None, *, log_windows=(), token=None, destination=None):
        self.authorize(path, "GET", None)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        url = "https://api.github.com/" + path
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + ((self.token if token is None else token) or ""),
            "User-Agent": "copilot-review-loop",
        })
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=self.read_timeout()):
                raise Rejected("Unexpected artifact download response")
        except urllib.error.HTTPError as error:
            try:
                if error.code != 302:
                    raise APIError(error.code, f"GitHub download failed with HTTP {error.code}") from error
                signed_url = error.headers["Location"]
            finally:
                error.close()
        parsed = urllib.parse.urlsplit(signed_url)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and parsed.port in {None, 443}, "Unsafe artifact redirect")
        # Signed destination came from the authenticated API. Never forward its bearer token.
        try:
            with ExitStack() as stack:
                response = stack.enter_context(urllib.request.urlopen(
                    signed_url, timeout=self.read_timeout()))
                sink = (stack.enter_context(Path(destination).open("wb"))
                        if destination is not None else None)
                if log_windows or limit is None or sink is not None:
                    payload = bytearray() if sink is None else Path(destination)
                    size = 0
                    deadline = time.monotonic() + 60
                    if self.deadline is not None:
                        deadline = min(deadline, self.deadline)
                    windows = [(start[:19].encode("ascii"), end[:19].encode("ascii"))
                               for start, end in log_windows]
                    read = response.readline if windows else response.read
                    line_start, selected = True, True
                    while chunk := read(65536):
                        if windows and line_start:
                            selected = any(start <= chunk[:19] <= end for start, end in windows)
                        if selected:
                            size += len(chunk)
                            if sink is None:
                                payload.extend(chunk)
                            else:
                                sink.write(chunk)
                        line_start = chunk.endswith(b"\n")
                        if time.monotonic() >= deadline:
                            raise DeadlineReached("Log download deadline reached")
                    if sink is None:
                        payload = bytes(payload)
                else:
                    payload = response.read(limit + 1)
                    size = len(payload)
        except urllib.error.HTTPError as error:
            error.close()
            raise APIError(error.code, f"Signed download failed with HTTP {error.code}") from error
        require(limit is None or size <= limit, "Artifact download exceeds limit")
        return payload
