#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台（Windows / Linux）自启逻辑的离线回归测试。

不真的调用 systemctl / 写注册表：所有系统调用都被替换成假的。
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


class SystemdUnitTests(unittest.TestCase):
    def test_unit_text_has_required_sections(self):
        text = wb._systemd_unit_text()
        for section in ("[Unit]", "[Service]", "[Install]"):
            self.assertIn(section, text)
        self.assertIn("ExecStart=", text)
        self.assertIn("WantedBy=default.target", text)

    def test_unit_execstart_quotes_paths(self):
        """路径含空格时必须保持为一个参数（systemd 不做 shell 解析）。"""
        saved = wb.__file__
        wb.__file__ = "/opt/my app/wb2api.py"
        try:
            with mock.patch.object(wb.os.path, "abspath", side_effect=lambda p: p):
                text = wb._systemd_unit_text()
        finally:
            wb.__file__ = saved
        line = [l for l in text.splitlines() if l.startswith("ExecStart=")][0]
        self.assertIn("'/opt/my app/wb2api.py'", line)

    def test_unit_uses_absolute_paths(self):
        text = wb._systemd_unit_text()
        self.assertIn(os.path.abspath(sys.executable), text)
        self.assertNotIn("ExecStart=python ", text)

    def test_systemd_dir_respects_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            self.assertEqual(wb._systemd_dir(), os.path.join("/tmp/xdg", "systemd", "user"))

    def test_systemd_path_name(self):
        self.assertTrue(wb._systemd_path().endswith("wb2api-lite.service"))


class LinuxStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-linux-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._restore)

    def _restore(self):
        pass

    def test_no_systemd_reports_unsupported(self):
        with mock.patch.object(wb, "_systemd_available", return_value=False):
            st = wb._linux_startup_status()
        self.assertFalse(st["supported"])
        self.assertFalse(st["enabled"])
        self.assertIn("systemd", st["message"])

    def test_no_systemd_set_fails_cleanly(self):
        with mock.patch.object(wb, "_systemd_available", return_value=False):
            st = wb._linux_set_startup(True)
        self.assertFalse(st["ok"])
        self.assertFalse(st["supported"])

    def test_enable_writes_unit_and_calls_systemctl(self):
        unit = os.path.join(self.tmp, "systemd", "user", "wb2api-lite.service")
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(wb, "_systemd_available", return_value=True), \
             mock.patch.object(wb, "_systemd_path", return_value=unit), \
             mock.patch.object(wb, "_systemd_dir", return_value=os.path.dirname(unit)), \
             mock.patch.object(wb.subprocess, "run", side_effect=fake_run):
            st = wb._linux_set_startup(True)
        self.assertTrue(os.path.isfile(unit), "应写入 systemd 单元文件")
        self.assertTrue(st["enabled"])
        self.assertTrue(any("enable" in c for c in calls), "应调用 systemctl enable")
        self.assertTrue(any("daemon-reload" in c for c in calls))
        with open(unit, encoding="utf-8") as f:
            self.assertIn("ExecStart=", f.read())

    def test_disable_removes_unit(self):
        unit = os.path.join(self.tmp, "wb2api-lite.service")
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        with open(unit, "w", encoding="utf-8") as f:
            f.write("x")

        def fake_run(args, **kw):
            return mock.Mock(returncode=0)

        with mock.patch.object(wb, "_systemd_available", return_value=True), \
             mock.patch.object(wb, "_systemd_path", return_value=unit), \
             mock.patch.object(wb.subprocess, "run", side_effect=fake_run):
            st = wb._linux_set_startup(False)
        self.assertFalse(os.path.exists(unit), "关闭自启应删除单元文件")
        self.assertFalse(st["enabled"])

    def test_disable_when_absent_is_ok(self):
        unit = os.path.join(self.tmp, "nope.service")
        with mock.patch.object(wb, "_systemd_available", return_value=True), \
             mock.patch.object(wb, "_systemd_path", return_value=unit), \
             mock.patch.object(wb.subprocess, "run", return_value=mock.Mock(returncode=0)):
            st = wb._linux_set_startup(False)
        self.assertFalse(st["enabled"], "重复关闭应成功且不报错")

    def test_enable_mentions_linger(self):
        unit = os.path.join(self.tmp, "wb2api-lite.service")
        with mock.patch.object(wb, "_systemd_available", return_value=True), \
             mock.patch.object(wb, "_systemd_path", return_value=unit), \
             mock.patch.object(wb.subprocess, "run", return_value=mock.Mock(returncode=0)):
            st = wb._linux_set_startup(True)
        self.assertIn("linger", st["message"], "应提示 loginctl enable-linger")


class StartupDispatchTests(unittest.TestCase):
    def test_non_nt_dispatches_to_linux(self):
        with mock.patch.object(wb.os, "name", "posix"), \
             mock.patch.object(wb, "_linux_startup_status", return_value={"ok": True, "enabled": True,
                                                                          "supported": True}) as m:
            st = wb.startup_status()
        m.assert_called_once()
        self.assertTrue(st["enabled"])

    def test_non_nt_set_dispatches_to_linux(self):
        saved = wb._api_key_persisted
        wb._api_key_persisted = True
        try:
            with mock.patch.object(wb.os, "name", "posix"), \
                 mock.patch.object(wb, "_linux_set_startup",
                                   return_value={"ok": True, "enabled": True}) as m:
                st = wb.set_startup(True)
            m.assert_called_once_with(True)
            self.assertTrue(st["enabled"])
        finally:
            wb._api_key_persisted = saved

    def test_non_nt_set_rejects_unpersisted_key(self):
        """Linux 上同样不能给「重启就丢」的密钥开自启。"""
        saved = wb._api_key_persisted
        wb._api_key_persisted = False
        try:
            with mock.patch.object(wb.os, "name", "posix"), \
                 mock.patch.object(wb, "_linux_set_startup") as m:
                st = wb.set_startup(True)
            m.assert_not_called()
            self.assertFalse(st["ok"])
            self.assertIn("密钥", st["message"])
        finally:
            wb._api_key_persisted = saved


class CrossPlatformPathTests(unittest.TestCase):
    def test_data_paths_built_with_os_path_join(self):
        """数据文件路径必须由 os.path.join 拼出来，不能写死反斜杠。

        不能在 Windows 上断言「运行时路径不含反斜杠」——那种路径本来就是对的。
        真正要防的是源码里写死 `data\\auth.json` 这种字符串。
        """
        import inspect
        src = inspect.getsource(wb._data_path)
        self.assertIn("os.path.join", src)

    def test_no_hardcoded_backslash_paths_in_source(self):
        """源码里不应出现写死的 Windows 路径分隔符拼接。"""
        import re
        with open(os.path.abspath(wb.__file__), encoding="utf-8") as f:
            src = f.read()
        # 形如 "abc\def" 的字符串字面量（排除转义字符与正则）
        bad = re.findall(r'"[A-Za-z0-9_.-]+\\\\[A-Za-z0-9_.-]+"', src)
        self.assertEqual(bad, [], "发现写死的反斜杠路径：%r" % bad[:5])

    def test_startup_command_is_shlex_on_posix(self):
        with mock.patch.object(wb.os, "name", "posix"):
            cmd = wb._startup_command()
        self.assertIn(str(wb._PORT), cmd)
        self.assertIn("--host", cmd)


if __name__ == "__main__":
    unittest.main()
