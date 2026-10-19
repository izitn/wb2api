#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 路由归一、鉴权与请求体边界的离线回归测试。

Handler 实例通过 __new__ 构造，不开启真实 socket 连接。
"""

import atexit
import email.message
import io
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


def make_handler(path="/v1/models", headers=None, body=b""):
    """构造一个假 Handler，只填充 _route 和 _body 需要的字段。"""
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
    return h


class RouteNormalizationTests(unittest.TestCase):
    def test_keeps_v1_prefix(self):
        h = make_handler("/v1/models")
        self.assertEqual(h._route(), "/v1/models")

    def test_strips_trailing_slash(self):
        h = make_handler("/v1/models/")
        self.assertEqual(h._route(), "/v1/models")

    def test_inserts_v1_for_known_openai_paths(self):
        for p in ("/models", "/chat/completions", "/responses", "/messages"):
            h = make_handler(p)
            self.assertEqual(h._route(), "/v1" + p)

    def test_leaves_unknown_paths_alone(self):
        h = make_handler("/admin/models")
        self.assertEqual(h._route(), "/admin/models")

    def test_handles_absolute_form_requests(self):
        h = make_handler("http://127.0.0.1:8787/v1/chat/completions?stream=1")
        self.assertEqual(h._route(), "/v1/chat/completions")

    def test_root_stays_root(self):
        h = make_handler("/")
        self.assertEqual(h._route(), "/")


class HostParsingTests(unittest.TestCase):
    def test_strips_port_from_ipv4(self):
        self.assertEqual(wb._host_name("127.0.0.1:8787"), "127.0.0.1")
        self.assertEqual(wb._host_name("192.168.1.1:9000"), "192.168.1.1")

    def test_strips_port_from_ipv6(self):
        self.assertEqual(wb._host_name("[::1]:8787"), "[::1]")
        self.assertEqual(wb._host_name("[fe80::1]:9000"), "[fe80::1]")

    def test_hostname_without_port(self):
        self.assertEqual(wb._host_name("localhost"), "localhost")
        self.assertEqual(wb._host_name("example.com"), "example.com")

    def test_is_loopback_ipv4(self):
        self.assertTrue(wb._is_loopback("127.0.0.1"))

    def test_is_loopback_ipv6(self):
        self.assertTrue(wb._is_loopback("::1"))
        self.assertTrue(wb._is_loopback("[::1]"))

    def test_is_loopback_localhost(self):
        self.assertTrue(wb._is_loopback("localhost"))

    def test_not_loopback(self):
        self.assertFalse(wb._is_loopback("0.0.0.0"))
        self.assertFalse(wb._is_loopback("192.168.1.1"))
        self.assertFalse(wb._is_loopback("example.com"))


class KeyAuthTests(unittest.TestCase):
    def setUp(self):
        self.saved_key = wb.LOCAL_API_KEY
        wb.LOCAL_API_KEY = "sk-test-secret"

    def tearDown(self):
        wb.LOCAL_API_KEY = self.saved_key

    def test_correct_key_accepted(self):
        self.assertTrue(wb._key_eq("sk-test-secret"))

    def test_wrong_key_rejected(self):
        self.assertFalse(wb._key_eq("sk-wrong"))
        self.assertFalse(wb._key_eq(""))

    def test_console_cookie_is_deterministic(self):
        c1 = wb._console_cookie()
        c2 = wb._console_cookie()
        self.assertEqual(c1, c2)
        self.assertNotEqual(c1, "sk-test-secret")


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.saved_bind = wb._BIND_HOST
        wb._BIND_HOST = "127.0.0.1"  # 模拟回环地址绑定

    def tearDown(self):
        wb._BIND_HOST = self.saved_bind

    def test_loopback_host_accepted(self):
        h = make_handler("/admin/models", {"Host": "127.0.0.1:8787"})
        self.assertTrue(h._admin_ok())

    def test_evil_host_rejected(self):
        h = make_handler("/admin/models", {"Host": "evil.example.com"})
        self.assertFalse(h._admin_ok())

    def test_same_origin_accepted(self):
        h = make_handler("/admin/models", {
            "Host": "127.0.0.1:8787",
            "Origin": "http://127.0.0.1:8787"})
        self.assertTrue(h._admin_ok())

    def test_origin_mismatch_rejected(self):
        h = make_handler("/admin/models", {
            "Host": "127.0.0.1:8787",
            "Origin": "http://evil.example.com"})
        self.assertFalse(h._admin_ok())

    def test_origin_null_rejected(self):
        h = make_handler("/admin/models", {
            "Host": "127.0.0.1:8787",
            "Origin": "null"})
        self.assertFalse(h._admin_ok())


class BodyParsingTests(unittest.TestCase):
    def test_valid_json_parsed(self):
        h = make_handler(headers={"Content-Length": "15"},
                         body=b'{"model":"gpt"}')
        self.assertEqual(h._body(), {"model": "gpt"})

    def test_no_content_length_returns_empty(self):
        h = make_handler()
        self.assertEqual(h._body(), {})

    def test_zero_content_length_returns_empty(self):
        h = make_handler(headers={"Content-Length": "0"})
        self.assertEqual(h._body(), {})

    def test_short_read_closes_connection(self):
        h = make_handler(headers={"Content-Length": "100"}, body=b"short")
        result = h._body()
        self.assertIsNone(result)
        self.assertTrue(h.close_connection)
        self.assertEqual(h._body_err[0], 400)
        self.assertIn("不完整", h._body_err[1])

    def test_invalid_json_returns_none(self):
        h = make_handler(headers={"Content-Length": "5"}, body=b"{oops")
        self.assertIsNone(h._body())
        self.assertFalse(h.close_connection)

    def test_oversized_body_rejected(self):
        h = make_handler(headers={"Content-Length": str(wb.MAX_BODY + 1)})
        result = h._body()
        self.assertIsNone(result)
        self.assertTrue(h.close_connection)
        self.assertEqual(h._body_err[0], 413)

    def test_chunked_transfer_rejected(self):
        h = make_handler(headers={"Transfer-Encoding": "chunked"})
        result = h._body()
        self.assertIsNone(result)
        self.assertTrue(h.close_connection)
        self.assertEqual(h._body_err[0], 411)

    def test_conflicting_content_length_rejected(self):
        h = make_handler(headers={"Content-Length": "10"})
        h.headers["Content-Length"] = "20"  # 添加第二个值
        result = h._body()
        self.assertIsNone(result)
        self.assertTrue(h.close_connection)
        self.assertEqual(h._body_err[0], 400)


if __name__ == "__main__":
    unittest.main()
