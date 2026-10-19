#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Responses API 适配的离线回归测试。

覆盖：缺 finish_reason 必须按失败收尾、usage 的 cached_tokens 映射、
allowed_tools 白名单过滤、历史缓存写入失败降级、提示去重集合上限。
"""

import atexit
import json
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


def sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


DONE = b"data: [DONE]\n"


def text_chunks(finish="stop"):
    chunks = [sse({"id": "c1", "model": "up-model",
                   "choices": [{"index": 0, "delta": {"content": "你好"}}]})]
    if finish is not None:
        chunks.append(sse({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": finish}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 2}}))
    chunks.append(DONE)
    return chunks


class ResponsesStreamTests(unittest.TestCase):
    def run_stream(self, chunks):
        ctx = wb.new_rctx()
        ctx["store"] = False          # 不往全局历史里写
        ctx["owner"] = "test-owner"
        acc = wb.ChatAccumulator()
        body = "".join(wb.responses_stream(chunks, "model-x", "resp_1", ctx, acc))
        return body, acc

    def test_missing_finish_reason_fails_instead_of_completed(self):
        body, acc = self.run_stream(text_chunks(finish=None))
        self.assertIn("response.failed", body)
        self.assertNotIn("response.completed", body)
        self.assertIn("finish_reason", acc.error)

    def test_normal_stream_completes(self):
        body, acc = self.run_stream(text_chunks())
        self.assertIn("response.completed", body)
        self.assertNotIn("response.failed", body)

    def test_length_finish_is_incomplete(self):
        body, _ = self.run_stream(text_chunks(finish="length"))
        self.assertIn("response.incomplete", body)
        self.assertIn("max_output_tokens", body)


class ResponsesUsageTests(unittest.TestCase):
    def test_maps_prompt_cache_hit_tokens(self):
        out = wb._responses_usage({"prompt_tokens": 100, "completion_tokens": 5,
                                   "prompt_cache_hit_tokens": 40})
        self.assertEqual(out["input_tokens_details"]["cached_tokens"], 40)

    def test_maps_cache_read_input_tokens(self):
        out = wb._responses_usage({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cache_read_input_tokens": 25})
        self.assertEqual(out["input_tokens_details"]["cached_tokens"], 25)

    def test_maps_prompt_tokens_details(self):
        out = wb._responses_usage({"prompt_tokens": 10, "completion_tokens": 1,
                                   "prompt_tokens_details": {"cached_tokens": 7}})
        self.assertEqual(out["input_tokens_details"]["cached_tokens"], 7)

    def test_no_cache_omits_details(self):
        out = wb._responses_usage({"prompt_tokens": 10, "completion_tokens": 1})
        self.assertNotIn("input_tokens_details", out)

    def test_missing_usage_zeroed(self):
        out = wb._responses_usage(None)
        self.assertEqual(out["input_tokens"], 0)
        self.assertEqual(out["output_tokens"], 0)

    def test_does_not_mutate_caller_dict(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 1,
                 "prompt_cache_hit_tokens": 3}
        wb._responses_usage(usage)
        self.assertNotIn("total_tokens", usage)


class AllowedToolsTests(unittest.TestCase):
    def build(self, allowed):
        body = {
            "input": "hi",
            "tools": [
                {"type": "function", "name": "keep",
                 "parameters": {"type": "object"}},
                {"type": "function", "name": "drop",
                 "parameters": {"type": "object"}},
            ],
            "tool_choice": {"type": "allowed_tools", "mode": "auto",
                            "tools": allowed},
        }
        chat, ctx = wb.responses_to_chat(body)
        return chat

    def test_filters_tools_by_allowlist(self):
        chat = self.build(["keep"])
        names = [t["function"]["name"] for t in chat.get("tools") or []]
        self.assertEqual(names, ["keep"])
        self.assertEqual(chat.get("tool_choice"), "auto")

    def test_empty_allowlist_removes_all_tools(self):
        chat = self.build([])
        self.assertNotIn("tools", chat)
        self.assertNotIn("tool_choice", chat)

    def test_allowlist_without_match_drops_tool_choice(self):
        chat = self.build(["nonexistent"])
        self.assertNotIn("tools", chat)
        self.assertNotIn("tool_choice", chat)


class RespStoreRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.store = wb._RespStore()

    def test_lone_surrogate_does_not_raise(self):
        stored = self.store.put("r1", [{"role": "user", "content": "\ud800"}], "o")
        self.assertTrue(stored)
        got = self.store.get("r1", "o")
        self.assertEqual(len(got), 1)

    def test_oversized_history_returns_false(self):
        old = wb.RESP_STORE_BYTES
        try:
            wb.RESP_STORE_BYTES = 128
            self.assertFalse(self.store.put("r2", [{"content": "x" * 500}], "o"))
            self.assertIsNone(self.store.get("r2", "o"))
        finally:
            wb.RESP_STORE_BYTES = old

    def test_owner_isolation(self):
        self.store.put("r3", [{"role": "user", "content": "a"}], "owner-a")
        self.assertIsNotNone(self.store.get("r3", "owner-a"))
        self.assertIsNone(self.store.get("r3", "owner-b"))


class WarnOnceTests(unittest.TestCase):
    def setUp(self):
        saved = set(wb._warned)
        self.addCleanup(self._restore, saved)
        self.addCleanup(setattr, wb, "log", wb.log)
        wb._warned.clear()
        wb.log = lambda msg: None        # 测试不刷屏

    def _restore(self, saved):
        wb._warned.clear()
        wb._warned.update(saved)

    def test_caps_set_size(self):
        for i in range(wb._WARNED_MAX + 50):
            wb._warn_once("msg-%d" % i)
        self.assertLessEqual(len(wb._warned), wb._WARNED_MAX)

    def test_deduplicates(self):
        wb._warn_once("same")
        wb._warn_once("same")
        self.assertEqual(len(wb._warned), 1)

    def test_sanitizes_newlines_and_truncates(self):
        wb._warn_once("line1\nline2\r\n" + "x" * 2000)
        stored = next(iter(wb._warned))
        self.assertNotIn("\n", stored)
        self.assertNotIn("\r", stored)
        self.assertLessEqual(len(stored), 500)


if __name__ == "__main__":
    unittest.main()
