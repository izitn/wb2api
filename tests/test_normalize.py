#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求体归一（OpenAI 兼容层）的离线回归测试。"""

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

import wb2api as wb  # noqa: E402  （必须在设置 WB_DATA_DIR 之后导入）


class NormalizeChatBodyTests(unittest.TestCase):
    def test_forces_stream_and_usage(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(out["stream"])
        self.assertTrue(out["stream_options"]["include_usage"])

    def test_keeps_existing_stream_options(self):
        out = wb.normalize_chat_body({
            "messages": [{"role": "user", "content": "hi"}],
            "stream_options": {"foo": 1}})
        self.assertEqual(out["stream_options"]["foo"], 1)
        self.assertTrue(out["stream_options"]["include_usage"])

    def test_inserts_system_message_when_missing(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["messages"][0]["content"], wb.DEFAULT_SYSTEM)

    def test_keeps_existing_system_first(self):
        out = wb.normalize_chat_body({"messages": [
            {"role": "system", "content": "自定义"}, {"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["content"], "自定义")
        self.assertEqual(len(out["messages"]), 2)

    def test_developer_role_becomes_system(self):
        out = wb.normalize_chat_body({"messages": [
            {"role": "developer", "content": "规则"}, {"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["messages"][0]["content"], "规则")

    def test_max_completion_tokens_alias(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}],
                                      "max_completion_tokens": 256})
        self.assertEqual(out["max_tokens"], 256)
        self.assertNotIn("max_completion_tokens", out)

    def test_existing_max_tokens_wins(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}],
                                      "max_tokens": 64, "max_completion_tokens": 256})
        self.assertEqual(out["max_tokens"], 64)

    def test_invalid_alias_dropped(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}],
                                      "max_completion_tokens": 0})
        self.assertNotIn("max_tokens", out)

    def test_zero_max_tokens_is_omitted(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": "hi"}],
                                      "max_tokens": 0})
        self.assertNotIn("max_tokens", out)

    def test_tool_choice_object_forms(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(wb.normalize_chat_body(
            dict(base, tool_choice={"type": "auto"}))["tool_choice"], "auto")
        self.assertEqual(wb.normalize_chat_body(
            dict(base, tool_choice={"type": "required"}))["tool_choice"], "required")
        self.assertEqual(wb.normalize_chat_body(
            dict(base, tool_choice={"type": "function",
                                    "function": {"name": "echo"}}))["tool_choice"], "echo")
        # 无 type 字段的对象形式会被当作无效值删除
        self.assertNotIn("tool_choice", wb.normalize_chat_body(
            dict(base, tool_choice={"function": {"name": "echo"}})))

    def test_tool_choice_none_removes_tools(self):
        out = wb.normalize_chat_body({
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "none"},
            "tools": [{"type": "function", "function": {"name": "echo"}}],
        })
        self.assertNotIn("tool_choice", out)
        self.assertNotIn("tools", out)

    def test_image_url_string_becomes_object(self):
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": "https://example.com/a.png"}]}]})
        part = out["messages"][-1]["content"][0]
        self.assertEqual(part["image_url"], {"url": "https://example.com/a.png"})

    def test_image_url_object_untouched(self):
        original = {"url": "https://example.com/a.png"}
        out = wb.normalize_chat_body({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": dict(original)}]}]})
        self.assertEqual(out["messages"][-1]["content"][0]["image_url"], original)


class UsageNormalizationTests(unittest.TestCase):
    def test_total_tokens_filled(self):
        usage = wb.norm_usage({"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(usage["total_tokens"], 15)

    def test_cache_hit_mapped_to_cached_tokens(self):
        usage = wb.norm_usage({"prompt_tokens": 10, "completion_tokens": 5,
                               "prompt_cache_hit_tokens": 4})
        details = usage["prompt_tokens_details"]
        self.assertEqual(details["cached_tokens"], 4)

    def test_non_dict_passthrough(self):
        self.assertIsNone(wb.norm_usage(None))


if __name__ == "__main__":
    unittest.main()
