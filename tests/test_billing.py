#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分/账单解析的离线回归测试。

所有用例只调用纯函数，不访问网络、不读写真实账号或凭证文件。
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402  （必须在设置 WB_DATA_DIR 之后导入）


class BillingEnvelopeTests(unittest.TestCase):
    def test_unwraps_known_envelopes(self):
        payload = {"code": 0, "data": {"Response": {"balance": 12}}}
        self.assertEqual(wb._billing_payload(payload), {"balance": 12})

    def test_unwrap_depth_is_bounded(self):
        node = {"balance": 1}
        for _ in range(wb.BILLING_ENVELOPE_MAX_DEPTH + 4):
            node = {"data": node}
        result = wb._billing_payload(node)
        self.assertIsInstance(result, dict)
        # 只拆前 N 层，剩余信封原样返回，不会继续向下解析
        self.assertIn("data", result)

    def test_non_mapping_payload_returned_as_is(self):
        self.assertEqual(wb._billing_payload([1, 2]), [1, 2])
        self.assertEqual(wb._billing_payload("text"), "text")
        self.assertEqual(wb._billing_payload(None), None)


class BillingTransportTests(unittest.TestCase):
    def setUp(self):
        self.account = {"accessToken": "token", "uid": "billing-test",
                        "realm": "global", "domain": "www.workbuddy.ai"}

    def test_readonly_query_retries_connection_abort_once(self):
        class BrokenResponse:
            status = 200

            def read(self):
                raise ConnectionAbortedError(10053, "connection aborted")

            def close(self):
                pass

        class GoodResponse:
            status = 200

            def read(self):
                return b'{"code":0,"data":{"balance":12}}'

            def close(self):
                pass

        with mock.patch.object(wb, "upstream_request",
                               side_effect=[BrokenResponse(), GoodResponse()]) as request, \
             mock.patch.object(wb.time, "sleep"):
            result = wb._billing_post(self.account, "/billing/meter/get-user-resource")
        self.assertEqual(result, {"balance": 12})
        self.assertEqual(request.call_count, 2)

    def test_checkin_does_not_retry_connection_abort(self):
        with mock.patch.object(wb, "upstream_request",
                               side_effect=ConnectionAbortedError(10053, "connection aborted")) as request, \
             self.assertRaises(ConnectionAbortedError):
            wb._billing_post(self.account, "/v2/billing/meter/daily-checkin")
        self.assertEqual(request.call_count, 1)


class NumberTests(unittest.TestCase):
    def test_rejects_bool_and_non_finite(self):
        self.assertIsNone(wb._number({"v": True}, ("v",)))
        self.assertIsNone(wb._number({"v": "abc"}, ("v",)))
        self.assertIsNone(wb._number({"v": float("inf")}, ("v",)))
        self.assertIsNone(wb._number({"v": float("nan")}, ("v",)))
        self.assertIsNone(wb._number({}, ("v",)))

    def test_scans_keys_in_order_and_parses_strings(self):
        self.assertEqual(wb._number({"v": "12.5"}, ("v",)), 12.5)
        self.assertEqual(wb._number({"a": "", "b": 7}, ("a", "b")), 7.0)
        self.assertEqual(wb._number({"a": None, "b": "3"}, ("a", "b")), 3.0)


class ResourceRowsTests(unittest.TestCase):
    def test_finds_known_row_lists(self):
        self.assertEqual(wb._resource_rows({"data": {"Accounts": [1, 2]}}), [1, 2])
        self.assertEqual(wb._resource_rows({"Packages": []}), [])

    def test_returns_none_when_unknown(self):
        self.assertIsNone(wb._resource_rows({"nothing": 1}))
        self.assertIsNone(wb._resource_rows([{"Accounts": []}]))


class PointsValueTests(unittest.TestCase):
    def test_remaining_precise_field(self):
        self.assertEqual(wb._points_value({"data": {"remaining": "100.5"}}), 100.5)
        self.assertEqual(wb._points_value({"CycleCapacityRemain": 8}), 8.0)

    def test_zero_balance_is_not_mistaken_for_missing(self):
        self.assertEqual(wb._points_value({"remaining": 0}), 0.0)
        self.assertEqual(wb._points_value({"Accounts": []}), 0.0)

    def test_total_minus_used(self):
        self.assertEqual(
            wb._points_value({"CapacitySize": 100, "CapacityUsed": 30}), 70.0)
        # 已用量超过总量时按 0 处理，不返回负数
        self.assertEqual(
            wb._points_value({"CapacitySize": 10, "CapacityUsed": 30}), 0.0)
        self.assertEqual(wb._points_value({"remaining": -5}), 0.0)

    def test_sums_nested_rows(self):
        obj = {"Accounts": [{"remaining": 10}, {"remaining": 2.5}]}
        self.assertEqual(wb._points_value(obj), 12.5)

    def test_unknown_row_makes_total_unknown(self):
        obj = {"Accounts": [{"remaining": 10}, {"nothing": 1}]}
        self.assertIsNone(wb._points_value(obj))

    def test_does_not_treat_package_totals_as_balance(self):
        # 只有套餐总量、没有剩余或已用量时，不能把总量当余额
        self.assertIsNone(wb._points_value({"CycleCapacitySize": 500}))

    def test_slice_details_fallback(self):
        obj = {"SlicePeriodUsageDetails": [{"remaining": 4}]}
        self.assertEqual(wb._points_value(obj), 4.0)

    def test_depth_guard_prevents_unbounded_recursion(self):
        node = {"remaining": 5}
        for _ in range(wb.BILLING_POINTS_MAX_DEPTH + 5):
            node = {"SlicePeriodUsageDetails": [node]}
        self.assertIsNone(wb._points_value(node))


class FactorTests(unittest.TestCase):
    def test_parses_multipliers(self):
        self.assertEqual(wb._parse_factor("x2"), 2.0)
        self.assertEqual(wb._parse_factor("×1.5"), 1.5)
        self.assertEqual(wb._parse_factor(0), 0.0)
        self.assertEqual(wb._parse_factor("0"), 0.0)

    def test_rejects_invalid(self):
        self.assertIsNone(wb._parse_factor(True))
        self.assertIsNone(wb._parse_factor("-1"))
        self.assertIsNone(wb._parse_factor("abc"))
        self.assertIsNone(wb._parse_factor(None))

    def test_to_int(self):
        self.assertEqual(wb.to_int("12"), 12)
        self.assertEqual(wb.to_int(None), 0)
        self.assertEqual(wb.to_int("x"), 0)
        self.assertEqual(wb.to_int(3.9), 3)


if __name__ == "__main__":
    unittest.main()
