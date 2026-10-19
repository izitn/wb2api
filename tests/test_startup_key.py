#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试自启动密钥持久化保护。"""

import atexit
import ast
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

SOURCE_PATH = os.path.join(_ROOT, "wb2api.py")


class ApiKeyPersistenceTests(unittest.TestCase):
    def test_explicit_key_not_persisted(self):
        """显式命令行/环境变量密钥标记为非持久化。"""
        key, src, persisted = wb.resolve_api_key("sk-explicit-test", False)
        self.assertEqual(key, "sk-explicit-test")
        self.assertEqual(src, "参数/环境变量")
        self.assertFalse(persisted)

    def test_ephemeral_key_not_persisted(self):
        """一次性随机密钥标记为非持久化。"""
        key, src, persisted = wb.resolve_api_key("", True)
        self.assertTrue(key.startswith("sk-"))
        self.assertIn("不落盘", src)
        self.assertFalse(persisted)

    def test_file_key_is_persisted(self):
        """从文件读取的密钥标记为已持久化。"""
        tmp = tempfile.mkdtemp(prefix="wb2api-key-")
        self.addCleanup(shutil.rmtree, tmp, True)
        key_file = os.path.join(tmp, "api_key.txt")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write("sk-from-file\n")
        
        saved = wb.API_KEY_FILE
        try:
            wb.API_KEY_FILE = key_file
            key, src, persisted = wb.resolve_api_key("", False)
            self.assertEqual(key, "sk-from-file")
            self.assertEqual(src, key_file)
            self.assertTrue(persisted)
        finally:
            wb.API_KEY_FILE = saved

    def test_generated_key_is_persisted(self):
        """新生成并保存的密钥标记为已持久化。"""
        tmp = tempfile.mkdtemp(prefix="wb2api-key-")
        self.addCleanup(shutil.rmtree, tmp, True)
        key_file = os.path.join(tmp, "api_key.txt")
        
        saved = wb.API_KEY_FILE
        try:
            wb.API_KEY_FILE = key_file
            key, src, persisted = wb.resolve_api_key("", False)
            self.assertTrue(key.startswith("sk-"))
            self.assertIn("新生成", src)
            self.assertTrue(persisted)
            # 验证密钥已写入文件
            self.assertTrue(os.path.exists(key_file))
            with open(key_file, "r", encoding="utf-8") as f:
                saved_key = f.read().strip()
            self.assertEqual(saved_key, key)
        finally:
            wb.API_KEY_FILE = saved

    def test_startup_rejects_non_persisted_key(self):
        """开启自启时拒绝非持久化密钥。"""
        if os.name != "nt":
            self.skipTest("Windows-only test")
        
        saved = wb._api_key_persisted
        try:
            wb._api_key_persisted = False
            result = wb.set_startup(True)
            self.assertFalse(result["ok"])
            self.assertIn("命令行参数或环境变量", result["message"])
            self.assertFalse(result["enabled"])
        finally:
            wb._api_key_persisted = saved


class KeyMaskingTests(unittest.TestCase):
    def test_long_key_is_masked(self):
        """长密钥只保留前缀与后缀（调用产品代码，而非在测试里重算）。"""
        masked = wb._mask_api_key("sk-1234567890abcdef1234567890abcdef")
        self.assertEqual(masked, "sk-1234...cdef")
        self.assertNotIn("567890abcdef1234567890", masked)

    def test_short_key_fully_masked(self):
        masked = wb._mask_api_key("short")
        self.assertEqual(masked, "***")

    def test_empty_key_masked(self):
        self.assertEqual(wb._mask_api_key(""), "***")

    def test_boundary_length(self):
        # 长度 15 不满足 > 15，应完全掩码；长度 16 才显示前后缀
        self.assertEqual(wb._mask_api_key("a" * 15), "***")
        masked = wb._mask_api_key("a" * 16)
        self.assertEqual(masked, "aaaaaaa...aaaa")
        self.assertNotEqual(masked, "a" * 16)


class MainGlobalDeclarationTests(unittest.TestCase):
    """静态回归：main() 给 _api_key_persisted 赋值时必须声明 global。

    漏写会让赋值落到 main() 的局部变量，模块级标志恒为 False，
    于是 set_startup() 永远拒绝开启自启（包括密钥已持久化的正常场景）。
    """

    def test_main_declares_global_for_api_key_persisted(self):
        with open(SOURCE_PATH, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        main_fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        self.assertIsNotNone(main_fn, "未找到 main()")

        assigned = any(isinstance(n, ast.Name) and n.id == "_api_key_persisted"
                       and isinstance(n.ctx, ast.Store) for n in ast.walk(main_fn))
        self.assertTrue(assigned, "main() 应给 _api_key_persisted 赋值")

        declared = set()
        for n in ast.walk(main_fn):
            if isinstance(n, ast.Global):
                declared.update(n.names)
        self.assertIn("_api_key_persisted", declared,
                      "main() 赋值 _api_key_persisted 前必须声明 global")


if __name__ == "__main__":
    unittest.main()
