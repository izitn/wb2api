#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对交接审查修复的 v2 离线回归测试。"""

import atexit
import email.message
import io
import os
import shutil
import sys
import tempfile
import threading
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


class _Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, *_args):
        body, self.body = self.body, b""
        return body

    def close(self):
        pass


class UpstreamSafetyTests(unittest.TestCase):
    def setUp(self):
        self.account = {"accessToken": "token", "uid": "handoff-v2",
                        "realm": "global", "domain": "www.workbuddy.ai"}
        self.saved = wb._gzip_ok, wb.GZIP_MIN, wb.UPSTREAM_RETRIES
        wb._gzip_ok, wb.GZIP_MIN, wb.UPSTREAM_RETRIES = True, 1, 2
        self.base = wb.upstream_base(self.account)
        with wb._gzip_stats_lock:
            wb._gzip_disabled_bases.pop(self.base, None)
        self.addCleanup(self.restore)

    def restore(self):
        wb._gzip_ok, wb.GZIP_MIN, wb.UPSTREAM_RETRIES = self.saved
        with wb._gzip_stats_lock:
            wb._gzip_disabled_bases.pop(self.base, None)

    def invoke(self, responses):
        with mock.patch.object(wb, "upstream_request", side_effect=responses) as request, \
                mock.patch.object(wb.time, "sleep"):
            result = wb.call_upstream({"model": "m", "messages": [{"content": "x" * 100}]},
                                      self.account)
        return result, request

    def test_5xx_is_returned_without_replay(self):
        response = _Response(502, b'{"message":"temporary error"}')
        result, request = self.invoke([response])
        self.assertIs(result, response)
        self.assertEqual(request.call_count, 1)

    def test_429_retries_with_stable_request_ids(self):
        seen = []

        def request(_method, _url, headers, _body):
            seen.append(tuple(headers[name] for name in (
                "X-Request-ID", "X-Conversation-Message-ID",
                "X-Conversation-Request-ID", "X-Root-Request-ID")))
            if len(seen) == 1:
                response = _Response(429)
                response.headers["Retry-After"] = "1"
                return response
            return _Response(200)

        with mock.patch.object(wb, "upstream_request", side_effect=request) as send, \
                mock.patch.object(wb.time, "sleep"):
            wb.call_upstream({"model": "m", "messages": []}, self.account)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(seen[0], seen[1])

    def test_private_request_id_is_not_serialized_into_body(self):
        captured = {}
        response = _Response(200)

        def request(_method, _url, headers, body):
            captured.update(headers=headers, body=body)
            return response

        with mock.patch.object(wb, "upstream_request", side_effect=request):
            wb.call_upstream({"model": "m", "messages": [],
                              "_wb2api_request_id": "logical-id"}, self.account)
        self.assertEqual(captured["headers"]["X-Request-ID"], "logical-id")
        self.assertNotIn(b"_wb2api_request_id", captured["body"])
        self.assertNotIn(b"logical-id", captured["body"])

    def test_ambiguous_disconnect_is_not_replayed(self):
        with mock.patch.object(wb, "upstream_request",
                               side_effect=ConnectionResetError("after request write")) as request:
            with self.assertRaises(ConnectionResetError):
                wb.call_upstream({"model": "m", "messages": []}, self.account)
        self.assertEqual(request.call_count, 1)

    def test_only_identified_gzip_rejection_disables_gzip_for_base(self):
        rejection = _Response(415, b'{"message":"unsupported Content-Encoding gzip"}')
        success = _Response(200)
        result, request = self.invoke([rejection, success])
        self.assertIs(result, success)
        self.assertEqual(request.call_count, 2)
        self.assertIn(self.base, wb._gzip_disabled_bases)

    def test_business_400_is_preserved_without_plain_retry(self):
        response = _Response(400, b'{"code":11102,"msg":"model not allowed"}')
        result, request = self.invoke([response])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.read(), b'{"code":11102,"msg":"model not allowed"}')
        self.assertNotIn(self.base, wb._gzip_disabled_bases)

    def test_request_write_disconnect_on_reused_socket_is_not_replayed(self):
        class Connection:
            sock = object()

            def request(self, *_args, **_kwargs):
                raise ConnectionResetError("write outcome is ambiguous")

            def close(self):
                pass

        pool = mock.Mock()
        pool.get.return_value = (("https", "example.test", 443, ""), Connection(), True)
        with mock.patch.object(wb, "POOL", pool):
            with self.assertRaises(ConnectionResetError):
                wb.upstream_request("POST", "https://example.test/chat", {}, b"body")
        self.assertEqual(pool.get.call_count, 1)


class UpstreamResponsePoolTests(unittest.TestCase):
    def test_truncated_body_is_not_returned_to_pool(self):
        class Response:
            will_close = False
            chunked = False
            status = 200
            headers = email.message.Message()

            def isclosed(self):
                return True

            def read(self, *_args):
                raise EOFError("truncated body")

            def close(self):
                pass

        class Connection:
            sock = None

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        connection = Connection()
        pool = mock.Mock()
        response = wb.UpstreamResponse(("https", "example.test", 443, ""),
                                       connection, Response())
        with mock.patch.object(wb, "POOL", pool):
            with self.assertRaises(EOFError):
                response.read()
            response.close()
        pool.put.assert_not_called()
        self.assertTrue(connection.closed)


class PrivateWriteConcurrencyTests(unittest.TestCase):
    def test_concurrent_private_writes_leave_one_complete_file(self):
        directory = tempfile.mkdtemp(prefix="wb2api-v2-write-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "auth.json")
        barrier = threading.Barrier(8)

        def write(index):
            barrier.wait()
            wb._write_private(path, '{"writer":%d}' % index)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with open(path, encoding="utf-8") as f:
            self.assertRegex(f.read(), r'^\{"writer":[0-7]\}$')
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(directory)))


class EnvironmentParsingTests(unittest.TestCase):
    def test_invalid_and_negative_values_are_clamped(self):
        with mock.patch.dict(os.environ, {"WB_TEST_INT": "invalid"}):
            self.assertEqual(wb._env_int("WB_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"WB_TEST_INT": "-2"}):
            self.assertEqual(wb._env_int("WB_TEST_INT", 7, 1), 1)
        with mock.patch.dict(os.environ, {"WB_TEST_FLOAT": "nan"}):
            self.assertEqual(wb._env_float("WB_TEST_FLOAT", 7.5), 7.5)

    def test_port_range_is_clamped(self):
        with mock.patch.dict(os.environ, {"WB_TEST_PORT": "0"}):
            self.assertEqual(wb._env_int("WB_TEST_PORT", 8787, 1, 65535), 1)
        with mock.patch.dict(os.environ, {"WB_TEST_PORT": "70000"}):
            self.assertEqual(wb._env_int("WB_TEST_PORT", 8787, 1, 65535), 65535)


class ChunkedDrainTests(unittest.TestCase):
    def make_handler(self, payload):
        handler = wb.Handler.__new__(wb.Handler)
        handler.connection = mock.Mock()
        handler.connection.gettimeout.return_value = 9
        handler.rfile = io.BytesIO(payload)
        return handler

    def test_complete_chunked_body_is_drained_and_timeout_restored(self):
        handler = self.make_handler(b"3\r\nabc\r\n0\r\nX-Test: yes\r\n\r\nNEXT")
        handler._drain_chunked()
        self.assertEqual(handler.rfile.read(), b"NEXT")
        self.assertEqual(handler.connection.settimeout.call_args_list[-1].args, (9,))

    def test_chunk_larger_than_budget_stops_before_reading_body(self):
        handler = self.make_handler(b"10\r\n" + b"x" * 16 + b"\r\n")
        handler._drain_chunked(max_bytes=12)
        self.assertEqual(handler.rfile.read(), b"x" * 16 + b"\r\n")


class RequestCompatibilityTests(unittest.TestCase):
    def test_stream_string_is_normalized(self):
        body = {"stream": "false", "messages": [{"role": "user", "content": "hi"}]}
        wb.validate_request("/v1/chat/completions", body)
        self.assertIs(body["stream"], False)

    def test_zero_token_limit_is_accepted_and_omitted(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 0}
        wb.validate_request("/v1/chat/completions", body)
        self.assertNotIn("max_tokens", wb.normalize_chat_body(body))

    def test_responses_instructions_can_supply_empty_input(self):
        wb.validate_request("/v1/responses", {"instructions": "Be concise"})

    def test_anthropic_server_side_tools_are_rejected(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10,
                "tools": [{"type": "computer_20250124", "name": "computer"}]}
        with self.assertRaisesRegex(ValueError, "服务端工具"):
            wb.validate_request("/v1/messages", body)


if __name__ == "__main__":
    unittest.main()
