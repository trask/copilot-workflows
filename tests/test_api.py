import io
import unittest
import urllib.error
from unittest.mock import Mock, call, patch

from loop.api import API, APIError, DeadlineReached
from loop.policy import CENTRAL, Rejected


def response(payload=b'{"sha":"example"}'):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = payload
    return result


def http_error(status):
    return urllib.error.HTTPError("https://api.github.com/private", status,
                                  "untrusted error text", {}, io.BytesIO(b"untrusted body"))


class APITests(unittest.TestCase):
    def test_transient_get_errors_retry_the_exact_authorized_request(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status), \
                    patch("loop.api.urllib.request.urlopen",
                          side_effect=[http_error(status), http_error(status), response()]) as open_request, \
                    patch("loop.api.time.sleep") as sleep, \
                    patch("loop.api.sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual({"sha": "example"}, API("test-token").call(f"repos/{CENTRAL}/git/ref/heads/main"))
                self.assertEqual(3, open_request.call_count)
                requests = [entry.args[0] for entry in open_request.call_args_list]
                self.assertTrue(all(request is requests[0] for request in requests))
                self.assertEqual("GET", requests[0].get_method())
                self.assertEqual([call(1), call(2)], sleep.call_args_list)
                self.assertNotIn("test-token", stderr.getvalue())
                self.assertNotIn("untrusted", stderr.getvalue())
                self.assertNotIn("private", stderr.getvalue())

    def test_persistent_get_failure_is_explicit_and_bounded(self):
        with patch("loop.api.urllib.request.urlopen",
                   side_effect=[http_error(504) for _ in range(3)]) as open_request, \
                patch("loop.api.time.sleep") as sleep, \
                patch("loop.api.sys.stderr", new_callable=io.StringIO), \
                self.assertRaisesRegex(APIError, "GET failed with HTTP 504") as error:
            API().call(f"repos/{CENTRAL}/actions/runs")
        self.assertEqual(504, error.exception.status)
        self.assertEqual(3, open_request.call_count)
        self.assertEqual([call(1), call(2)], sleep.call_args_list)

    def test_get_timeout_retries_including_partial_response_reads(self):
        for failure in (TimeoutError("connection timed out"),
                        urllib.error.URLError(TimeoutError("connection timed out"))):
            interrupted = response()
            interrupted.read.side_effect = TimeoutError("read operation timed out")
            with self.subTest(failure=type(failure).__name__), \
                    patch("loop.api.urllib.request.urlopen",
                          side_effect=[failure, interrupted, response()]) as open_request, \
                    patch("loop.api.time.sleep") as sleep, \
                    patch("loop.api.sys.stderr", new_callable=io.StringIO):
                self.assertEqual({"sha": "example"}, API().call(f"repos/{CENTRAL}/actions/runs"))
            self.assertEqual(3, open_request.call_count)
            interrupted.__exit__.assert_called_once()
            self.assertEqual([call(1), call(2)], sleep.call_args_list)

    def test_persistent_get_timeout_raises_original_failure(self):
        for failure in (TimeoutError("read operation timed out"),
                        urllib.error.URLError(TimeoutError("connection timed out"))):
            with self.subTest(failure=type(failure).__name__), \
                    patch("loop.api.urllib.request.urlopen", side_effect=failure) as open_request, \
                    patch("loop.api.time.sleep") as sleep, \
                    patch("loop.api.sys.stderr", new_callable=io.StringIO), \
                    self.assertRaises(type(failure)) as error:
                API().call(f"repos/{CENTRAL}/actions/runs")
            self.assertIs(failure, error.exception)
            self.assertEqual(3, open_request.call_count)
            self.assertEqual(2, sleep.call_count)

    def test_non_timeout_transport_failures_are_not_retried(self):
        failure = urllib.error.URLError("certificate verification failed")
        with patch("loop.api.urllib.request.urlopen", side_effect=failure) as open_request, \
                patch("loop.api.time.sleep") as sleep, self.assertRaises(urllib.error.URLError) as error:
            API().call(f"repos/{CENTRAL}/actions/runs")
        self.assertIs(failure, error.exception)
        open_request.assert_called_once()
        sleep.assert_not_called()

    def test_permission_validation_and_rate_limit_failures_are_not_retried(self):
        for status in (400, 401, 403, 404, 409, 422, 429):
            with self.subTest(status=status), \
                    patch("loop.api.urllib.request.urlopen", side_effect=http_error(status)) as open_request, \
                    patch("loop.api.time.sleep") as sleep, \
                    self.assertRaises(APIError) as error:
                API().call(f"repos/{CENTRAL}/actions/runs")
            self.assertEqual(status, error.exception.status)
            open_request.assert_called_once()
            sleep.assert_not_called()

    def test_mutations_and_graphql_posts_are_never_retried(self):
        for method, path in (("POST", f"repos/{CENTRAL}/actions/workflows/coordinator.yml/dispatches"),
                             ("PATCH", f"repos/{CENTRAL}/git/refs/heads/review-loop-state"),
                             ("PUT", f"repos/{CENTRAL}/contents/checkpoint.json"),
                             ("DELETE", f"repos/{CENTRAL}/actions/runs/1"),
                             ("POST", "graphql")):
            for failure in (http_error(503), TimeoutError("response lost"),
                            urllib.error.URLError(TimeoutError("connection timed out"))):
                with self.subTest(method=method, path=path, failure=type(failure).__name__), \
                        patch("loop.api.urllib.request.urlopen", side_effect=failure) as open_request, \
                        patch("loop.api.time.sleep") as sleep, \
                        self.assertRaises(APIError if isinstance(failure, urllib.error.HTTPError) else type(failure)):
                    API().call(path, method=method, data={})
                open_request.assert_called_once()
                sleep.assert_not_called()

    def test_retry_timeouts_shrink_to_the_existing_deadline(self):
        api = API()
        api.deadline = 110
        with patch("loop.api.time.monotonic", side_effect=[100, 102, 103]), \
                patch("loop.api.urllib.request.urlopen", side_effect=[http_error(504), response()]) as open_request, \
                patch("loop.api.time.sleep") as sleep, \
                patch("loop.api.sys.stderr", new_callable=io.StringIO):
            self.assertEqual({"sha": "example"}, api.call(f"repos/{CENTRAL}/actions/runs"))
        self.assertEqual([10, 7], [entry.kwargs["timeout"] for entry in open_request.call_args_list])
        sleep.assert_called_once_with(1)
        self.assertEqual(110, api.deadline)

    def test_retry_backoff_cannot_outlive_deadline(self):
        api = API()
        api.deadline = 101
        with patch("loop.api.time.monotonic", return_value=100), \
                patch("loop.api.urllib.request.urlopen", side_effect=http_error(504)) as open_request, \
                patch("loop.api.time.sleep") as sleep, \
                self.assertRaises(DeadlineReached):
            api.call(f"repos/{CENTRAL}/actions/runs")
        open_request.assert_called_once()
        sleep.assert_not_called()

    def test_deadline_expiring_after_backoff_prevents_another_request(self):
        api = API()
        api.deadline = 110
        with patch("loop.api.time.monotonic", side_effect=[100, 101, 110]), \
                patch("loop.api.urllib.request.urlopen", side_effect=http_error(504)) as open_request, \
                patch("loop.api.time.sleep") as sleep, \
                patch("loop.api.sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(DeadlineReached):
            api.call(f"repos/{CENTRAL}/actions/runs")
        open_request.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_authorization_and_response_limits_still_fail_closed(self):
        with patch("loop.api.urllib.request.urlopen") as open_request, \
                self.assertRaises(Rejected):
            API().call("repos/other/project/issues", method="POST", data={})
        open_request.assert_not_called()
        with patch("loop.api.urllib.request.urlopen", return_value=response(b"oversized")) as open_request, \
                patch("loop.api.time.sleep") as sleep, self.assertRaises(Rejected):
            API().call(f"repos/{CENTRAL}/actions/runs", limit=2)
        open_request.assert_called_once()
        sleep.assert_not_called()

    def test_raw_responses_keep_their_exact_bytes_after_retry(self):
        with patch("loop.api.urllib.request.urlopen",
                   side_effect=[http_error(502), response(b"raw response")]), \
                patch("loop.api.time.sleep"), \
                patch("loop.api.sys.stderr", new_callable=io.StringIO):
            self.assertEqual(b"raw response", API().call(f"repos/{CENTRAL}/actions/runs", raw=True))

    def test_signed_log_tails_stream_without_forwarding_credentials(self):
        for size in (10, 3 * 65536):
            payload = b"x" * size + b"\nFAILURE: root cause\n"
            downloaded = response()
            downloaded.read.side_effect = io.BytesIO(payload).read
            redirect = http_error(302)
            redirect.headers["Location"] = "https://example.com/signed-log"
            with self.subTest(size=size), \
                    patch("loop.api.urllib.request.build_opener") as opener, \
                    patch("loop.api.urllib.request.urlopen", return_value=downloaded) as download, \
                    patch("loop.api.sys.stderr", new_callable=io.StringIO) as stderr:
                opener.return_value.open.side_effect = redirect
                self.assertEqual(payload[-100:], API("secret").signed_download(
                    "repos/target/repo/actions/jobs/1/logs", 100, tail=True))
            download.assert_called_once_with("https://example.com/signed-log", timeout=60)
            self.assertTrue(all(c.args == (65536,) for c in downloaded.read.call_args_list))
            self.assertEqual(size > 100, "READ EXCERPT" in stderr.getvalue())
            self.assertNotIn("secret", stderr.getvalue())
            self.assertNotIn("signed-log", stderr.getvalue())

    def test_signed_artifacts_still_require_the_complete_download(self):
        redirect = http_error(302)
        redirect.headers["Location"] = "https://example.com/artifact"
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", return_value=response(b"oversized")), \
                self.assertRaisesRegex(Rejected, "Artifact download exceeds limit"):
            opener.return_value.open.side_effect = redirect
            API().artifact_zip(1, 2)

    def test_failed_step_log_window_excludes_later_cleanup_and_bounds_long_lines(self):
        payload = (b"2026-10-07T01:01:00.123Z " + b"x" * 200000 + b"\n"
                   b"2026-10-07T01:01:01.456Z ##[error]FAILURE: expected remote parent\n"
                   + b"2026-10-07T01:01:01.789Z Post job cleanup: cache saved\n" * 1000)
        downloaded = response()
        downloaded.readline.side_effect = io.BytesIO(payload).readline
        redirect = http_error(302)
        redirect.headers["Location"] = "https://example.com/signed-log"
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", return_value=downloaded):
            opener.return_value.open.side_effect = redirect
            excerpt = API().signed_download("repos/target/repo/actions/jobs/1/logs", 100,
                                           tail=True, log_windows=[
                                               ("2026-10-07T01:01:00Z", "2026-10-07T01:01:01Z")])
        self.assertTrue(excerpt.endswith(b"FAILURE: expected remote parent\n"))
        self.assertNotIn(b"Post job cleanup", excerpt)
        self.assertLessEqual(len(excerpt), 100)
        self.assertTrue(all(c.args == (65536,) for c in downloaded.readline.call_args_list))
        downloaded.read.assert_not_called()

    def test_signed_download_permission_errors_reach_the_caller(self):
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen") as download, \
                self.assertRaises(APIError) as error:
            opener.return_value.open.side_effect = http_error(403)
            API().signed_download("repos/target/repo/actions/jobs/1/logs", 100, tail=True)
        self.assertEqual(403, error.exception.status)
        download.assert_not_called()

    def test_missing_signed_blob_reaches_the_caller_without_exposing_its_url(self):
        redirect = http_error(302)
        redirect.headers["Location"] = "https://example.com/log?signature=secret"
        missing = http_error(404)
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", side_effect=missing) as download, \
                self.assertRaises(APIError) as error:
            opener.return_value.open.side_effect = redirect
            API("read-token").signed_download("repos/target/repo/actions/jobs/1/logs",
                                             100, tail=True)
        self.assertEqual(404, error.exception.status)
        self.assertEqual("Signed download failed with HTTP 404", str(error.exception))
        self.assertTrue(missing.fp.closed)
        download.assert_called_once_with(redirect.headers["Location"], timeout=60)

    def test_streaming_log_download_has_an_elapsed_deadline(self):
        redirect = http_error(302)
        redirect.headers["Location"] = "https://example.com/signed-log"
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", return_value=response(b"chunk")), \
                patch("loop.api.time.monotonic", side_effect=[100, 160]), \
                self.assertRaisesRegex(DeadlineReached, "Log download deadline"):
            opener.return_value.open.side_effect = redirect
            API().signed_download("repos/target/repo/actions/jobs/1/logs", 100, tail=True)
