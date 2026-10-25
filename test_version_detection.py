import io
import json
import os
import unittest
from unittest.mock import patch

import wb2api


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class VersionDetectionTests(unittest.TestCase):
    def test_download_pages_update_only_their_own_realm(self):
        calls = {"history": 0, "intl": 0}
        def open_url(request, timeout=0):
            url = request.full_url
            if url.endswith("/Download-History"):
                calls["history"] += 1
                return _Response(b"<h1>v5.8.3</h1>")
            if "/v2/update?platform=workbuddy-win32-x64-user" in url:
                calls["intl"] += 1
                return _Response(b'{"version":"5.8.4.41366630"}')
            if url.endswith("/release-notes/README"):
                return _Response(b"<h1>v2.162.0</h1>")
            raise AssertionError("unexpected fallback API request: " + url)

        with patch("urllib.request.urlopen", side_effect=open_url):
            versions = wb2api._fetch_latest_versions()

        self.assertEqual(versions["client_version"], "5.8.3")
        self.assertEqual(versions["intl_client_version"], "5.8.4")
        self.assertEqual(versions["cli_version"], "2.162.0")
        self.assertEqual(versions["intl_cli_version"], "2.162.0")
        self.assertEqual(calls["history"], 1)
        self.assertEqual(calls["intl"], 1)
        self.assertGreater(versions["cn_timestamp"], 0)
        self.assertGreater(versions["intl_timestamp"], 0)
        self.assertGreater(versions["timestamp"], 0)

    def test_partial_success_does_not_mark_both_realms_fresh(self):
        history_count = 0
        def open_url(request, timeout=0):
            nonlocal history_count
            if request.full_url.endswith("/Download-History"):
                history_count += 1
                return _Response(b"<h1>v5.8.3</h1>")
            if "/v2/update?platform=workbuddy-win32-x64-user" in request.full_url:
                raise OSError("offline")
            if request.full_url.endswith("/release-notes/README"):
                return _Response(b"<h1>v2.162.0</h1>")
            raise OSError("offline")

        with patch("urllib.request.urlopen", side_effect=open_url):
            versions = wb2api._fetch_latest_versions()

        self.assertGreater(versions["cn_timestamp"], 0)
        self.assertEqual(versions["intl_timestamp"], 0)
        self.assertEqual(versions["timestamp"], 0)

    def test_version_parser_ignores_script_versions(self):
        html = "<script>const oldVersion='1.0.0'</script><h1>v5.8.3</h1>"
        self.assertEqual(wb2api._latest_document_version(html), "5.8.3")

    def test_cli_parser_prefers_release_heading(self):
        html = "<nav>v2.100.0</nav><h1>CodeBuddy Code v2.162.0 Release</h1>"
        self.assertEqual(wb2api._latest_cli_version(html), "2.162.0")

    def test_each_environment_override_is_applied_independently(self):
        env = {"WB_INTL_CLIENT_VERSION": "9.8.7"}
        with patch.dict("os.environ", env, clear=True), \
                patch.object(wb2api, "_load_cached_versions", return_value=None):
            versions = wb2api._get_client_versions()

        self.assertEqual(versions["intl_client_version"], "9.8.7")
        self.assertEqual(versions["client_version"], wb2api._DEFAULT_CLIENT_VERSION)

    def test_old_shared_timestamp_cache_is_not_trusted(self):
        old_cache = {
            "timestamp": wb2api.time.time(),
            "client_version": "5.9.2",
            "intl_client_version": "5.9.2",
        }
        with patch("os.path.exists", return_value=True), \
                patch("builtins.open", return_value=io.StringIO(json.dumps(old_cache))):
            cached = wb2api._load_cached_versions()
        self.assertIsNone(cached)

    def test_stale_cache_values_remain_as_fallback_until_daily_refresh(self):
        now = wb2api.time.time()
        cache = {
            "client_version": "6.1.0",
            "intl_client_version": "7.2.3",
            "cn_timestamp": now,
            "intl_timestamp": now - 2 * wb2api._VERSION_CHECK_INTERVAL,
            "cn_checked_date": "2026-10-23",
            "intl_checked_date": "2026-10-22",
        }
        with patch("os.path.exists", return_value=True), \
                patch("builtins.open", return_value=io.StringIO(json.dumps(cache))):
            cached = wb2api._load_cached_versions()
        self.assertEqual(cached["client_version"], "6.1.0")
        self.assertEqual(cached["intl_client_version"], "7.2.3")
        self.assertTrue(wb2api._version_refresh_due(cached, today="2026-10-23"))

    def test_daily_refresh_waits_until_each_realm_succeeds(self):
        checked_today = {
            "cn_checked_date": "2026-10-23",
            "intl_checked_date": "2026-10-23",
            "cli_checked_date": "2026-10-23",
        }
        self.assertFalse(wb2api._version_refresh_due(checked_today, today="2026-10-23"))
        checked_today["intl_checked_date"] = "2026-10-22"
        self.assertTrue(wb2api._version_refresh_due(checked_today, today="2026-10-23"))

    def test_cached_request_headers_rebuild_after_version_changes(self):
        account = {"accessToken": "token", "uid": "uid", "realm": "cn"}
        with patch.object(wb2api, "_chat_static", {"key": None, "h": None}), \
                patch.object(wb2api, "is_global", return_value=False), \
                patch.object(wb2api, "common_headers", side_effect=lambda _: {}), \
                patch.object(wb2api, "get_client_version", side_effect=["5.8.3", "5.8.3", "5.8.4", "5.8.4"]), \
                patch.object(wb2api, "get_cli_version", return_value="2.162.0"):
            first = wb2api.chat_headers(account)
            second = wb2api.chat_headers(account)

        self.assertEqual(first["X-IDE-Version"], "5.8.3")
        self.assertEqual(second["X-IDE-Version"], "5.8.4")


if __name__ == "__main__":
    unittest.main()
