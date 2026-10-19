#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型目录缓存与模型解析的离线回归测试。

覆盖：部分探测失败不得用残缺并集覆盖完整目录、默认模型不得静默改写
显式请求的模型、刷新标记泄漏、磁盘缓存坏字段容错。
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
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


def payload(*model_ids, promo=None):
    body = {"models": [{"modelId": m, "credits": "1"} for m in model_ids]}
    if promo is not None:
        body["modelPromotions"] = [promo]
    return body


PROMO = {"enabled": True, "modelIds": ["promo-model"], "badge": {"label": "L"},
          "discount": {"discountedCredits": "0.5"}}


class CatalogState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-cat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.account = wb._normalize_auth({
            "accessToken": "tok", "realm": "global",
            "account": {"uid": "u-cat", "nickname": "目录测试"}})
        self.aid = wb.account_id(self.account)
        saved = (wb._auth, wb._catalogs, wb._models_cache, wb._models_refreshing,
                 wb._selection, wb._aliases, wb._default_model, wb._enforce,
                 wb._account_selections, wb._selection_owner, wb._selection_scoped,
                 wb.MODELS_CACHE_FILE, wb._selection_mtime)
        self.addCleanup(self._restore, saved)
        wb._auth = self.account
        wb._catalogs = {}
        wb._models_cache = {"at": 0.0, "data": None, "owner": None}
        wb._models_refreshing = set()
        wb._selection, wb._aliases, wb._default_model, wb._enforce = [], {}, "", False
        wb._account_selections, wb._selection_owner, wb._selection_scoped = {}, "", False
        wb._selection_mtime = 0.0
        wb.MODELS_CACHE_FILE = os.path.join(self.tmp, "models_cache.json")

    def _restore(self, saved):
        (wb._auth, wb._catalogs, wb._models_cache, wb._models_refreshing,
         wb._selection, wb._aliases, wb._default_model, wb._enforce,
         wb._account_selections, wb._selection_owner, wb._selection_scoped,
         wb.MODELS_CACHE_FILE, wb._selection_mtime) = saved

    def seed(self, models, promotions=None, at=None, complete=True):
        with wb._models_lock:
            wb._catalogs[self.aid] = {
                "at": time.time() if at is None else at,
                "complete": complete,
                "owner": [self.aid],
                "data": {"models": [{"id": m, "credits": "1"} for m in models],
                          "promotions": promotions or []},
            }

    def ids(self, cat):
        return [m["id"] for m in cat["models"]]


class MergeHelperTests(unittest.TestCase):
    def test_merge_models_prefers_new_and_keeps_old(self):
        old = [{"id": "a", "credits": "1"}, {"id": "b", "credits": "1"}]
        new = [{"id": "b", "credits": "2"}, {"id": "c", "credits": "1"}]
        merged = wb._merge_models(old, new)
        self.assertEqual([m["id"] for m in merged], ["b", "c", "a"])
        self.assertEqual(merged[0]["credits"], "2")     # 新条目优先

    def test_merge_models_skips_invalid(self):
        merged = wb._merge_models(["bad", None, {"id": 1}], [{"id": "ok"}])
        self.assertEqual([m["id"] for m in merged], ["ok"])

    def test_merge_promotions_dedupes_by_content(self):
        merged = wb._merge_promotions([PROMO, "junk"], [dict(PROMO)])
        self.assertEqual(len(merged), 1)


class PartialFailureTests(CatalogState):
    def test_partial_failure_merges_instead_of_overwriting(self):
        old_promo = {"modelIds": ["old-promo-model"], "label": "OLD",
                     "discountedCredits": "0.9", "factor": None, "note": ""}
        self.seed(["old-model"], promotions=[old_promo])
        results = [
            (payload("new-model", promo=PROMO), None),
            (None, RuntimeError("timeout")),
            (None, RuntimeError("timeout")),
            (None, RuntimeError("timeout")),
        ]
        with mock.patch.object(wb, "_parallel", return_value=results), \
                mock.patch.object(wb, "_save_models_disk") as save:
            cat = wb._fetch_catalog_now(self.account)

        ids = self.ids(cat)
        self.assertIn("old-model", ids)     # 旧目录里的模型不能丢
        self.assertIn("new-model", ids)
        # 优惠项同样按内容合并：旧的 + 新的都在
        promo_labels = sorted(p.get("label") or "" for p in cat["promotions"])
        self.assertEqual(promo_labels, ["L", "OLD"])
        save.assert_not_called()            # 残缺目录不落盘
        self.assertFalse(wb._catalog_for(self.account)["complete"])

    def test_enterprise_failure_is_optional_not_incomplete(self):
        """企业端点在个人账号上本就可能没有模型，不能因此判定目录残缺。"""
        results = [
            (payload("new-a"), None),
            (payload("new-b"), None),
            (payload("new-c"), None),
            (None, RuntimeError("enterprise 404")),
        ]
        with mock.patch.object(wb, "_parallel", return_value=results), \
                mock.patch.object(wb, "_save_models_disk") as save:
            cat = wb._fetch_catalog_now(self.account)
        self.assertTrue(wb._catalog_for(self.account)["complete"])
        save.assert_called_once()                 # 核心目录完整 → 可以落盘
        self.assertEqual(sorted(self.ids(cat)), ["new-a", "new-b", "new-c"])

    def test_core_route_failure_marks_incomplete_and_skips_disk(self):
        results = [
            (payload("new-a"), None),
            (payload("new-b"), None),
            (None, RuntimeError("cli timeout")),
            (payload("ent"), None),
        ]
        with mock.patch.object(wb, "_parallel", return_value=results), \
                mock.patch.object(wb, "_save_models_disk") as save:
            wb._fetch_catalog_now(self.account)
        self.assertFalse(wb._catalog_for(self.account)["complete"])
        save.assert_not_called()

    def test_all_success_persists_and_marks_complete(self):
        results = [(payload("new-a"), None), (payload("new-b"), None),
                   (payload("new-c"), None), (payload("ent"), None)]
        with mock.patch.object(wb, "_parallel", return_value=results), \
                mock.patch.object(wb, "_save_models_disk") as save:
            cat = wb._fetch_catalog_now(self.account)
        save.assert_called_once()
        self.assertTrue(wb._catalog_for(self.account)["complete"])
        self.assertEqual(sorted(self.ids(cat)), ["ent", "new-a", "new-b", "new-c"])

    def test_all_failed_without_cache_records_attempt_for_backoff(self):
        results = [(None, RuntimeError("x"))] * 4
        with mock.patch.object(wb, "_parallel", return_value=results):
            wb._fetch_catalog_now(self.account)
        self.assertGreater(wb._catalog_for(self.account).get("attemptAt", 0), 0)


class ResolveModelTests(CatalogState):
    def setUp(self):
        super().setUp()
        wb._default_model = "ide"

    def test_selected_model_is_not_rewritten(self):
        self.seed(["ide"], complete=True)          # desk 不在目录里
        wb._selection = ["desk", "ide"]
        model, err = wb.resolve_model("desk", self.account)
        self.assertEqual((model, err), ("desk", None))

    def test_incomplete_catalog_does_not_trigger_fallback(self):
        self.seed(["ide"], complete=False)
        model, err = wb.resolve_model("unknown-model", self.account)
        self.assertEqual((model, err), ("unknown-model", None))

    def test_complete_catalog_falls_back_for_unknown_client_name(self):
        self.seed(["ide"], complete=True)
        model, err = wb.resolve_model("gpt-4o", self.account)
        self.assertEqual((model, err), ("ide", None))

    def test_default_equals_requested_model_untouched(self):
        self.seed(["ide"], complete=True)
        model, err = wb.resolve_model("ide", self.account)
        self.assertEqual((model, err), ("ide", None))


class RefreshFlagTests(CatalogState):
    def test_thread_start_failure_clears_refreshing_flag(self):
        class _BoomThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        self.seed([], at=0, complete=False)
        with mock.patch.object(wb.threading, "Thread", _BoomThread):
            wb._refresh_catalog_bg(self.account)
        self.assertNotIn(self.aid, wb._models_refreshing)

    def test_failed_probe_backoff_skips_new_thread(self):
        with wb._models_lock:
            wb._catalogs[self.aid] = {"at": 0.0, "data": None, "owner": [self.aid],
                                       "complete": False, "attemptAt": time.time()}
        created = []

        class _SpyThread:
            def __init__(self, *a, **k):
                created.append(1)

            def start(self):
                raise AssertionError("不应启动线程")

        with mock.patch.object(wb.threading, "Thread", _SpyThread):
            wb._refresh_catalog_bg(self.account)
        self.assertEqual(created, [])

    def test_clear_cache_discards_refreshing_flag(self):
        wb._models_refreshing.add(self.aid)
        wb.clear_models_cache(self.account)
        self.assertNotIn(self.aid, wb._models_refreshing)


class DiskCacheToleranceTests(CatalogState):
    def test_bad_disk_fields_are_filtered(self):
        with open(wb.MODELS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"owner": [self.aid], "at": [1],
                       "data": {"models": [{"id": "ok"}, "bad", {"id": 5}],
                                 "promotions": ["bad", {"label": "p"}]}}, f)
        with wb._models_lock:
            cached = wb._catalog_for(self.account)
        self.assertEqual(cached["at"], 0.0)
        self.assertEqual([m["id"] for m in cached["data"]["models"]], ["ok"])
        self.assertEqual(len(cached["data"]["promotions"]), 1)
        self.assertTrue(cached["complete"])

    def test_credit_factor_tolerates_junk_entries(self):
        with wb._models_lock:
            wb._catalogs[self.aid] = {
                "at": time.time(), "complete": True, "owner": [self.aid],
                "data": {"models": ["junk", {"id": "m", "credits": "x2"}],
                          "promotions": ["junk", {"modelIds": ["p"]}]}}
        self.assertEqual(wb._credit_factor("m", self.account), 2.0)
        self.assertIsNone(wb._credit_factor("nope", self.account))


if __name__ == "__main__":
    unittest.main()
