#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for issues verified from the 2026-10-20 handoff."""

import atexit
import email.message
import os
import shutil
import socket
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
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, *_args):
        body, self._body = self._body, b""
        return body

    def close(self):
        pass


class UpstreamRetryTests(unittest.TestCase):
    def setUp(self):
        self.account = {"accessToken": "token", "uid": "retry-test", "realm": "global",
                        "domain": "www.workbuddy.ai"}
        self.saved = (wb._gzip_ok, wb.UPSTREAM_RETRIES)
        wb._gzip_ok = False
        wb.UPSTREAM_RETRIES = 2
        self.addCleanup(self._restore)

    def _restore(self):
        wb._gzip_ok, wb.UPSTREAM_RETRIES = self.saved

    def test_5xx_is_not_replayed(self):
        response = _Response(502, b'{"msg":"upstream failed"}')
        with mock.patch.object(wb, "upstream_request", return_value=response) as request:
            result = wb.call_upstream({"model": "m", "messages": []}, self.account)
        self.assertIs(result, response)
        self.assertEqual(request.call_count, 1)

    def test_ambiguous_disconnect_is_not_replayed(self):
        with mock.patch.object(wb, "upstream_request",
                               side_effect=ConnectionResetError("peer closed after request")) as request:
            with self.assertRaises(ConnectionResetError):
                wb.call_upstream({"model": "m", "messages": []}, self.account)
        self.assertEqual(request.call_count, 1)

    def test_429_retries_with_the_same_request_id(self):
        responses = [_Response(429, headers={"Retry-After": "1"}), _Response(200)]
        seen = []

        def request(_method, _url, headers, _body):
            seen.append((headers["X-Request-ID"], headers["X-Conversation-Request-ID"]))
            return responses.pop(0)

        with mock.patch.object(wb, "upstream_request", side_effect=request), \
                mock.patch.object(wb.time, "sleep"):
            wb.call_upstream({"model": "m", "messages": []}, self.account)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_gzip_business_400_is_returned_without_plain_retry(self):
        saved = (wb._gzip_ok, wb.GZIP_MIN)
        wb._gzip_ok, wb.GZIP_MIN = True, 1
        self.addCleanup(setattr, wb, "_gzip_ok", saved[0])
        self.addCleanup(setattr, wb, "GZIP_MIN", saved[1])
        base = wb.upstream_base(self.account)
        wb._gzip_disabled_bases.discard(base)
        self.addCleanup(wb._gzip_disabled_bases.discard, base)
        response = _Response(400, b'{"code":11102,"msg":"model not allowed"}')
        with mock.patch.object(wb, "upstream_request", return_value=response) as request:
            result = wb.call_upstream({"model": "m", "messages": [{"content": "x" * 100}]},
                                      self.account)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.read(), b'{"code":11102,"msg":"model not allowed"}')
        self.assertNotIn(base, wb._gzip_disabled_bases)


class ConnectionReuseTests(unittest.TestCase):
    def test_truncated_response_is_not_returned_to_pool(self):
        class Response:
            will_close = False
            chunked = False
            status = 200
            headers = email.message.Message()

            def isclosed(self):
                return True

            def read(self, *_args):
                raise EOFError("truncated")

            def close(self):
                pass

        class Connection:
            sock = None

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Pool:
            returned = False

            def put(self, *_args):
                self.returned = True

        connection, pool = Connection(), Pool()
        response = wb.UpstreamResponse(("http", "example", 80, ""), connection, Response())
        with mock.patch.object(wb, "POOL", pool):
            with self.assertRaises(EOFError):
                response.read()
            response.close()
        self.assertFalse(pool.returned)
        self.assertTrue(connection.closed)


class ChunkedDrainTests(unittest.TestCase):
    def test_rejected_chunked_body_is_drained_with_bounded_socket_timeout(self):
        client, peer = socket.socketpair()
        try:
            peer.sendall(b"4\r\ntest\r\n0\r\n\r\n")
            peer.shutdown(socket.SHUT_WR)
            handler = wb.Handler.__new__(wb.Handler)
            handler.connection = client
            handler.rfile = client.makefile("rb")
            handler.headers = email.message.Message()
            handler.headers["Transfer-Encoding"] = "chunked"
            handler.close_connection = False
            handler._body_err = None
            self.assertIsNone(handler._body())
            self.assertEqual(handler._body_err[0], 411)
            self.assertTrue(handler.close_connection)
            self.assertIsNone(client.gettimeout())
        finally:
            peer.close()
            client.close()


class PrivateWriteTests(unittest.TestCase):
    def test_concurrent_writes_leave_valid_complete_file(self):
        tmp = tempfile.mkdtemp(prefix="wb2api-private-write-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "state.json")
        barrier = threading.Barrier(8)

        def write(index):
            barrier.wait()
            wb._write_private(path, '{"writer": %d}' % index)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with open(path, encoding="utf-8") as f:
            self.assertRegex(f.read(), r'^\{"writer": [0-7]\}$')
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(tmp)))


class EnvironmentParsingTests(unittest.TestCase):
    def test_invalid_and_out_of_range_integer_settings_are_safe(self):
        with mock.patch.dict(os.environ, {"WB_TEST_INT": "broken"}):
            self.assertEqual(wb._env_int("WB_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"WB_TEST_INT": "-4"}):
            self.assertEqual(wb._env_int("WB_TEST_INT", 7, 1), 1)
        with mock.patch.dict(os.environ, {"WB_TEST_INT": "0"}):
            self.assertEqual(wb._env_int("WB_TEST_INT", 7), 0)


if __name__ == "__main__":
    unittest.main()
