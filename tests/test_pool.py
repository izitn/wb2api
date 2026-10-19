#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号池离线回归测试：双池隔离、换号、会话粘性、冷却与熔断。

不访问网络：所有上游调用都被替换成假的。
"""

import atexit
import os
import shutil
import sys
import tempfile
import threading
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


def _acct(uid, realm="cn", nick=None):
    return wb._normalize_auth({
        "accessToken": "tok-" + uid,
        "realm": realm,
        "account": {"uid": uid, "nickname": nick or uid},
    })


class _PoolBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2api-pool-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = (wb._auth, wb._accounts, wb._shared_queries)
        self.addCleanup(self._restore)
        with wb._pool_lock:
            wb._pool_health.clear()
            wb._pool_sticky.clear()
            wb._pool_stats.update({"switch": 0, "exhausted": 0})
        wb._accounts = {}
        wb._auth = None

    def _restore(self):
        wb._auth, wb._accounts, wb._shared_queries = self._saved
        with wb._pool_lock:
            wb._pool_health.clear()
            wb._pool_sticky.clear()

    def add(self, a):
        a["accountId"] = wb.account_id(a)
        wb._accounts[a["accountId"]] = a
        return a

    def make_pool(self, n_cn=2, n_intl=2):
        cn = [self.add(_acct("cn%d" % i, "cn")) for i in range(n_cn)]
        intl = [self.add(_acct("intl%d" % i, "global")) for i in range(n_intl)]
        wb._auth = cn[0] if cn else intl[0]
        return cn, intl


class PoolIsolationTests(_PoolBase):
    """两个池必须物理隔离：绝不跨 realm 选号。"""

    def test_members_split_by_realm(self):
        self.make_pool(2, 3)
        self.assertEqual(len(wb._pool_members("cn")), 2)
        self.assertEqual(len(wb._pool_members("global")), 3)

    def test_pick_never_crosses_pool(self):
        self.make_pool(1, 2)
        # 国内版只有一个成员，且把它排除后，国际版账号绝不能被选中
        pick = wb._pool_pick("cn", exclude={wb.account_id(wb._auth)})
        self.assertIsNone(pick, "国内版池耗尽时不能借用国际版账号")

    def test_pick_returns_requested_realm(self):
        self.make_pool(2, 2)
        for realm in ("cn", "global"):
            got = wb._pool_pick(realm)
            self.assertIsNotNone(got)
            self.assertEqual(wb.realm_of(got), realm)

    def test_empty_pool_returns_none(self):
        self.make_pool(2, 0)
        self.assertIsNone(wb._pool_pick("global"))


class PoolSelectionTests(_PoolBase):
    def test_current_account_preferred(self):
        """单账号或当前账号可用时，行为与原来完全一致。"""
        cn, _ = self.make_pool(3, 0)
        for _ in range(5):
            self.assertIs(wb._pool_pick("cn"), cn[0])

    def test_skips_cooled_account(self):
        cn, _ = self.make_pool(2, 0)
        bad = wb.account_id(cn[0])
        with wb._pool_lock:
            wb._pool_health[bad] = {"cooldownUntil": time.time() + 600, "fails": 0,
                                    "breakerUntil": 0.0, "lastError": "", "softCount": 1}
        got = wb._pool_pick("cn")
        self.assertIs(got, cn[1])

    def test_all_cooled_returns_none(self):
        cn, _ = self.make_pool(2, 0)
        with wb._pool_lock:
            for m in cn:
                wb._pool_health[wb.account_id(m)] = {
                    "cooldownUntil": time.time() + 600, "fails": 0,
                    "breakerUntil": 0.0, "lastError": "", "softCount": 1}
        self.assertIsNone(wb._pool_pick("cn"))

    def test_exclude_skips(self):
        cn, _ = self.make_pool(2, 0)
        got = wb._pool_pick("cn", exclude={wb.account_id(cn[0])})
        self.assertIs(got, cn[1])


class StickySessionTests(_PoolBase):
    def test_sticky_hits_same_account(self):
        cn, _ = self.make_pool(3, 0)
        target = cn[2]
        wb._pool_bind("s1", wb.account_id(target))
        # 即使当前账号是 cn[0]，粘性也要把它绑回 cn[2]
        self.assertIs(wb._pool_pick("cn", "s1"), target)

    def test_sticky_expires(self):
        cn, _ = self.make_pool(2, 0)
        with wb._pool_lock:
            wb._pool_sticky["s2"] = (wb.account_id(cn[1]), time.time() - 1)
        self.assertIs(wb._pool_pick("cn", "s2"), cn[0])

    def test_unbind_releases(self):
        cn, _ = self.make_pool(2, 0)
        wb._pool_bind("s3", wb.account_id(cn[1]))
        wb._pool_unbind("s3", wb.account_id(cn[1]))
        self.assertIs(wb._pool_pick("cn", "s3"), cn[0])

    def test_sticky_ignored_when_account_cooled(self):
        cn, _ = self.make_pool(2, 0)
        aid = wb.account_id(cn[1])
        wb._pool_bind("s4", aid)
        with wb._pool_lock:
            wb._pool_health[aid] = {"cooldownUntil": time.time() + 600, "fails": 0,
                                    "breakerUntil": 0.0, "lastError": "", "softCount": 1}
        self.assertIs(wb._pool_pick("cn", "s4"), cn[0])

    def test_sticky_table_bounded(self):
        cn, _ = self.make_pool(1, 0)
        saved = wb.POOL_STICKY_MAX
        wb.POOL_STICKY_MAX = 10
        try:
            for i in range(50):
                wb._pool_bind("k%d" % i, wb.account_id(cn[0]))
            with wb._pool_lock:
                self.assertLessEqual(len(wb._pool_sticky), wb.POOL_STICKY_MAX)
        finally:
            wb.POOL_STICKY_MAX = saved


class PenaltyTests(_PoolBase):
    def test_429_sets_exponential_cooldown(self):
        a = _acct("x", "cn")
        wb._pool_penalize(a, 429, "too many")
        h = wb._health(wb.account_id(a))
        first = h["cooldownUntil"] - time.time()
        self.assertGreater(first, 0)
        wb._pool_penalize(a, 429, "too many")
        second = h["cooldownUntil"] - time.time()
        self.assertGreater(second, first, "第二次 429 应退避更久")

    def test_429_cooldown_capped(self):
        a = _acct("y", "cn")
        saved = wb.POOL_SOFT_MAX
        wb.POOL_SOFT_MAX = 100
        try:
            for _ in range(12):
                wb._pool_penalize(a, 429, "x")
            h = wb._health(wb.account_id(a))
            self.assertLessEqual(h["cooldownUntil"] - time.time(), 100 + 2)
        finally:
            wb.POOL_SOFT_MAX = saved

    def test_401_cools_immediately(self):
        a = _acct("z", "cn")
        self.assertTrue(wb._pool_penalize(a, 401, "bad token"))
        self.assertFalse(wb._pool_available(wb.account_id(a)))

    def test_breaker_after_threshold(self):
        a = _acct("b", "cn")
        for _ in range(wb.POOL_FAIL_THRESHOLD):
            wb._pool_penalize(a, 502, "boom")
        h = wb._health(wb.account_id(a))
        self.assertGreater(h["breakerUntil"], time.time(), "达到阈值应熔断")
        self.assertFalse(wb._pool_available(wb.account_id(a)))

    def test_client_errors_not_penalized(self):
        """400/404 是请求本身的问题，惩罚账号只会白白踢掉好账号。"""
        a = _acct("c", "cn")
        for st in (400, 404, 409, 422):
            self.assertFalse(wb._pool_penalize(a, st, "bad request"))
        self.assertTrue(wb._pool_available(wb.account_id(a)))

    def test_mark_ok_resets(self):
        a = _acct("d", "cn")
        wb._pool_penalize(a, 502, "boom")
        wb._pool_mark_ok(a)
        h = wb._health(wb.account_id(a))
        self.assertEqual(h["fails"], 0)
        self.assertEqual(h["softCount"], 0)
        self.assertEqual(h["lastError"], "")


class SwitchTests(_PoolBase):
    def test_switch_moves_to_other_account(self):
        cn, _ = self.make_pool(2, 0)
        tried = set()
        nxt = wb._pool_switch(cn[0], 429, "slow down", "", tried)
        self.assertIs(nxt, cn[1])
        self.assertIn(wb.account_id(cn[0]), tried)

    def test_switch_returns_none_when_exhausted(self):
        cn, _ = self.make_pool(1, 0)
        self.assertIsNone(wb._pool_switch(cn[0], 429, "x", "", set()))

    def test_switch_retries_each_once(self):
        cn, _ = self.make_pool(3, 0)
        tried = set()
        a = cn[0]
        seen = [wb.account_id(a)]
        while True:
            nxt = wb._pool_switch(a, 429, "x", "", tried)
            if nxt is None:
                break
            a = nxt
            seen.append(wb.account_id(a))
        self.assertEqual(len(seen), 3, "3 个账号应各被尝试一次后耗尽")

    def test_switch_unbinds_sticky(self):
        cn, _ = self.make_pool(2, 0)
        wb._pool_bind("sk", wb.account_id(cn[0]))
        wb._pool_switch(cn[0], 429, "x", "sk", set())
        with wb._pool_lock:
            self.assertNotIn("sk", wb._pool_sticky)

    def test_can_switch_respects_status(self):
        cn, _ = self.make_pool(2, 0)
        self.assertTrue(wb._pool_can_switch(cn[0], 429, set()))
        self.assertTrue(wb._pool_can_switch(cn[0], 502, set()))
        self.assertFalse(wb._pool_can_switch(cn[0], 400, set()))
        self.assertFalse(wb._pool_can_switch(cn[0], 404, set()))

    def test_can_switch_respects_max(self):
        cn, _ = self.make_pool(5, 0)
        tried = {wb.account_id(m) for m in cn[:wb.POOL_MAX_SWITCH]}
        self.assertFalse(wb._pool_can_switch(cn[0], 429, tried))

    def test_disabled_pool_never_switches(self):
        cn, _ = self.make_pool(2, 0)
        saved = wb.POOL_ENABLED
        wb.POOL_ENABLED = False
        try:
            self.assertFalse(wb._pool_can_switch(cn[0], 429, set()))
        finally:
            wb.POOL_ENABLED = saved


class StatusTests(_PoolBase):
    def test_pool_status_shape(self):
        self.make_pool(2, 2)
        st = wb.pool_status()
        self.assertEqual(st["cn"]["count"], 2)
        self.assertEqual(st["global"]["count"], 2)
        self.assertEqual(st["cn"]["available"], 2)
        # 成员按 account id 排序，不能假设顺序；只断言恰好有一个是当前账号
        self.assertEqual(sum(1 for m in st["cn"]["members"] if m["active"]), 1)
        self.assertIn("stickyCount", st)
        self.assertIn("switch", st["stats"])

    def test_status_reflects_cooldown(self):
        cn, _ = self.make_pool(2, 0)
        wb._pool_penalize(cn[0], 429, "slow")
        st = wb.pool_status()
        self.assertEqual(st["cn"]["available"], 1)
        cooled = [m for m in st["cn"]["members"] if not m["available"]]
        self.assertEqual(len(cooled), 1)
        self.assertGreater(cooled[0]["cooldownIn"], 0)


class ConcurrencyTests(_PoolBase):
    def test_concurrent_pick_consistent(self):
        self.make_pool(2, 2)
        errors = []
        results = []

        def work():
            try:
                for _ in range(200):
                    got = wb._pool_pick("cn")
                    results.append(wb.realm_of(got))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(15)
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"cn"}, "并发选号不得跨池")

    def test_concurrent_health_updates(self):
        a = _acct("cc", "cn")
        errors = []

        def work():
            try:
                for _ in range(200):
                    wb._pool_penalize(a, 429, "x")
                    wb._pool_available(wb.account_id(a))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(15)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
