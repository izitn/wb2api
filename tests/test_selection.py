#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型选择保存与账号切换竞态的离线回归测试。"""

import atexit
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


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-select-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        
        # 保存所有全局状态
        self.saved = (
            wb.SELECTION_FILE,
            wb._auth, wb._accounts,
            wb._selection, wb._aliases, wb._default_model, wb._enforce,
            wb._account_selections, wb._selection_owner,
            wb._selection_scoped, wb._selection_mtime
        )
        self.addCleanup(self._restore)
        
        # 设置测试环境
        wb.SELECTION_FILE = os.path.join(self.tmp, "selection.json")
        wb._auth = wb._normalize_auth({
            "accessToken": "tok", "realm": "global",
            "account": {"uid": "u-test", "nickname": "测试账号"}
        })
        wb._accounts = {wb.account_id(wb._auth): wb._auth}
        wb._selection, wb._aliases, wb._default_model, wb._enforce = [], {}, "", False
        wb._account_selections, wb._selection_owner = {}, ""
        wb._selection_scoped, wb._selection_mtime = False, 0.0

    def _restore(self):
        (wb.SELECTION_FILE, wb._auth, wb._accounts,
         wb._selection, wb._aliases, wb._default_model, wb._enforce,
         wb._account_selections, wb._selection_owner,
         wb._selection_scoped, wb._selection_mtime) = self.saved

    def test_save_and_reload_roundtrip(self):
        # 保存去重后的模型列表
        wb.save_selection(["model-a", "model-b", " model-a "])
        self.assertTrue(os.path.exists(wb.SELECTION_FILE))
        
        # 重新加载
        wb._selection = []
        wb._selection_mtime = 0.0
        wb.load_selection()
        
        # 验证去重和 trim
        self.assertEqual(wb._selection, ["model-a", "model-b"])

    def test_expected_account_id_mismatch_rejected(self):
        # 保存时传入错误的 expected_account_id 应该抛 ValueError
        with self.assertRaises(ValueError) as ctx:
            wb.save_selection(["model-x"], expected_account_id="wrong-account-id")
        self.assertIn("账号", str(ctx.exception))
        
        # 文件不应该被写入
        self.assertFalse(os.path.exists(wb.SELECTION_FILE))

    def test_selection_for_other_account(self):
        # 为当前账号保存选择
        wb.save_selection(["model-1", "model-2"])
        
        # 创建另一个账号
        other = wb._normalize_auth({
            "accessToken": "tok2", "realm": "cn",
            "account": {"uid": "u-other", "nickname": "其他账号"}
        })
        other_id = wb.account_id(other)
        
        # 未保存过的账号返回空列表
        self.assertEqual(wb.selection_for_account(other), [])
        
        # 为另一个账号手动设置选择
        wb._account_selections[other_id] = ["model-9"]
        self.assertEqual(wb.selection_for_account(other), ["model-9"])
        
        # 当前活跃账号的选择不受影响
        self.assertEqual(wb._selection, ["model-1", "model-2"])

    def test_empty_selection_allowed(self):
        wb.save_selection([])
        wb._selection = None
        wb.load_selection()
        self.assertEqual(wb._selection, [])

    def test_file_modification_time_tracked(self):
        wb.save_selection(["model-a"])
        self.assertGreater(wb._selection_mtime, 0)

        # 重新保存后，重新加载应读到最新内容（而不是旧快照）
        wb.save_selection(["model-b"])
        wb._selection = []
        wb._selection_mtime = 0.0
        wb.load_selection()
        self.assertEqual(wb._selection, ["model-b"])

    def test_scoped_selection_records_owner(self):
        wb.save_selection(["model-x"])
        self.assertTrue(wb._selection_scoped)
        self.assertEqual(wb._selection_owner, wb.account_id(wb._auth))


if __name__ == "__main__":
    unittest.main()
