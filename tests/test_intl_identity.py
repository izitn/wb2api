#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际版出站身份（渠道校验）回归测试。

2026-10-19 起上游对国际版加强渠道校验：请求缺会话头族 / 身份头与官方客户端
不一致时返回 400 / code 11128（"request illegal"，displayTips 明说"没通过渠道校验"）。
这里把对 WorkBuddy 5.8.4 抓包得到的 wire 形状固定下来，防止后续改动回退。
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402

CONV_HEADERS = ("X-Conversation-ID", "X-Conversation-Request-ID",
                "X-Root-Request-ID", "X-Conversation-Message-ID")
HEX32_HEADERS = ("X-Request-ID", "X-Conversation-Request-ID",
                 "X-Root-Request-ID", "X-Conversation-Message-ID")


def account(realm="global"):
    if realm == "global":
        return {"accessToken": "tok", "uid": "u-1", "realm": "global",
                "domain": "www.workbuddy.ai", "enterpriseId": ""}
    return {"accessToken": "tok", "uid": "u-2", "realm": "cn",
            "domain": "www.codebuddy.cn", "enterpriseId": ""}


class IntlIdentityTests(unittest.TestCase):
    def setUp(self):
        self.h = wb.chat_headers(account("global"))

    def test_conversation_header_family_present(self):
        """缺会话头族会被上游判非官方客户端（400 / 11128）。"""
        for key in CONV_HEADERS:
            self.assertTrue(self.h.get(key), key)

    def test_id_shapes_and_relations(self):
        conv = self.h["X-Conversation-ID"]
        self.assertEqual(len(conv), 36)
        self.assertEqual(conv.count("-"), 4)
        for key in HEX32_HEADERS:
            self.assertEqual(len(self.h[key]), 32, key)
            self.assertTrue(all(c in "0123456789abcdef" for c in self.h[key]), key)
        # 官方语义：消息 id 与请求 id 同值，根请求 id 与会话请求 id 同值
        self.assertEqual(self.h["X-Conversation-Message-ID"], self.h["X-Request-ID"])
        self.assertEqual(self.h["X-Root-Request-ID"], self.h["X-Conversation-Request-ID"])

    def test_conversation_id_is_fresh_per_request(self):
        other = wb.chat_headers(account("global"))
        self.assertNotEqual(self.h["X-Conversation-ID"], other["X-Conversation-ID"])

    def test_desktop_identity_matches_capture(self):
        self.assertEqual(self.h["X-IDE-Version"], "5.8.4")
        self.assertEqual(self.h["User-Agent"],
                         "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0")
        self.assertEqual(self.h["X-Product"], "SaaS")
        self.assertEqual((self.h["X-Agent-Type"], self.h["X-Agent-Purpose"],
                          self.h["X-Agent-Intent"]), ("main", "conversation", "craft"))
        self.assertEqual(self.h["X-Private-Data"], "true")

    def test_trace_family_present_and_well_formed(self):
        for key in ("X-Trace-ID", "traceparent", "b3", "X-B3-TraceId",
                    "X-B3-SpanId", "X-B3-Sampled"):
            self.assertTrue(self.h.get(key), key)
        self.assertTrue(self.h["traceparent"].startswith("00-"))
        self.assertTrue(self.h["traceparent"].endswith("-01"))

    def test_chat_drops_feedback_only_ids(self):
        """X-Machine-ID / X-Session-ID 只用于反馈上报，官方对话请求不带。"""
        self.assertNotIn("X-Machine-ID", self.h)
        self.assertNotIn("X-Session-ID", self.h)

    def test_cn_identity_unchanged(self):
        """国内版没有对应抓包且目前正常，身份维持原样。"""
        h = wb.chat_headers(account("cn"))
        self.assertEqual(h["X-IDE-Version"], wb.CLIENT_VERSION)
        self.assertEqual(h["X-Product"], "WorkBuddy")
        self.assertEqual(h["User-Agent"], wb.desktop_ua(account("cn")))
        self.assertTrue(h.get("X-Conversation-ID"))

    def test_uuid7_is_time_ordered(self):
        a, b = wb._uuid7(), wb._uuid7()
        self.assertEqual(a.version, 7)
        # 同一毫秒内随机位不保序，只比较 48 位时间戳前缀
        self.assertLessEqual(a.hex[:12], b.hex[:12])


if __name__ == "__main__":
    unittest.main()
