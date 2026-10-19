#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Anthropic Messages 与上游 Chat Completions 之间转换的离线回归测试。

上游用构造的 SSE 字节流模拟，不建立任何网络连接。
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

import wb2api as wb  # noqa: E402  （必须在设置 WB_DATA_DIR 之后导入）


def sse(obj):
    """把一帧 JSON 编码成上游 SSE 的字节行。"""
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


DONE = b"data: [DONE]\n"


class AnthropicContentTests(unittest.TestCase):
    def test_plain_string_passthrough(self):
        self.assertEqual(wb._anthropic_content("你好"), "你好")

    def test_text_blocks_join_into_string(self):
        content = [{"type": "text", "text": "你"},
                   {"type": "text", "text": "好"}]
        self.assertEqual(wb._anthropic_content(content), "你好")

    def test_base64_image_becomes_data_url(self):
        content = [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "QUJD"}}]
        out = wb._anthropic_content(content)
        self.assertEqual(out[0]["type"], "image_url")
        self.assertEqual(out[0]["image_url"]["url"], "data:image/png;base64,QUJD")

    def test_url_image(self):
        content = [{"type": "image", "source": {
            "type": "url", "url": "https://example.com/a.png"}}]
        self.assertEqual(wb._anthropic_content(content)[0]["image_url"]["url"],
                         "https://example.com/a.png")

    def test_mixed_content_kept_as_blocks(self):
        content = [{"type": "text", "text": "看图"},
                   {"type": "image", "source": {
                       "type": "url", "url": "https://example.com/a.png"}}]
        out = wb._anthropic_content(content)
        self.assertEqual([p["type"] for p in out], ["text", "image_url"])

    def test_invalid_image_source_dropped(self):
        self.assertEqual(wb._anthropic_content([
            {"type": "image", "source": {"type": "base64"}}]), "")

    def test_non_list_content(self):
        self.assertEqual(wb._anthropic_content(None), "")
        self.assertEqual(wb._anthropic_content(42), "")


class AnthropicRequestTests(unittest.TestCase):
    def test_system_and_text_message(self):
        out = wb.anthropic_to_chat({
            "model": "claude-x", "max_tokens": 128,
            "system": "你是助手",
            "messages": [{"role": "user", "content": "你好"}],
        })
        self.assertTrue(out["stream"])
        self.assertEqual(out["model"], "claude-x")
        self.assertEqual(out["max_tokens"], 128)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "你好"})

    def test_system_blocks_are_concatenated(self):
        out = wb.anthropic_to_chat({
            "messages": [{"role": "user", "content": "hi"}],
            "system": [{"type": "text", "text": "A"},
                       {"type": "text", "text": "B"}],
        })
        self.assertEqual(out["messages"][0]["content"], "AB")

    def test_tool_use_becomes_tool_calls(self):
        out = wb.anthropic_to_chat({"messages": [
            {"role": "assistant", "content": [
                {"type": "text", "text": "我来查"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "北京"}}]},
        ]})
        msg = out["messages"][0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "我来查")
        call = msg["tool_calls"][0]
        self.assertEqual(call["id"], "toolu_1")
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "北京"})

    def test_tool_result_becomes_tool_message(self):
        out = wb.anthropic_to_chat({"messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "晴"}]}]},
        ]})
        tool_msg = out["messages"][0]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["tool_call_id"], "toolu_1")
        self.assertEqual(tool_msg["content"], "晴")

    def test_multiple_tool_results_emit_multiple_messages(self):
        out = wb.anthropic_to_chat({"messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                {"type": "tool_result", "tool_use_id": "b", "content": "2"}]},
        ]})
        self.assertEqual([m["role"] for m in out["messages"]], ["tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in out["messages"]], ["a", "b"])

    def test_tools_mapping(self):
        out = wb.anthropic_to_chat({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "echo", "description": "回显",
                       "input_schema": {"type": "object",
                                        "properties": {"text": {"type": "string"}}}}],
        })
        tool = out["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "echo")
        self.assertEqual(tool["function"]["parameters"]["properties"]["text"]["type"],
                         "string")

    def test_tool_choice_mapping(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(wb.anthropic_to_chat(
            dict(base, tool_choice={"type": "any"}))["tool_choice"], "required")
        self.assertEqual(wb.anthropic_to_chat(
            dict(base, tool_choice={"type": "auto"}))["tool_choice"], "auto")
        self.assertEqual(wb.anthropic_to_chat(
            dict(base, tool_choice={"type": "none"}))["tool_choice"], "none")
        named = wb.anthropic_to_chat(
            dict(base, tool_choice={"type": "tool", "name": "echo"}))
        self.assertEqual(named["tool_choice"],
                         {"type": "function", "function": {"name": "echo"}})

    def test_disable_parallel_tool_use(self):
        out = wb.anthropic_to_chat({
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        })
        self.assertFalse(out["parallel_tool_calls"])

    def test_sampling_fields_and_metadata(self):
        out = wb.anthropic_to_chat({
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2, "top_p": 0.9, "top_k": 40,
            "stop_sequences": ["END"], "metadata": {"user_id": "u-9"},
        })
        self.assertEqual(out["temperature"], 0.2)
        self.assertEqual(out["top_p"], 0.9)
        self.assertEqual(out["top_k"], 40)
        self.assertEqual(out["stop"], ["END"])
        self.assertEqual(out["user"], "u-9")


class AnthropicUsageTests(unittest.TestCase):
    def test_basic_usage(self):
        out = wb._anthropic_usage({"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(out, {"input_tokens": 100, "output_tokens": 20})

    def test_cache_hit_is_subtracted_and_reported(self):
        out = wb._anthropic_usage({"prompt_tokens": 100, "completion_tokens": 5,
                                   "prompt_cache_hit_tokens": 40})
        self.assertEqual(out["cache_read_input_tokens"], 40)
        self.assertEqual(out["input_tokens"], 60)

    def test_missing_usage(self):
        self.assertEqual(wb._anthropic_usage(None),
                         {"input_tokens": 0, "output_tokens": 0})


class ChatToAnthropicTests(unittest.TestCase):
    def _accumulate(self, *chunks):
        acc = wb.ChatAccumulator()
        for chunk in chunks:
            acc.feed(chunk)
        return acc

    def test_text_message(self):
        acc = self._accumulate(
            {"model": "up-model", "choices": [{"index": 0,
                                               "delta": {"content": "你好"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        msg = wb.chat_to_anthropic(acc, "fallback", "msg_1")
        self.assertEqual(msg["type"], "message")
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["model"], "up-model")
        self.assertEqual(msg["content"], [{"type": "text", "text": "你好"}])
        self.assertEqual(msg["stop_reason"], "end_turn")
        self.assertEqual(msg["usage"]["input_tokens"], 10)

    def test_stop_reason_mapping(self):
        cases = [("length", "max_tokens"), ("content_filter", "refusal")]
        for finish, expected in cases:
            acc = self._accumulate(
                {"choices": [{"index": 0, "delta": {"content": "x"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            self.assertEqual(
                wb.chat_to_anthropic(acc, "m", "id")["stop_reason"], expected)

    def test_parallel_tool_calls(self):
        acc = self._accumulate(
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function",
                 "function": {"name": "a", "arguments": "{\"x\":1}"}},
                {"index": 1, "id": "call_b", "type": "function",
                 "function": {"name": "b", "arguments": "{\"y\":2}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        msg = wb.chat_to_anthropic(acc, "m", "id")
        tools = [b for b in msg["content"] if b["type"] == "tool_use"]
        self.assertEqual([t["name"] for t in tools], ["a", "b"])
        self.assertEqual(tools[0]["input"], {"x": 1})
        self.assertEqual(msg["stop_reason"], "tool_use")

    def test_invalid_tool_arguments_raise(self):
        acc = self._accumulate(
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function",
                 "function": {"name": "a", "arguments": "{oops"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        with self.assertRaises(wb.UpstreamError):
            wb.chat_to_anthropic(acc, "m", "id")


class AnthropicStreamTests(unittest.TestCase):
    def test_text_stream(self):
        chunks = [
            sse({"id": "c1", "model": "up-model",
                 "choices": [{"index": 0, "delta": {"content": "你好"}}]}),
            sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2}}),
            DONE,
        ]
        acc = wb.ChatAccumulator()
        body = "".join(wb.anthropic_stream(chunks, "model-x", acc))

        self.assertIn("event: message_start", body)
        self.assertIn("content_block_start", body)
        self.assertIn("你好", body)
        self.assertIn('"stop_reason":"end_turn"', body)
        self.assertEqual(body.count("event: message_stop"), 1)

    def test_tool_call_stream(self):
        chunks = [
            sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"city\":"}}]}}]}),
            sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "\"北京\"}"}}]}}]}),
            sse({"choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls"}]}),
            DONE,
        ]
        acc = wb.ChatAccumulator()
        body = "".join(wb.anthropic_stream(chunks, "model-x", acc))

        self.assertIn('"type":"tool_use"', body.replace(" ", ""))
        self.assertIn("get_weather", body)
        self.assertIn("input_json_delta", body)
        self.assertIn('"stop_reason":"tool_use"', body)

    def test_error_frame_is_reported_in_stream(self):
        chunks = [
            sse({"choices": [{"index": 0, "delta": {"content": "半"}}]}),
            sse({"error": {"message": "上游炸了", "code": "boom"}}),
        ]
        acc = wb.ChatAccumulator()
        body = "".join(wb.anthropic_stream(chunks, "model-x", acc))
        self.assertIn("event: error", body)
        self.assertIn("上游炸了", body)

    def test_missing_finish_reason_is_reported(self):
        chunks = [sse({"choices": [{"index": 0, "delta": {"content": "截断"}}]}), DONE]
        acc = wb.ChatAccumulator()
        body = "".join(wb.anthropic_stream(chunks, "model-x", acc))
        self.assertIn("event: error", body)
        self.assertIn("缺少 finish_reason", body)


if __name__ == "__main__":
    unittest.main()
