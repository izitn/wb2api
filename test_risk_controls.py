import json
import os
import tempfile
import unittest
from unittest.mock import patch

import wb2api


class _FakeResponse:
    def __init__(self, status, body, content_type="application/json"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body
        self.closed = False

    def read(self, *_args):
        body, self._body = self._body, b""
        return body

    def __iter__(self):
        body, self._body = self._body, b""
        yield from body.splitlines(True)

    def close(self):
        self.closed = True


class RiskControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd(), ignore_cleanup_errors=True)
        self.patchers = [
            patch.object(wb2api, "RISK_STATE_FILE", os.path.join(self.tmp.name, "risk_state.json")),
            patch.object(wb2api, "_pool_health", {}),
            patch.object(wb2api, "_pool_sticky", {}),
            patch.object(wb2api, "_pool_stats", {"switch": 0, "exhausted": 0,
                                                  "restricted": 0, "wavePauses": 0}),
            patch.object(wb2api, "_pool_risk_events", {
                realm: wb2api.collections.deque(maxlen=256) for realm in wb2api.REALMS}),
            patch.object(wb2api, "_pool_risk_waves", {
                realm: {"pausedUntil": 0.0, "reason": "", "lastRestrictedAt": 0.0}
                for realm in wb2api.REALMS}),
            patch.object(wb2api, "_pool_channel_pauses", {}),
            patch.object(wb2api, "_risk_state_loaded", False),
            patch.object(wb2api, "_accounts", {}),
            patch.object(wb2api, "_auth", None),
            patch.object(wb2api, "_points_state", {}),
            patch.object(wb2api, "_auto_checkin", False),
            patch.object(wb2api, "_chat_paths", {}),
            patch.object(wb2api, "_refresh_failures", {}),
            patch.object(wb2api, "CHECKIN_WINDOW", 0),
            patch.object(wb2api, "RECHECK_COOLDOWN", 0),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    @staticmethod
    def account(n, realm="global"):
        domain = "workbuddy.ai" if realm == "global" else "codebuddy.cn"
        return {
            "accessToken": "token-%s" % n,
            "refreshToken": "refresh-%s" % n,
            "expiresAt": int(wb2api.time.time()) + 3600,
            "domain": domain,
            "realm": realm,
            "uid": "uid-%s" % n,
            "enterpriseId": "",
            "nickname": "账号%s" % n,
        }

    def add_accounts(self, *accounts):
        for account in accounts:
            wb2api._accounts[wb2api.account_id(account)] = account
        wb2api._auth = accounts[0]

    def test_failure_classifier_separates_request_account_and_limits(self):
        restricted = wb2api.classify_upstream_failure(
            403, 11140, "账号存在异常行为，已被限制使用")
        self.assertEqual(restricted["kind"], "restricted")
        self.assertFalse(restricted["switch"])

        channel = wb2api.classify_upstream_failure(
            400, 11128, "Illegal API invocation from an unapproved channel")
        self.assertEqual(channel["kind"], "channel_check")
        self.assertFalse(channel["switch"])

        model_limit = wb2api.classify_upstream_failure(429, 6000, "请切换其他模型")
        self.assertEqual(model_limit["kind"], "model_limit")
        self.assertFalse(model_limit["switch"])

        queue = wb2api.classify_upstream_failure(429, 6020, "queue.waiting")
        self.assertEqual(queue["kind"], "queue")
        self.assertFalse(queue["switch"])

        rate_limit = wb2api.classify_upstream_failure(429, 9999, "too many requests")
        self.assertEqual(rate_limit["kind"], "rate_limit")
        self.assertTrue(rate_limit["switch"])

        unknown_auth = wb2api.classify_upstream_failure(
            401, 401, "edge page", auth_state=wb2api.REFRESH_INCONCLUSIVE)
        self.assertEqual(unknown_auth["kind"], "auth_inconclusive")
        self.assertFalse(unknown_auth["switch"])
        self.assertLessEqual(unknown_auth["accountCooldown"], wb2api.POOL_AUTH_INCONCLUSIVE)

    def test_model_and_queue_429_do_not_freeze_account(self):
        a = self.account(1)
        self.add_accounts(a)
        model_failure = wb2api.classify_upstream_failure(429, 6000, "switch model")
        wb2api._pool_record_failure(a, model_failure, model="gpt-test")
        aid = wb2api.account_id(a)
        self.assertTrue(wb2api._pool_available(aid))
        self.assertGreater(wb2api._pool_model_cooldown_remaining(a, "gpt-test"), 0)

        queue_failure = wb2api.classify_upstream_failure(429, 6020, "queue.waiting")
        wb2api._pool_record_failure(a, queue_failure, model="gpt-test")
        self.assertTrue(wb2api._pool_available(aid))
        pause = wb2api._risk_pause_status("global")
        self.assertTrue(pause["paused"])
        self.assertEqual(pause["reason"], "queue")

    def test_two_restricted_accounts_pause_pool_and_survive_restart(self):
        a1, a2, a3 = self.account(1), self.account(2), self.account(3)
        self.add_accounts(a1, a2, a3)
        failure = wb2api.classify_upstream_failure(403, 11140, "账号存在异常行为，已被限制使用")
        self.assertTrue(wb2api._pool_record_failure(a1, failure))
        self.assertFalse(wb2api._pool_can_switch(a1, failure, set()))
        pause = wb2api._risk_pause_status("global")
        self.assertFalse(pause["paused"])
        self.assertTrue(wb2api._health(wb2api.account_id(a1))["restricted"])

        self.assertTrue(wb2api._pool_record_failure(a2, failure))
        pause = wb2api._risk_pause_status("global")
        self.assertTrue(pause["paused"])
        self.assertGreater(pause["remaining"], 0)
        self.assertIsNone(wb2api._pool_pick("global"))

        status = wb2api.pool_status()["global"]
        self.assertEqual(status["restricted"], 2)
        self.assertTrue(status["risk"]["paused"])
        self.assertTrue(os.path.exists(wb2api.RISK_STATE_FILE))

        wb2api._pool_health.clear()
        wb2api._pool_risk_waves["global"] = {"pausedUntil": 0.0, "reason": "", "lastRestrictedAt": 0.0}
        wb2api._risk_state_loaded = False
        wb2api.load_risk_state()
        self.assertTrue(wb2api._health(wb2api.account_id(a1))["restricted"])
        self.assertTrue(wb2api._health(wb2api.account_id(a2))["restricted"])
        self.assertTrue(wb2api._risk_pause_status("global")["paused"])
        self.assertGreaterEqual(len(wb2api._pool_risk_events["global"]), 2)

    def test_health_reports_restricted_wave(self):
        a1, a2 = self.account(1), self.account(2)
        self.add_accounts(a1, a2)
        failure = wb2api.classify_upstream_failure(403, 11140, "request illegal")
        wb2api._pool_record_failure(a1, failure)
        wb2api._pool_record_failure(a2, failure)
        health = wb2api.health()
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["pool"]["global"]["restricted"], 2)
        self.assertTrue(health["pool"]["global"]["paused"])

    def test_manual_recheck_requires_alive_refresh(self):
        a = self.account(1)
        self.add_accounts(a)
        failure = wb2api.classify_upstream_failure(403, 11140, "restricted")
        wb2api._pool_record_failure(a, failure)
        with patch.object(wb2api, "refresh_auth_state", return_value=wb2api.REFRESH_INCONCLUSIVE):
            result = wb2api.recheck_account_risk(wb2api.account_id(a))
        self.assertFalse(result["ok"])
        self.assertTrue(wb2api._health(wb2api.account_id(a))["restricted"])

        with patch.object(wb2api, "refresh_auth_state", return_value=wb2api.REFRESH_ALIVE):
            result = wb2api.recheck_account_risk(wb2api.account_id(a))
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleared"])
        self.assertFalse(wb2api._health(wb2api.account_id(a))["restricted"])
        self.assertTrue(wb2api._pool_available(wb2api.account_id(a)))

    def test_system_rewrite_is_off_unless_explicitly_enabled(self):
        original = "x" * 1200
        messages = [{"role": "system", "content": original},
                    {"role": "user", "content": "hello"}]
        with patch.object(wb2api, "_REMOVE_SYSTEM", False), \
                patch.object(wb2api, "_LIMIT_SYSTEM_LENGTH", False), \
                patch.object(wb2api, "_SYSTEM_TO_USER", False):
            wb2api._apply_system_message_workarounds(messages)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], original)

    def test_pool_disabled_env_uses_only_current_account(self):
        """WB_POOL=0 必须真的关闭池内选号与换号，而不只是改展示。"""
        a1, a2 = self.account(1), self.account(2)
        self.add_accounts(a1, a2)
        failure = wb2api.classify_upstream_failure(429, 9999, "too many requests")

        with patch.object(wb2api, "POOL_ENABLED", True):
            self.assertIsNotNone(wb2api._pool_pick("global"))
            self.assertTrue(wb2api._pool_can_switch(a1, failure, set()))

        with patch.object(wb2api, "POOL_ENABLED", False):
            self.assertIsNone(wb2api._pool_pick("global"))
            self.assertFalse(wb2api._pool_can_switch(a1, failure, set()))
            # 关闭后仍可用当前账号，但绝不借用池内其他账号
            self.assertEqual(wb2api.account_id(wb2api._pick_request_account("global", "")),
                             wb2api.account_id(a1))
            wb2api._health(wb2api.account_id(a1))["restricted"] = True
            self.assertIsNone(wb2api._pick_request_account("global", ""))

    def test_desktop_ua_is_deterministic_and_matches_ide_version(self):
        a = self.account(1)
        with patch.object(wb2api, "get_intl_client_version", return_value="5.8.4"), \
                patch.object(wb2api, "get_intl_cli_version", return_value="2.162.0"):
            first = wb2api.desktop_ua(a)
            second = wb2api.desktop_ua(a)
        self.assertEqual(first, second)
        self.assertIn("WorkBuddy/5.8.4", first)

        with patch.object(wb2api, "_chat_static", {"key": None, "h": None}), \
                patch.object(wb2api, "is_global", return_value=True), \
                patch.object(wb2api, "common_headers", return_value={}), \
                patch.object(wb2api, "get_intl_client_version", return_value="5.8.4"), \
                patch.object(wb2api, "get_intl_cli_version", return_value="2.162.0"):
            headers = wb2api.chat_headers(a)
        self.assertEqual(headers["X-IDE-Version"], "5.8.4")
        self.assertIn("WorkBuddy/5.8.4", headers["User-Agent"])

    def test_11128_retries_alternate_path_only_once(self):
        a = self.account(1)
        calls = []
        responses = [
            _FakeResponse(400, json.dumps({"code": 11128, "msg": "unapproved channel"}).encode()),
            _FakeResponse(200, b"data: [DONE]\n\n", "text/event-stream"),
        ]

        def fake_request(method, url, headers, body, timeout=0):
            calls.append(url)
            return responses.pop(0)

        with patch.object(wb2api, "upstream_request", side_effect=fake_request), \
                patch.object(wb2api, "chat_headers", return_value={}):
            resp = wb2api.call_upstream({"model": "test", "messages": []}, a)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith(wb2api.CHAT_PATH))
        self.assertTrue(calls[1].endswith(wb2api.CHAT_PATH_ALT))
        resp.close()

    def test_refresh_state_separates_edge_html_invalid_grant_and_restriction(self):
        a = self.account(1)

        def state_for(response):
            with patch.object(wb2api, "upstream_request", return_value=response), \
                    patch.object(wb2api, "common_headers", return_value={}):
                wb2api._refresh_failures.clear()
                return wb2api._refresh_unlocked(a)

        self.assertEqual(state_for(_FakeResponse(401, b"<html>edge blocked</html>", "text/html")),
                         wb2api.REFRESH_INCONCLUSIVE)
        self.assertEqual(state_for(_FakeResponse(
            400, json.dumps({"code": 400, "msg": "invalid_grant"}).encode())),
            wb2api.REFRESH_DEAD)
        self.assertEqual(state_for(_FakeResponse(
            403, json.dumps({"code": 11140, "msg": "request illegal"}).encode())),
            wb2api.REFRESH_RESTRICTED)

    def test_6020_queue_is_returned_without_transport_retry(self):
        a = self.account(1)
        calls = []

        def fake_request(method, url, headers, body, timeout=0):
            calls.append(url)
            return _FakeResponse(429, json.dumps({"code": 6020, "msg": "queue.waiting"}).encode())

        with patch.object(wb2api, "upstream_request", side_effect=fake_request), \
                patch.object(wb2api, "chat_headers", return_value={}):
            resp = wb2api.call_upstream({"model": "test", "messages": []}, a)
        self.assertEqual(resp.status, 429)
        self.assertEqual(len(calls), 1)
        resp.close()

    def test_11128_stops_after_alternate_path_failure(self):
        a = self.account(1)
        calls = []

        def fake_request(method, url, headers, body, timeout=0):
            calls.append(url)
            return _FakeResponse(400, json.dumps({
                "code": 11128, "msg": "unapproved channel"}).encode())

        with patch.object(wb2api, "upstream_request", side_effect=fake_request), \
                patch.object(wb2api, "chat_headers", return_value={}):
            resp = wb2api.call_upstream({"model": "test", "messages": []}, a)
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(calls), 2)
        resp.close()

    def test_read_error_marks_11140_as_account_restriction(self):
        resp = _FakeResponse(
            403,
            json.dumps({"code": 11140, "msg": "request illegal",
                        "displayMsg": {"zh": "账号存在异常行为，已被限制使用"}},
                       ensure_ascii=False).encode(),
        )
        status, message, code = wb2api.read_error(resp)
        self.assertEqual(status, 403)
        self.assertEqual(code, 11140)
        self.assertIn("停止自动换号", message)
        self.assertEqual(resp.wb2api_error_meta.get("code"), 11140)

    def test_200_business_429_keeps_its_limit_classification(self):
        resp = _FakeResponse(200, json.dumps({"code": 6000, "msg": "switch model"}).encode())
        status, message, code = wb2api.read_error(resp)
        self.assertEqual(status, 429)
        self.assertEqual(code, 6000)
        failure = wb2api.classify_upstream_failure(status, code=code, message=message)
        self.assertEqual(failure["kind"], "model_limit")

    def test_checkin_11140_marks_account_restricted(self):
        a = self.account(1, "cn")
        self.add_accounts(a)
        with patch.object(wb2api, "_billing_fallback", side_effect=wb2api.BillingError(
                403, "11140", "账号存在异常行为，已被限制使用")):
            result = wb2api.daily_checkin(a)
        self.assertFalse(result.get("checkin"))
        self.assertTrue(result.get("checkinRestricted"))
        self.assertTrue(wb2api._health(wb2api.account_id(a))["restricted"])
        self.assertFalse(wb2api._pool_available(wb2api.account_id(a)))

    def test_sse_business_11140_preserves_restriction_code(self):
        payload = json.dumps({"code": 11140, "msg": "request illegal",
                              "requestId": "01a167a4b4d87ca3ac8ee722bb714697"},
                             ensure_ascii=False).encode()
        resp = _FakeResponse(200, b"data: " + payload + b"\n\n", "text/event-stream")
        with self.assertRaises(wb2api.UpstreamError) as raised:
            wb2api.aggregate_sse(resp)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(wb2api.to_int(raised.exception.code), 11140)
        self.assertEqual(raised.exception.request_id,
                         "01a167a4b4d87ca3ac8ee722bb714697")

    def test_auto_checkin_stops_after_restriction(self):
        a1, a2 = self.account(1, "cn"), self.account(2, "cn")
        self.add_accounts(a1, a2)
        wb2api._auto_checkin = True
        with patch.object(wb2api, "_supports_checkin", return_value=True), \
                patch.object(wb2api, "_pool_available", return_value=True), \
                patch.object(wb2api, "_risk_pause_status", return_value={"paused": False}), \
                patch("random.shuffle", side_effect=lambda values: None), \
                patch("random.uniform", return_value=77), \
                patch.object(wb2api, "daily_checkin", return_value={
                    "checkin": False, "checkinRestricted": True,
                    "checkinMessage": "受限"}) as checkin, \
                patch.object(wb2api.time, "sleep") as sleep:
            wb2api._run_auto_checkin_cycle()
        self.assertEqual(checkin.call_count, 1)
        sleep.assert_not_called()

    def test_auto_checkin_spaces_out_accounts(self):
        a1, a2 = self.account(1, "cn"), self.account(2, "cn")
        self.add_accounts(a1, a2)
        wb2api._auto_checkin = True
        with patch.object(wb2api, "_supports_checkin", return_value=True), \
                patch.object(wb2api, "_pool_available", return_value=True), \
                patch.object(wb2api, "_risk_pause_status", return_value={"paused": False}), \
                patch("random.shuffle", side_effect=lambda values: None), \
                patch("random.uniform", return_value=77), \
                patch.object(wb2api, "daily_checkin", return_value={
                    "checkin": True, "checkinMessage": "成功"}) as checkin, \
                patch.object(wb2api.time, "sleep") as sleep:
            wb2api._run_auto_checkin_cycle()
        self.assertEqual(checkin.call_count, 2)
        sleep.assert_called_once_with(77)


if __name__ == "__main__":
    unittest.main()
