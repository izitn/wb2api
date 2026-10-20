#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号池换号的端到端回归：经 _completion 走完整换号循环。

上游用假对象替换，不产生真实网络请求。
"""

import atexit
import email.message
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402

#: 合法的最小对话消息体（validate_request 会检查 role/content 形态）
MSGS = [{"role": "user", "content": "hello"}]


def _acct(uid, realm="cn"):
    return wb._normalize_auth({
        "accessToken": "tok-" + uid, "realm": realm,
        "account": {"uid": uid, "nickname": uid},
    })


def make_handler(path="/v1/chat/completions", headers=None, body=b""):
    h = wb.Handler.__new__(wb.Handler)
    h.path = path
    h.requestline = "POST %s HTTP/1.1" % path
    h.command = "POST"
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers = msg
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.close_connection = False
    h._body_err = None
    h._last_error = ""
    return h


class _FakeResp:
    """最小可用的假上游响应：一个 SSE 帧 + 结束。"""

    def __init__(self, status=200, ctype="text/event-stream"):
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype
        self.closed = False

    def read(self):
        return b""

    def close(self):
        self.closed = True

    def __iter__(self):
        # iter_chunks 读的是 SSE 原始字节行（b"data: {...}\n"），不是解析后的 dict
        yield b'data: ' + json.dumps({"choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "hi"}}]}).encode() + b"\n\n"
        yield b'data: ' + json.dumps({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode() + b"\n\n"
        yield b"data: [DONE]\n\n"


class FailoverE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = (wb._auth, wb._accounts, wb.USAGE_FILE, wb.POOL_ENABLED)
        self.addCleanup(self._restore)
        wb.USAGE_FILE = os.path.join(self.tmp, "usage.jsonl")
        en = _acct("en", "cn")
        two = _acct("two", "cn")
        for a in (en, two):
            a["accountId"] = wb.account_id(a)
        wb._accounts = {wb.account_id(en): en, wb.account_id(two): two}
        wb._auth = en
        with wb._pool_lock:
            wb._pool_health.clear()
            wb._pool_sticky.clear()
            wb._pool_stats.update({"switch": 0, "exhausted": 0})

    def _restore(self):
        wb._auth, wb._accounts, wb.USAGE_FILE, wb.POOL_ENABLED = self._saved
        with wb._pool_lock:
            wb._pool_health.clear()
            wb._pool_sticky.clear()

    def _call(self, handler, body):
        """跑一次 _completion，返回 (captured, 调用过的账号 id 列表)。"""
        used = []
        # 让“池里第一个被选中的账号”永远报 429：这样能稳定验证换号，
        # 又不依赖 account id 的排序（当前账号优先是首位）。
        bad = [wb.account_id(wb._pool_pick("cn"))]

        def fake_call(payload, a):
            aid = wb.account_id(a)
            used.append(aid)
            if aid == bad[0]:
                raise wb.UpstreamError(429, "rate limited")
            return _FakeResp()

        with mock.patch.object(wb, "call_upstream", side_effect=fake_call), \
             mock.patch.object(wb, "resolve_model", side_effect=lambda m, a: (m or "m1", None)), \
             mock.patch.object(wb, "normalize_chat_body", side_effect=lambda b, a=None: b):
            captured = {}
            handler._json = lambda code, obj, headers=None: captured.update(code=code, obj=obj)
            handler._error = lambda code, msg, etype="", **kw: captured.update(code=code, err=msg)
            handler._completion("/v1/chat/completions", dict(body))
        return captured, used

    def test_fails_over_to_second_account(self):
        """坏账号 429 时，请求应自动换到同池另一个账号并成功。"""
        captured, used = self._call(make_handler(), {"model": "m1", "messages": MSGS})
        self.assertEqual(captured.get("code"), 200, captured)
        self.assertEqual(len(used), 2, "应尝试两次：失败账号 + 换号后账号")
        self.assertEqual(len(set(used)), 2, "两次调用必须是不同账号")
        self.assertEqual(wb._pool_stats["switch"], 1)

    def test_ambiguous_connection_error_does_not_fail_over(self):
        h = make_handler()
        err = ConnectionResetError("connection dropped after request write")
        used = []

        def fail_after_send(_payload, account):
            used.append(wb.account_id(account))
            raise err

        with mock.patch.object(wb, "call_upstream", side_effect=fail_after_send), \
             mock.patch.object(wb, "resolve_model", side_effect=lambda m, a: (m or "m1", None)), \
             mock.patch.object(wb, "normalize_chat_body", side_effect=lambda b, a=None: b):
            captured = {}
            h._json = lambda code, obj, headers=None: captured.update(code=code, obj=obj)
            h._error = lambda code, msg, etype="", **kw: captured.update(code=code, err=msg)
            h._completion("/v1/chat/completions", {"model": "m1", "messages": MSGS})
        self.assertEqual(len(used), 1)
        self.assertEqual(captured.get("code"), 502)

    def test_bad_account_goes_into_cooldown(self):
        """换号后坏账号应进入冷却，下次直接跳过它。"""
        self._call(make_handler(), {"model": "m1", "messages": MSGS})
        cooled = [aid for aid, h in wb._pool_health.items() if h["cooldownUntil"] > time.time()]
        self.assertEqual(len(cooled), 1, "恰好一个账号应被冷却")

    def test_single_account_does_not_switch(self):
        """只有一个账号时不应换号，直接返回上游错误。"""
        keep = wb.account_id(wb._auth)
        wb._accounts = {k: v for k, v in wb._accounts.items() if wb.account_id(v) == keep}
        h = make_handler()
        with mock.patch.object(wb, "call_upstream", side_effect=wb.UpstreamError(429, "rate limited")), \
             mock.patch.object(wb, "resolve_model", side_effect=lambda m, a: (m or "m1", None)), \
             mock.patch.object(wb, "normalize_chat_body", side_effect=lambda b, a=None: b):
            captured = {}
            h._json = lambda code, obj, headers=None: captured.update(code=code, obj=obj)
            h._error = lambda code, msg, etype="", **kw: captured.update(code=code, err=msg)
            h._completion("/v1/chat/completions", {"model": "m1", "messages": MSGS})
        self.assertEqual(captured.get("code"), 429)
        self.assertEqual(wb._pool_stats["switch"], 0)

    def test_disabled_pool_keeps_old_behavior(self):
        """WB_POOL=0 时退化为原行为：不换号。"""
        wb.POOL_ENABLED = False
        h = make_handler()
        with mock.patch.object(wb, "call_upstream", side_effect=wb.UpstreamError(429, "rate limited")), \
             mock.patch.object(wb, "resolve_model", side_effect=lambda m, a: (m or "m1", None)), \
             mock.patch.object(wb, "normalize_chat_body", side_effect=lambda b, a=None: b):
            captured = {}
            h._json = lambda code, obj, headers=None: captured.update(code=code, obj=obj)
            h._error = lambda code, msg, etype="", **kw: captured.update(code=code, err=msg)
            h._completion("/v1/chat/completions", {"model": "m1", "messages": MSGS})
        self.assertEqual(captured.get("code"), 429)
        self.assertEqual(wb._pool_stats["switch"], 0)

    def test_current_account_unchanged_after_failover(self):
        """换号只影响本次请求，不能把控制台的当前账号改掉。"""
        before = wb.account_id(wb._auth)
        self._call(make_handler(), {"model": "m1", "messages": MSGS})
        self.assertEqual(wb.account_id(wb._auth), before)

    def test_recently_cooled_account_is_skipped_first(self):
        """已冷却的账号在下一个请求里应被直接跳过，只调用一次上游。"""
        self._call(make_handler(), {"model": "m1", "messages": MSGS})
        used2 = []
        with mock.patch.object(wb, "call_upstream",
                               side_effect=lambda p, a: (used2.append(wb.account_id(a)), _FakeResp())[1]), \
             mock.patch.object(wb, "resolve_model", side_effect=lambda m, a: (m or "m1", None)), \
             mock.patch.object(wb, "normalize_chat_body", side_effect=lambda b, a=None: b):
            h = make_handler()
            captured = {}
            h._json = lambda code, obj, headers=None: captured.update(code=code, obj=obj)
            h._error = lambda code, msg, etype="", **kw: captured.update(code=code, err=msg)
            h._completion("/v1/chat/completions", {"model": "m1", "messages": MSGS})
        self.assertEqual(captured.get("code"), 200)
        self.assertEqual(len(used2), 1, "冷却账号不该再被尝试")


class SessionKeyTests(unittest.TestCase):
    def test_header_session(self):
        h = make_handler(headers={"X-Session-Id": "abc"})
        self.assertEqual(wb._pool_session_key(h), "h:abc")

    def test_conversation_header(self):
        h = make_handler(headers={"X-Conversation-Id": "conv-1"})
        self.assertEqual(wb._pool_session_key(h), "h:conv-1")

    def test_no_header_empty(self):
        self.assertEqual(wb._pool_session_key(make_handler()), "")

    def test_blank_header_ignored(self):
        h = make_handler(headers={"X-Session-Id": "   "})
        self.assertEqual(wb._pool_session_key(h), "")


if __name__ == "__main__":
    unittest.main()
