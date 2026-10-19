#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志安全测试：验证敏感数据不会泄漏到日志中。"""

import atexit
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402  （必须在设置 WB_DATA_DIR 之后导入）


class SafeLogPayloadTests(unittest.TestCase):
    def test_removes_messages(self):
        payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "secret"}]}
        safe = wb._safe_log_payload(payload)
        self.assertNotIn("messages", safe)
        self.assertEqual(safe["model"], "gpt-4")

    def test_removes_system(self):
        payload = {"model": "claude-3", "system": "You are a secret agent"}
        safe = wb._safe_log_payload(payload)
        self.assertNotIn("system", safe)
        self.assertEqual(safe["model"], "claude-3")

    def test_removes_prompt(self):
        payload = {"model": "claude-2", "prompt": "Confidential question"}
        safe = wb._safe_log_payload(payload)
        self.assertNotIn("prompt", safe)
        self.assertEqual(safe["model"], "claude-2")

    def test_keeps_metadata_fields(self):
        payload = {
            "model": "gpt-4",
            "temperature": 0.7,
            "max_tokens": 1000,
            "stream": True,
            "messages": [{"role": "user", "content": "secret"}]
        }
        safe = wb._safe_log_payload(payload)
        self.assertEqual(safe["model"], "gpt-4")
        self.assertEqual(safe["temperature"], 0.7)
        self.assertEqual(safe["max_tokens"], 1000)
        self.assertEqual(safe["stream"], True)
        self.assertNotIn("messages", safe)

    def test_removes_responses_api_fields(self):
        payload = {
            "model": "gpt-4",
            "input": "Confidential input text",
            "instructions": "Secret system instructions",
        }
        safe = wb._safe_log_payload(payload)
        self.assertNotIn("input", safe)
        self.assertNotIn("instructions", safe)
        self.assertEqual(safe["model"], "gpt-4")

    def test_non_dict_payload_returns_empty(self):
        self.assertEqual(wb._safe_log_payload(None), {})
        self.assertEqual(wb._safe_log_payload(["messages"]), {})
        self.assertEqual(wb._safe_log_payload("messages"), {})

    def test_empty_payload(self):
        safe = wb._safe_log_payload({})
        self.assertEqual(safe, {})

    def test_no_sensitive_fields(self):
        payload = {"model": "gpt-3.5", "temperature": 0.5}
        safe = wb._safe_log_payload(payload)
        self.assertEqual(safe, payload)


class DebugLogRedactionTests(unittest.TestCase):
    """端到端验证 DEBUG 日志：保留排查信息，但不写入用户对话正文。"""

    def setUp(self):
        self.saved = (wb.DEBUG, wb.UPSTREAM_RETRIES, wb._gzip_ok)
        self.addCleanup(self._restore)
        wb.DEBUG = True
        wb.UPSTREAM_RETRIES = 0
        wb._gzip_ok = False

    def _restore(self):
        wb.DEBUG, wb.UPSTREAM_RETRIES, wb._gzip_ok = self.saved

    def test_upstream_debug_log_does_not_leak_conversation(self):
        secret = "绝不能出现在日志里的用户对话"
        payload = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": secret},
                {"role": "user", "content": secret},
            ],
            "temperature": 0.5,
        }
        account = {"accessToken": "tok", "realm": "global", "uid": "u-1"}

        buf = io.StringIO()
        with mock.patch.object(wb, "upstream_request", side_effect=OSError("boom")):
            with redirect_stderr(buf):
                with self.assertRaises(OSError):
                    wb.call_upstream(payload, account)

        out = buf.getvalue()
        # 排查信息仍然保留
        self.assertIn("test-model", out)
        self.assertIn("messages=2", out)
        # 用户对话正文绝不落日志
        self.assertNotIn(secret, out)


class UsageErrorRedactionTests(unittest.TestCase):
    """usage.jsonl 里的错误摘要必须抹掉全部凭证字段。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (wb.USAGE_FILE, wb.LOCAL_API_KEY, wb._auth, wb._accounts)
        self.addCleanup(self._restore, saved)
        self.addCleanup(wb.close_usage)

        wb.USAGE_FILE = os.path.join(self.tmp, "usage.jsonl")
        wb.LOCAL_API_KEY = "sk-local-secret"
        wb._auth = wb._normalize_auth({
            "accessToken": "acc-secret", "refreshToken": "ref-secret",
            "device_token": "dev-secret", "realm": "global",
            "account": {"uid": "u-usage", "nickname": "用量账号"}})
        wb._accounts = {wb.account_id(wb._auth): wb._auth}

    def _restore(self, saved):
        wb.close_usage()
        wb.USAGE_FILE, wb.LOCAL_API_KEY, wb._auth, wb._accounts = saved

    def test_usage_error_redacts_every_credential(self):
        error = "boom local=%s acc=%s ref=%s dev=%s" % (
            wb.LOCAL_API_KEY,
            wb._auth["accessToken"],
            wb._auth["refreshToken"],
            wb._auth["deviceToken"])
        wb.record_usage("/v1/chat/completions", "m-1", 500, None, 0.1, None,
                        False, error, account=wb._auth)
        wb.close_usage()

        with open(wb.USAGE_FILE, encoding="utf-8") as f:
            raw = f.read()

        self.assertIn("[已隐藏]", raw)
        for secret in ("sk-local-secret", "acc-secret", "ref-secret", "dev-secret"):
            self.assertNotIn(secret, raw)


if __name__ == "__main__":
    unittest.main()
