#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage.jsonl 抗损坏测试：单行坏数据不能让启动或统计崩溃。"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "wb2api" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="wb2api-tests-")
    os.environ["WB_DATA_DIR"] = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

import wb2api as wb  # noqa: E402


class UsageReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-usage-r-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(wb.close_usage)
        saved = wb.USAGE_FILE
        self.addCleanup(setattr, wb, "USAGE_FILE", saved)
        wb.USAGE_FILE = os.path.join(self.tmp, "usage.jsonl")

    def write_lines(self, lines):
        with open(wb.USAGE_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")

    def test_corrupted_lines_do_not_break_replay(self):
        now = time.time()
        self.write_lines([
            123,                                            # 非对象
            None,                                           # JSON null
            [],                                             # 数组
            "{not json",                                    # 半行/坏 JSON
            {"ts": now, "model": {}, "status": None,
             "in": "x", "out": None},                        # 字段类型全错
            {"ts": now, "model": "good", "status": 200,
             "in": 10, "out": 5},                            # 唯一一条有效
            {"ts": "bad-ts", "model": "m2", "status": 200, "in": 1, "out": 1},
        ])
        wb.load_usage_totals()                               # 不得抛异常
        month = time.strftime("%Y-%m")
        summary = wb.usage_summary()
        good = summary["month"].get("good")
        self.assertIsNotNone(good)
        self.assertEqual(good["requests"], 1)
        self.assertEqual(good["input_tokens"], 10)
        self.assertEqual(good["output_tokens"], 5)
        self.assertEqual(summary["monthName"], month)

    def test_missing_file_is_fine(self):
        wb.load_usage_totals()
        self.assertEqual(wb.usage_summary()["today"], {})

    def test_truncated_last_line_ignored(self):
        now = time.time()
        with open(wb.USAGE_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "model": "ok", "status": 200,
                                "in": 1, "out": 1}) + "\n")
            f.write('{"ts": %d, "model": "trunc' % now)     # 崩溃留下的半行
        wb.load_usage_totals()
        self.assertIn("ok", wb.usage_summary()["month"])


class BumpToleranceTests(unittest.TestCase):
    def test_bump_handles_junk_fields(self):
        bucket = {}
        wb._bump(bucket, {"model": None, "status": None, "in": "x",
                          "out": [], "credit": "nope", "factor": {}})
        self.assertEqual(bucket[""]["requests"], 1)
        self.assertEqual(bucket[""]["errors"], 0)
        self.assertEqual(bucket[""]["input_tokens"], 0)

    def test_bump_counts_errors(self):
        bucket = {}
        wb._bump(bucket, {"model": "m", "status": 500, "in": 1, "out": 2,
                          "credit": 1.5, "factor": 2.0})
        self.assertEqual(bucket["m"]["errors"], 1)
        self.assertEqual(bucket["m"]["credits"], 1.5)
        self.assertGreater(bucket["m"]["weighted_k"], 0)


if __name__ == "__main__":
    unittest.main()
