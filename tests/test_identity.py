#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站身份（设备/会话标识）离线回归测试。

技术路线参考同类项目 wb_fingerprint.derive_id：真实客户端的 X-Machine-ID /
X-Session-ID 是带连字符的 UUID，且必须按账号派生而不是按进程派生。
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402


def _acct(uid, realm="cn"):
    return wb._normalize_auth({
        "accessToken": "tok-" + uid, "realm": realm,
        "account": {"uid": uid, "nickname": uid},
    })


class StableIdShapeTests(unittest.TestCase):
    def test_is_uuid_shape(self):
        """官方客户端的这两个头都是带连字符的 UUID，裸 hex 反而是网关特征。"""
        for purpose in ("machine", "session"):
            v = wb.stable_id("u-1", purpose)
            self.assertRegex(v, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}$",
                             "%s 不是 UUIDv4 形状：%s" % (purpose, v))
            uuid.UUID(v)  # 必须能被标准库解析

    def test_deterministic_across_calls(self):
        self.assertEqual(wb.stable_id("u-1", "machine"), wb.stable_id("u-1", "machine"))

    def test_distinct_per_account(self):
        ids = {wb.stable_id("u%d" % i, "machine") for i in range(50)}
        self.assertEqual(len(ids), 50, "不同账号必须派生出不同的设备标识")

    def test_distinct_per_purpose(self):
        self.assertNotEqual(wb.stable_id("u-1", "machine"), wb.stable_id("u-1", "session"))

    def test_salt_not_renamed(self):
        """盐值字面量不能变：改了会让已保存账号的设备指纹整体漂移。"""
        import hashlib
        uid = "u-1"
        h = hashlib.sha256(("wb2oai:machine:%s" % uid).encode()).hexdigest()
        h = h[:12] + "4" + h[13:16] + "a" + h[17:]
        expect = "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])
        self.assertEqual(wb.stable_id(uid, "machine"), expect)


class HeaderPlacementTests(unittest.TestCase):
    def test_common_headers_carry_ids(self):
        """计费/上报类请求需要这两个头，且按账号派生。"""
        h = wb.common_headers(_acct("u-1"))
        self.assertEqual(h["X-Machine-ID"], wb.stable_id("u-1", "machine"))
        self.assertEqual(h["X-Session-ID"], wb.stable_id("u-1", "session"))

    def test_chat_headers_drop_ids(self):
        """官方对话请求不带这两个头，带了反而是一处网关特征。"""
        h = wb.chat_headers(_acct("u-1"))
        self.assertNotIn("X-Machine-ID", h)
        self.assertNotIn("X-Session-ID", h)

    def test_chat_headers_have_session_family(self):
        h = wb.chat_headers(_acct("u-1"))
        for k in ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Root-Request-ID",
                  "X-Conversation-Message-ID", "X-Request-ID", "X-Trace-ID",
                  "traceparent", "b3", "X-B3-TraceId", "X-B3-SpanId", "X-B3-Sampled"):
            self.assertIn(k, h, "缺少会话/链路头：%s" % k)
        self.assertEqual(h["X-Conversation-Request-ID"], h["X-Root-Request-ID"])
        self.assertEqual(h["X-Conversation-Message-ID"], h["X-Request-ID"])
        uuid.UUID(h["X-Conversation-ID"])

    def test_chat_headers_are_fresh_per_request(self):
        """会话 id 必须每次现生成，不能沿用上一次的值。"""
        a = _acct("u-1")
        first = wb.chat_headers(a)
        second = wb.chat_headers(a)
        self.assertNotEqual(first["X-Conversation-ID"], second["X-Conversation-ID"])
        self.assertNotEqual(first["X-Request-ID"], second["X-Request-ID"])

    def test_two_accounts_do_not_share_device(self):
        """同一进程代表多个账号时，设备标识必须按账号隔离。"""
        a, b = wb.common_headers(_acct("u-1")), wb.common_headers(_acct("u-2"))
        self.assertNotEqual(a["X-Machine-ID"], b["X-Machine-ID"])
        self.assertNotEqual(a["X-Session-ID"], b["X-Session-ID"])

    def test_intl_headers_align_5_8_4(self):
        h = wb.chat_headers(_acct("u-1", "global"))
        self.assertEqual(h["X-IDE-Version"], wb.INTL_CLIENT_VERSION)
        self.assertEqual(h["X-Product"], "SaaS")
        self.assertEqual(h["X-Agent-Intent"], "craft")
        self.assertEqual(h["X-Agent-Type"], "main")
        self.assertEqual(h["X-Private-Data"], "true")
        self.assertIn("WorkBuddy/%s" % wb.INTL_CLIENT_VERSION, h["User-Agent"])

    def test_cn_headers_unchanged(self):
        h = wb.chat_headers(_acct("u-1", "cn"))
        self.assertEqual(h["X-IDE-Version"], wb.CLIENT_VERSION)
        self.assertEqual(h["X-Product"], "WorkBuddy")


if __name__ == "__main__":
    unittest.main()
