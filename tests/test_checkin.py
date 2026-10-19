#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日签到逻辑的离线回归测试。

不访问网络：_billing_post / _billing_fallback 全部替换成假的。
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

import wb2api as wb  # noqa: E402


def _acct(realm="cn", ent=""):
    return wb._normalize_auth({
        "accessToken": "tok", "realm": realm,
        "account": {"uid": "u1", "nickname": "n1", "enterpriseId": ent},
    })


def _bill_err(code, msg="", status=200):
    e = wb.BillingError(status, code, msg or ("code %s" % code))
    return e


class SupportsCheckinTests(unittest.TestCase):
    def test_cn_personal_supported(self):
        self.assertTrue(wb._supports_checkin(_acct("cn")))

    def test_intl_not_supported(self):
        self.assertFalse(wb._supports_checkin(_acct("global")))

    def test_enterprise_not_supported(self):
        self.assertFalse(wb._supports_checkin(_acct("cn", "ent-1")))


class AlreadyCheckinTests(unittest.TestCase):
    def test_known_codes(self):
        """10001（hub 实测）与 14001（panel 实测）都应视为已签到而非失败。"""
        for code in ("10001", "14001"):
            self.assertTrue(wb._checkin_already(_bill_err(code)), code)

    def test_message_markers(self):
        for msg in ("今天已签到", "已签到", "already checked in"):
            self.assertTrue(wb._checkin_already(_bill_err("999", msg)), msg)

    def test_other_business_error_is_not_already(self):
        self.assertFalse(wb._checkin_already(_bill_err("11101", "参数错误")))

    def test_bare_already_not_matched(self):
        """裸 "already" 会误伤 "already in progress" 这类瞬时错误，把失败记成成功。"""
        for msg in ("already in progress", "already exists", "already processing"):
            self.assertFalse(wb._checkin_already(_bill_err("10000", msg)), msg)

    def test_non_billing_error_not_treated_as_already(self):
        """网络/解析错误绝不能当成“已签到”，否则补签会误记成功。"""
        self.assertFalse(wb._checkin_already(OSError("boom")))


class TransientTests(unittest.TestCase):
    def test_5xx_transient(self):
        self.assertTrue(wb._checkin_transient(_bill_err("10000", "500", status=500)))

    def test_oserror_transient(self):
        self.assertTrue(wb._checkin_transient(OSError("reset")))

    def test_business_error_not_transient(self):
        self.assertFalse(wb._checkin_transient(_bill_err("11101", "bad", status=200)))


class CheckinPostRetryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_retries_transient_500_then_succeeds(self):
        def fake(a, path, body=None):
            self.calls.append(path)
            if len(self.calls) == 1:
                raise _bill_err("10000", "500", status=500)
            return {}

        with mock.patch.object(wb, "_billing_post", side_effect=fake), \
             mock.patch.object(wb.time, "sleep"):
            wb._billing_post_checkin(_acct())
        self.assertEqual(len(self.calls), 2, "首次 500 应重试一次后成功")

    def test_gives_up_after_three(self):
        def fake(a, path, body=None):
            self.calls.append(path)
            raise _bill_err("10000", "500", status=500)

        with mock.patch.object(wb, "_billing_post", side_effect=fake), \
             mock.patch.object(wb.time, "sleep"):
            with self.assertRaises(wb.BillingError):
                wb._billing_post_checkin(_acct())
        self.assertEqual(len(self.calls), 3, "持续 500 应封顶 3 次")

    def test_no_retry_on_business_error(self):
        def fake(a, path, body=None):
            self.calls.append(path)
            raise _bill_err("11101", "bad")

        with mock.patch.object(wb, "_billing_post", side_effect=fake), \
             mock.patch.object(wb.time, "sleep"):
            with self.assertRaises(wb.BillingError):
                wb._billing_post_checkin(_acct())
        self.assertEqual(len(self.calls), 1, "业务错误不该重试")

    def test_no_retry_on_already(self):
        def fake(a, path, body=None):
            self.calls.append(path)
            raise _bill_err("14001", "今日已签到")

        with mock.patch.object(wb, "_billing_post", side_effect=fake), \
             mock.patch.object(wb.time, "sleep"):
            with self.assertRaises(wb.BillingError):
                wb._billing_post_checkin(_acct())
        self.assertEqual(len(self.calls), 1)


class DailyCheckinFlowTests(unittest.TestCase):
    def setUp(self):
        self.a = _acct("cn")
        wb._points_state.clear()
        self.saved = (wb._billing_fallback, wb._billing_post, wb._shared_queries, wb.account_points)
        self.addCleanup(self._restore)

    def _restore(self):
        (wb._billing_fallback, wb._billing_post,
         wb._shared_queries, wb.account_points) = self.saved
        wb._points_state.clear()

    def _run(self, status_obj, post=None):
        wb._billing_fallback = lambda a, paths, body=None: status_obj
        wb._billing_post = post or (lambda a, p, b=None: {})
        wb.account_points = lambda a, force=False: dict(wb._points_state.get(wb.account_id(a)) or {})
        wb._shared_queries = mock.Mock()
        return wb.daily_checkin(self.a)

    def test_skips_when_already_checked_today(self):
        posted = []
        out = self._run({"active": True, "today_checked_in": True},
                        post=lambda a, p, b=None: posted.append(p))
        self.assertTrue(out.get("checkin"))
        self.assertEqual(posted, [], "已签到就不该再提交")

    def test_posts_when_not_checked(self):
        posted = []
        out = self._run({"active": True, "today_checked_in": False},
                        post=lambda a, p, b=None: posted.append(p))
        self.assertTrue(out.get("checkin"))
        self.assertEqual(len(posted), 1)

    def test_already_code_counts_as_success(self):
        def post(a, p, b=None):
            raise _bill_err("14001", "今日已签到")
        out = self._run({"active": True, "today_checked_in": False}, post=post)
        self.assertTrue(out.get("checkin"), "14001 应算签到完成，而不是失败")

    def test_real_failure_reported(self):
        def post(a, p, b=None):
            raise _bill_err("11101", "参数错误")
        out = self._run({"active": True, "today_checked_in": False}, post=post)
        self.assertFalse(out.get("checkin"))
        self.assertIn("失败", out.get("checkinMessage", ""))

    def test_activity_closed_reported(self):
        out = self._run({"active": False})
        self.assertFalse(out.get("checkin"))
        self.assertIn("未开放", out.get("checkinMessage", ""))

    def test_missing_status_field_reported(self):
        out = self._run({"active": True})
        self.assertFalse(out.get("checkin"))


if __name__ == "__main__":
    unittest.main()
