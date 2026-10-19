#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭证归一化、账号标识与账号库读写的离线回归测试。

测试把 AUTH_FILE / accounts.json 重定向到临时目录，不会触碰真实凭证。
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


class NormalizeAuthTests(unittest.TestCase):
    def test_flat_form(self):
        a = wb._normalize_auth({
            "accessToken": "tok", "refreshToken": "rt",
            "expiresAt": "1700000000", "domain": "www.workbuddy.ai",
        })
        self.assertEqual(a["accessToken"], "tok")
        self.assertEqual(a["refreshToken"], "rt")
        self.assertEqual(a["expiresAt"], 1700000000)
        self.assertEqual(a["domain"], "www.workbuddy.ai")
        self.assertEqual(a["realm"], "")
        self.assertEqual(len(a["accountId"]), 24)

    def test_nested_client_export(self):
        a = wb._normalize_auth({
            "auth": {"accessToken": "tok", "refreshToken": "rt"},
            "account": {"uid": "u-1", "nickname": "小明"},
            "realm": "cn",
        })
        self.assertEqual(a["accessToken"], "tok")
        self.assertEqual(a["uid"], "u-1")
        self.assertEqual(a["nickname"], "小明")
        self.assertEqual(a["realm"], "cn")

    def test_account_name_fallbacks(self):
        a = wb._normalize_auth({"accessToken": "tok",
                                "account": {"displayName": " 显示名 "}})
        self.assertEqual(a["nickname"], "显示名")

    def test_missing_token_rejected(self):
        with self.assertRaises(ValueError):
            wb._normalize_auth({"refreshToken": "rt"})
        with self.assertRaises(ValueError):
            wb._normalize_auth({"accessToken": ""})

    def test_non_object_rejected(self):
        with self.assertRaises(ValueError):
            wb._normalize_auth([])
        with self.assertRaises(ValueError):
            wb._normalize_auth("x")

    def test_unknown_realm_is_cleared(self):
        a = wb._normalize_auth({"accessToken": "tok", "realm": "mars"})
        self.assertEqual(a["realm"], "")

    def test_auth_payload_hides_unknown_fields_and_keeps_device_token(self):
        a = wb._normalize_auth({"accessToken": "tok", "realm": "global",
                                "device_token": "dev-1", "extra": "drop-me"})
        payload = wb._auth_payload(a)
        self.assertEqual(payload["device_token"], "dev-1")
        self.assertNotIn("extra", payload)
        self.assertNotIn("deviceToken", payload)

        plain = wb._auth_payload(wb._normalize_auth({"accessToken": "tok"}))
        self.assertNotIn("device_token", plain)


class AccountIdTests(unittest.TestCase):
    def test_stable_for_same_identity(self):
        base = {"uid": "u-1", "realm": "global"}
        self.assertEqual(wb.account_id(dict(base)), wb.account_id(dict(base)))

    def test_realm_and_enterprise_change_identity(self):
        self.assertNotEqual(
            wb.account_id({"uid": "u-1", "realm": "global"}),
            wb.account_id({"uid": "u-1", "realm": "cn"}))
        self.assertNotEqual(
            wb.account_id({"uid": "u-1", "realm": "cn"}),
            wb.account_id({"uid": "u-1", "realm": "cn", "enterpriseId": "e-1"}))

    def test_falls_back_to_stored_account_id(self):
        self.assertEqual(wb.account_id({"accountId": "abc123", "uid": ""}), "abc123")


class RealmTests(unittest.TestCase):
    def test_realm_field_wins(self):
        self.assertEqual(wb.realm_of({"realm": "cn", "domain": "www.workbuddy.ai"}), "cn")

    def test_domain_detection(self):
        self.assertEqual(wb.realm_of({"domain": "www.workbuddy.ai"}), "global")
        self.assertEqual(wb.realm_of({"domain": "www.codebuddy.cn"}), "cn")

    def test_none_account_uses_default_upstream(self):
        self.assertIn(wb.realm_of(None), ("global", "cn"))

    @unittest.skipIf(wb.UPSTREAM_OVERRIDE, "WB_UPSTREAM_BASE 覆盖了上游地址")
    def test_upstream_base_by_realm(self):
        self.assertEqual(wb.upstream_base({"realm": "cn"}), "https://copilot.tencent.com")
        self.assertEqual(wb.upstream_base({"realm": "global"}), "https://www.workbuddy.ai")


class AuthFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-auth-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_write_private_roundtrip(self):
        path = os.path.join(self.tmp, "nested", "auth.json")
        wb._write_private(path, json.dumps({"accessToken": "tok"}))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(wb.load_auth(path)["accessToken"], "tok")
        self.assertFalse(os.path.exists(path + ".tmp"))
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_accounts_library_roundtrip(self):
        saved = (wb.AUTH_FILE, wb._accounts, wb._auth,
                 os.environ.get("WB_ACCOUNTS_FILE"))
        self.addCleanup(self._restore_accounts, saved)
        wb.AUTH_FILE = os.path.join(self.tmp, "auth.json")
        os.environ.pop("WB_ACCOUNTS_FILE", None)
        wb._accounts, wb._auth = {}, None

        acc = wb._normalize_auth({"accessToken": "tok-1", "realm": "global",
                                  "account": {"uid": "u-1", "nickname": "甲"}})
        wb.save_auth(acc)
        self.assertTrue(os.path.exists(wb.accounts_file()))
        self.assertTrue(os.path.exists(wb.AUTH_FILE))

        wb._accounts, wb._auth = {}, None
        active = wb.load_accounts()
        self.assertIsNotNone(active)
        self.assertEqual(active["accessToken"], "tok-1")
        self.assertEqual(active["nickname"], "甲")
        self.assertIn(wb.account_id(acc), wb._accounts)

        with open(wb.accounts_file(), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["activeAccountId"], wb.account_id(acc))

    def test_invalid_accounts_file_rejected(self):
        saved = (wb.AUTH_FILE, wb._accounts, wb._auth,
                 os.environ.get("WB_ACCOUNTS_FILE"))
        self.addCleanup(self._restore_accounts, saved)
        wb.AUTH_FILE = os.path.join(self.tmp, "auth.json")
        os.environ.pop("WB_ACCOUNTS_FILE", None)
        with open(wb.accounts_file(), "w", encoding="utf-8") as f:
            f.write('{"accounts": "not-a-list"}')
        with self.assertRaises(ValueError):
            wb.load_accounts()

    def _restore_accounts(self, saved):
        wb.AUTH_FILE, wb._accounts, wb._auth, env = saved
        if env is None:
            os.environ.pop("WB_ACCOUNTS_FILE", None)
        else:
            os.environ["WB_ACCOUNTS_FILE"] = env


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-settings-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (os.environ.get("WB_SETTINGS_FILE"), wb._auto_checkin)
        self.addCleanup(self._restore_settings, saved)
        os.environ["WB_SETTINGS_FILE"] = os.path.join(self.tmp, "settings.json")

    def _restore_settings(self, saved):
        env, wb._auto_checkin = saved
        if env is None:
            os.environ.pop("WB_SETTINGS_FILE", None)
        else:
            os.environ["WB_SETTINGS_FILE"] = env

    def test_roundtrip(self):
        result = wb.set_auto_checkin(True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["autoCheckin"])
        wb._auto_checkin = False
        wb.load_settings()
        self.assertTrue(wb._auto_checkin)

    def test_missing_file_means_disabled(self):
        wb._auto_checkin = True
        wb.load_settings()
        self.assertFalse(wb._auto_checkin)

    def test_invalid_settings_rejected(self):
        with open(os.environ["WB_SETTINGS_FILE"], "w", encoding="utf-8") as f:
            f.write('{"autoCheckin": "yes"}')
        with self.assertRaises(ValueError):
            wb.load_settings()


if __name__ == "__main__":
    unittest.main()
