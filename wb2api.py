#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb2api-lite —— 把 www.workbuddy.ai 原生服务封装成 OpenAI / Anthropic 兼容接口。

使用 Python 标准库，为 WorkBuddy 账号提供接口代理、账号热切换与中文网页控制台。
登录凭证、模型配置和用量保存在数据目录中。

覆盖端点：
    POST /v1/chat/completions   流式 + 非流式
    POST /v1/responses          流式 + 非流式（薄适配）
    POST /v1/messages           Anthropic Messages 兼容接口
    GET  /v1/models
    GET  /healthz

用法：
    python wb2api.py                          # 读 ./auth.json，监听 127.0.0.1:8787
    python wb2api.py --port 9000 --host 0.0.0.0
    WB_API_KEY=sk-local python wb2api.py      # 固定本地访问密钥

客户端接入：
    export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
    export OPENAI_API_KEY=<本地访问密钥>
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import http.client
import json
import math
import os
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ===========================================================================
# 一、上游事实（来自对官方桌面端出站流量的观测，不是猜的）
# ===========================================================================
# 显式设置 WB_UPSTREAM_BASE 时一律走它；否则按账号 realm 选上游（见 REALMS）
UPSTREAM_OVERRIDE = os.environ.get("WB_UPSTREAM_BASE", "").rstrip("/")
UPSTREAM_BASE = UPSTREAM_OVERRIDE or "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"          # 国际版主路径
CHAT_PATH_ALT = "/console/chat/completions"  # 上游新旧路径分叉，404/405 时回落
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
MODELS_PATH = "/v3/config"

# 客户端版本：上游提高最低版本要求时，用环境变量覆盖即可，不必改源码
CLIENT_VERSION = os.environ.get("WB_CLIENT_VERSION", "5.5.4")      # UA 里的 WorkBuddy/<ver>
CLI_VERSION = os.environ.get("WB_CLI_VERSION", "2.137.1")          # UA 里的 CLI/<ver>
# 国际版桌面端身份：按对 WorkBuddy 5.8.4 的抓包对齐（2026-10-16 样本）。
# 2026-10-19 起上游加强国际版渠道校验（400 / code 11128 "request illegal"，
# displayTips 明说"没通过渠道校验，可能不是官方客户端发出的"），旧的 5.5.x 身份
# 与缺失的会话头族会被判非官方。国内版没有对应抓包且目前正常，维持原身份不动。
INTL_CLIENT_VERSION = os.environ.get("WB_INTL_CLIENT_VERSION", "5.8.4")
INTL_CLI_VERSION = os.environ.get("WB_INTL_CLI_VERSION", "2.161.0")
IDE_UA = os.environ.get("WB_IDE_UA", "CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0")  # 目录探测路之一
CLI_UA = os.environ.get("WB_CLI_UA", "CLI/2.63.2 CodeBuddy/2.63.2")           # 目录探测路之一
# realm → (上游 base, Origin, X-Domain, UA 平台段, Accept-Language, 显示名)
# 国内版的 chat/目录端点沿用登录同域，这是推断而非实测，可用 WB_UPSTREAM_BASE 覆盖
REALMS = {
    "global": {"base": "https://www.workbuddy.ai", "origin": "https://www.workbuddy.ai",
               "domain": "www.workbuddy.ai", "platform": "WorkBuddy AI",
               "lang": "en-US", "label": "国际版"},
    "cn": {"base": "https://copilot.tencent.com", "origin": "https://www.codebuddy.cn",
           "domain": "www.codebuddy.cn", "platform": "WorkBuddy",
           "lang": "zh-CN", "label": "国内版"},
}

REFRESH_SKEW = 300        # 到期前 5 分钟主动刷新
REFRESH_AHEAD = 900       # 后台线程提前 15 分钟刷新，请求路径上基本不再同步刷新
REFRESH_BACKOFF = 60      # 刷新失败后的冷却，避免每个请求都卡 30s 超时
UPSTREAM_TIMEOUT = 300
UPSTREAM_RETRIES = 2      # 对 429 与确认未发送请求的建连失败最多重试次数
RETRY_BASE_DELAY = 0.5    # 首次重试等待秒数；之后按 RETRY_FACTOR 指数退避
RETRY_FACTOR = 3.0
RETRY_MAX_DELAY = 8.0     # 退避超过该值就不再重试，直接把错误交给客户端
MODELS_TTL = 600
# --- 账号池 ---
# 只在国内版/国际版各自池内换号，绝不跨池：两个区域的 token 与上游域都不同，
# 混池会把请求发到错误的上游。
POOL_ENABLED = os.environ.get("WB_POOL", "1") not in ("", "0", "false", "False")
MAX_BODY = 16 * 1024 * 1024
def _env_int(name: str, default: int, minimum: int = 0,
             maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(minimum, value)


# --- 账号池 ---
POOL_SOFT_COOLDOWN = _env_float("WB_POOL_SOFT_COOLDOWN", 600)   # 429 等：指数退避，起点 600s
POOL_SOFT_MAX = _env_float("WB_POOL_SOFT_MAX", 7200)            # 软冷却封顶
POOL_HARD_COOLDOWN = _env_float("WB_POOL_HARD_COOLDOWN", 0)     # 0 表示硬错误用默认
POOL_FAIL_THRESHOLD = _env_int("WB_POOL_FAIL_THRESHOLD", 3, 1)  # 连续失败多少次熔断
POOL_BREAKER = _env_float("WB_POOL_BREAKER", 1800)
POOL_STICKY_TTL = _env_float("WB_POOL_STICKY_TTL", 1800)
POOL_STICKY_MAX = _env_int("WB_POOL_STICKY_MAX", 4096, 1)
POOL_MAX_SWITCH = _env_int("WB_POOL_MAX_SWITCH", 3)


MAX_INFLIGHT = _env_int("WB_MAX_INFLIGHT", 32, 1)   # 同时在途的对话请求上限
CLIENT_TIMEOUT = 120      # 下游连接空闲超时（防慢速连接占线程）
RESP_STORE_MAX = 256      # previous_response_id 内存会话条数
RESP_STORE_TTL = 6 * 3600
RESP_STORE_BYTES = 32 * 1024 * 1024  # 会话历史序列化后的总容量上限
DEBUG = os.environ.get("WB_DEBUG", "") not in ("", "0")
# 大请求体 gzip 后再发上游（实测上游支持；Codex 长上下文 133KB → 41KB）。失败会自动关闭并明文重发
GZIP_MIN = _env_int("WB_GZIP_MIN", 16384)      # 0 = 关闭
_gzip_ok = GZIP_MIN > 0
_gzip_disabled_bases: set[str] = set()
_gzip_stats = {"requests": 0, "saved_bytes": 0}
_gzip_stats_lock = threading.Lock()


def _gzip_disabled(base: str) -> bool:
    with _gzip_stats_lock:
        return base in _gzip_disabled_bases


KEEPALIVE_EVERY = 15      # 上游长时间思考期间，至少每 15 秒给客户端发一次 SSE 心跳
# 上游强制首条消息为 system；客户端没带时补这一条（可用环境变量改）
DEFAULT_SYSTEM = os.environ.get("WB_DEFAULT_SYSTEM", "You are a helpful assistant.")

_HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(_HERE, "ui.html")
# 状态文件统一放数据目录（默认脚本所在目录），不再随启动时的工作目录漂移
DATA_DIR = os.environ.get("WB_DATA_DIR") or _HERE


def _data_path(env: str, name: str) -> str:
    return os.environ.get(env) or os.path.join(DATA_DIR, name)


AUTH_FILE = _data_path("WB_AUTH_FILE", "auth.json")
LOCAL_API_KEY = os.environ.get("WB_API_KEY", "")
API_KEY_FILE = _data_path("WB_API_KEY_FILE", "api_key.txt")
SELECTION_FILE = _data_path("WB_SELECTION_FILE", "selection.json")
USAGE_FILE = _data_path("WB_USAGE_FILE", "usage.jsonl")
MODELS_CACHE_FILE = _data_path("WB_MODELS_CACHE_FILE", "models_cache.json")

_lock = threading.RLock()
_private_write_locks = tuple(threading.Lock() for _ in range(64))
_models_lock = threading.Lock()
_models_refreshing: set[str] = set()
_auth: dict | None = None
_accounts: dict[str, dict] = {}
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_failures: dict[str, float] = {}
_models_cache: dict = {"at": 0.0, "data": None, "owner": None}
_catalogs: dict[str, dict] = {}
_selection: list[str] = []          # 用户勾选的模型子集；空 = 不过滤
_account_selections: dict[str, list[str]] = {}
_selection_owner = ""
_selection_scoped = False
_aliases: dict[str, str] = {}       # 模型别名：客户端写死的名字 → 实际模型
_default_model = ""                 # 请求没带 model 或模型不在目录里时使用
_enforce = False                    # True：只允许调用已勾选的模型
_selection_mtime = 0.0
_selection_checked = 0.0
_selection_lock = threading.RLock()
_pending_login: dict | None = None  # 网页端登录流程的临时上下文
_login_generation = 0
_login_lock = threading.Lock()
_BIND_HOST = "127.0.0.1"
_PORT = 8787
_SERVE_UI = True
HIDE_REASONING = False
_slots = threading.BoundedSemaphore(MAX_INFLIGHT)
_inflight_lock = threading.Lock()
_inflight = 0
_started_at = time.time()
_points_lock = threading.RLock()
_points_state: dict[str, dict] = {}
_checkin_locks: dict[str, threading.Lock] = {}
_auto_checkin = False
_settings_lock = threading.Lock()
_checkin_wake = threading.Event()
_STARTUP_VALUE = "wb2api-lite"
_STARTUP_LEGACY_VALUES = ("wb2oai-lite",)   # 改名前的启动项，迁移时一并清理
_STARTUP_REGISTRY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_api_key_persisted = False  # 密钥是否已持久化（来自文件或刚生成并落盘）

# --- 账号池状态 ---
# 每个账号一条健康记录：冷却到期时间、连续失败次数、熔断到期时间、最近一次错误。
# 换号只发生在向客户端写出任何字节之前，流式请求中途失败不会二次换号。
_pool_lock = threading.RLock()
_pool_health: dict[str, dict] = {}
# 会话粘性：会话键 → (账号 id, 过期时间)。同一对话尽量落在同一账号，
# 既符合官方客户端"一个会话一台设备"的语义，也避免多轮上下文在两个池成员间跳变。
_pool_sticky: dict[str, tuple[str, float]] = {}
_pool_stats = {"switch": 0, "exhausted": 0}


def settings_file() -> str:
    return _data_path("WB_SETTINGS_FILE", "settings.json")


def load_settings() -> None:
    global _auto_checkin
    with _settings_lock:
        try:
            with open(settings_file(), encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            _auto_checkin = False
            return
        if not isinstance(cfg, dict) or not isinstance(cfg.get("autoCheckin", False), bool):
            raise ValueError("自动签到设置格式不正确")
        _auto_checkin = cfg.get("autoCheckin", False)


def set_auto_checkin(enabled: bool) -> dict:
    global _auto_checkin
    with _settings_lock:
        _write_private(settings_file(), json.dumps({"autoCheckin": enabled}, ensure_ascii=False, indent=2))
        _auto_checkin = enabled
    _checkin_wake.set()
    return {"ok": True, "autoCheckin": enabled, "message": "已开启每日自动签到" if enabled else "已关闭每日自动签到"}


def _billing_base(a: dict) -> str:
    if UPSTREAM_OVERRIDE:
        return UPSTREAM_OVERRIDE
    if realm_of(a) == "global":
        return "https://www.workbuddy.ai"
    domain = str(a.get("domain") or "").lower().strip()
    return "https://www.workbuddy.cn" if domain in ("workbuddy.cn", "www.workbuddy.cn") else "https://www.codebuddy.cn"


class BillingError(RuntimeError):
    def __init__(self, status, code, message):
        self.status, self.code = status, str(code)
        super().__init__(message)


BILLING_ENVELOPE_MAX_DEPTH = 6   # 只拆已知信封，深度受限，避免畸形响应把解析拖进死循环
BILLING_POINTS_MAX_DEPTH = 8     # 积分汇总允许的最大嵌套层数，超出即判定无法识别


def _billing_payload(obj):
    """只拆开已知响应信封，绝不递归捡取任意数字。"""
    for _ in range(BILLING_ENVELOPE_MAX_DEPTH):
        if not isinstance(obj, dict):
            break
        for key in ("data", "Response", "Data"):
            if isinstance(obj.get(key), (dict, list)):
                obj = obj[key]
                break
        else:
            break
    return obj


def _billing_post(a: dict, path: str, body: dict | None = None):
    """账单走账号所属官网域；只读查询遇到传输抖动时最多安全重试一次。"""
    with _lock:
        a = dict(_accounts.get(account_id(a)) or a)
    refreshed = False
    if a.get("expiresAt") and a["expiresAt"] - REFRESH_SKEW <= time.time():
        refreshed = True
        refresh_auth(a, stale_token=a.get("accessToken"))
    base = _billing_base(a)
    readonly = path != "/v2/billing/meter/daily-checkin"
    transport_retries = 1 if readonly else 0
    transport_attempt = 0
    while True:
        headers = common_headers(a)
        headers.update({"Origin": base, "Referer": base + "/profile/plans-usage",
                        "X-Client-Platform": "web", "Accept": "application/json, text/plain, */*"})
        if a.get("enterpriseId"):
            headers["X-Tenant-Id"] = a["enterpriseId"]
        try:
            resp = upstream_request("POST", base + path, headers,
                                    json.dumps(body or {}).encode("utf-8"), 15)
            try:
                raw = resp.read()
            finally:
                resp.close()
        except (OSError, http.client.HTTPException):
            if transport_attempt >= transport_retries:
                raise
            transport_attempt += 1
            time.sleep(0.2)
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise BillingError(resp.status, resp.status, "积分服务返回了无法解析的数据（HTTP %s）" % resp.status)
        if not isinstance(payload, dict):
            raise BillingError(resp.status, -1, "积分服务响应格式不正确")
        code = str(payload.get("code", 0 if "data" in payload or "Response" in payload else -1))
        message = str(payload.get("message") or payload.get("msg") or "积分服务返回错误码 %s" % code)
        unauthorized = resp.status in (401, 403) or code in ("401", "403")
        if unauthorized and not refreshed and a.get("refreshToken"):
            refreshed = True
            if refresh_auth(a, stale_token=a.get("accessToken")):
                continue
        if resp.status >= 400 or code not in ("0", "200") or payload.get("ok") is False:
            raise BillingError(resp.status, code, message)
        return _billing_payload(payload)
    raise RuntimeError("登录授权已失效，请重新登录")


def _number(obj: dict, keys):
    for key in keys:
        value = obj.get(key)
        if value is None or isinstance(value, bool) or value == "":
            continue
        try:
            value = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(value):
            return value
    return None


def _resource_rows(obj):
    obj = _billing_payload(obj)
    if isinstance(obj, dict):
        for key in ("Accounts", "accounts", "Packages", "packages"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return None


def _points_value(obj, _depth: int = 0):
    """从已知字段推导剩余积分。嵌套超过 BILLING_POINTS_MAX_DEPTH 视为无法识别。

    深度上限是防御性的：上游正常情况下最多两三层（信封 → Accounts → 单项），
    畸形或恶意的深层嵌套不应该在这里无限递归。
    """
    if _depth > BILLING_POINTS_MAX_DEPTH:
        return None
    obj = _billing_payload(obj)
    rows = _resource_rows(obj)
    if rows is not None:
        if not rows:
            return 0.0
        values = [_points_value(row, _depth + 1) for row in rows]
        return sum(values) if all(v is not None for v in values) else None
    if not isinstance(obj, dict):
        return None
    remaining = _number(obj, ("CycleCapacityRemainPrecise", "cycleCapacityRemainPrecise",
        "CycleCapacityRemain", "CycleRemainCapacity", "CapacityRemainPrecise", "CapacityRemain",
        "SlicePeriodCapacityRemainPrecise", "SlicePeriodCapacityRemain", "balance", "remaining"))
    if remaining is not None:
        return max(0.0, remaining)
    total = _number(obj, ("CycleCapacitySizePrecise", "CycleCapacitySize", "CycleTotalCapacity",
                         "CapacitySizePrecise", "CapacitySize"))
    used = _number(obj, ("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CycleUsedCapacity",
                        "CapacityUsedPrecise", "CapacityUsed"))
    if total is not None and used is not None:
        return max(0.0, total - used)
    slices = obj.get("SlicePeriodUsageDetails")
    if isinstance(slices, list) and slices:
        return _points_value(slices[0], _depth + 1)
    return None


def _resource_pages(a, path, body):
    rows = []
    for page in range(1, 101):
        data = _billing_post(a, path, {**body, "PageNumber": page, "PageSize": 200})
        batch = _resource_rows(data)
        if batch is None or any(not isinstance(row, dict) for row in batch):
            raise RuntimeError("积分资源列表格式无法识别")
        rows.extend(batch)
        total = _number(data, ("TotalCount", "totalCount")) if isinstance(data, dict) else None
        if (total is not None and len(rows) >= total) or len(batch) < 200:
            return rows
    raise RuntimeError("积分资源分页数量过多，未返回不完整的余额")


def _query_points(a):
    if a.get("enterpriseId"):
        paths = ["/v2/billing/meter/get-enterprise-user-usage",
                 "/billing/meter/get-enterprise-user-usage"]
        data = _billing_fallback(a, paths, {})
        limit = _number(data, ("limitNum", "limit_num"))
        used = _number(data, ("credit", "used_num"))
        if limit is None or used is None:
            raise RuntimeError("企业积分配额格式无法识别")
        return {"balance": None if limit == -1 else max(0, limit - used), "unlimited": limit == -1}
    if realm_of(a) == "cn":
        try:
            data = _billing_post(a, "/billing/meter/get-user-resource-summary")
            value = _points_value(data)
            if value is not None:
                return {"balance": value, "unlimited": False}
            packages = _resource_rows(data)
            codes = list(dict.fromkeys(str(p["PackageCode"]) for p in (packages or [])
                                        if isinstance(p, dict) and p.get("PackageCode")))
            if codes:
                rows, seen = [], set()
                for suffix in ("free", "paid"):
                    batch = _resource_pages(a, "/billing/meter/get-user-resource-%s-packages" % suffix,
                        {"PackageCodes": codes, "Status": [0, 3], "NeedRenewInfo": True})
                    for row in batch:
                        rid = row.get("AccountId") or row.get("ResourceId")
                        if rid and str(rid) in seen:
                            continue
                        if rid:
                            seen.add(str(rid))
                        rows.append(row)
                value = _points_value({"Accounts": rows})
                if value is not None:
                    return {"balance": value, "unlimited": False}
            raise RuntimeError("积分余额字段无法识别")
        except BillingError as e:
            if e.status != 404 and e.code != "404":
                raise
    paths = ["/v2/billing/meter/get-user-resource"] if realm_of(a) == "cn" else [
        "/billing/meter/get-user-resource", "/v2/billing/meter/get-user-resource"]
    body = {"ProductCode": "p_tcaca", "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": "2126-12-31 23:59:59"}
    for index, path in enumerate(paths):
        try:
            rows = _resource_pages(a, path, body)
            value = _points_value({"Accounts": rows})
            if value is None:
                raise RuntimeError("积分余额字段无法识别")
            return {"balance": value, "unlimited": False}
        except BillingError as e:
            if index + 1 == len(paths) or (e.status != 404 and e.code != "404"):
                raise
    raise RuntimeError("积分查询失败")


def _billing_fallback(a, paths, body=None):
    for index, path in enumerate(paths):
        try:
            return _billing_post(a, path, body)
        except BillingError as e:
            if index + 1 == len(paths) or (e.status != 404 and e.code != "404"):
                raise


def _update_points(aid, patch):
    with _points_lock:
        state = _points_state.setdefault(aid, {})
        state.update(patch)
        return dict(state)


class _SharedQueries:
    """同一键的在途查询共用结果或异常；网络等待期间不持有全局锁。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def wait(self, key):
        with self._lock:
            flight = self._pending.get(key)
        if flight is not None:
            flight["done"].wait()

    def run(self, key, query):
        with self._lock:
            flight = self._pending.get(key)
            leader = flight is None
            if leader:
                flight = {"done": threading.Event()}
                self._pending[key] = flight
        if leader:
            try:
                flight["result"] = query()
            except BaseException as e:
                flight["error"] = e
            finally:
                with self._lock:
                    self._pending.pop(key, None)
                    flight["done"].set()
        else:
            flight["done"].wait()
        if "error" in flight:
            raise flight["error"]
        return flight["result"]


_shared_queries = _SharedQueries()


def account_points(a: dict, force=False) -> dict:
    aid = account_id(a)
    with _points_lock:
        old = dict(_points_state.get(aid) or {})
    if old.get("at") and not force and time.time() - old["at"] < 120:
        return old
    def query():
        try:
            result = {**_query_points(a), "error": "", "updatedAt": time.time()}
        except Exception as e:
            result = {"error": "积分查询失败：" + str(e)}
        return _update_points(aid, {**result, "accountId": aid, "at": time.time()})
    return dict(_shared_queries.run(("points", aid), query))


def _checkin_day():
    # 国内活动按北京时间换日，不随运行主机时区漂移。
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def _supports_checkin(a):
    return realm_of(a) == "cn" and not a.get("enterpriseId")


#: 上游"今天已经签过"的业务码。10001 来自 hub 的实测；14001 来自 panel 的实测
#: （panel 的 IsAlreadyCheckin 用它判定幂等拒绝）。两者都按"已签到"处理，
#: 否则重启补签时会把一个正常状态报成失败。
CHECKIN_ALREADY_CODES = ("10001", "14001")
#: 文案兜底。只放明确表达"已经签过"的短语：裸 "already" 会误伤
#: "already in progress" 这类瞬时错误，把失败记成成功。
CHECKIN_ALREADY_MARKERS = ("已签到", "今天已签到", "already checked in", "already signed in")


def _checkin_already(e: "BillingError") -> bool:
    """判断上游拒绝是否只是"今天已经签过了"（幂等，不是失败）。

    只认带分类的业务错误：网络层/解析层错误绝不能当成"已签到"，
    否则停机补签遇到抖动会误记成功，账号当天其实没签到。
    """
    if not isinstance(e, BillingError):
        return False
    if e.code in CHECKIN_ALREADY_CODES:
        return True
    msg = str(e)
    low = msg.lower()
    return any(m in msg or m.lower() in low for m in CHECKIN_ALREADY_MARKERS)


def _checkin_transient(e) -> bool:
    """上游 5xx / 连接抖动值得重试；业务错误重试只会原样再失败一次。"""
    if isinstance(e, BillingError):
        return e.status >= 500
    return isinstance(e, (OSError, http.client.HTTPException))


def _billing_post_checkin(a: dict) -> None:
    """提交每日签到。瞬时 5xx 有界重试（共 3 次），业务错误立即放弃。

    上游偶发返回 HTTP 500 / code 10000（"API request failed with status code: 500"），
    这类抖动重试一次通常就成功；而"今日已签到"之类的业务拒绝重试没有意义。
    """
    last = None
    for attempt in range(3):
        try:
            _billing_post(a, "/v2/billing/meter/daily-checkin")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            if not _checkin_transient(e) or _checkin_already(e):
                raise
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if last is not None:
        raise last


def daily_checkin(a: dict) -> dict:
    aid = account_id(a)
    if not _supports_checkin(a):
        return _update_points(aid, {"accountId": aid, "checkin": False, "checkinSupported": False,
            "checkinMessage": "仅国内版个人账号支持每日签到"})
    with _points_lock:
        mutex = _checkin_locks.setdefault(aid, threading.Lock())
    with mutex:
        today = _checkin_day()
        with _points_lock:
            old = dict(_points_state.get(aid) or {})
        if old.get("checkinDay") == today and old.get("checkin") is True:
            return old
        patch = {"accountId": aid, "checkinSupported": True, "lastCheckinAttempt": time.time()}
        try:
            status = _billing_fallback(a, ["/v2/billing/meter/checkin-activity-status",
                                          "/v2/billing/meter/checkin-status"])
            if not isinstance(status, dict):
                raise RuntimeError("签到状态格式无法识别")
            if status.get("active") is False:
                raise RuntimeError("当前签到活动未开放")
            checked = status.get("today_checked_in", status.get("todayCheckedIn"))
            if not isinstance(checked, bool):
                raise RuntimeError("签到状态缺少今日是否签到字段")
            already = checked
            if not checked:
                try:
                    _billing_post_checkin(a)
                except BillingError as e:
                    if _checkin_already(e):
                        already = True
                    else:
                        raise
            patch.update(checkin=True, checkinDay=today, checkinMessage="今日已签到" if already else "签到成功，已领取今日积分")
        except Exception as e:
            patch.update(checkin=False, checkinMessage="签到失败：" + str(e))
        out = _update_points(aid, patch)
        if out.get("checkin"):
            # 先排空签到前的查询，确保新余额不会复用领积分前的结果。
            _shared_queries.wait(("points", aid))
            out = account_points(a, True)
        return out


def _run_auto_checkin_cycle():
    if not _auto_checkin:
        return
    with _lock:
        accounts = dict(_accounts)
        if _auth:
            accounts[account_id(_auth)] = _auth
    for aid, a in accounts.items():
        if not _auto_checkin:
            break
        if not _supports_checkin(a):
            continue
        with _points_lock:
            state = dict(_points_state.get(aid) or {})
        if state.get("checkinDay") == _checkin_day() and state.get("checkin") is True:
            continue
        if time.time() - state.get("lastCheckinAttempt", 0) < 1800:
            continue
        result = daily_checkin(a)
        log("自动签到：%s，%s" % (account_name(a) or "未命名账号", result.get("checkinMessage", "")))


def _auto_checkin_loop():
    while True:
        _checkin_wake.clear()
        try:
            _run_auto_checkin_cycle()
        except Exception as e:
            log("自动签到检查失败：%s" % e)
        _checkin_wake.wait(60)


def _startup_command() -> str:
    python = os.path.abspath(sys.executable)
    if os.name == "nt" and os.path.basename(python).lower() == "python.exe":
        windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(windowless):
            python = windowless
    cmd = [python, os.path.abspath(__file__), "--host", _BIND_HOST, "--port", str(_PORT),
           "--data-dir", os.path.abspath(DATA_DIR), "--auth", os.path.abspath(AUTH_FILE)]
    if not _SERVE_UI:
        cmd.append("--no-ui")
    if HIDE_REASONING:
        cmd.append("--hide-reasoning")
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


# --- Linux：systemd 用户单元 -------------------------------------------------
# 只用「用户级」单元（~/.config/systemd/user），不需要 sudo、也不改系统目录。
# 用户级单元还需要 `loginctl enable-linger` 才能在未登录时启动，这一点在
# message 里明确告诉用户，不偷偷改系统状态。
_SYSTEMD_UNIT = "wb2api-lite.service"


def _systemd_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user")


def _systemd_path() -> str:
    return os.path.join(_systemd_dir(), _SYSTEMD_UNIT)


def _systemd_available() -> bool:
    """有 systemd 且 systemctl 可用（WSL / 容器里通常没有）。"""
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def _systemd_unit_text() -> str:
    args = [os.path.abspath(sys.executable), os.path.abspath(__file__),
            "--host", _BIND_HOST, "--port", str(_PORT),
            "--data-dir", os.path.abspath(DATA_DIR), "--auth", os.path.abspath(AUTH_FILE)]
    if not _SERVE_UI:
        args.append("--no-ui")
    if HIDE_REASONING:
        args.append("--hide-reasoning")
    # systemd 的 ExecStart 不是 shell，逐项加引号转义
    exec_start = " ".join(shlex.quote(x) for x in args)
    return (
        "[Unit]\n"
        "Description=wb2api-lite local proxy\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s\n"
        "WorkingDirectory=%s\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (exec_start, os.path.abspath(DATA_DIR))


def _linux_startup_status() -> dict:
    if not _systemd_available():
        return {"ok": True, "supported": False, "enabled": False,
                "message": "当前 Linux 环境没有 systemd（WSL/容器常见），"
                           "请用 systemd、tmux、nohup 或容器编排自行常驻"}
    path = _systemd_path()
    enabled = os.path.isfile(path)
    return {"ok": True, "supported": True, "enabled": enabled,
            "path": path,
            "message": ("wb2api 已启用 Linux 用户级自启（%s）" % path) if enabled
            else "wb2api 未启用 Linux 用户级自启"}


def _linux_set_startup(enabled: bool) -> dict:
    if not _systemd_available():
        return {**_linux_startup_status(), "ok": False}
    path = _systemd_path()
    try:
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(_systemd_unit_text())
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            subprocess.run(["systemctl", "--user", "enable", _SYSTEMD_UNIT],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        else:
            subprocess.run(["systemctl", "--user", "disable", _SYSTEMD_UNIT],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        result = _linux_startup_status()
        if result.get("enabled") != enabled:
            result.update(ok=False, message="systemd 单元写入后状态未生效，请检查 ~/.config/systemd 权限")
        elif enabled:
            result["message"] += "；注销后仍需运行，请执行：loginctl enable-linger $USER"
        return result
    except Exception as e:  # noqa: BLE001
        return {**_linux_startup_status(), "ok": False, "message": "设置 Linux 自启失败：" + str(e)}


def _drop_legacy_startup_values() -> None:
    """删除改名前的自启项，避免旧脚本名残留成失效启动项。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
            for name in _STARTUP_LEGACY_VALUES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def startup_status() -> dict:
    if os.name != "nt":
        return _linux_startup_status()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REGISTRY, 0, winreg.KEY_READ) as key:
            try:
                command, _ = winreg.QueryValueEx(key, _STARTUP_VALUE)
            except FileNotFoundError:
                # 旧版本的自启项：按新名字重建后再清理，保留用户原有的自启选择
                try:
                    winreg.QueryValueEx(key, _STARTUP_LEGACY_VALUES[0])
                except FileNotFoundError:
                    raise
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _STARTUP_REGISTRY, 0,
                                        winreg.KEY_SET_VALUE) as wkey:
                    winreg.SetValueEx(wkey, _STARTUP_VALUE, 0, winreg.REG_SZ, _startup_command())
                _drop_legacy_startup_values()
                command = _startup_command()
        enabled = command == _startup_command()
        return {"ok": True, "supported": True, "enabled": enabled,
                "message": "wb2api 已启用开机自启（当前用户登录后启动）" if enabled else "wb2api 未启用开机自启"}
    except FileNotFoundError:
        return {"ok": True, "supported": True, "enabled": False, "message": "wb2api 未启用开机自启"}
    except OSError as e:
        return {"ok": False, "supported": True, "enabled": False, "message": "读取 wb2api 自启设置失败：" + str(e)}


def set_startup(enabled: bool) -> dict:
    if os.name != "nt":
        if enabled and not _api_key_persisted:
            return {
                "ok": False, "supported": True, "enabled": False,
                "message": "当前访问密钥来自命令行参数或环境变量，重启后会丢失。"
                           "请移除 --api-key 或 WB_API_KEY，让程序自动生成并保存密钥文件后再开启自启",
            }
        return _linux_set_startup(enabled)
    # 检查密钥是否已持久化：非持久化密钥在重启后会丢失，必须拒绝
    if enabled and not _api_key_persisted:
        return {
            "ok": False,
            "supported": True,
            "enabled": False,
            "message": "当前访问密钥来自命令行参数或环境变量，重启后会丢失。"
                      "请移除 --api-key 或 WB_API_KEY，让程序自动生成并保存密钥文件后再开启自启"
        }
    try:
        import winreg
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _STARTUP_REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _STARTUP_VALUE, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
                    try:
                        winreg.DeleteValue(key, _STARTUP_VALUE)
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
        _drop_legacy_startup_values()
        result = startup_status()
        if result.get("ok") and result["enabled"] != enabled:
            result.update(ok=False, message="wb2api 自启设置写入后未生效，请检查当前用户的系统策略")
        return result
    except OSError as e:
        return {**startup_status(), "ok": False, "message": "设置 wb2api 开机自启失败：" + str(e)}




# ===========================================================================
# 二、通用工具
# ===========================================================================
def log(msg: str) -> None:
    if sys.stderr is not None:  # pythonw 开机后台启动没有终端输出流
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()


def stable_id(uid: str, purpose: str) -> str:
    """按 uid 稳定派生带连字符的 UUID 形状设备/会话标识。

    技术路线对齐官方客户端抓包（参见同类项目 wb_fingerprint.derive_id 的做法）：
    真实客户端的 X-Machine-ID / X-Session-ID 都是**带连字符的 UUID**，不是 36 位
    无连字符的裸 hex。裸 hex 这种形态上游见不到，本身就是一处"网关特征"。

    取值仍然按账号派生，这是与官方客户端的关键差异：官方"一个进程=一台设备"，
    而本程序是一个长驻进程同时代表多个账号。若按进程维度取一次，所有账号会共用
    同一个 machineId，反而把它们关联到同一台机器上，与"每账号一台设备"相悖。

    幂等：同一账号跨重启恒定、不同账号互异。
    盐值故意保留改名前的字面量，改名不应让已保存账号的设备指纹发生变化。
    """
    seed = "wb2oai:%s:%s" % (purpose, uid)
    h = hashlib.sha256(seed.encode()).hexdigest()
    # 与 hub 的实现保持一致：把版本位固定成 4、变体位固定成 a，凑成 UUIDv4 形状
    h = h[:12] + "4" + h[13:16] + "a" + h[17:]
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ===========================================================================
# 三、凭证：加载 / 保存 / 刷新
# ===========================================================================
def load_auth(path: str) -> dict:
    """兼容官方客户端导出的嵌套形与手写的扁平形两种凭证文件。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _normalize_auth(raw)


def _normalize_auth(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("账号凭证必须是 JSON 对象")
    nested = raw.get("auth") if isinstance(raw.get("auth"), dict) else None
    acc = raw.get("account") if isinstance(raw.get("account"), dict) else {}
    src = nested or raw
    a = {
        "accessToken": src.get("accessToken") or "",
        "refreshToken": src.get("refreshToken") or "",
        "expiresAt": to_int(src.get("expiresAt")),
        "domain": src.get("domain") or "",
        "realm": raw.get("realm") if raw.get("realm") in REALMS else "",
        "uid": acc.get("uid") or raw.get("uid") or "",
        "enterpriseId": acc.get("enterpriseId") or raw.get("enterpriseId") or "",
        "deviceToken": raw.get("device_token") or raw.get("deviceToken") or "",
        "nickname": account_name(acc) or account_name(raw),
        "accountId": raw.get("accountId") or "",
    }
    if not a["accessToken"]:
        raise ValueError("账号凭证缺少 accessToken")
    a["accountId"] = account_id(a)
    return a


def account_name(a: dict) -> str:
    for key in ("nickname", "nickName", "username", "userName", "displayName", "name"):
        value = a.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def account_id(a: dict) -> str:
    """用户、区域及企业共同标识账号；没有用户 ID 时保留首次分配的标识。"""
    uid = a.get("uid")
    if not uid and a.get("accountId"):
        return a["accountId"]
    identity = [realm_of(a), str(uid or a.get("accessToken") or ""), str(a.get("enterpriseId") or "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def accounts_file() -> str:
    return os.environ.get("WB_ACCOUNTS_FILE") or os.path.join(os.path.dirname(os.path.abspath(AUTH_FILE)), "accounts.json")


def _auth_payload(a: dict) -> dict:
    payload = {
        "accessToken": a["accessToken"],
        "refreshToken": a["refreshToken"],
        "expiresAt": a["expiresAt"],
        "domain": a["domain"],
        "realm": a.get("realm") or "",
        "uid": a["uid"],
        "enterpriseId": a["enterpriseId"],
        "nickname": account_name(a),
        "accountId": account_id(a),
    }
    if a.get("deviceToken"):
        payload["device_token"] = a["deviceToken"]
    return payload


def _save_accounts(accounts: dict, active: dict | None) -> None:
    """账号库为权威记录，先原子提交，再更新兼容旧版的当前账号文件。"""
    payload = {"version": 1, "activeAccountId": account_id(active) if active else "",
               "accounts": [_auth_payload(a) for a in accounts.values()]}
    _write_private(accounts_file(), json.dumps(payload, ensure_ascii=False, indent=2))
    if active:
        try:
            _write_private(AUTH_FILE, json.dumps(_auth_payload(active), ensure_ascii=False, indent=2))
        except OSError as e:
            log("账号库已保存，当前账号兼容文件更新失败：%s" % e)


def load_accounts() -> dict | None:
    """启动时读取账号库；旧 auth.json 自动迁移，空目录不创建账号文件。"""
    global _auth, _accounts
    with _lock:
        try:
            with open(accounts_file(), encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            if not os.path.exists(AUTH_FILE):
                _accounts, _auth = {}, None
                return None
            active = load_auth(AUTH_FILE)
            accounts = {account_id(active): active}
            _save_accounts(accounts, active)
        else:
            if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
                raise ValueError("账号库格式不正确，请检查 accounts.json")
            accounts = {}
            for item in raw["accounts"]:
                a = _normalize_auth(item)
                accounts[account_id(a)] = a
            active = accounts.get(raw.get("activeAccountId"))
            if accounts and active is None:
                raise ValueError("账号库中的当前账号不存在，请检查 accounts.json")
        _accounts, _auth = accounts, active
        return active


def save_auth(a: dict) -> None:
    """刷新任意账号时保存该账号，但不会切换当前账号。调用可重入。"""
    global _accounts
    with _lock:
        accounts = dict(_accounts)
        if _auth:
            accounts[account_id(_auth)] = _auth
        accounts[account_id(a)] = a
        _save_accounts(accounts, _auth or a)
        _accounts = accounts


def activate_account(a: dict) -> dict:
    """持久化成功后原子切换内存指针；在途请求保留其原有账号引用。"""
    global _accounts, _auth
    global _selection, _selection_owner, _account_selections, _selection_scoped, _selection_mtime
    with _lock:
        reload_selection_if_changed()
        accounts = dict(_accounts)
        if _auth:
            accounts[account_id(_auth)] = _auth
        a["accountId"] = account_id(a)
        accounts[a["accountId"]] = a
        with _selection_lock:
            profiles = {key: list(value) for key, value in _account_selections.items()}
            owner = _selection_owner or account_id(_auth or a)
            profiles[owner] = list(_selection)
            # 先将旧全局勾选归属原账号，避免切换后重启把旧勾选迁移给新账号。
            if not _selection_scoped and os.path.exists(SELECTION_FILE):
                payload = {"models": list(_selection), "accounts": profiles, "activeAccountId": owner,
                           "aliases": dict(_aliases), "default_model": _default_model, "enforce": _enforce}
                _write_private(SELECTION_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                _selection_mtime = os.path.getmtime(SELECTION_FILE)
                _selection_scoped = True
            _save_accounts(accounts, a)
            _accounts, _auth = accounts, a
            _account_selections, _selection_owner = profiles, a["accountId"]
            _selection = list(profiles.get(_selection_owner, []))
        _select_models_cache(a)
        return a


def switch_account(aid: str) -> dict:
    if not isinstance(aid, str) or not aid:
        raise ValueError("请选择需要切换的账号")
    with _lock:
        if aid not in _accounts:
            raise ValueError("该账号不存在，请先登录添加")
        return activate_account(_accounts[aid])


def account_summary(a: dict) -> dict:
    """仅返回展示字段，不把任何已保存的上游令牌交给网页。"""
    return {"id": account_id(a), "nickname": account_name(a),
            "displayName": account_name(a) or "未命名账号", "uid": a.get("uid") or "",
            "realm": realm_of(a), "enterpriseId": a.get("enterpriseId") or "",
            "domain": a.get("domain") or "", "expiresAt": a.get("expiresAt") or 0}


def _write_private(path: str, text: str) -> None:
    """原子写 + 仅本人可读（0600）。凭证与密钥都走这里。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    target = os.path.abspath(path)
    lock = _private_write_locks[hash(target) % len(_private_write_locks)]
    with lock:
        tmp = "%s.%d.%d.tmp" % (target, os.getpid(), threading.get_ident())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _refresh_unlocked(a: dict) -> bool:
    """刷新 access token。refresh 响应里 accessToken/refreshToken 总是一起轮换。
    调用方必须持有该账号的刷新锁；网络等待不占用账号切换锁。"""
    if not a.get("refreshToken"):
        return False
    aid = account_id(a)
    if time.time() - _refresh_failures.get(aid, 0.0) < REFRESH_BACKOFF:
        return False
    h = common_headers(a)
    h["X-Refresh-Token"] = a["refreshToken"]
    h["X-Auth-Refresh-Source"] = "plugin"
    try:
        tok = http_json("POST", upstream_base(a) + REFRESH_PATH, h, b"", timeout=30)
    except Exception as e:  # noqa: BLE001 - 刷新失败不应打断请求，交由调用方降级
        log("登录令牌刷新失败：%s" % e)
        _refresh_failures[aid] = time.time()
        a["_lastRefreshOk"] = False
        return False
    # 兼容裸对象与 {code,msg,data} 信封两种形态
    if isinstance(tok, dict) and isinstance(tok.get("data"), dict) and not tok.get("accessToken"):
        tok = tok["data"]
    if not isinstance(tok, dict) or not tok.get("accessToken"):
        log("登录令牌刷新响应缺少 accessToken —— 需要重新登录")
        _refresh_failures[aid] = time.time()
        a["_lastRefreshOk"] = False
        return False
    _refresh_failures.pop(aid, None)
    with _lock:
        current = _accounts.get(aid)
        if current is not None and current is not a:
            # 此账号在网络等待期间重新登录了，旧刷新结果不能覆盖新授权。
            a.update(current)
            return True
        a["accessToken"] = tok["accessToken"]
        a["_lastRefreshOk"] = True
        if tok.get("refreshToken"):
            a["refreshToken"] = tok["refreshToken"]
        if to_int(tok.get("expiresIn")) > 0:
            a["expiresAt"] = int(time.time()) + to_int(tok["expiresIn"])
        save_auth(a)
    log("登录令牌已刷新，新到期时间 %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(a["expiresAt"])))
    return True


def refresh_auth(a: dict, stale_token: str | None = None) -> bool:
    """加锁刷新。传入 stale_token 时，若别的线程已经换过 token 就直接复用。"""
    with _lock:
        mutex = _refresh_locks.setdefault(account_id(a), threading.Lock())
    with mutex:
        with _lock:
            current = _accounts.get(account_id(a)) or a
        if stale_token is not None and current.get("accessToken") != stale_token:
            if current is not a:
                a.update(current)
            return True
        result = _refresh_unlocked(current)
        if current is not a:
            a.update(current)
        return result


def ensure_auth() -> dict:
    """取当前凭证。正常情况下后台线程会提前刷新；只有真的快过期了才在请求路径上同步刷新。"""
    a = _auth
    if a is None:
        raise RuntimeError("尚未加载登录凭证")
    if not (a["expiresAt"] and a["expiresAt"] - REFRESH_SKEW <= time.time()):
        return a           # 快路径：不碰锁
    refresh_auth(a, stale_token=a.get("accessToken"))
    return a


# ===========================================================================
# 三之二、账号池：国内版 / 国际版双池，池内自动换号 + 会话粘性
# ===========================================================================
# 设计约束（改这里之前请先读）：
#   1. 双池物理隔离——不同 realm 的 token、上游域、出站身份都不同，绝不跨池。
#   2. 只在"还没给客户端写出任何字节"时换号；流式请求开始收尾后不能再换。
#   3. 换号只改变"这次请求用哪个账号"，**不改** _auth（控制台上的当前账号）。
#      当前账号是用户的选择，账号池是负载与容错策略，两者不能互相覆盖。
#   4. 同一会话尽量粘在同一账号；粘性账号不可用时会解绑并改投他人。

def _pool_realm(a: dict | None) -> str:
    return realm_of(a)


def _pool_members(realm: str) -> list[dict]:
    """某个池的成员：该 realm 下所有可用账号（按 id 排序保证选择稳定）。"""
    with _lock:
        accounts = dict(_accounts)
        if _auth:
            accounts[account_id(_auth)] = _auth
    members = [a for a in accounts.values() if realm_of(a) == realm and a.get("accessToken")]
    members.sort(key=account_id)
    return members


def _health(aid: str) -> dict:
    with _pool_lock:
        h = _pool_health.get(aid)
        if h is None:
            h = {"cooldownUntil": 0.0, "fails": 0, "breakerUntil": 0.0, "lastError": "", "softCount": 0}
            _pool_health[aid] = h
        return h


def _pool_available(aid: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    h = _health(aid)
    with _pool_lock:
        return h["cooldownUntil"] <= now and h["breakerUntil"] <= now


def _pool_pick(realm: str, session_key: str = "", exclude: set | None = None) -> dict | None:
    """在指定池内挑一个可用账号。

    优先级：会话粘性命中 > 当前账号（让单账号用户行为完全不变）> 池内其余可用账号。
    """
    exclude = exclude or set()
    members = _pool_members(realm)
    if not members:
        return None
    by_id = {account_id(m): m for m in members}
    now = time.time()
    # 1) 会话粘性
    if session_key:
        with _pool_lock:
            hit = _pool_sticky.get(session_key)
            if hit and hit[1] <= now:
                _pool_sticky.pop(session_key, None)   # 过期即清
                hit = None
        # 注意：_pool_available 自己也会取 _pool_lock，不要在上面的 with 里调用它
        if hit and hit[1] > now:
            aid = hit[0]
            if aid in by_id and aid not in exclude and _pool_available(aid, now):
                return by_id[aid]
    # 2) 当前账号优先：只有一个账号的人不会感到任何变化
    cur = _auth
    if cur is not None and realm_of(cur) == realm:
        cid = account_id(cur)
        if cid in by_id and cid not in exclude and _pool_available(cid, now):
            return by_id[cid]
    # 3) 池内轮转：跳过冷却/熔断的账号
    for aid in by_id:
        if aid in exclude:
            continue
        if _pool_available(aid, now):
            return by_id[aid]
    return None


def _pool_bind(session_key: str, aid: str) -> None:
    if not session_key:
        return
    now = time.time()
    with _pool_lock:
        if len(_pool_sticky) >= POOL_STICKY_MAX:
            # 简单淘汰：先清过期，仍然超限就丢掉最早到期的那批
            dead = [k for k, v in _pool_sticky.items() if v[1] <= now]
            for k in dead:
                _pool_sticky.pop(k, None)
            if len(_pool_sticky) >= POOL_STICKY_MAX:
                for k, _ in sorted(_pool_sticky.items(), key=lambda kv: kv[1][1])[:POOL_STICKY_MAX // 4]:
                    _pool_sticky.pop(k, None)
        _pool_sticky[session_key] = (aid, now + POOL_STICKY_TTL)


def _pool_unbind(session_key: str, aid: str) -> None:
    """粘性账号失效时解绑，下次请求才能改投他人。"""
    if not session_key:
        return
    with _pool_lock:
        hit = _pool_sticky.get(session_key)
        if hit and hit[0] == aid:
            _pool_sticky.pop(session_key, None)


def _pool_mark_ok(a: dict) -> None:
    h = _health(account_id(a))
    with _pool_lock:
        h["fails"] = 0
        h["softCount"] = 0
        h["lastError"] = ""


def _pool_penalize(a: dict, status: int, reason: str) -> bool:
    """记录一次失败并决定该账号是否应被移出轮转。返回是否真的惩罚了。"""
    aid = account_id(a)
    h = _health(aid)
    now = time.time()
    with _pool_lock:
        h["lastError"] = (reason or "")[:200]
        if status == 429:
            # 软冷却：指数退避，封顶 POOL_SOFT_MAX。
            # softCount 必须夹紧：2**n 无上限时，反复命中 429 会把指数顶到
            # float 溢出，直接把请求线程打挂（OverflowError）。
            h["softCount"] = min(h["softCount"] + 1, 30)
            delay = min(POOL_SOFT_COOLDOWN * (2 ** (h["softCount"] - 1)), POOL_SOFT_MAX)
            h["cooldownUntil"] = max(h["cooldownUntil"], now + delay)
            return True
        if status in (401, 403):
            # 授权类：立刻冷却，等令牌刷新或用户重新登录；不计入熔断
            h["cooldownUntil"] = max(h["cooldownUntil"], now + POOL_SOFT_COOLDOWN)
            return True
        if status == 402:
            # 余额/积分不足：硬冷却较久（默认 1 小时），别在它身上反复烧请求
            h["cooldownUntil"] = max(h["cooldownUntil"],
                                     now + (POOL_HARD_COOLDOWN or 3600))
            return True
        if status >= 500:
            h["fails"] += 1
            if h["fails"] >= POOL_FAIL_THRESHOLD:
                h["breakerUntil"] = now + POOL_BREAKER
                h["fails"] = 0
                log("账号池：%s 连续失败，熔断 %.0f 分钟" % (account_name(a) or aid[:8], POOL_BREAKER / 60))
            return True
        return False


def pool_status() -> dict:
    """控制台展示用：按两池汇总成员与健康状态。"""
    now = time.time()
    out = {}
    for realm in REALMS:
        rows = []
        for m in _pool_members(realm):
            aid = account_id(m)
            h = _health(aid)
            with _pool_lock:
                rows.append({
                    "accountId": aid,
                    "name": account_name(m) or aid[:8],
                    "available": h["cooldownUntil"] <= now and h["breakerUntil"] <= now,
                    "cooldownIn": max(0, int(h["cooldownUntil"] - now)),
                    "breakerIn": max(0, int(h["breakerUntil"] - now)),
                    "fails": h["fails"],
                    "lastError": h["lastError"],
                    "active": bool(_auth and account_id(_auth) == aid),
                })
        out[realm] = {"count": len(rows), "available": sum(1 for r in rows if r["available"]),
                      "members": rows}
    out["enabled"] = POOL_ENABLED
    with _pool_lock:
        out["stickyCount"] = len(_pool_sticky)
        out["stats"] = dict(_pool_stats)
    return out


def _pool_session_key(handler) -> str:
    """从请求里提取稳定的会话标识，用于会话粘性。

    优先显式头（客户端自己带）；其次 OpenAI 语义上的 user 字段是每个请求都变的，
    不能用；最后退回连接级 cookie，保证同一个客户端连接组落在同一账号。
    """
    for name in ("X-Session-Id", "X-Conversation-Id", "Session-Id", "X-Claude-Code-Session"):
        v = handler.headers.get(name)
        if v and v.strip():
            return "h:" + v.strip()[:128]
    return ""


#: 这些状态码意味着"换一个账号可能就好了"；其余 4xx 是请求本身的问题，
#: 换号只会把同一个错误在池里跑一遍，徒增上游请求与积分消耗。
POOL_SWITCH_STATUS = frozenset((401, 402, 403, 429, 500, 502, 503, 504))


def _pool_can_switch(a: dict, status: int, tried: set) -> bool:
    """判断这次的失败是否值得换号。"""
    if not POOL_ENABLED:
        return False
    if status not in POOL_SWITCH_STATUS:
        return False
    if len(tried) >= POOL_MAX_SWITCH:
        return False
    return bool(_pool_members(realm_of(a)))


def _pool_switch(a: dict, status: int, reason: str, session_key: str,
                 tried: set) -> dict | None:
    """惩罚当前账号并在同池内改投一个可用账号；没有可用账号时返回 None。"""
    aid = account_id(a)
    tried.add(aid)
    _pool_penalize(a, status, reason)
    # 粘性账号刚失败：解绑，否则下一次请求还会被粘回这个坏账号
    _pool_unbind(session_key, aid)
    nxt = _pool_pick(realm_of(a), session_key, exclude=tried)
    if nxt is None:
        with _pool_lock:
            _pool_stats["exhausted"] += 1
        log("账号池：%s 池内已无可用账号（本次请求失败于 HTTP %s）"
            % (REALMS[realm_of(a)]["label"], status))
        return None
    with _pool_lock:
        _pool_stats["switch"] += 1
    log("账号池：HTTP %s，%s → %s 换号重试"
        % (status, account_name(a) or aid[:8], account_name(nxt) or account_id(nxt)[:8]))
    return nxt


def _refresher_loop() -> None:
    """后台续期：到期前 REFRESH_AHEAD 秒刷新，请求永远不用等刷新。"""
    while True:
        time.sleep(30)
        try:
            RESP_STORE.prune()
            a = _auth
            if a and a.get("expiresAt") and a["expiresAt"] - REFRESH_AHEAD <= time.time():
                refresh_auth(a, stale_token=a.get("accessToken"))
        except Exception as e:  # noqa: BLE001
            log("后台续期异常：%s" % e)


# ---------------------------------------------------------------------------
# 设备授权登录（服务端签发 state，无 PKCE，三步）
#   POST /v2/plugin/auth/state?platform=CLI   → state + authUrl
#   浏览器完成授权
#   GET  /v2/plugin/auth/token?state=...      → accessToken / refreshToken / expiresIn
#   GET  /v2/plugin/login/account?state=...   → uid / enterpriseId / nickname
# ---------------------------------------------------------------------------
_LOGIN_OPENER = None


def _login_opener():
    """登录才需要 cookie；延迟导入 http.cookiejar，服务启动少加载一批模块。"""
    global _LOGIN_OPENER
    if _LOGIN_OPENER is None:
        import http.cookiejar
        _LOGIN_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return _LOGIN_OPENER


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__("上游错误码=%s，说明=%s" % (code, msg))
        self.code = code


def login_headers(origin: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": CLI_UA,
    }


def api_call(url: str, headers: dict, method: str = "GET", body=None):
    """上游统一 {code,msg,data} 信封；code != 0 抛 ApiError。"""
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _login_opener().open(req, timeout=30) as r:
        env = json.loads(r.read().decode("utf-8"))
    if env.get("code") != 0:
        raise ApiError(env.get("code"), env.get("msg"))
    return env.get("data")


def login_endpoints(realm: str):
    """返回 (base, origin, 显示名)。国内版 base 与 Origin 分属两个域，国际版同域。"""
    r = REALMS.get(realm)
    if r is None:
        raise ValueError("未知 realm：%r（可选 cn / global）" % realm)
    return r["base"], r["origin"], r["label"]


def login_start(realm: str) -> dict:
    """申请设备授权，返回上下文（含 state 与 authUrl）。"""
    base, origin, label = login_endpoints(realm)
    data = api_call(base + "/v2/plugin/auth/state?platform=CLI",
                    login_headers(origin), "POST")
    state = (data or {}).get("state")
    auth_url = (data or {}).get("authUrl")
    if not state or not auth_url:
        raise RuntimeError("上游响应缺少 state / authUrl")
    return {"realm": realm, "base": base, "origin": origin, "label": label,
            "state": state, "authUrl": auth_url}


def login_poll(ctx: dict):
    """轮询一次。返回 (是否完成, token 或 None)。未完成时上游给业务 code != 0。"""
    try:
        got = api_call(ctx["base"] + "/v2/plugin/auth/token?state="
                       + urllib.parse.quote(ctx["state"], safe=""),
                       login_headers(ctx["origin"]))
    except ApiError:
        return False, None
    except Exception as e:  # noqa: BLE001
        log("轮询异常（继续）：%s" % e)
        return False, None
    if isinstance(got, dict) and got.get("accessToken"):
        return True, got
    return False, None


def login_finish(ctx: dict, tok: dict, out_path: str):
    """拉账号信息、写凭证、热更新内存态。返回 (auth, 账号信息)。"""
    acct: dict = {}
    try:
        ah = dict(login_headers(ctx["origin"]))
        ah["Authorization"] = "Bearer " + tok["accessToken"]
        acct = api_call(ctx["base"] + "/v2/plugin/login/account?state="
                        + urllib.parse.quote(ctx["state"], safe=""), ah) or {}
    except Exception as e:  # noqa: BLE001
        log("拉取账号信息失败（不影响使用）：%s" % e)

    global AUTH_FILE
    AUTH_FILE = out_path
    expires_in = to_int(tok.get("expiresIn"))
    a = {
        "accessToken": tok.get("accessToken") or "",
        "refreshToken": tok.get("refreshToken") or "",
        # 缺 expiresIn 时记 0（未知），否则会被当成“已过期”导致每个请求都去刷新
        "expiresAt": int(time.time()) + expires_in if expires_in > 0 else 0,
        "domain": tok.get("domain") or "",
        "realm": ctx.get("realm") or "",
        "uid": acct.get("uid") or "",
        "enterpriseId": acct.get("enterpriseId") or "",
        "deviceToken": "",
        "nickname": account_name(acct),
    }
    activate_account(a)
    clear_models_cache(a)   # 仅使重新授权的账号目录失效
    return a, acct


def do_login(realm: str, out_path: str) -> None:
    log("正在申请设备授权…")
    try:
        ctx = login_start(realm)
    except Exception as e:  # noqa: BLE001
        raise SystemExit("申请授权失败：%s" % e)

    print()
    print("  请在浏览器打开下面的链接完成登录（%s）：" % ctx["label"])
    print()
    print("    " + ctx["authUrl"])
    print()
    log("等待授权完成…（最长 5 分钟，Ctrl+C 取消）")

    deadline = time.time() + 300
    tok = None
    while time.time() < deadline:
        ok, got = login_poll(ctx)
        if ok:
            tok = got
            break
        time.sleep(3)
    if not tok:
        raise SystemExit("登录超时或未完成，请重新执行 --login")

    a, acct = login_finish(ctx, tok, out_path)
    log("登录成功 → %s" % out_path)
    log("  用户=%s  昵称=%s  企业=%s"
        % (a["uid"] or "-", acct.get("nickname") or "-", a["enterpriseId"] or "-"))
    if a["expiresAt"]:
        log("  到期时间 %s（到期前会自动刷新）"
            % time.strftime("%Y-%m-%d %H:%M", time.localtime(a["expiresAt"])))


# ===========================================================================
# 四、请求头：三组（common / chat / refresh）
# ===========================================================================
def realm_of(a: dict | None) -> str:
    """账号 realm：优先落盘字段，其次 domain，最后看上游地址。"""
    a = a or {}
    if a.get("realm") in REALMS:
        return a["realm"]
    d = a.get("domain") or ""
    if d:
        return "global" if "workbuddy.ai" in d else "cn"
    return "global" if "workbuddy.ai" in UPSTREAM_BASE else "cn"


def upstream_base(a: dict | None) -> str:
    return UPSTREAM_OVERRIDE or REALMS[realm_of(a)]["base"]


def common_headers(a: dict) -> dict:
    r = REALMS[realm_of(a)]
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": r["origin"],
        "Referer": r["origin"] + "/",
        "User-Agent": desktop_ua(a),
        "X-CodeBuddy-Request": "1",   # 官方风控闸门头，所有 API 必带
        "Accept-Language": r["lang"],  # 国际版账号发 en-US，与官方客户端一致
    }
    if a.get("accessToken"):
        h["Authorization"] = "Bearer " + a["accessToken"]
    uid = a.get("uid") or ""
    if uid:
        h["X-User-Id"] = uid
        h["X-Machine-ID"] = stable_id(uid, "machine")
        h["X-Session-ID"] = stable_id(uid, "session")
    if a.get("enterpriseId"):
        h["X-Enterprise-Id"] = a["enterpriseId"]
    else:
        # 个人账号：显式声明无企业 + 声明账号所属域
        h["X-No-Enterprise-Id"] = "1"
        h["X-Domain"] = a.get("domain") or r["domain"]
    if a.get("deviceToken"):
        h["X-Device-Token"] = a["deviceToken"]
    return h


_chat_static: dict = {"key": None, "h": None}
_chat_static_lock = threading.Lock()


def _uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7（时间有序）。

    5.8.4 客户端的会话 id 是带连字符的 UUID、请求/消息 id 是 32 位 hex，
    都取这个形态。上游并不校验版本位（抓包里 v4 与 v7 都被接受），
    这里只求形态贴近。
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (rand_a << 64)
                     | (0b10 << 62) | rand_b)


def chat_headers(a: dict) -> dict:
    """common 之上叠加 chat 专属头，并按官方客户端的 wire 形状补齐会话/链路头族。

    上游的渠道校验（400 / code 11128）看的就是这些：官方对话请求固定携带
      * X-Conversation-ID              带连字符的 UUID，每次请求现生成
      * X-Conversation-Request-ID == X-Root-Request-ID   32 位 hex
      * X-Conversation-Message-ID == X-Request-ID         32 位 hex
      * X-Agent-Intent / X-Agent-Type / X-Private-Data
      * trace 家族（traceparent / b3 / X-B3-* / X-Trace-ID）
    另外官方对话请求**不**带 X-Machine-ID / X-Session-ID（那两个只用于反馈上报），
    恒定的派生值反而是一处"网关特征"，这里发 chat 前剔除。

    静态部分按 (token, uid, 企业, 域, realm) 缓存，每个请求只补现生成的 ID。
    """
    key = (a.get("accessToken"), a.get("uid"), a.get("enterpriseId"), a.get("domain"),
           a.get("realm"), a.get("deviceToken"), UPSTREAM_OVERRIDE)
    intl = is_global(a)
    with _chat_static_lock:
        if _chat_static["key"] != key:
            h = common_headers(a)
            h["Accept"] = "application/json, text/event-stream"
            h["X-Agent-Purpose"] = "conversation"
            h["X-IDE-Name"] = "WorkBuddy"
            h["X-IDE-Type"] = "WorkBuddy"
            h.pop("X-Machine-ID", None)
            h.pop("X-Session-ID", None)
            if intl:
                h["X-IDE-Version"] = INTL_CLIENT_VERSION
                h["X-Product"] = "SaaS"
                h["X-Agent-Intent"] = "craft"
                h["X-Agent-Type"] = "main"
                h["X-Private-Data"] = "true"
                h["User-Agent"] = desktop_ua(a)
            else:
                h["X-IDE-Version"] = CLIENT_VERSION
                h["X-Product"] = "WorkBuddy"
            _chat_static.update(key=key, h=h)
        h = dict(_chat_static["h"])
    # 会话头族每请求现生成：上游不拿这个 id 存历史，补的是"有"而不是"稳定"
    msg_id, conv_req_id = _uuid7().hex, _uuid7().hex
    trace_id, span_id = _uuid7().hex, secrets.token_hex(8)
    h["X-Conversation-ID"] = str(_uuid7())
    h["X-Conversation-Request-ID"] = conv_req_id
    h["X-Root-Request-ID"] = conv_req_id
    h["X-Conversation-Message-ID"] = msg_id
    h["X-Request-ID"] = msg_id
    h["X-Trace-ID"] = trace_id
    h["traceparent"] = "00-%s-%s-01" % (trace_id, span_id)
    h["b3"] = "%s-%s-1" % (trace_id, span_id)
    h["X-B3-TraceId"] = trace_id
    h["X-B3-SpanId"] = span_id
    h["X-B3-Sampled"] = "1"
    return h


# ===========================================================================
# 五、请求体归一：让"标准 OpenAI 请求"能通过上游的严格校验
# ===========================================================================
def validate_request(path: str, body: dict) -> None:
    """在调用上游之前拒绝会导致类型异常或明显无效的输入。"""
    if body.get("model") is not None and not isinstance(body["model"], str):
        raise ValueError("model 必须是字符串")
    if "stream" in body:
        stream = body["stream"]
        if stream is True or (type(stream) is int and stream == 1) or stream in ("1", "true"):
            body["stream"] = True
        elif stream is False or (type(stream) is int and stream == 0) or stream in ("0", "false"):
            body["stream"] = False
        else:
            raise ValueError("stream 必须是布尔值")
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key in body and (type(body[key]) is not int or body[key] < 0):
            raise ValueError("%s 必须是非负整数" % key)
    if "n" in body and (type(body["n"]) is not int or body["n"] <= 0):
        raise ValueError("n 必须是正整数")
    if "tools" in body and (not isinstance(body["tools"], list) or
                           any(not isinstance(t, dict) for t in body["tools"])):
        raise ValueError("tools 必须是工具对象组成的数组")
    if path == "/v1/messages":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
                for m in messages):
            raise ValueError("messages 必须是非空数组，role 只能是 user 或 assistant")
        if type(body.get("max_tokens")) is not int or body["max_tokens"] < 0:
            raise ValueError("Anthropic 请求必须提供非负整数 max_tokens")
        if "system" in body and not isinstance(body["system"], (str, list)):
            raise ValueError("system 必须是字符串或内容数组")
        _validate_anthropic_content(body.get("system", ""), "system")
        for message in messages:
            _validate_anthropic_content(message.get("content"), message["role"])
        for tool in body.get("tools", []):
            if tool.get("type", "custom") != "custom":
                raise ValueError("当前代理不支持 Anthropic 服务端工具")
            if (not isinstance(tool.get("name"), str) or not tool["name"].strip()
                    or not isinstance(tool.get("input_schema"), dict)):
                raise ValueError("tools 中每个工具都需要 name 和 input_schema 对象")
        choice = body.get("tool_choice")
        if choice is not None:
            if not isinstance(choice, dict) or choice.get("type") not in ("auto", "any", "tool", "none"):
                raise ValueError("tool_choice.type 必须是 auto、any、tool 或 none")
            if choice["type"] == "tool" and not isinstance(choice.get("name"), str):
                raise ValueError("tool_choice 指定工具时必须提供 name")
            if "disable_parallel_tool_use" in choice and type(choice["disable_parallel_tool_use"]) is not bool:
                raise ValueError("disable_parallel_tool_use 必须是布尔值")
        if "stop_sequences" in body and (not isinstance(body["stop_sequences"], list) or
                any(not isinstance(s, str) for s in body["stop_sequences"])):
            raise ValueError("stop_sequences 必须是字符串数组")
    elif path == "/v1/chat/completions":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in messages):
            raise ValueError("messages 必须是非空消息数组，每条消息都需要字符串类型的 role")
    else:
        prev = body.get("previous_response_id")
        if prev is not None and not isinstance(prev, str):
            raise ValueError("previous_response_id 必须是字符串")
        inp = body.get("input")
        if not isinstance(inp, (str, list)) and not (inp is None and (prev or body.get("instructions"))):
            raise ValueError("input 必须是字符串或消息数组")
        if isinstance(inp, list) and any(not isinstance(i, (str, dict)) for i in inp):
            raise ValueError("input 数组只能包含消息对象或字符串")
        if body.get("instructions") is not None and not isinstance(body["instructions"], str):
            raise ValueError("instructions 必须是字符串")


def normalize_chat_body(obj: dict, account: dict | None = None) -> dict:
    # 1) 上游只吃流式，非流式由本代理在本地聚合
    obj["stream"] = True
    # 国际版不认 stream_options（会 400 code=11128）；国内版依赖它拿末帧 usage。
    # account 为空视为未知（沿用国内版口径），仅在明确是国际版账号时剔除。
    if isinstance(account, dict) and is_global(account):
        obj.pop("stream_options", None)
    else:
        so = obj.get("stream_options") if isinstance(obj.get("stream_options"), dict) else {}
        obj["stream_options"] = dict(so, include_usage=True)  # 末帧才有 usage；用量统计也依赖它

    # 2) max_completion_tokens 是 OpenAI 新别名，上游只认 max_tokens
    alias = obj.pop("max_completion_tokens", None)
    if alias is not None and "max_tokens" not in obj:
        if isinstance(alias, int) and alias > 0:
            obj["max_tokens"] = alias
    if obj.get("max_tokens") == 0:
        obj.pop("max_tokens", None)

    # 3) tool_choice 上游是 string 类型，对象形式会 400
    tc = obj.get("tool_choice")
    if isinstance(tc, dict):
        t = str(tc.get("type") or "").lower()
        if t == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
            obj.pop("functions", None)
        elif t in ("auto", "required"):
            obj["tool_choice"] = t
        elif t == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "").strip()
            obj["tool_choice"] = name or "auto"
        else:
            obj.pop("tool_choice", None)
    elif isinstance(tc, str) and tc.lower() == "none":
        obj.pop("tool_choice", None)
        obj.pop("tools", None)
        obj.pop("functions", None)

    # 4) 消息级归一：developer → system；image_url 字符串 → 对象
    msgs = obj.get("messages")
    # 上游要求首条必须是 system（否则 400 code=11128）；客户端没给就补一条最小的
    if isinstance(msgs, list):
        first = msgs[0] if msgs and isinstance(msgs[0], dict) else {}
        first_role = str(first.get("role") or "").lower()
        if first_role not in ("system", "developer"):
            msgs.insert(0, {"role": "system", "content": DEFAULT_SYSTEM})
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if isinstance(role, str) and role.lower() == "developer":
                m["role"] = "system"
            parts = m.get("content")
            if isinstance(parts, list):
                for p in parts:
                    if (
                        isinstance(p, dict)
                        and p.get("type") == "image_url"
                        and isinstance(p.get("image_url"), str)
                    ):
                        p["image_url"] = {"url": p["image_url"]}
    return obj


# ===========================================================================
# Anthropic Messages 适配
# ===========================================================================
def _validate_anthropic_content(content, role: str) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise ValueError("content 必须是字符串或内容块数组")
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("内容块必须是对象")
        typ = block.get("type")
        if typ == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("text 内容块必须包含字符串 text")
        elif typ == "image" and role in ("user", "tool"):
            src = block.get("source")
            if not isinstance(src, dict):
                raise ValueError("image 内容块必须包含 source 对象")
            if src.get("type") == "url" and isinstance(src.get("url"), str) and src["url"]:
                continue
            if (src.get("type") == "base64" and isinstance(src.get("media_type"), str)
                    and src["media_type"] and isinstance(src.get("data"), str) and src["data"]):
                continue
            raise ValueError("image.source 必须提供 url 或 base64 数据及 media_type")
        elif typ == "tool_use" and role == "assistant":
            if (not isinstance(block.get("id"), str) or not block["id"]
                    or not isinstance(block.get("name"), str) or not block["name"]
                    or not isinstance(block.get("input"), dict)):
                raise ValueError("tool_use 必须包含 id、name 和 input 对象")
        elif typ == "tool_result" and role == "user":
            if not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]:
                raise ValueError("tool_result 必须包含 tool_use_id")
            _validate_anthropic_content(block.get("content", ""), "tool")
        elif typ in ("thinking", "redacted_thinking") and role == "assistant":
            continue  # 历史签名只对 Anthropic 原生推理接口有意义
        else:
            raise ValueError("不支持 %s 消息中的内容块类型：%s" % (role, typ))


def _anthropic_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            p.get("text") or "" for p in value
            if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _anthropic_content(content):
    """Anthropic 内容块转成 OpenAI 消息内容；工具结果单独由调用方处理。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for p in content:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        typ = p.get("type")
        if typ == "text":
            out.append({"type": "text", "text": p.get("text") or ""})
        elif typ == "image":
            src = p.get("source") if isinstance(p.get("source"), dict) else {}
            if src.get("type") == "base64" and src.get("media_type") and src.get("data"):
                out.append({"type": "image_url", "image_url": {
                    "url": "data:%s;base64,%s" % (src["media_type"], src["data"])}})
            elif src.get("type") == "url" and src.get("url"):
                out.append({"type": "image_url", "image_url": {"url": src["url"]}})
    if out and all(p.get("type") == "text" for p in out):
        return "".join(p["text"] for p in out)
    return out or ""


def anthropic_to_chat(body: dict) -> dict:
    """把 Anthropic Messages 请求翻译成内部使用的 Chat Completions 请求。"""
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _anthropic_text(system)})
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        normal, calls, results = [], [], []
        for block in blocks:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    normal.append({"type": "text", "text": block})
                continue
            typ = block.get("type")
            if typ == "tool_use":
                call = {"id": block.get("id") or "call_" + uuid.uuid4().hex[:16],
                        "type": "function",
                        "function": {"name": block.get("name") or "",
                                       "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}}
                calls.append(call)
            elif typ == "tool_result":
                results.append({"role": "tool", "tool_call_id": block.get("tool_use_id") or "",
                                "content": _anthropic_content(block.get("content", ""))})
            else:
                normal.append(block)
        messages.extend(results)
        if normal or calls or not results:
            message = {"role": role, "content": _anthropic_content(normal) or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
    out = {"model": body.get("model"), "messages": messages, "stream": True}
    if isinstance(body.get("max_tokens"), int) and body["max_tokens"] > 0:
        out["max_tokens"] = body["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("top_k", "top_k"), ("stop_sequences", "stop")):
        if src in body:
            out[dst] = body[src]
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str):
        out["user"] = metadata["user_id"]
    if isinstance(body.get("tools"), list):
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name") or "", "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in body["tools"] if isinstance(t, dict)]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        typ = choice.get("type")
        if typ == "any":
            out["tool_choice"] = "required"
        elif typ == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": choice.get("name") or ""}}
        elif typ in ("auto", "none"):
            out["tool_choice"] = typ
        if "disable_parallel_tool_use" in choice:
            out["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    return out


def _anthropic_usage(u) -> dict:
    u = u if isinstance(u, dict) else {}
    out = {"input_tokens": to_int(u.get("prompt_tokens")),
           "output_tokens": to_int(u.get("completion_tokens"))}
    details = u.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = to_int(u.get("prompt_cache_hit_tokens") or u.get("cache_read_input_tokens")
                    or u.get("cached_tokens") or details.get("cached_tokens"))
    if cached:
        out["cache_read_input_tokens"] = cached
        out["input_tokens"] = max(0, out["input_tokens"] - cached)
    return out


def chat_to_anthropic(acc: ChatAccumulator, model: str, msg_id: str) -> dict:
    msg = acc.message(0)
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError) as e:
            raise UpstreamError(502, "上游工具参数不是有效 JSON：%s" % e) from e
        if not isinstance(inp, dict):
            raise UpstreamError(502, "上游工具参数必须是 JSON 对象")
        content.append({"type": "tool_use", "id": tc.get("id") or "call_" + uuid.uuid4().hex[:16],
                        "name": fn.get("name") or "", "input": inp})
    finish = acc.finish(0)
    stop = ("max_tokens" if finish == "length" else
            "tool_use" if msg.get("tool_calls") else
            "refusal" if finish == "content_filter" else "end_turn")
    return {"id": msg_id, "type": "message", "role": "assistant",
            "model": acc.model or model, "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": _anthropic_usage(acc.usage)}


def _anthropic_event(name: str, payload: dict) -> str:
    return "event: %s\ndata: %s\n\n" % (name, _dumps(dict(payload, type=name)))


def anthropic_stream(resp, model: str, acc: ChatAccumulator, ping=None):
    """把统一的 OpenAI chat SSE 转为 Anthropic Messages SSE。"""
    mid = "msg_" + uuid.uuid4().hex[:24]
    yield _anthropic_event("message_start", {"message": {
        "id": mid, "type": "message", "role": "assistant",
        "content": [], "model": model, "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    text_index = None
    next_index = 0
    tools = {}

    def open_tool(state):
        nonlocal next_index
        slot = state["slot"]
        if not slot["id"]:
            slot["id"] = "call_" + uuid.uuid4().hex[:24]
        state["index"] = next_index
        next_index += 1
        yield _anthropic_event("content_block_start", {"index": state["index"],
            "content_block": {"type": "tool_use", "id": slot["id"],
                "name": slot["function"]["name"], "input": {}}})
        args = slot["function"]["arguments"]
        if args:
            yield _anthropic_event("content_block_delta", {"index": state["index"],
                "delta": {"type": "input_json_delta", "partial_json": args}})

    def close_tools():
        for state in tools.values():
            if state["index"] is None:
                if not state["slot"]["function"]["name"]:
                    raise UpstreamError(502, "上游工具调用缺少名称")
                yield from open_tool(state)
        for state in sorted(tools.values(), key=lambda s: s["index"]):
            if not state["closed"]:
                state["closed"] = True
                yield _anthropic_event("content_block_stop", {"index": state["index"]})

    try:
        for chunk in iter_chunks(resp, ping):
            err = sse_error(chunk)
            if err:
                raise UpstreamError(502, "上游返回错误：%s" % err)
            for ev in acc.feed(chunk):
                if ev[1] != 0:
                    continue
                if ev[0] == "content":
                    yield from close_tools()
                    if text_index is None:
                        text_index = next_index
                        next_index += 1
                        yield _anthropic_event("content_block_start", {"index": text_index,
                            "content_block": {"type": "text", "text": ""}})
                    yield _anthropic_event("content_block_delta", {"index": text_index,
                        "delta": {"type": "text_delta", "text": ev[2]}})
                elif ev[0] == "tool":
                    if text_index is not None:
                        yield _anthropic_event("content_block_stop", {"index": text_index})
                        text_index = None
                    _, _, ti, slot, args = ev
                    state = tools.setdefault(ti, {"slot": slot, "index": None, "closed": False})
                    if state["closed"]:
                        raise UpstreamError(502, "上游在工具调用结束后继续发送参数")
                    if state["index"] is None:
                        if slot["function"]["name"]:
                            yield from open_tool(state)
                    elif args:
                        yield _anthropic_event("content_block_delta", {"index": state["index"],
                            "delta": {"type": "input_json_delta", "partial_json": args}})
        if not acc.finish(0):
            raise UpstreamError(502, "上游响应流未完整结束（缺少 finish_reason）")
        msg = chat_to_anthropic(acc, model, mid)
        yield from close_tools()
        if text_index is not None:
            yield _anthropic_event("content_block_stop", {"index": text_index})
        yield _anthropic_event("message_delta", {"delta": {
            "stop_reason": msg["stop_reason"], "stop_sequence": None},
            "usage": msg["usage"]})
        yield _anthropic_event("message_stop", {})
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        raise
    except Exception as e:  # noqa: BLE001 - 已发送 HTTP 200，只能用 SSE 报错
        acc.error = "上游响应流中断：%s" % e
        acc.error_status = e.status if isinstance(e, UpstreamError) else 502
        log("Anthropic 流中断：%s" % e)
        yield _anthropic_event("error", {"error": {"type": "api_error", "message": acc.error}})


# ===========================================================================
# 六、上游调用：长连接池 + 首字节前重试
# ===========================================================================
@__import__("functools").lru_cache(maxsize=64)
def _proxy_for(scheme: str, host: str) -> str:
    """与 urllib 相同的代理判定（HTTP(S)_PROXY / NO_PROXY / Windows 系统代理），按主机缓存。"""
    try:
        if urllib.request.proxy_bypass(host):
            return ""
    except Exception:  # noqa: BLE001
        pass
    return urllib.request.getproxies().get(scheme, "")


class _Pool:
    """按 (scheme, host, port, 代理) 复用 HTTP(S) 长连接。

    urllib 每次请求都新建 TCP+TLS，跨境首字延迟会白白多一次握手（100~300ms）。
    连接只在响应被完整读完后才归还；中途放弃的连接直接关闭，绝不复用脏连接。
    """
    MAX_IDLE = min(MAX_INFLIGHT, 16)   # 与并发上限对齐：并发突发后连接不被白白关掉重建
    IDLE_TTL = 40     # 秒；实测上游空闲 45~60 秒断开，留余量，少拿到已被对端关掉的连接

    def __init__(self):
        self._idle: dict = {}
        self._mu = threading.Lock()
        self._ctx = ssl.create_default_context()
        self.created = 0
        self.reused = 0

    def get(self, scheme: str, host: str, port: int, timeout: float):
        key = (scheme, host, port, _proxy_for(scheme, host))
        now = time.time()
        with self._mu:
            lst = self._idle.get(key) or []
            while lst:
                conn, at = lst.pop()
                if now - at < self.IDLE_TTL and conn.sock is not None:
                    conn.timeout = timeout
                    conn.sock.settimeout(timeout)
                    self.reused += 1
                    return key, conn, True
                conn.close()
        self.created += 1
        return key, self._new(key, timeout), False

    def _new(self, key, timeout):
        scheme, host, port, proxy = key
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        kw = {"context": self._ctx} if scheme == "https" else {}
        if not proxy:
            return _use_fast_response(cls(host, port, timeout=timeout, **kw))
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        conn = cls(p.hostname, p.port or 80, timeout=timeout, **kw)
        hdr = {}
        if p.username:
            import base64
            cred = "%s:%s" % (urllib.parse.unquote(p.username), urllib.parse.unquote(p.password or ""))
            hdr["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        conn.set_tunnel(host, port, headers=hdr)   # CONNECT 隧道，TLS 仍与目标站直连
        return _use_fast_response(conn)

    def warm(self, url: str) -> None:
        """预先完成 TCP+TLS 握手放进池里：启动后的第一个请求也不用等握手。"""
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        key, conn, reused = self.get(u.scheme, u.hostname, port, 30)
        if reused:
            self.put(key, conn)
            return
        try:
            conn.connect()
            self.put(key, conn)
        except Exception as e:  # noqa: BLE001
            conn.close()
            log("上游预连接失败（不影响使用）：%s" % e)

    def put(self, key, conn) -> None:
        with self._mu:
            lst = self._idle.setdefault(key, [])
            if len(lst) < self.MAX_IDLE:
                lst.append((conn, time.time()))
                return
        conn.close()


POOL = _Pool()


def _read_header_lines(fp, limit: int = 1000) -> list:
    """自包含的响应头读取：私有 helper 缺失时仍能解析头部。

    与标准库一样读到空行为止；超出上限时抛错而不是静默截断，避免残留字节污染连接。
    """
    lines = []
    for _ in range(limit):
        line = fp.readline()
        if not line or line in (b"\r\n", b"\n"):
            return lines
        lines.append(line)
    raise http.client.HTTPException("响应头行数超出上限")


def _fast_parse_headers(fp, _class=http.client.HTTPMessage):
    """http.client 默认用 email 包解析响应头（每次 ~80µs）；上游头部都是简单的
    「名: 值」，这里直接切分，结果仍是 HTTPMessage，行为一致（~15µs）。"""
    try:
        lines = http.client._read_headers(fp)
    except AttributeError:                    # Python 内部实现变化：自己读头部，不依赖私有函数
        lines = _read_header_lines(fp)
    m = _class()
    hs = m._headers
    for ln in lines:
        t = ln.decode("iso-8859-1")
        if t[:1] in " \t" and hs:                # 续行
            k, v = hs[-1]
            hs[-1] = (k, v + " " + t.strip())
            continue
        k, sep, v = t.partition(":")
        if sep:
            hs.append((k.strip(), v.strip()))
    return m


class _FastResponse(http.client.HTTPResponse):
    pass


# 复用标准库 begin() 的字节码，只把它看到的全局 parse_headers 换成快速版。
# Python 内部实现变化时保留标准实现，避免优化代码影响代理启动。
try:
    _FastResponse.begin = __import__("types").FunctionType(
        http.client.HTTPResponse.begin.__code__,
        dict(vars(http.client), parse_headers=_fast_parse_headers),
        "begin", http.client.HTTPResponse.begin.__defaults__,
        http.client.HTTPResponse.begin.__closure__)
except (AttributeError, TypeError, ValueError):
    _FastResponse.begin = http.client.HTTPResponse.begin


def _use_fast_response(conn):
    conn.response_class = _FastResponse
    return conn


class UpstreamResponse:
    """包一层 http.client 响应：可逐行迭代 / read()；close() 时能复用就归还连接。"""

    def __init__(self, key, conn, resp):
        self._key, self._conn, self._r = key, conn, resp
        self.status = resp.status
        self.headers = resp.headers
        self.saw_done = False     # iter_chunks 见到 [DONE] 后置位：剩余字节很少，值得排空复用
        self._eof = False
        self._closed = False
        self._gen = None          # 自解分块的生成器：排空必须继续用它（字节已在它的缓冲里）

    def __iter__(self):
        r = self._r
        if r.chunked and r.fp is not None and not r.isclosed():
            self._gen = self._iter_chunked()
            return self._gen
        return self._iter_standard()

    def _iter_standard(self):
        completed = False
        try:
            yield from self._r
            completed = True
        finally:
            if completed and self._r.isclosed() and not self._r.will_close:
                self._eof = True

    def _iter_chunked(self):
        """自己解分块编码并按行切分：http.client 的逐行读取对每个小块都要走几层
        Python 调用（_read_next_chunk_size / _get_chunk_left），SSE 帧多时是主要开销。
        这里每次 read1 拿一整批字节，再一次性切行。读完终止块后把响应标记为完成，连接可复用。"""
        r = self._r
        fp = r.fp
        read1 = fp.read1
        pending = b""             # 正文里还没遇到换行的尾巴
        raw = b""                 # 尚未解析的分块编码字节
        body_left = 0             # 当前块剩余正文字节
        crlf_left = 0             # 当前块结尾还要跳过的 CRLF 字节
        while True:
            if not raw:
                raw = read1(65536)
                if not raw:
                    break                         # 对端提前断开：交出残余，由调用方判定
            if body_left:
                take = raw[:body_left]
                raw = raw[body_left:]
                body_left -= len(take)
                data = pending + take if pending else take
                parts = data.split(b"\n")
                pending = parts.pop()
                for line in parts:
                    yield line + b"\n"
                continue
            if crlf_left:
                k = min(crlf_left, len(raw))
                raw = raw[k:]
                crlf_left -= k
                continue
            i = raw.find(b"\r\n")
            while i < 0:
                more = read1(65536)
                if not more:
                    raw = b""
                    break
                raw += more
                i = raw.find(b"\r\n")
            if i < 0:
                break
            size = int(raw[:i].split(b";", 1)[0], 16)
            raw = raw[i + 2:]
            if size == 0:
                # 只有完整读到 trailer 终止空行，才能把连接视为干净。
                trailer_end = raw.find(b"\r\n\r\n")
                trailers_complete = raw.startswith(b"\r\n") or trailer_end >= 0
                while not trailers_complete:
                    more = read1(1024)
                    if not more:
                        break
                    raw += more
                    trailer_end = raw.find(b"\r\n\r\n")
                    trailers_complete = raw.startswith(b"\r\n") or trailer_end >= 0
                if not trailers_complete:
                    break
                r.chunk_left = None
                r.length = 0
                r._close_conn()
                self._eof = True
                break
            body_left, crlf_left = size, 2
        if pending:
            yield pending

    def read(self) -> bytes:
        data = self._r.read()
        if self._r.isclosed() and not self._r.will_close:
            self._eof = True
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        r = self._r
        try:
            if not r.isclosed() and self.saw_done and not r.will_close:
                if self._conn.sock is not None:
                    self._conn.sock.settimeout(2)
                if self._gen is not None:
                    for _ in zip(range(64), self._gen):   # [DONE] 之后只剩换行与终止块
                        pass
                else:
                    r.read(65536)
                    if r.isclosed():
                        self._eof = True
        except Exception:  # noqa: BLE001
            pass
        if self._eof and r.isclosed() and not r.will_close:
            POOL.put(self._key, self._conn)
        else:
            try:
                r.close()
            finally:
                self._conn.close()


def upstream_request(method: str, url: str, headers: dict, body: bytes | None = None,
                     timeout: float = UPSTREAM_TIMEOUT) -> UpstreamResponse:
    u = urllib.parse.urlsplit(url)
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    target = (u.path or "/") + ("?" + u.query if u.query else "")
    for attempt in range(2):
        key, conn, reused = POOL.get(u.scheme, u.hostname, port, timeout)
        try:
            if conn.sock is None:
                try:
                    conn.connect()
                except (OSError, http.client.HTTPException) as e:
                    e.wb2api_retry_safe = True
                    raise
            conn.request(method, target, body=body, headers=headers)
            r = conn.getresponse()
        except (ConnectionError, http.client.BadStatusLine) as e:
            conn.close()
            if reused and attempt == 0 and getattr(e, "wb2api_retry_safe", False):
                continue
            raise
        except BaseException:
            conn.close()
            raise
        return UpstreamResponse(key, conn, r)
    raise ConnectionError("无法连接上游")  # pragma: no cover


def http_json(method: str, url: str, headers: dict, body: bytes | None = None, timeout: float = 30):
    """走连接池的 JSON 请求；HTTP >= 400 抛 RuntimeError。"""
    resp = upstream_request(method, url, headers, body, timeout)
    try:
        raw = resp.read()
    finally:
        resp.close()
    if resp.status >= 400:
        raise RuntimeError("HTTP %s: %s" % (resp.status, raw[:200].decode("utf-8", "replace")))
    return json.loads(raw.decode("utf-8"))


class UpstreamError(Exception):
    def __init__(self, status: int, msg: str, retry_after: str = ""):
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.retry_after = retry_after


def _looks_like_gzip_rejection(status: int, body: bytes) -> bool:
    if status not in (400, 415):
        return False
    text = body[:4096].decode("utf-8", "replace").lower()
    return "gzip" in text or "content-encoding" in text or "decompress" in text


class _BufferedResponse:
    def __init__(self, response, body: bytes):
        self._response = response
        self._body = body
        self.status = response.status
        self.headers = response.headers

    def read(self, *_args):
        body, self._body = self._body, b""
        return body

    def __iter__(self):
        body, self._body = self._body, b""
        for line in body.splitlines(True):
            yield line

    def close(self):
        self._response.close()


def _discard(resp) -> None:
    try:
        resp.read()
    except Exception:  # noqa: BLE001
        pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


def _retry_delay(resp, attempt: int) -> float:
    ra = to_int((resp.headers.get("Retry-After") or "").strip()) if resp is not None else 0
    return float(ra) if ra > 0 else RETRY_BASE_DELAY * (RETRY_FACTOR ** (attempt - 1))


SENSITIVE_PAYLOAD_FIELDS = ("messages", "system", "prompt", "input", "instructions")


def _safe_log_payload(payload: object) -> dict:
    """从 payload 中移除用户内容字段，仅保留元数据用于日志记录。

    对话正文可能出现在 chat 格式的 messages/system，也可能出现在 Responses 原生
    格式的 input/instructions，这些都不允许写入日志。
    """
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k not in SENSITIVE_PAYLOAD_FIELDS}


_chat_path = CHAT_PATH
_chat_paths: dict[str, str] = {}  # 按上游域名记住路径，国内外请求互不干扰


def call_upstream(payload: dict, a: dict) -> UpstreamResponse:
    """调用上游；只重试可确认安全的连接建立失败与 429。"""
    base = upstream_base(a)
    chat_path = _chat_paths.get(base, CHAT_PATH)
    # ensure_ascii=True：代码/英文为主的上下文序列化快 3~4 倍；体积膨胀的部分 gzip 会吃掉
    request_id = payload.get("_wb2api_request_id") or _uuid7().hex
    upstream_payload = {key: value for key, value in payload.items()
                        if key != "_wb2api_request_id"}
    data = json.dumps(upstream_payload, separators=(",", ":")).encode("ascii")
    gz = _gzip(data) if _gzip_ok and not _gzip_disabled(base) and len(data) >= GZIP_MIN else None
    refreshed = tried_alt = False
    attempt = 0
    while True:
        token = a.get("accessToken")
        url = base + chat_path
        h = chat_headers(a)
        h["X-Request-ID"] = request_id
        h["X-Conversation-Message-ID"] = request_id
        h["X-Conversation-Request-ID"] = request_id
        h["X-Root-Request-ID"] = request_id
        body = data
        if gz is not None and _gzip_ok and not _gzip_disabled(base):
            h["Content-Encoding"] = "gzip"
            body = gz
            with _gzip_stats_lock:
                _gzip_stats["requests"] += 1
                _gzip_stats["saved_bytes"] += len(data) - len(gz)
        if DEBUG:
            # 只记录元数据：payload 里的 messages/system 是用户对话正文，绝不能进日志。
            # _safe_log_payload 负责剔除这些字段，这里只补充体积、条数等排查信息。
            meta = _safe_log_payload(payload)
            msgs = payload.get("messages") or []
            first_msg = msgs[0] if msgs and isinstance(msgs, list) and msgs[0] and isinstance(msgs[0], dict) else {}
            log("→ POST %s model=%s messages=%d (首条role=%s) fields=%d %dB%s" % (
                url, meta.get("model"), len(msgs), first_msg.get("role", "N/A"), len(meta),
                len(body), "（gzip，原 %dB）" % len(data) if body is gz else ""))
        try:
            resp = upstream_request("POST", url, h, body)
        except (OSError, http.client.HTTPException) as e:
            if getattr(e, "wb2api_retry_safe", False) and attempt < UPSTREAM_RETRIES:
                attempt += 1
                log("上游连接失败（%s），第 %d 次重试" % (e, attempt))
                time.sleep(_retry_delay(None, attempt))
                continue
            raise
        st = resp.status
        if DEBUG:
            log("← %s %s" % (st, resp.headers.get("Content-Type")))
        if body is gz and st in (400, 415):
            raw = resp.read()
            if _looks_like_gzip_rejection(st, raw):
                resp.close()
                with _gzip_stats_lock:
                    _gzip_disabled_bases.add(base)
                log("上游 %s 拒绝 gzip 请求体，已按该上游关闭压缩" % st)
                continue
            resp = _BufferedResponse(resp, raw)
        if st == 401 and not refreshed:
            refreshed = True
            _discard(resp)
            if refresh_auth(a, stale_token=token):
                continue
            raise UpstreamError(401, "上游登录已过期且刷新失败，请重新登录")
        if st in (404, 405) and not tried_alt:
            tried_alt = True
            _discard(resp)
            chat_path = CHAT_PATH_ALT if chat_path == CHAT_PATH else CHAT_PATH
            _chat_paths[base] = chat_path
            log("对话接口路径回退 → %s" % chat_path)
            continue
        if st == 429 and attempt < UPSTREAM_RETRIES:
            delay = _retry_delay(resp, attempt + 1)
            if delay <= RETRY_MAX_DELAY:
                attempt += 1
                _discard(resp)
                log("上游 %s，%.1fs 后第 %d 次重试" % (st, delay, attempt))
                time.sleep(delay)
                continue
        return resp


_ENC = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
_dumps = _ENC.encode       # 下行帧：紧凑、不转义中文（体积小）；复用编码器对象省每次构造


def _gzip(data: bytes) -> bytes:
    import zlib
    c = zlib.compressobj(3, zlib.DEFLATED, 31)      # 31 = gzip 封装；level 3 是速度/压缩比的拐点
    return c.compress(data) + c.flush()


def is_sse(resp) -> bool:
    """上游偶尔用 HTTP 200 返回 JSON 业务错误，而不是 SSE。"""
    ct = ""
    try:
        ct = resp.headers.get("Content-Type") or ""
    except Exception:  # noqa: BLE001
        pass
    return not ct or "event-stream" in ct.lower()


def sse_error(obj) -> str | None:
    """识别 SSE 帧里内嵌的错误：{"error":{...}} 或 {"code":非0,"msg":...}。"""
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if err:
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or err)
        return str(err)
    code = obj.get("code")
    if "choices" not in obj and isinstance(code, int) and code != 0:
        return "%s (code=%s)" % (obj.get("msg") or obj.get("message") or "上游返回错误", code)
    return None


def read_error(resp) -> tuple[int, str]:
    status = getattr(resp, "status", 502) or 502
    try:
        raw = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        raw = ""
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        obj = json.loads(raw)
        msg = sse_error(obj) or obj.get("msg") or obj.get("message") or raw
        code = obj.get("code")
        if code is not None and "code=" not in str(msg):
            msg = "%s (code=%s)" % (msg, code)
    except Exception:  # noqa: BLE001
        msg = raw or "上游返回错误"
    if status < 400:      # 200 + JSON 业务错误
        status = 502
    if status == 403 and "11140" in str(msg):
        msg = "%s —— 可能需要更新客户端版本：设置 WB_CLIENT_VERSION / WB_CLI_VERSION" % msg
    return status, "上游拒绝请求：%s" % msg


# ===========================================================================
# 七、SSE：统一解析 + 增量累加（非流式聚合 / chat 流 / Responses 流共用）
# ===========================================================================
_SSE_MAX_LINES = 256


def iter_chunks(resp, ping=None):
    """逐帧产出上游 SSE 里的 JSON 对象（只产出 dict），遇 [DONE] 结束。

    按 SSE 规范一个事件可由多行 data: 组成（以空行结束）；但为了不给首字加延迟，
    单行 data 能独立解析时立即产出，不等空行。
    """
    buf: list[str] = []
    dumped = 0 if DEBUG else 6
    loads = json.loads
    saw_data = False
    for raw in resp:
        if not raw.startswith(b"data:"):          # 热路径：字节层判断，不先解码
            if raw.startswith(b":"):
                if ping is not None:
                    ping()                        # 上游心跳：转给客户端，防止长思考时被客户端判空闲断开
            elif raw.strip() == b"":
                buf.clear()                       # 事件边界：残留的半截数据作废
            continue                              # event: / id: / retry: 都用不上
        saw_data = True
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if dumped < 6:
            dumped += 1
            log("  流事件：%s" % line[:300])
        data = line[6:] if line.startswith("data: ") else line[5:]
        if not buf:
            if data == "[DONE]" or data.strip() == "[DONE]":
                if hasattr(resp, "saw_done"):
                    resp.saw_done = True
                    # 先把上游连接还回池，再给客户端写收尾帧：客户端紧接着发下一个请求时就能复用
                    resp.close()
                return
            try:
                obj = loads(data)                 # 绝大多数帧：单行 data 一次解析成功
            except ValueError:
                buf.append(data)
                continue
        else:
            buf.append(data)
            try:
                obj = loads("\n".join(buf))
            except ValueError:
                if len(buf) > _SSE_MAX_LINES:
                    buf.clear()
                continue
            buf.clear()
        if isinstance(obj, dict):
            yield obj
    if not saw_data:
        # HTTP 200 但正文里没有任何 data: 行：通常是 JSON 业务错误或非 SSE 响应，不能当成空成功
        raise UpstreamError(502, "上游响应不是事件流（没有 data: 帧）")


class ChatAccumulator:
    """把 chat SSE 帧增量合并成完整消息；feed() 同时返回本帧的增量事件。

    事件：("reasoning", 选项序号, 文本) / ("content", 选项序号, 文本)
          ("tool", 选项序号, 工具序号, 累积槽位, 本帧参数增量)
    """

    def __init__(self):
        self.id = ""
        self.model = ""
        self.error = ""
        self.error_status = 0
        self.usage: dict | None = None
        self.choices: dict[int, dict] = {}

    def _choice(self, i: int) -> dict:
        st = self.choices.get(i)
        if st is None:
            st = self.choices[i] = {"role": "assistant", "content": [], "reasoning": [],
                                    "refusal": [], "tools": {}, "finish": None}
        return st

    def feed(self, chunk: dict) -> list:
        evs: list = []
        if chunk.get("id"):
            self.id = chunk["id"]
        if chunk.get("model"):
            self.model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for c in chunk.get("choices") or []:
            if not isinstance(c, dict):
                continue
            i = to_int(c.get("index"))
            st = self._choice(i)
            if c.get("finish_reason"):
                st["finish"] = c["finish_reason"]
            d = c.get("delta") if isinstance(c.get("delta"), dict) else c.get("message")
            if not isinstance(d, dict):
                continue
            if d.get("role"):
                st["role"] = d["role"]
            t = d.get("reasoning_content")
            if isinstance(t, str) and t:
                st["reasoning"].append(t)
                evs.append(("reasoning", i, t))
            t = d.get("content")
            if isinstance(t, str) and t:
                st["content"].append(t)
                evs.append(("content", i, t))
            t = d.get("refusal")
            if isinstance(t, str) and t:
                st["refusal"].append(t)
            for tc in d.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                ti = to_int(tc.get("index"))
                slot = st["tools"].get(ti)
                if slot is None:
                    slot = st["tools"][ti] = {"id": "", "type": "function",
                                              "function": {"name": "", "arguments": ""}}
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                args = fn.get("arguments") if isinstance(fn.get("arguments"), str) else ""
                if args:
                    slot["function"]["arguments"] += args
                evs.append(("tool", i, ti, slot, args))
        return evs

    def finish(self, i: int = 0):
        st = self.choices.get(i)
        return st["finish"] if st else None

    def message(self, i: int = 0, reasoning: bool = True) -> dict:
        st = self.choices.get(i) or self._choice(i)
        msg: dict = {"role": st["role"], "content": "".join(st["content"]) or None}
        if reasoning and st["reasoning"] and not HIDE_REASONING:
            msg["reasoning_content"] = "".join(st["reasoning"])
        if st["refusal"]:
            msg["refusal"] = "".join(st["refusal"])
        if st["tools"]:
            for slot in st["tools"].values():
                if not slot["id"]:       # 上游没给 id：补一个，保证客户端回传结果时能对上
                    slot["id"] = "call_" + uuid.uuid4().hex[:24]
            msg["tool_calls"] = [st["tools"][k] for k in sorted(st["tools"])]
        return msg

    def usage_out(self) -> dict | None:
        return norm_usage(self.usage)

    def completion(self, model: str = "") -> dict:
        idxs = sorted(self.choices) or [0]
        out = {
            "id": self.id or "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model or model,
            "choices": [{"index": i, "message": self.message(i),
                         "finish_reason": self.finish(i) or "stop"} for i in idxs],
        }
        if self.usage_out() is not None:
            out["usage"] = self.usage
        return out


def norm_usage(u):
    """补 total_tokens；把上游的 prompt_cache_hit_tokens 映射到 OpenAI 的 cached_tokens，
    客户端（如 Codex）才能显示缓存命中。原地修改并返回。"""
    if u.__class__ is not dict:
        return u
    if "total_tokens" not in u:
        u["total_tokens"] = to_int(u.get("prompt_tokens")) + to_int(u.get("completion_tokens"))
    hit = to_int(u.get("prompt_cache_hit_tokens") or u.get("cache_read_input_tokens")
                 or u.get("cached_tokens"))
    if hit:
        ptd = u.get("prompt_tokens_details")
        if ptd.__class__ is not dict:
            ptd = u["prompt_tokens_details"] = {}
        if not to_int(ptd.get("cached_tokens")):
            ptd["cached_tokens"] = hit
    return u


def aggregate_sse(resp, acc: ChatAccumulator | None = None) -> dict:
    acc = acc or ChatAccumulator()
    for chunk in iter_chunks(resp):
        err = sse_error(chunk)
        if err:
            raise UpstreamError(502, "上游返回错误：%s" % err)
        acc.feed(chunk)
    return acc.completion()


def normalize_chunk(obj: dict, model: str = "") -> dict:
    """给上游流式帧补齐 OpenAI 规范必备字段。

    黑名单式：只删空值、补缺失字段，其余字段（logprobs / refusal / annotations
    以及上游将来新增的字段）原样保留。错误帧原样透传。
    """
    if sse_error(obj):
        return obj
    out = obj                                    # 原地修改：帧对象只用这一次，省一次拷贝
    if not out.get("id"):
        out["id"] = "chatcmpl-wb2api"
    if "object" not in out:
        out["object"] = "chat.completion.chunk"
    if "created" not in out:
        out["created"] = int(time.time())
    if model and not out.get("model"):
        out["model"] = model
    u = out.get("usage", 0)
    if u is None:
        del out["usage"]                         # 每帧都带 usage:null，删掉省流量
    elif u.__class__ is dict:
        norm_usage(u)
    choices = obj.get("choices") or []
    for c in choices:
        if c.__class__ is not dict:
            continue
        d = c.get("delta")
        if d.__class__ is dict:
            if HIDE_REASONING:
                d.pop("reasoning_content", None)
            empty = [k for k, v in d.items() if v is None or v == "" or v == []]
            for k in empty:
                del d[k]
        else:
            c["delta"] = {}
        if "index" not in c:
            c["index"] = 0
        if "logprobs" in c and c["logprobs"] is None:
            del c["logprobs"]
        if not c.get("finish_reason"):
            c["finish_reason"] = None           # 上游用 "" 表示未结束，OpenAI 规范是 null
    out["choices"] = [c for c in choices if c.__class__ is dict]
    return out


# ===========================================================================
# 八、Responses API 适配（input/instructions → messages；chat → response）
#
# Codex 等客户端除 function 工具外还会发：
#   custom      自由格式工具（如 apply_patch），入参是一段原文而非 JSON
#   local_shell 内置 shell 工具
# 上游 chat 只认 function，这里把它们映射成 function，回程再还原成对应的 item 类型。
# ===========================================================================
LOCAL_SHELL_FN = "local_shell"
_LOCAL_SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "array", "items": {"type": "string"},
                    "description": "要执行的命令及参数数组"},
        "workdir": {"type": "string", "description": "工作目录"},
        "timeout_ms": {"type": "integer", "description": "超时时间（毫秒）"},
    },
    "required": ["command"],
}
_WARNED_MAX = 256                 # 提示去重集合上限：不能被请求内容无限撑大
_warned: set = set()
_warned_lock = threading.Lock()


def _warn_once(msg: str) -> None:
    """同一提示只打印一次。

    消息可能拼进客户端可控内容（工具 type 等），所以先压平控制字符并截断，
    再限制去重集合条数——否则逐请求换一个值就能把字符串永久堆在内存里，
    还能靠 \\n 伪造日志行。
    """
    text = " ".join(str(msg).replace("\r", " ").replace("\n", " ").split())[:500]
    with _warned_lock:
        if text in _warned or len(_warned) >= _WARNED_MAX:
            return
        _warned.add(text)
    log(text)


def new_rctx() -> dict:
    return {"custom": set(), "shell": False, "history": [], "store": True}


def _text_of(v) -> str:
    """function_call_output.output 可能是字符串，也可能是内容数组。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(p.get("text") or "" for p in v if isinstance(p, dict))
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False)


def _responses_content(content):
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    parts = []
    for p in content:
        if isinstance(p, str):
            parts.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "refusal"):
            parts.append({"type": "text", "text": p.get("text") or p.get("refusal") or ""})
        elif t == "input_image":
            img = p.get("image_url")
            url = img.get("url") if isinstance(img, dict) else img
            if url:
                iu = {"url": url}
                if p.get("detail"):
                    iu["detail"] = p["detail"]
                parts.append({"type": "image_url", "image_url": iu})
    # 纯文本消息压成字符串，兼容只认字符串 content 的模型
    if parts and all(x["type"] == "text" for x in parts):
        return "".join(x["text"] for x in parts)
    return parts or ""


def _custom_schema(t: dict) -> dict:
    desc = "此工具的原始输入，必须原样传递。"
    fmt = t.get("format")
    if isinstance(fmt, dict) and fmt.get("type") == "grammar" and fmt.get("definition"):
        desc += " 输入必须符合以下 %s 语法：\n%s" % (
            fmt.get("syntax") or "", fmt["definition"])
    return {"type": "object",
            "properties": {"input": {"type": "string", "description": desc}},
            "required": ["input"]}


def _responses_tools(tools, ctx: dict) -> list:
    """Responses 工具 → chat function 工具。custom / local_shell 做映射，其余内置工具丢弃并提示。"""
    out, dropped = [], []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        typ = t.get("type")
        if typ == "function":
            if isinstance(t.get("function"), dict):
                out.append(t)
                continue
            fn = {"name": t.get("name") or ""}
            for k in ("description", "parameters", "strict"):
                if k in t:
                    fn[k] = t[k]
            out.append({"type": "function", "function": fn})
        elif typ == "custom":
            name = t.get("name") or ""
            ctx["custom"].add(name)
            out.append({"type": "function", "function": {
                "name": name, "description": t.get("description") or "",
                "parameters": _custom_schema(t)}})
        elif typ == "local_shell":
            ctx["shell"] = True
            out.append({"type": "function", "function": {
                "name": LOCAL_SHELL_FN,
                "description": "在用户的计算机上执行命令并返回输出。",
                "parameters": _LOCAL_SHELL_SCHEMA}})
        else:
            dropped.append(str(typ))
    if dropped:
        _warn_once("Responses 工具类型上游不支持，已忽略：%s" % ", ".join(sorted(set(dropped))))
    return out


def _append_call(messages: list, call: dict) -> None:
    """连续的工具调用合并进同一条 assistant 消息（并行工具调用）。"""
    last = messages[-1] if messages else None
    if last and last.get("role") == "assistant" and "tool_calls" in last:
        last["tool_calls"].append(call)
    elif last and last.get("role") == "assistant" and not last.get("content"):
        last["tool_calls"] = [call]
    else:
        messages.append({"role": "assistant", "content": None, "tool_calls": [call]})


def _history_call(item: dict) -> dict | None:
    typ = item.get("type")
    cid = item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:16]
    if typ == "function_call":
        name, args = item.get("name") or "", item.get("arguments") or "{}"
    elif typ == "custom_tool_call":
        name = item.get("name") or ""
        args = json.dumps({"input": item.get("input") or ""}, ensure_ascii=False)
    elif typ == "local_shell_call":
        act = item.get("action") if isinstance(item.get("action"), dict) else {}
        a = {"command": act.get("command") or []}
        if act.get("working_directory"):
            a["workdir"] = act["working_directory"]
        if act.get("timeout_ms"):
            a["timeout_ms"] = act["timeout_ms"]
        name, args = LOCAL_SHELL_FN, json.dumps(a, ensure_ascii=False)
    else:
        return None
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output", "local_shell_call_output")


class _RespStore:
    """按条数、历史字节数和寿命限制会话；序列化快照避免调用方修改缓存。"""

    def __init__(self):
        self._d: collections.OrderedDict = collections.OrderedDict()
        self._mu = threading.Lock()
        self._bytes = 0

    def _drop(self, rid):
        entry = self._d.pop(rid)
        self._bytes -= len(entry[1])

    def _prune(self, now):
        while self._d:
            rid, entry = next(iter(self._d.items()))
            if now - entry[0] < RESP_STORE_TTL:
                break
            self._drop(rid)

    def prune(self):
        with self._mu:
            self._prune(time.monotonic())

    def put(self, rid: str, messages: list, owner: str = "") -> bool:
        """缓存一段历史。返回是否真的缓存成功（供调用方决定是否提示）。"""
        try:
            payload = json.dumps(messages, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
        except (UnicodeEncodeError, TypeError, ValueError):
            # 内容里有孤立代理字符等无法用 UTF-8 编码的内容：退回 ASCII 转义再试
            try:
                payload = json.dumps(messages, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")
            except Exception:  # noqa: BLE001 - 无法序列化就不缓存，绝不能影响响应
                return False
        with self._mu:
            now = time.monotonic()
            self._prune(now)
            if rid in self._d:
                self._drop(rid)
            if len(payload) > RESP_STORE_BYTES or RESP_STORE_MAX <= 0:
                return False
            self._d[rid] = (now, payload, owner)
            self._bytes += len(payload)
            while len(self._d) > RESP_STORE_MAX or self._bytes > RESP_STORE_BYTES:
                self._drop(next(iter(self._d)))
        return True

    def get(self, rid: str, owner: str = ""):
        with self._mu:
            self._prune(time.monotonic())
            v = self._d.get(rid)
            if not v or v[2] != owner:
                return None
            payload = v[1]
        return json.loads(payload)


RESP_STORE = _RespStore()


def responses_to_chat(body: dict, owner: str = "") -> tuple[dict, dict]:
    """把 /v1/responses 请求体翻译成上游能吃的 chat 请求体；同时返回回程还原所需的上下文。"""
    ctx = new_rctx()
    ctx["owner"] = owner
    ctx["store"] = body.get("store") is not False
    messages: list[dict] = []
    prev = body.get("previous_response_id")
    if prev:
        hist = RESP_STORE.get(prev, owner)
        if hist is None:
            raise ValueError("当前账号找不到 previous_response_id '%s'：本代理仅在内存中保存最近 "
                             "%d 条响应，重启或过期后请在 input 中发送完整对话"
                             % (prev, RESP_STORE_MAX))
        messages = hist

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            call = _history_call(item)
            if call:
                _append_call(messages, call)
                continue
            if typ in _OUTPUT_TYPES:
                messages.append({"role": "tool", "tool_call_id": item.get("call_id") or "",
                                 "content": _text_of(item.get("output"))})
                continue
            if typ not in (None, "message"):
                continue    # reasoning / web_search_call / item_reference 等无对应 chat 形态
            role = item.get("role") or "user"
            messages.append({"role": role, "content": _responses_content(item.get("content"))})
    if not messages:
        messages.append({"role": "user", "content": ""})
    ctx["history"] = messages

    head = []
    instr = body.get("instructions")
    if isinstance(instr, str) and instr.strip():
        head.append({"role": "system", "content": instr})
    chat = {"model": body.get("model"), "messages": head + messages, "stream": True}
    for k in ("temperature", "top_p", "parallel_tool_calls", "stop", "user", "seed"):
        if k in body:
            chat[k] = body[k]
    if isinstance(body.get("max_output_tokens"), int) and body["max_output_tokens"] > 0:
        chat["max_tokens"] = body["max_output_tokens"]
    tools = _responses_tools(body.get("tools"), ctx)
    if tools:
        chat["tools"] = tools
    tc = body.get("tool_choice")
    if isinstance(tc, str):
        chat["tool_choice"] = tc
    elif isinstance(tc, dict):
        if tc.get("type") in ("function", "custom") and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif tc.get("type") == "local_shell":
            chat["tool_choice"] = {"type": "function", "function": {"name": LOCAL_SHELL_FN}}
        elif tc.get("type") == "allowed_tools":
            # 白名单必须真的生效：只转发 mode 会让模型调用白名单外的工具，
            # 客户端照着 function_call 执行就等于绕过了它自己的工具白名单。
            allow = [t for t in (tc.get("tools") or []) if isinstance(t, str) and t.strip()]
            chat["tool_choice"] = tc.get("mode") or "auto"
            if allow:
                if isinstance(chat.get("tools"), list):
                    chat["tools"] = [t for t in chat["tools"]
                                     if isinstance(t, dict)
                                     and ((t.get("function") or {}).get("name") in allow)]
                if not chat.get("tools"):
                    chat.pop("tools", None)
                    chat.pop("tool_choice", None)
            else:
                # 空白名单：一个工具都不允许
                chat.pop("tools", None)
                chat.pop("tool_choice", None)
    rea = body.get("reasoning")
    if isinstance(rea, dict) and rea.get("effort"):
        chat["reasoning_effort"] = rea["effort"]
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_object":
            chat["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema":
            js = {k: fmt[k] for k in ("name", "schema", "strict", "description") if k in fmt}
            chat["response_format"] = {"type": "json_schema", "json_schema": js}
    return chat, ctx


def _call_kind(name: str, ctx: dict) -> str:
    if name in ctx["custom"]:
        return "custom_tool_call"
    if ctx["shell"] and name == LOCAL_SHELL_FN:
        return "local_shell_call"
    return "function_call"


def _call_item(call_id: str, name: str, args: str, ctx: dict, item_id: str = "",
               status: str = "completed") -> dict:
    """把一次 chat 工具调用还原成 Responses 的 item（function / custom / local_shell）。"""
    kind = _call_kind(name, ctx)
    if kind == "custom_tool_call":
        inp = args
        try:
            v = json.loads(args).get("input")
            if isinstance(v, str):
                inp = v
        except Exception:  # noqa: BLE001 - 模型没按 JSON 包装时原样作为输入
            pass
        return {"id": item_id or "ctc_" + uuid.uuid4().hex[:24], "type": kind,
                "call_id": call_id, "name": name, "input": inp, "status": status}
    if kind == "local_shell_call":
        try:
            a = json.loads(args) if args else {}
        except Exception:  # noqa: BLE001
            a = {}
        a = a if isinstance(a, dict) else {}
        cmd = a.get("command") or []
        if isinstance(cmd, str):
            cmd = ["bash", "-lc", cmd]
        action = {"type": "exec", "command": cmd, "env": {}}
        if a.get("workdir"):
            action["working_directory"] = a["workdir"]
        if a.get("timeout_ms"):
            action["timeout_ms"] = a["timeout_ms"]
        return {"id": item_id or "lsh_" + uuid.uuid4().hex[:24], "type": kind,
                "call_id": call_id, "action": action, "status": status}
    return {"id": item_id or "fc_" + uuid.uuid4().hex[:24], "type": kind, "call_id": call_id,
            "name": name, "arguments": args, "status": status}


def _responses_usage(u) -> dict:
    u = norm_usage(dict(u)) if isinstance(u, dict) else {}
    i, o = to_int(u.get("prompt_tokens")), to_int(u.get("completion_tokens"))
    out = {"input_tokens": i, "output_tokens": o,
           "total_tokens": to_int(u.get("total_tokens")) or i + o}
    ptd = u.get("prompt_tokens_details")
    if isinstance(ptd, dict) and to_int(ptd.get("cached_tokens")):
        out["input_tokens_details"] = {"cached_tokens": to_int(ptd["cached_tokens"])}
    ctd = u.get("completion_tokens_details")
    if isinstance(ctd, dict) and "reasoning_tokens" in ctd:
        out["output_tokens_details"] = {"reasoning_tokens": to_int(ctd["reasoning_tokens"])}
    return out


def _finish_status(finish: str | None):
    if finish == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def _remember(rid: str, ctx: dict, acc: ChatAccumulator) -> None:
    """把本轮历史存进内存，供 previous_response_id 续接。

    缓存失败只记日志：响应已经（或即将）写给客户端，绝不能因为缓存问题
    把一次成功的调用变成半截流或断连。
    """
    if not ctx.get("store"):
        return
    try:
        stored = RESP_STORE.put(rid, ctx["history"] + [acc.message(0, reasoning=False)],
                                ctx.get("owner", ""))
    except Exception as e:  # noqa: BLE001
        log("会话历史缓存失败（不影响本次响应）：%s" % e)
        return
    if not stored:
        log("会话历史过大或无法序列化，未缓存 previous_response_id：%s" % rid)


def chat_to_response(acc: ChatAccumulator, model: str, rid: str, ctx: dict) -> dict:
    """把聚合好的结果包成 Responses 形状。"""
    msg = acc.message(0)
    text = msg.get("content") or ""
    status, details = _finish_status(acc.finish(0))
    output: list[dict] = []
    if msg.get("reasoning_content"):
        output.append({"id": "rs_" + uuid.uuid4().hex[:24], "type": "reasoning",
                       "summary": [{"type": "summary_text", "text": msg["reasoning_content"]}]})
    if text or not msg.get("tool_calls"):
        output.append({
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append(_call_item(tc["id"], fn.get("name") or "", fn.get("arguments") or "", ctx))
    out = {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": acc.model or model,
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "usage": _responses_usage(acc.usage),
    }
    if details:
        out["incomplete_details"] = details
    _remember(rid, ctx, acc)
    return out


def responses_stream(resp, model, rid: str, ctx: dict, acc: ChatAccumulator, ping=None):
    """把上游 chat SSE **实时**转成 Responses 事件流（逐 token 转发，不缓冲）。

    状态机：reasoning / message 同一时刻最多开一个；工具调用按上游 index
    各自开启，名字到达后才发 output_item.added。工具参数可以和文本交错，
    每个 item 只在确认流正常结束后发自己的终结事件。
    """
    created = int(time.time())
    seq = [0]
    output: list = []
    cur: dict | None = None
    tools: dict[int, dict] = {}
    terminal_frames: dict[int, list[tuple[str, dict]]] = {}
    def ev(name, payload):
        payload = dict(payload)
        payload["type"] = name
        payload["sequence_number"] = seq[0]
        seq[0] += 1
        return "event: %s\ndata: %s\n\n" % (name, _dumps(payload))

    def snapshot(status, items):
        return {"id": rid, "object": "response", "created_at": created,
                "model": model, "status": status, "output": items}

    def open_item(kind):
        idx = len(output)
        if kind == "reasoning":
            item = {"id": "rs_" + uuid.uuid4().hex[:24], "type": "reasoning", "summary": []}
        else:
            item = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message",
                    "role": "assistant", "status": "in_progress", "content": []}
        output.append(item)
        st = {"kind": kind, "idx": idx, "item": item, "buf": []}
        frames = [ev("response.output_item.added", {"output_index": idx, "item": dict(item)})]
        if kind == "reasoning":
            frames.append(ev("response.reasoning_summary_part.added", {
                "item_id": item["id"], "output_index": idx, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""}}))
        else:
            frames.append(ev("response.content_part.added", {
                "item_id": item["id"], "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}}))
        return st, frames

    def close_item(st):
        item, idx, text = st["item"], st["idx"], "".join(st["buf"])
        if st["kind"] == "reasoning":
            part = {"type": "summary_text", "text": text}
            item["summary"] = [part]
            terminal = [("response.reasoning_summary_text.done", {
                "item_id": item["id"], "output_index": idx, "summary_index": 0, "text": text}),
                ("response.reasoning_summary_part.done", {
                "item_id": item["id"], "output_index": idx, "summary_index": 0, "part": part})]
        else:
            part = {"type": "output_text", "text": text, "annotations": []}
            item["content"] = [part]
            terminal = [("response.output_text.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0, "text": text}),
                ("response.content_part.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0, "part": part})]
        terminal.append(("response.output_item.done", {"output_index": idx, "item": item}))
        terminal_frames[idx] = terminal

    def text_delta(kind, text):
        nonlocal cur
        if cur is None or cur["kind"] != kind:
            if cur is not None:
                close_item(cur)
            cur, frames = open_item(kind)
            yield from frames
        cur["buf"].append(text)
        item = cur["item"]
        if kind == "reasoning":
            item["summary"] = [{"type": "summary_text", "text": "".join(cur["buf"])}]
            yield ev("response.reasoning_summary_text.delta", {
                "item_id": item["id"], "output_index": cur["idx"], "summary_index": 0,
                "delta": text})
        else:
            item["content"] = [{"type": "output_text", "text": "".join(cur["buf"]), "annotations": []}]
            yield ev("response.output_text.delta", {
                "item_id": item["id"], "output_index": cur["idx"], "content_index": 0,
                "delta": text})

    def open_tool(t):
        slot = t["slot"]
        if not slot["id"]:
            slot["id"] = "call_" + uuid.uuid4().hex[:24]
        name = slot["function"]["name"]
        item = _call_item(slot["id"], name, "", ctx, status="in_progress")
        if item["type"] == "custom_tool_call":
            item["input"] = ""
        elif item["type"] == "local_shell_call":
            item["action"] = {"type": "exec", "command": [], "env": {}}
        t.update(idx=len(output), item=item, open=True)
        output.append(item)
        yield ev("response.output_item.added", {"output_index": t["idx"], "item": dict(item)})
        buffered = slot["function"]["arguments"]
        if buffered and item["type"] == "function_call":
            item["arguments"] = buffered
            yield ev("response.function_call_arguments.delta", {
                "item_id": item["id"], "output_index": t["idx"], "delta": buffered})
        elif buffered and item["type"] == "custom_tool_call":
            item["input"] = buffered

    def close_tools():
        pending = [t for t in tools.values() if t["idx"] is None]
        for t in pending:                       # 到最后都没给名字的，也要按出现顺序开出来
            yield from open_tool(t)
        for t in sorted(tools.values(), key=lambda x: x["idx"]):
            if not t["open"]:
                continue
            t["open"] = False
            slot, item = t["slot"], t["item"]
            args = slot["function"]["arguments"]
            final = _call_item(slot["id"], slot["function"]["name"], args, ctx, item["id"])
            output[t["idx"]] = final
            terminal = []
            if final["type"] == "function_call":
                terminal.append(("response.function_call_arguments.done", {
                    "item_id": final["id"], "output_index": t["idx"], "arguments": args}))
            elif final["type"] == "custom_tool_call":
                terminal.append(("response.custom_tool_call_input.done", {
                    "item_id": final["id"], "output_index": t["idx"], "input": final["input"]}))
            terminal.append(("response.output_item.done", {"output_index": t["idx"], "item": final}))
            terminal_frames[t["idx"]] = terminal

    yield ev("response.created", {"response": snapshot("in_progress", [])})
    yield ev("response.in_progress", {"response": snapshot("in_progress", [])})

    try:
        for chunk in iter_chunks(resp, ping):
            err = sse_error(chunk)
            if err:
                raise UpstreamError(502, err)
            for e in acc.feed(chunk):
                if e[1] != 0:
                    continue
                if e[0] in ("reasoning", "content"):
                    if e[0] == "reasoning" and HIDE_REASONING:
                        continue
                    yield from text_delta("reasoning" if e[0] == "reasoning" else "message", e[2])
                    continue
                _, _, ti, slot, args = e
                t = tools.get(ti)
                if t is None:
                    t = tools[ti] = {"slot": slot, "idx": None, "item": None, "open": False}
                if t["idx"] is None:
                    if slot["function"]["name"]:
                        yield from open_tool(t)      # 会顺带补发已缓冲的参数
                    continue
                if args and t["open"] and t["item"]["type"] == "function_call":
                    t["item"]["arguments"] = t["slot"]["function"]["arguments"]
                    yield ev("response.function_call_arguments.delta", {
                        "item_id": t["item"]["id"], "output_index": t["idx"], "delta": args})
                elif args and t["open"] and t["item"]["type"] == "custom_tool_call":
                    t["item"]["input"] = t["slot"]["function"]["arguments"]
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        raise
    except Exception as e:  # noqa: BLE001 - 上游中途断流：给客户端一个明确的失败事件
        log("Responses 流中断：%s" % e)
        acc.error = "上游响应流中断：%s" % e
        acc.error_status = e.status if isinstance(e, UpstreamError) else 502
        failed = snapshot("failed", output)
        failed["error"] = {"code": "server_error", "message": acc.error}
        yield ev("response.failed", {"response": failed})
        return

    finish = acc.finish(0)
    if finish is None:
        # 上游在给出 finish_reason 之前就结束了：文本/工具参数可能是截断的，
        # 不能当成 completed 交给客户端（Anthropic 端点对同一情形会报 502）。
        acc.error = "上游响应流未完整结束（缺少 finish_reason）"
        acc.error_status = 502
        log("Responses 流未收到 finish_reason，按失败收尾")
        failed = snapshot("failed", output)
        failed["error"] = {"code": "server_error", "message": acc.error}
        yield ev("response.failed", {"response": failed})
        return

    if cur is not None:
        close_item(cur)
    yield from close_tools()
    for idx in sorted(terminal_frames):
        for name, payload in terminal_frames[idx]:
            if name == "response.output_item.done" and payload["item"].get("type") == "message":
                payload["item"]["status"] = "completed"
            yield ev(name, payload)
    status, details = _finish_status(finish)
    final = snapshot(status, output)
    final["output_text"] = "".join(
        p.get("text") or "" for it in output if it.get("type") == "message"
        for p in it.get("content") or [])
    final["usage"] = _responses_usage(acc.usage)
    if details:
        final["incomplete_details"] = details
    _remember(rid, ctx, acc)
    yield ev("response.incomplete" if status == "incomplete" else "response.completed",
             {"response": final})


# ===========================================================================
# 九、模型目录
#
# /v3/config 对**不同 UA 下发不同模型集合**，这是最容易踩的坑：
#   IDE UA   → 14 条，含 o4-mini / enhance-1.0 / auto-chat，**没有 deepseek 全家**
#   CLI UA   → 22 条，含 deepseek-v4.1-flash / gpt-6-astra / kimi-k2.8-preview
#   桌面端 UA → 又一批独有条目
# 三路各有独有模型，**只探一路必然漏**。故三路并发取并集，另加企业端点补缺
# （gpt-5.3-codex 这类只在企业端点出现）。
# ===========================================================================
ENTERPRISE_PATHS = ("/v2/enterprises/personal/models",
                    "/console/enterprises/personal/models")

# 非对话模型：补全通道 + 图像/视频生成类，被选中时上游会拒（code=11102）
NON_CHAT_PREFIX = ("nes-", "completion-", "codewise-")
NON_CHAT_TAGS = {"text-to-image", "image-to-image", "text-to-video", "image-to-video"}

_ARRAY_KEYS = ("models", "items", "list", "data", "result")


def is_global(a: dict | None = None) -> bool:
    """判定账号属于国际版还是国内版。

    优先看账号落盘的 domain（登录响应会带回），其次才看上游地址——
    这样即使用户把 WB_UPSTREAM_BASE 指到自建镜像，realm 判定也不会跑偏。
    """
    return realm_of(a) == "global"


def desktop_ua(a: dict | None = None) -> str:
    """官方桌面端 UA。

    国际版按 5.8.4 抓包：`WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0`
    （末段 CLI 版本是客户端内置 CLI 的版本，不是桌面端自身版本）；
    国内版维持既有身份——没有对应抓包，改动风险大于收益。
    """
    if is_global(a):
        return "WorkBuddy/%s workbuddy-ai/%s CLI/%s" % (
            INTL_CLIENT_VERSION, INTL_CLIENT_VERSION, INTL_CLI_VERSION)
    return "WorkBuddy/%s WorkBuddy/%s CLI/%s" % (CLIENT_VERSION, CLIENT_VERSION, CLI_VERSION)


def models_headers(a: dict, ua: str) -> dict:
    """模型目录专用头。与 chat 头有三处不同，缺一个就可能拿到精简目录或 4xx：
    `X-Product: SaaS`、宽松 `Accept`、显式 `X-Domain`。"""
    h = common_headers(a)
    h["Accept"] = "application/json, text/plain, */*"
    h["X-Product"] = "SaaS"
    h["X-Domain"] = a.get("domain") or REALMS[realm_of(a)]["domain"]
    if ua:
        h["User-Agent"] = ua
    return h


def _resolve_models_array(payload):
    """递归下钻定位模型数组：payload 本身是数组则直接用；
    是对象则按 models/items/list/data/result 依次下钻，取第一个能解出数组的分支。
    最后兜底识别 {模型id: {...}} 这种映射形态（部分上游变体这么下发）。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in _ARRAY_KEYS:
            if k in payload:
                got = _resolve_models_array(payload[k])
                if got is not None:
                    return got
        vals = [v for v in payload.values() if isinstance(v, dict)]
        if vals and len(vals) == len(payload):
            return [{"id": k, **v} for k, v in payload.items()]
    return None


def _model_id(m) -> str:
    """id 回退链 id → modelId → model → name（宽松键只出现在部分目录变体里）。"""
    if isinstance(m, str):
        return m.strip()
    if not isinstance(m, dict):
        return ""
    for k in ("id", "modelId", "model", "name"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _is_chat_model(m) -> bool:
    if not isinstance(m, dict):
        return True
    if m.get("disabled") is True:
        return False
    mid = _model_id(m).lower()
    if any(mid.startswith(p) for p in NON_CHAT_PREFIX):
        return False
    mo = m.get("maxOutputTokens")
    if isinstance(mo, (int, float)) and 0 < mo <= 256:
        return False
    tags = m.get("tags")
    if isinstance(tags, list) and any(t in NON_CHAT_TAGS for t in tags):
        return False
    return True


def _probe_config(a: dict, path: str, ua: str):
    """单路探测，返回信封里的 data 层（dict 或 list）。"""
    env = http_json("GET", upstream_base(a) + path, models_headers(a, ua), timeout=30)
    payload = env
    if isinstance(env, dict):
        code = env.get("code")
        if isinstance(code, int) and code != 0:
            raise RuntimeError("上游错误码=%s，说明=%s" % (code, env.get("msg")))
        inner = env.get("data")
        if isinstance(inner, (dict, list)):
            payload = inner
    return payload


def _probe_models(a: dict, path: str, ua: str) -> list:
    """单路探测，返回原始条目列表（元素可能是 dict 或裸字符串）。"""
    arr = _resolve_models_array(_probe_config(a, path, ua))
    if arr is None:
        raise RuntimeError("响应里找不到模型数组")
    return arr


def _probe_enterprise(a: dict) -> list:
    """企业端点家族：按序回落，首个成功即返回。"""
    last = None
    for path in ENTERPRISE_PATHS:
        try:
            return _probe_models(a, path, "")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("企业端点全部失败")


def _norm_promotion(p) -> dict | None:
    """归一化一条限时优惠（牌价倍率 → 生效价）。"""
    if not isinstance(p, dict) or not p.get("enabled"):
        return None
    ids = p.get("modelIds")
    if not isinstance(ids, list):
        return None
    disc = p.get("discount") if isinstance(p.get("discount"), dict) else {}
    badge = p.get("badge") if isinstance(p.get("badge"), dict) else {}
    hover = p.get("hover") if isinstance(p.get("hover"), dict) else {}
    return {
        "modelIds": [i for i in ids if isinstance(i, str)],
        "label": badge.get("label") or "",
        "factor": disc.get("factor"),
        "discountedCredits": disc.get("discountedCredits", ""),
        "note": hover.get("textZh") or "",
    }


def _build_entry(m, now: float) -> dict | None:
    """把上游一条模型条目映射成对外输出条目（含积分倍率）。"""
    mid = _model_id(m)
    if not mid or not _is_chat_model(m):
        return None
    entry = {"id": mid, "object": "model", "created": int(now), "owned_by": "workbuddy"}
    if not isinstance(m, dict):
        return entry
    if m.get("vendor"):
        entry["owned_by"] = m["vendor"]
    if _parse_factor(m.get("credits")) is not None:
        entry["credits"] = str(m["credits"]).strip()   # 数字 0 也是有效倍率
    if m.get("name"):
        entry["name"] = m["name"]
    if m.get("descriptionZh"):
        entry["description"] = m["descriptionZh"]
    if isinstance(m.get("tags"), list) and m["tags"]:
        entry["tags"] = m["tags"]
    if m.get("isDefault") is True:
        entry["is_default"] = True
    ctx = m.get("maxInputTokens")
    if isinstance(ctx, int) and ctx > 0:
        entry["context_length"] = ctx
    mo = m.get("maxOutputTokens")
    if isinstance(mo, int) and mo > 0:
        entry["max_output_tokens"] = mo
    rea = m.get("reasoning")
    if isinstance(rea, dict) and rea.get("supportedEfforts"):
        entry["reasoning_supported_efforts"] = rea["supportedEfforts"]
    if m.get("supportsImages") is True:
        entry["supports_images"] = True
    return entry


def _catalog_for(a: dict) -> dict:
    """调用方持有 _models_lock；每个账号各自保留目录和优惠。"""
    aid = account_id(a)
    if aid not in _catalogs:
        cached = {"at": 0.0, "data": None, "owner": [aid], "complete": False}
        try:
            with open(MODELS_CACHE_FILE, encoding="utf-8") as f:
                disk = json.load(f)
            disk_data = disk.get("data") if isinstance(disk, dict) else None
            if (isinstance(disk, dict) and disk.get("owner") == [aid]
                    and isinstance(disk_data, dict)
                    and isinstance(disk_data.get("models"), list)):
                at = disk.get("at")
                models = [m for m in disk_data["models"]
                          if isinstance(m, dict) and isinstance(m.get("id"), str)]
                promos = disk_data.get("promotions")
                promos = ([p for p in promos if isinstance(p, dict)]
                          if isinstance(promos, list) else [])
                cached.update(
                    at=float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0,
                    complete=True,     # 磁盘只会在完整探测成功后写入
                    data={"models": models, "promotions": promos})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        _catalogs[aid] = cached
    return _catalogs[aid]


def _select_models_cache(a: dict) -> None:
    with _models_lock:
        _models_cache.update(_catalog_for(a))


def fetch_catalog(a: dict, wait: bool = True) -> dict:
    """按账号缓存；切换时可立即复用目录，旧账号的后台请求不会污染新账号。"""
    aid = account_id(a)
    with _models_lock:
        cached = _catalog_for(a)
        if _auth is None or account_id(_auth) == aid:
            _models_cache.update(cached)
        data, age = cached["data"], time.time() - cached["at"]
        revision = cached.get("revision", 0)
    if data and age < MODELS_TTL:
        return data
    if data or not wait:
        _refresh_catalog_bg(a)
        return data or {"models": [], "promotions": []}
    return _fetch_catalog_locked(a, revision)


def _refresh_catalog_bg(a: dict) -> None:
    aid = account_id(a)
    with _models_lock:
        if aid in _models_refreshing:
            return
        cached = _catalog_for(a)
        now = time.time()
        if cached["data"] and now - cached["at"] < MODELS_TTL:
            return
        # 目录为空且上次探测刚失败过：退避，避免每个请求都新起一轮探测
        if not cached["data"] and now - (cached.get("attemptAt") or 0.0) < CATALOG_RETRY_BACKOFF:
            return
        _models_refreshing.add(aid)
        revision = cached.get("revision", 0)

    def run():
        try:
            _fetch_catalog_locked(a, revision)
        except Exception as e:
            log("模型目录后台刷新失败：%s" % e)
        finally:
            with _models_lock:
                _models_refreshing.discard(aid)

    try:
        threading.Thread(target=run, daemon=True, name="catalog-refresh").start()
    except RuntimeError as e:
        # 线程创建失败必须清标记，否则该账号再也不会自动刷新目录
        with _models_lock:
            _models_refreshing.discard(aid)
        log("模型目录后台刷新线程启动失败：%s" % e)


def _parallel(routes) -> list:
    """并发跑几个探测，返回 [(结果, 异常)]。用裸线程代替 concurrent.futures（省一次重量级导入）。"""
    out: list = [(None, None)] * len(routes)

    def run(i, fn):
        try:
            out[i] = (fn(), None)
        except Exception as e:  # noqa: BLE001
            out[i] = (None, e)

    ts = [threading.Thread(target=run, args=(i, fn), daemon=True) for i, (_, fn) in enumerate(routes)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def clear_models_cache(a: dict | None = None) -> None:
    with _models_lock:
        if a is None:
            _catalogs.clear()
            _models_refreshing.clear()
            _models_cache.update({"at": 0.0, "data": None, "owner": None})
        else:
            aid = account_id(a)
            _catalogs[aid] = {"at": 0.0, "data": None, "owner": [aid], "complete": False}
            _models_refreshing.discard(aid)
            if _auth is None or account_id(_auth) == aid:
                _models_cache.update(_catalogs[aid])


def _save_models_disk() -> None:
    try:
        payload = {"owner": _models_cache["owner"], "at": _models_cache["at"],
                   "data": _models_cache["data"]}
        _write_private(MODELS_CACHE_FILE, json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        log("模型目录缓存落盘失败：%s" % e)


def _fetch_catalog_locked(a: dict, revision: int | None = None) -> dict:
    """同步、后台与手动刷新共用一次探测；已完成的排队刷新不重复探测。"""
    def query():
        with _models_lock:
            cached = _catalog_for(a)
            if revision is not None and cached.get("revision", 0) != revision:
                return cached["data"] or {"models": [], "promotions": []}
        result = _fetch_catalog_now(a)
        with _models_lock:
            cached = _catalog_for(a)
            cached["revision"] = cached.get("revision", 0) + 1
        return result
    return _shared_queries.run(("catalog", account_id(a)), query)


CATALOG_RETRY_BACKOFF = 30   # 目录为空且探测全失败时的最短重试间隔（秒）


def _merge_models(old: list, new: list) -> list:
    """按 id 合并新旧目录：新条目优先，旧目录里仍存在的模型保留。

    用于「部分探测路由失败」的场景——残缺并集不能直接覆盖完整目录。
    """
    out: list = []
    seen: set = set()
    for m in list(new) + list(old):
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(m)
    return out


def _merge_promotions(old: list, new: list) -> list:
    out: list = []
    seen: set = set()
    for p in list(new) + list(old):
        if not isinstance(p, dict):
            continue
        key = (p.get("label"), p.get("discountedCredits"), tuple(p.get("modelIds") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _fetch_catalog_now(a: dict) -> dict:
    now = time.time()
    owner = [account_id(a)]
    routes = [
        ("v3/desktop", lambda: _probe_config(a, MODELS_PATH, desktop_ua(a))),
        ("v3/ide", lambda: _probe_config(a, MODELS_PATH, IDE_UA)),
        ("v3/cli", lambda: _probe_config(a, MODELS_PATH, CLI_UA)),
        ("enterprise", lambda: _probe_enterprise(a)),
    ]

    merged: dict[str, dict] = {}
    promotions: list[dict] = []
    promo_seen: set = set()
    errors: list[str] = []
    core_errors = 0          # 只有三路 /v3/config 决定「目录是否完整」
    results = _parallel(routes)
    for name, (payload, exc) in zip([n for n, _ in routes], results):   # 按提交顺序 → 输出稳定
        core = name != "enterprise"
        if exc is not None:
            errors.append("%s(%s)" % (name, exc))
            if core:
                core_errors += 1
            continue
        if core and isinstance(payload, dict):
            for p in payload.get("modelPromotions") or []:
                np = _norm_promotion(p)
                if not np:
                    continue
                # 三路 UA 会各自下发同一份优惠，按内容去重
                key = (np["label"], np["discountedCredits"], tuple(np["modelIds"]))
                if key in promo_seen:
                    continue
                promo_seen.add(key)
                promotions.append(np)
        entries = _resolve_models_array(payload)
        if entries is None:
            errors.append("%s(找不到模型数组)" % name)
            if core:
                core_errors += 1
            continue
        for m in entries:
            entry = _build_entry(m, now)
            if entry and entry["id"] not in merged:
                merged[entry["id"]] = entry

    models = list(merged.values())
    if errors:
        log("模型目录部分路由失败：%s" % "；".join(errors))
    # 企业端点在个人账号上本来就可能没有模型，不能因此判定目录不完整；
    # 否则个人账号会永远是 complete=False，默认模型回退彻底失效。
    complete = core_errors == 0
    with _models_lock:
        cached = _catalog_for(a)
        old = cached.get("data") or {}
        if models and errors:
            # 部分路由失败时不能整体替换：与旧目录取并集，
            # 否则失败路由独有的模型和优惠会消失 10 分钟，并被写进磁盘带过重启。
            models = _merge_models(old.get("models") or [], models)
            promotions = _merge_promotions(old.get("promotions") or [], promotions)
        if models:
            cached.update(at=now, complete=complete,
                          data={"models": models, "promotions": promotions})
            log("模型目录已刷新：%d 个模型%s" % (
                len(models),
                ("（部分路由失败，已与旧目录合并%s）" %
                 ("，目录标记为不完整" if not complete else "，仅可选端点失败"))
                if errors else ""))
        elif cached["data"]:
            log("模型目录探测全部失败，沿用该账号上次缓存")
            cached["at"] = now - MODELS_TTL + 60
        else:
            cached["attemptAt"] = now      # 空目录也要记退避，否则每个请求都会重新探测
        if _auth is None or account_id(_auth) == owner[0]:
            _models_cache.update(cached)
            # 核心路由残缺时不落盘：磁盘保留上一次完整结果，重启后不会从残缺开始
            if cached["data"] and complete:
                _save_models_disk()
        return cached["data"] or {"models": [], "promotions": []}


def fetch_models(a: dict) -> list[dict]:
    """/v1/models 对外输出：应用用户选择的模型子集，并附带别名条目。"""
    reload_selection_if_changed()
    selected = selection_for_account(a)
    cat = fetch_catalog(a)
    discounted = {}
    for promo in cat.get("promotions") or []:
        factor = _parse_factor(promo.get("discountedCredits"))
        if factor is not None:
            for mid in promo.get("modelIds") or []:
                discounted.setdefault(mid, factor)
    models = [m for m in (cat.get("models") or [])
              if isinstance(m, dict) and m.get("id")
              and (m["id"] in discounted or _parse_factor(m.get("credits")) is not None)]
    if selected or _enforce:
        chosen = set(selected)
        picked = [m for m in models if m["id"] in chosen]
        if not picked and models and not _enforce:
            _warn_once("selection.json 里选中的模型在上游全部消失，/v1/models 暂时返回全量")
        models = picked if _enforce else (picked or models)
    if _aliases:
        known = {m["id"] for m in models}
        by_id = {m["id"]: m for m in models}
        extra = []
        for alias, target in _aliases.items():
            if alias not in known and target in by_id:
                e = dict(by_id[target], id=alias)
                e["alias_of"] = target
                extra.append(e)
        models = models + extra
    return models


def resolve_model(name: str, a: dict) -> tuple[str, str | None]:
    """把客户端给的模型名解析成上游真实模型。返回 (模型, 错误信息或 None)。

    顺序：别名 → 原名 → 默认模型。enforce 打开时只允许已勾选的模型。
    """
    reload_selection_if_changed()
    selected = selection_for_account(a)
    if name is not None and not isinstance(name, str):
        return "", "model 必须是字符串"
    name = (name or "").strip()
    if name in _aliases:
        name = _aliases[name]
    if not name and _default_model:
        name = _default_model
    if not name:
        return "", "必须提供 model，或先设置默认模型"
    if not (_enforce or _default_model):
        return name, None                       # 默认行为：原样透传，不额外探测目录
    cat = fetch_catalog(a, wait=False)["models"]
    known = {m["id"] for m in cat if isinstance(m, dict) and m.get("id")}
    with _models_lock:
        complete = bool(_catalog_for(a).get("complete"))
    # 只有「目录可信 + 客户端没有显式启用该模型」时才回退默认模型。
    # 目录残缺（部分探测失败）或用户明确勾选了该模型时静默改写，会让客户端
    # 花着 A 模型的钱拿到 B 模型的回答，且从结果上完全看不出来。
    if (known and complete and name not in known and _default_model
            and name not in selected and _default_model != name):
        log("模型 %s 不在目录里，改用默认模型 %s" % (name, _default_model))
        name = _default_model
    if _enforce and name not in selected:
        return "", ("模型 '%s' 未启用（已启用：%s）"
                    % (name, ", ".join(selected) or "无"))
    return name, None


# ---------------------------------------------------------------------------
# 模型选择（按账号持久化到 selection.json；空 = 不过滤）
#   {"models": [...], "aliases": {"gpt-4o": "deepseek-v4.1-flash"},
#    "default_model": "deepseek-v4.1-flash", "enforce": false}
# 文件改动会自动热加载，不用重启。
# ---------------------------------------------------------------------------
def load_selection() -> None:
    """仅在完整读取有效配置后更新内存，编辑中的无效文件不会解除模型限制。"""
    global _selection, _aliases, _default_model, _enforce, _selection_mtime
    global _account_selections, _selection_owner, _selection_scoped
    with _lock, _selection_lock:
        try:
            mt = os.path.getmtime(SELECTION_FILE)
            with open(SELECTION_FILE, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, (dict, list)):
                raise ValueError("配置必须是对象或模型数组")
            ids = raw.get("models", []) if isinstance(raw, dict) else raw
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError("models 必须是字符串数组")
            raw = raw if isinstance(raw, dict) else {}
            al = raw.get("aliases", {})
            dm = raw.get("default_model", "")
            enforce = raw.get("enforce", False)
            if not isinstance(al, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in al.items()):
                raise ValueError("aliases 必须是模型名称之间的映射")
            if not isinstance(dm, str) or not isinstance(enforce, bool):
                raise ValueError("默认模型或强制筛选开关的类型不正确")
            profiles = raw.get("accounts", {})
            owner = raw.get("activeAccountId", "")
            if not isinstance(owner, str) or not isinstance(profiles, dict) or any(
                    not isinstance(key, str) or not key or not isinstance(value, list) or
                    any(not isinstance(i, str) for i in value) for key, value in profiles.items()):
                raise ValueError("账号模型勾选配置格式不正确")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _warn_once("模型配置读取失败，保留原配置：%s" % e)
            return
        selected = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
        profiles = {key: list(dict.fromkeys(i.strip() for i in value if i.strip()))
                    for key, value in profiles.items()}
        active = account_id(_auth) if _auth else ""
        scoped = "accounts" in raw
        if not scoped and active:
            profiles[active] = selected
        elif owner:
            profiles[owner] = selected
        _selection = list(profiles.get(active, [])) if scoped else selected
        _account_selections, _selection_owner, _selection_scoped = profiles, active, scoped
        _aliases = {k.strip(): v.strip() for k, v in al.items() if k.strip() and v.strip()}
        _default_model, _enforce, _selection_mtime = dm.strip(), enforce, mt


def reload_selection_if_changed() -> None:
    """最多每 2 秒 stat 一次，文件被手改后自动生效。"""
    global _selection_checked
    now = time.time()
    if now - _selection_checked < 2:
        return
    _selection_checked = now
    try:
        mt = os.path.getmtime(SELECTION_FILE)
    except OSError:
        return
    if mt != _selection_mtime:
        load_selection()
        log("selection.json 已变化，已热加载")


def resolve_api_key(explicit: str = "", ephemeral: bool = False) -> tuple[str, str, bool]:
    """确定本地访问密钥，返回 (密钥, 来源, 是否已持久化)。

    优先级：--api-key / WB_API_KEY > 密钥文件 > 新生成。
    默认把新生成的密钥写进密钥文件，重启后不变，客户端不用跟着改配置；
    ephemeral=True 时每次启动都用一次性随机密钥，不读也不写文件。
    
    返回的第三个值标记密钥是否已持久化：
    - True: 来自文件或刚生成并落盘，重启后自动可用
    - False: 仅存在于命令行/环境变量，重启后会丢失
    """
    if explicit:
        return explicit, "参数/环境变量", False
    if ephemeral:
        return "sk-" + uuid.uuid4().hex, "本次随机生成（不落盘）", False
    try:
        with open(API_KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key, API_KEY_FILE, True
    except FileNotFoundError:
        pass
    key = "sk-" + uuid.uuid4().hex
    _write_private(API_KEY_FILE, key + "\n")
    return key, "新生成并保存到 " + API_KEY_FILE, True


def _mask_api_key(key: str) -> str:
    """日志里只显示密钥前缀与后缀，避免完整密钥进入日志/监控系统。"""
    if not isinstance(key, str) or not key:
        return "***"
    if len(key) > 15:
        return key[:7] + "..." + key[-4:]
    return "***"


def selection_for_account(a: dict) -> list[str]:
    """以请求捕获的账号读取勾选，切换账号不影响在途请求。"""
    with _lock, _selection_lock:
        aid = account_id(a)
        if _auth and account_id(_auth) == aid:
            return list(_selection)
        return list(_account_selections.get(aid, []))


def save_selection(ids: list | None = None, **extra) -> dict:
    """串行保存配置；落盘成功后才更新内存，部分更新不会覆盖并发修改。"""
    global _selection, _aliases, _default_model, _enforce, _selection_mtime
    global _account_selections, _selection_owner, _selection_scoped
    with _lock, _selection_lock:
        aid = account_id(_auth) if _auth else ""
        if extra.get("expected_account_id") is not None and extra["expected_account_id"] != aid:
            raise ValueError("当前账号已变化，请刷新页面后重新勾选模型")
        selected = list(_selection) if ids is None else list(dict.fromkeys(
            i.strip() for i in ids if isinstance(i, str) and i.strip()))
        aliases = dict(_aliases)
        if isinstance(extra.get("aliases"), dict):
            aliases = {k.strip(): v.strip() for k, v in extra["aliases"].items()
                       if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
        default = extra["default_model"].strip() if isinstance(extra.get("default_model"), str) else _default_model
        enforce = extra["enforce"] if isinstance(extra.get("enforce"), bool) else _enforce
        payload = {"models": selected, "aliases": aliases, "default_model": default, "enforce": enforce}
        profiles = {key: list(value) for key, value in _account_selections.items()}
        if aid:
            profiles[aid] = list(selected)
        payload.update(accounts=profiles, activeAccountId=aid)
        _write_private(SELECTION_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        _selection, _aliases, _default_model, _enforce = selected, aliases, default, enforce
        _account_selections, _selection_owner, _selection_scoped = profiles, aid, True
        _selection_mtime = os.path.getmtime(SELECTION_FILE)
        return {"ok": True, "accountId": aid, "selected": list(selected), "aliases": dict(aliases),
                "defaultModel": default, "enforce": enforce}


# ---------------------------------------------------------------------------
# 用量记录：每个请求一行 usage.jsonl，控制台据此估算积分消耗
# ---------------------------------------------------------------------------
_usage_lock = threading.Lock()
_usage_tot: dict = {"day": "", "today": {}, "month": "", "this_month": {}}


def _credit_factor(model: str, account: dict | None = None) -> float | None:
    """当前生效积分倍率（限时优惠优先）。拿不到返回 None。"""
    with _models_lock:
        cached = _catalogs.get(account_id(account), {}) if account else _models_cache
        data = cached.get("data") or {}
    for p in data.get("promotions") or []:
        if not isinstance(p, dict):
            continue
        if model in (p.get("modelIds") or []):
            f = _parse_factor(p.get("discountedCredits"))
            if f is not None:
                return f
    for m in data.get("models") or []:
        if isinstance(m, dict) and m.get("id") == model:
            return _parse_factor(m.get("credits"))
    return None


def _parse_factor(s) -> float | None:
    if isinstance(s, bool):
        return None
    try:
        value = float(str(s).strip().lstrip("xX×").strip())
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def _bump(bucket: dict, rec: dict) -> None:
    """累计一条用量记录。字段类型一律归一，损坏的 replay 数据不能把统计打崩。"""
    key = rec.get("model")
    if not isinstance(key, str):
        key = ""
    m = bucket.setdefault(key, {"requests": 0, "errors": 0, "input_tokens": 0,
                                "output_tokens": 0, "weighted_k": 0.0, "credits": 0.0})
    m["requests"] += 1
    if to_int(rec.get("status")) >= 400:
        m["errors"] += 1
    tokens_in, tokens_out = to_int(rec.get("in")), to_int(rec.get("out"))
    m["input_tokens"] += tokens_in
    m["output_tokens"] += tokens_out
    credit = rec.get("credit")
    if isinstance(credit, (int, float)) and not isinstance(credit, bool):
        m["credits"] = round(m.get("credits", 0.0) + credit, 4)   # 上游 usage 里的真实积分
    factor = rec.get("factor")
    if isinstance(factor, (int, float)) and not isinstance(factor, bool):
        # 加权用量 = 积分倍率 × 千 tokens。上游真实计费口径未公开，这只用于横向比较，不是账单
        m["weighted_k"] = round(m.get("weighted_k", 0.0) + factor * (tokens_in + tokens_out) / 1000, 3)


def record_usage(path: str, model: str, status: int, usage: dict | None, secs: float,
                 ttft: float | None, stream: bool, error: str = "", account: dict | None = None) -> None:
    u = usage if isinstance(usage, dict) else {}
    rec = {"ts": int(time.time()), "ep": path.rsplit("/", 1)[-1], "model": model or "",
           "status": status, "stream": stream,
           "in": to_int(u.get("prompt_tokens")), "out": to_int(u.get("completion_tokens")),
           "secs": round(secs, 2), "factor": _credit_factor(model, account)}
    if account:
        rec.update(accountId=account_id(account), accountName=account_name(account), realm=realm_of(account))
    if error:
        # 只保存错误摘要；不记录请求正文，并去除当前凭证和本地密钥。
        with _lock:
            saved = list(_accounts.values()) + [_auth or {}, account or {}]
            secrets = [LOCAL_API_KEY] + [
                a.get(k) for a in saved
                for k in ("accessToken", "refreshToken", "deviceToken")]
        for secret in secrets:
            if secret:
                error = error.replace(secret, "[已隐藏]")
        rec["error"] = error[:1000]
    cr = u.get("credit")
    if isinstance(cr, (int, float)) and not isinstance(cr, bool):
        rec["credit"] = cr
    if ttft is not None:
        rec["ttft"] = round(ttft, 2)
    day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    with _usage_lock:
        _roll_usage(day, month)
        _bump(_usage_tot["today"], rec)
        _bump(_usage_tot["this_month"], rec)
        try:
            f = _usage_fh()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()                    # 只 flush 到系统缓冲，不 fsync：每请求省 ~0.3ms 的 open/close
        except Exception as e:  # noqa: BLE001
            _usage_fh_state[:] = [None, None]
            _warn_once("用量记录写入失败：%s" % e)


_usage_fh_state: list = [None, None]     # [文件对象, 路径]


def close_usage() -> None:
    with _usage_lock:
        f = _usage_fh_state[0]
        _usage_fh_state[:] = [None, None]
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass


def _usage_fh():
    """调用方持有 _usage_lock。usage.jsonl 保持打开，路径变了（测试 / --data-dir）就重开。"""
    f, path = _usage_fh_state
    if f is None or path != USAGE_FILE:
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        f = open(USAGE_FILE, "a", encoding="utf-8")
        _usage_fh_state[:] = [f, USAGE_FILE]
    return f


def _roll_usage(day: str, month: str) -> None:
    if _usage_tot["day"] != day:
        _usage_tot.update(day=day, today={})
    if _usage_tot["month"] != month:
        _usage_tot.update(month=month, this_month={})


def load_usage_totals() -> None:
    """启动时从 usage.jsonl 回放本月数据（只读尾部 4MB，足够覆盖一个月的个人用量）。"""
    day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    with _usage_lock:
        _usage_tot.update(day=day, today={}, month=month, this_month={})
        try:
            size = os.path.getsize(USAGE_FILE)
            with open(USAGE_FILE, "rb") as f:
                if size > 4 << 20:
                    f.seek(size - (4 << 20))
                    f.readline()
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # 文件可能被手工编辑/别的程序写坏：单行异常只跳过该行，不能让启动失败
            if not isinstance(rec, dict):
                continue
            try:
                t = time.localtime(to_int(rec.get("ts")))
                if time.strftime("%Y-%m", t) != month:
                    continue
                _bump(_usage_tot["this_month"], rec)
                if time.strftime("%Y-%m-%d", t) == day:
                    _bump(_usage_tot["today"], rec)
            except Exception:  # noqa: BLE001 - 单行损坏不影响其余统计
                continue


def usage_summary() -> dict:
    day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    with _usage_lock:
        _roll_usage(day, month)
        return json.loads(json.dumps({"today": _usage_tot["today"],
                                      "month": _usage_tot["this_month"],
                                      "day": day, "monthName": month}))


# ===========================================================================
# 十、HTTP 处理
# ===========================================================================
FALLBACK_UI = """<!doctype html><meta charset="utf-8">
<title>wb2api-lite</title>
<body style="font-family:system-ui;padding:40px;line-height:1.7">
<h2>缺少 ui.html</h2>
<p>请把 <code>ui.html</code> 放在与本脚本同一目录：<br><code>%s</code></p>
<p>代理本身工作正常，可直接用 <code>/v1</code> 接口。</p>
</body>""" % UI_FILE

# OpenAI 风格的 error.type / error.code：SDK 依据它们决定是否自动重试
_ERR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
              403: "permission_error", 404: "not_found_error", 408: "timeout",
              411: "invalid_request_error", 413: "invalid_request_error",
              415: "invalid_request_error", 429: "rate_limit_error",
              503: "service_unavailable"}
_ERR_CODES = {401: "invalid_api_key", 404: "model_not_found", 413: "request_too_large",
              429: "rate_limit_exceeded"}


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


def _host_name(host: str) -> str:
    """Host 头去掉端口：`127.0.0.1:8787` → `127.0.0.1`，`[::1]:8787` → `[::1]`。"""
    host = (host or "").strip()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _key_eq(got: str) -> bool:
    return bool(LOCAL_API_KEY) and hmac.compare_digest(
        (got or "").encode("utf-8"), LOCAL_API_KEY.encode("utf-8"))


def _console_cookie() -> str:
    """控制台会话 cookie 值：由密钥派生，不把密钥本身放进 cookie / URL。"""
    return hmac.new(LOCAL_API_KEY.encode("utf-8"), b"wb2api-console", hashlib.sha256).hexdigest()


def admin_state() -> dict:
    """网页首屏所需的全部状态（不含模型列表，模型走单独接口）。"""
    with _lock, _selection_lock:
        a = _auth
        accounts = dict(_accounts)
        if a:
            accounts[account_id(a)] = a
        summaries = [account_summary(value) for value in accounts.values()]
        selected = list(_selection)
        aliases, default_model, enforce = dict(_aliases), _default_model, _enforce
    logged_in = bool(a and a.get("accessToken"))
    host = "127.0.0.1" if _is_loopback(_BIND_HOST) or _BIND_HOST in ("0.0.0.0", "::") \
        else _BIND_HOST
    out = {
        "loggedIn": logged_in,
        "baseUrl": "http://%s:%d/v1" % (host, _PORT),
        "apiKey": LOCAL_API_KEY,
        "upstream": upstream_base(a),
        "realm": realm_of(a),
        "activeAccountId": account_id(a) if a else "",
        "accounts": summaries,
        "authFile": os.path.abspath(AUTH_FILE),
        "selectionFile": os.path.abspath(SELECTION_FILE),
        "selected": selected,
        "aliases": aliases,
        "defaultModel": default_model,
        "enforce": enforce,
        "modelsCached": bool(_models_cache["data"]),
        "usage": usage_summary(),
    }
    if logged_in:
        out["account"] = account_summary(a)
        with _points_lock:
            p = dict(_points_state.get(account_id(a)) or {})
        out["points"] = {k: v for k, v in p.items() if k != "at"}
    out["autoCheckin"] = _auto_checkin
    out["startup"] = startup_status()
    try:
        out["pool"] = pool_status()
    except Exception:  # noqa: BLE001 - 池统计失败不能拖垮整个面板首屏
        out["pool"] = None
    return out


def health() -> dict:
    a = _auth
    exp = (a or {}).get("expiresAt") or 0
    with _inflight_lock:
        inflight = _inflight
    with _gzip_stats_lock:
        gzip_requests = _gzip_stats["requests"]
        gzip_saved = _gzip_stats["saved_bytes"]
        gzip_enabled = _gzip_ok and upstream_base(a) not in _gzip_disabled_bases
        gzip_disabled = sorted(_gzip_disabled_bases)
    out = {"status": "ok", "upstream": upstream_base(a), "loggedIn": bool(a),
           "uptime": int(time.time() - _started_at), "inflight": inflight,
           "upstreamConns": {"created": POOL.created, "reused": POOL.reused},
           "gzip": {"enabled": gzip_enabled, "disabledBases": gzip_disabled,
                    "requests": gzip_requests,
                    "savedKB": gzip_saved // 1024}}
    if exp:
        out["tokenExpiresIn"] = int(exp - time.time())
    if a and "_lastRefreshOk" in a:
        out["lastRefreshOk"] = a["_lastRefreshOk"]
    if _models_cache.get("at"):
        out["modelsAge"] = int(time.time() - _models_cache["at"])
    if a and exp and exp - time.time() < 0:
        out["status"] = "degraded"
    # 账号池概况：不加 --verbose 也能一眼看出是否还有可用账号
    try:
        st = pool_status()
        realms = {realm: {"count": st[realm]["count"], "available": st[realm]["available"]}
                  for realm in REALMS}
        realms["enabled"] = st["enabled"]
        realms["switches"] = st["stats"].get("switch", 0)
        out["pool"] = realms
    except Exception:  # noqa: BLE001 - 健康检查不能因为池统计出错而失败
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wb2api-lite"
    timeout = CLIENT_TIMEOUT          # 慢速 / 空闲连接不会无限占用线程

    def setup(self):
        super().setup()
        try:   # 关掉 Nagle：SSE 小帧立即发出，不攒包
            import socket
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:  # noqa: BLE001
            pass

    # ---- 基础工具 ----
    def _json(self, code: int, obj, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, etype: str = "",
               retry_after: str = "") -> None:
        self._last_error = message
        h = {"Retry-After": retry_after} if retry_after else None
        if self._route() == "/v1/messages":
            typ = {400: "invalid_request_error", 401: "authentication_error",
                   403: "permission_error", 404: "not_found_error",
                   411: "invalid_request_error", 413: "request_too_large",
                   415: "invalid_request_error", 429: "rate_limit_error",
                   529: "overloaded_error"}.get(code, "api_error")
            return self._json(code, {"type": "error",
                                    "error": {"type": typ, "message": message}}, h)
        etype = etype or _ERR_TYPES.get(code) or ("server_error" if code >= 500 else
                                                  "upstream_error")
        self._json(code, {"error": {"message": message, "type": etype, "param": None,
                                    "code": _ERR_CODES.get(code) or str(code)}}, h)

    def _open_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

    def _chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        if data:
            self.wfile.write(b"%x\r\n%b\r\n" % (len(data), data))   # 一次拼接、一次写
            self.wfile.flush()

    def _end_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass

    def _authorized(self) -> bool:
        if not LOCAL_API_KEY:
            return True
        return self._bearer_ok() or _key_eq(self.headers.get("x-api-key") or "")

    def _bearer_ok(self) -> bool:
        auth = (self.headers.get("Authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            auth = auth[7:].strip()
        return _key_eq(auth)

    def _body(self) -> dict:
        """读取并解析 JSON 体。非对象 / 非法 JSON 返回 None；空体返回 {}。
        超限时不读，标记关闭连接（否则残留字节会污染 keep-alive 上的下一个请求）。"""
        self._body_err = None
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._drain_chunked()
            self.close_connection = True
            self._body_err = (411, "不支持分块请求正文，请提供 Content-Length")
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            return {}                       # 无 Content-Length 按 HTTP 语义视为无正文
        values = {v.strip() for v in lengths}
        if len(values) != 1:
            # 多个冲突长度无法确定正文边界，必须断开，否则残留字节会被当成下一个请求
            self.close_connection = True
            self._body_err = (400, "Content-Length 重复且不一致")
            return None
        try:
            n = int(values.pop())
            if n < 0:
                raise ValueError
        except (TypeError, ValueError):
            self.close_connection = True
            self._body_err = (400, "Content-Length 必须是非负整数")
            return None
        if n > MAX_BODY:
            self.close_connection = True
            self._body_err = (413, "请求正文过大（最多 %d MB）" % (MAX_BODY >> 20))
            return None
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            # 连接在正文读完前就断了：正文边界已不可信，残留字节可能被当成下一个
            # keep-alive 请求，必须断开并拒绝，不能当作空正文继续处理。
            self.close_connection = True
            self._body_err = (400, "请求正文不完整（连接提前关闭）")
            return None
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return obj if isinstance(obj, dict) else None

    def _drain_chunked(self, max_bytes: int = 1024 * 1024, timeout: float = 0.5) -> None:
        """尽力排空被拒绝的 chunked 正文，减少客户端收不到 411 的概率。"""
        connection = getattr(self, "connection", None)
        if connection is None:
            return
        old_timeout = connection.gettimeout()
        total = 0
        try:
            connection.settimeout(timeout)
            while total <= max_bytes:
                line = self.rfile.readline(8192)
                if not line or not line.endswith(b"\n"):
                    return
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    return
                if size < 0 or total + size > max_bytes:
                    return
                total += size
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                if len(self.rfile.read(size + 2)) != size + 2:
                    return
        except (OSError, ValueError):
            return
        finally:
            try:
                connection.settimeout(old_timeout)
            except OSError:
                pass

    def log_message(self, fmt, *args):  # 静音默认访问日志，只留我们自己的
        return

    def _drain_chunked(self, max_bytes: int = 1024 * 1024,
                       timeout: float = 0.5) -> None:
        """有界尝试排空被拒绝的 chunked 正文，超限或超时即停止并关闭连接。"""
        connection = getattr(self, "connection", None)
        if connection is None:
            return
        old_timeout = connection.gettimeout()
        total = 0
        try:
            connection.settimeout(timeout)
            while total <= max_bytes:
                line = self.rfile.readline(8192)
                if not line or not line.endswith(b"\n"):
                    return
                if total + len(line) > max_bytes:
                    return
                total += len(line)
                size_text = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    return
                if size < 0 or total + size + 2 > max_bytes:
                    return
                if size == 0:
                    trailer_bytes = 0
                    while trailer_bytes < 65536:
                        trailer = self.rfile.readline(8192)
                        if not trailer:
                            return
                        trailer_bytes += len(trailer)
                        if trailer_bytes > 65536:
                            return
                        if trailer in (b"\r\n", b"\n"):
                            return
                    return
                chunk = self.rfile.read(size + 2)
                if len(chunk) != size + 2 or not chunk.endswith(b"\r\n"):
                    return
                total += len(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                connection.settimeout(old_timeout)
            except OSError:
                pass

    def _route(self) -> str:
        """取归一化后的路由路径。

        经企业/系统代理（HTTP_PROXY）转发时，客户端可能发出 **绝对形式** 请求行
        （`POST http://host/v1/chat/completions`），此时 self.path 带完整 URL。
        httpx / openai SDK 在 trust_env 下就是这种行为，必须兼容。
        另外容忍少写 /v1 的 base_url（`/chat/completions`）与结尾斜杠。
        """
        p = self.path
        if p.startswith("http://") or p.startswith("https://"):
            i = p.find("/", p.find("://") + 3)
            p = p[i:] if i != -1 else "/"
        p = p.split("?", 1)[0]
        if len(p) > 1:
            p = p.rstrip("/")
        if p in ("/chat/completions", "/responses", "/models", "/messages"):
            p = "/v1" + p
        return p

    def _query(self, key: str, default: str = "") -> str:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return (q.get(key) or [default])[0]

    def _cookie_ok(self) -> bool:
        c = self.headers.get("Cookie") or ""
        want = _console_cookie()
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "wb2api_console" and hmac.compare_digest(v.encode(), want.encode()):
                return True
        return False

    def _admin_ok(self, allow_query: bool = False) -> bool:
        """管理接口鉴权。

        绑定回环地址时免密，但要挡住两类浏览器侧攻击：
          * DNS rebinding：Host 必须是回环名，否则恶意域名能读到 /admin/state 里的密钥；
          * CSRF：带 Origin 时必须与 Host 同源。
        绑定到其它地址时必须带本地密钥（Bearer 或首次 ?pwd= 换来的会话 cookie），
        否则同网段任何人打开页面就能拿到你的账号凭证。
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if origin and origin != "null":
            if urllib.parse.urlsplit(origin).netloc.lower() != host.lower():
                return False
        elif origin == "null":
            return False
        if _is_loopback(_BIND_HOST):
            return _is_loopback(_host_name(host))
        if not LOCAL_API_KEY:
            return False
        return (self._cookie_ok() or self._bearer_ok()
                or (allow_query and _key_eq(self._query("pwd"))))

    def _serve_ui(self) -> None:
        # 首次带 ?pwd= 打开：校验后种 HttpOnly cookie 并 302 到干净地址，密钥不留在地址栏 / 历史里
        if self._query("pwd") and _key_eq(self._query("pwd")):
            self.send_response(302)
            self.send_header("Set-Cookie", "wb2api_console=%s; Path=/; HttpOnly; SameSite=Strict"
                             % _console_cookie())
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            with open(UI_FILE, "rb") as f:
                body = f.read()
        except Exception:  # noqa: BLE001
            body = FALLBACK_UI.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _discard_request_body(self) -> None:
        """GET / OPTIONS 等不应带正文；带了就必须读完或断开，否则残留字节会被当成下一个请求。"""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._drain_chunked()
            self.close_connection = True
            return
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            return
        values = {v.strip() for v in lengths}
        if len(values) != 1:
            self.close_connection = True
            return
        try:
            n = int(values.pop())
            if n < 0:
                raise ValueError
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if n > MAX_BODY:
            self.close_connection = True
            return
        if n:
            try:
                self.rfile.read(n)
            except Exception:  # noqa: BLE001
                self.close_connection = True

    # ---- 路由 ----
    def do_OPTIONS(self):  # noqa: N802 - 不开放 CORS：跨站预检一律拒绝
        self._discard_request_body()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self._discard_request_body()
        path = self._route()
        if path in ("/", "/index.html"):
            if not _SERVE_UI:
                return self._error(404, "网页控制台和管理接口已关闭（--no-ui）", "invalid_request_error")
            if not self._admin_ok(allow_query=True):
                return self._error(401, "需要面板密钥：请在地址后加 ?pwd=<密钥>",
                                   "invalid_request_error")
            return self._serve_ui()
        if path == "/healthz":
            return self._json(200, health())

        if path.startswith("/admin/"):
            if not _SERVE_UI:
                return self._error(404, "网页控制台和管理接口已关闭（--no-ui）", "invalid_request_error")
            if not self._admin_ok():
                return self._error(401, "未授权，请提供有效的访问密钥", "invalid_request_error")
            if path == "/admin/state":
                reload_selection_if_changed()
                return self._json(200, admin_state())
            if path == "/admin/usage":
                return self._json(200, usage_summary())
            if path == "/admin/points":
                try: a = ensure_auth()
                except Exception as e: return self._error(400, str(e))
                return self._json(200, account_points(a, True))
            if path == "/admin/startup":
                return self._json(200, startup_status())
            if path == "/admin/pool":
                return self._json(200, pool_status())
            if path == "/admin/models":
                try:
                    a = ensure_auth()
                except Exception as e:  # noqa: BLE001
                    return self._error(400, str(e))
                cat = fetch_catalog(a)
                return self._json(200, {
                    "accountId": account_id(a),
                    "models": cat["models"],
                    "promotions": cat["promotions"],
                    "selected": selection_for_account(a),
                })
            if path == "/admin/login/poll":
                return self._admin_login_poll()
            return self._error(404, "接口不存在：" + path, "invalid_request_error")

        if path == "/v1/models" or path.startswith("/v1/models/"):
            if not self._authorized():
                return self._error(401, "访问密钥无效", "invalid_request_error")
            try:
                a = ensure_auth()
            except Exception as e:  # noqa: BLE001
                return self._error(401, "未登录：%s" % e, "authentication_error")
            models = fetch_models(a)
            if path == "/v1/models":
                return self._json(200, {"object": "list", "data": models})
            mid = urllib.parse.unquote(path[len("/v1/models/"):])
            for m in models:
                if m["id"] == mid:
                    return self._json(200, m)
            return self._error(404, "找不到模型：" + mid)
        self._error(404, "接口不存在：" + path, "invalid_request_error")

    def _admin_login_poll(self):
        """网页端轮询登录状态；完成后写凭证并返回账号信息。"""
        global _pending_login
        # 只在读取/提交共享状态时持锁；轮询上游可能阻塞数十秒，不能占住
        # 登录锁，否则账号切换和新的登录请求都会被无关的网络延迟卡住。
        with _login_lock:
            ctx = _pending_login
            if not ctx:
                return self._json(200, {"status": "expired",
                                        "message": "登录会话已失效，请重新点击登录"})
            if self._query("state") != ctx["state"]:
                return self._json(200, {"status": "expired",
                                        "message": "登录会话已被替换，请重新选择版本并登录"})
            if time.time() > ctx["deadline"]:
                _pending_login = None
                return self._json(200, {"status": "timeout", "message": "授权超时，请重试"})
        # 网络调用必须在锁外进行。多个标签页可能同时轮询，完成后的提交阶段
        # 会再次校验 ctx 身份，只有第一个成功者可以兑换并写入凭证。
        ok, tok = login_poll(ctx)
        if not ok:
            with _login_lock:
                if _pending_login is not ctx:
                    return self._json(200, {"status": "expired",
                                            "message": "登录会话已被替换，请重新选择版本并登录"})
                if time.time() > ctx["deadline"]:
                    _pending_login = None
                    return self._json(200, {"status": "timeout", "message": "授权超时，请重试"})
            return self._json(200, {"status": "pending"})

        # login_finish 也包含网络请求和文件写入。保持现有的单写者语义，
        # 但先重新确认会话仍是当前会话，避免账号切换后旧轮询覆盖新账号。
        with _login_lock:
            if _pending_login is not ctx:
                return self._json(200, {"status": "expired",
                                        "message": "登录会话已被替换，请重新选择版本并登录"})
            try:
                a, acct = login_finish(ctx, tok, AUTH_FILE)
            except Exception as e:
                return self._error(502, "保存登录状态失败，请重试：%s" % e)
            _pending_login = None
        if a:
            _refresh_catalog_bg(a)       # 登录完马上在后台拉目录，用户切到模型表时已就绪
        return self._json(200, {"status": "done", "state": admin_state(),
                                "nickname": acct.get("nickname") or ""})

    def _admin_login(self, realm: str):
        """网页端点「登录」时调用：申请设备授权并返回授权链接。"""
        global _pending_login, _login_generation
        if not isinstance(realm, str) or realm not in REALMS:
            return self._error(400, "登录版本必须为国内版（cn）或国际版（global）", "invalid_request_error")
        with _login_lock:
            _login_generation += 1
            generation = _login_generation
            _pending_login = None
        try:
            ctx = login_start(realm)
        except Exception as e:  # noqa: BLE001
            return self._error(502, "申请授权失败：%s" % e)
        ctx["deadline"] = time.time() + 300
        with _login_lock:
            if generation != _login_generation:
                return self._error(409, "已有新的登录请求，请继续最新的授权流程")
            _pending_login = ctx
        log("网页端发起登录（%s）" % ctx["label"])
        return self._json(200, {"authUrl": ctx["authUrl"], "state": ctx["state"],
                                "label": ctx["label"]})

    def do_POST(self):  # noqa: N802
        global _inflight
        path = self._route()

        if path.startswith("/admin/"):
            if not _SERVE_UI:
                self.close_connection = True
                return self._error(404, "网页控制台和管理接口已关闭（--no-ui）", "invalid_request_error")
            if not self._admin_ok():
                self.close_connection = True
                return self._error(401, "未授权，请提供有效的访问密钥", "invalid_request_error")
        elif path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
            if not self._authorized():
                self.close_connection = True
                return self._error(401, "访问密钥无效", "invalid_request_error")
        else:
            self.close_connection = True
            return self._error(404, "接口不存在：" + path, "invalid_request_error")

        # 认证通过后才读取请求体；拒绝请求关闭连接，避免未读正文污染 keep-alive。
        body = self._body()
        if self._body_err:
            return self._error(*self._body_err)

        if path.startswith("/admin/"):
            # 强制 JSON 类型：跨站 <form>/text/plain 请求无法设置它，又会触发 CORS 预检
            if "application/json" not in (self.headers.get("Content-Type") or "").lower():
                return self._error(415, "请求类型 Content-Type 必须为 application/json",
                                   "invalid_request_error")
            if body is None:
                return self._error(400, "请求正文不是有效的 JSON 对象", "invalid_request_error")
            if path == "/admin/login":
                return self._admin_login(body.get("realm") or "global")
            if path == "/admin/points/checkin":
                try: a = ensure_auth()
                except Exception as e: return self._error(400, str(e))
                return self._json(200, daily_checkin(a))
            if path == "/admin/settings":
                if not isinstance(body.get("autoCheckin"), bool):
                    return self._error(400, "自动签到开关必须是布尔值")
                try:
                    return self._json(200, set_auto_checkin(body["autoCheckin"]))
                except OSError as e:
                    return self._error(500, "保存自动签到设置失败：" + str(e))
            if path == "/admin/startup":
                if not isinstance(body.get("enabled"), bool) or "autoCheckin" in body:
                    return self._error(400, "自启接口只接受 enabled 布尔值；自动签到请使用设置接口")
                result = set_startup(body["enabled"])
                if not result.get("ok"):
                    return self._error(500, result["message"])
                return self._json(200, result)
            if path == "/admin/accounts/switch":
                global _pending_login, _login_generation
                try:
                    with _login_lock:
                        a = switch_account(body.get("accountId"))
                        _pending_login = None
                        _login_generation += 1
                except ValueError as e:
                    return self._error(400, str(e), "invalid_request_error")
                except OSError as e:
                    return self._error(500, "切换账号保存失败，仍使用原账号：%s" % e)
                fetch_catalog(a, wait=False)
                log("已热切换账号：%s（%s）" % (account_name(a) or "未命名账号", REALMS[realm_of(a)]["label"]))
                return self._json(200, {"ok": True, "state": admin_state()})
            if path == "/admin/selection":
                ids = body.get("models")
                if "models" in body and (not isinstance(ids, list) or
                        any(not isinstance(i, str) or not i.strip() for i in ids)):
                    return self._error(400, "models 必须是非空模型名称组成的数组", "invalid_request_error")
                for key, kind in (("aliases", dict), ("default_model", str), ("enforce", bool)):
                    if key in body and not isinstance(body[key], kind):
                        return self._error(400, "配置字段 %s 的类型不正确" % key, "invalid_request_error")
                if "aliases" in body and any(not isinstance(v, str) or not k.strip() or not v.strip()
                                              for k, v in body["aliases"].items()):
                    return self._error(400, "别名和目标模型都必须是非空字符串", "invalid_request_error")
                try:
                    saved = save_selection(ids, aliases=body.get("aliases"),
                                   default_model=body.get("default_model"),
                                   enforce=body.get("enforce"), expected_account_id=body.get("accountId"))
                except ValueError as e:
                    return self._error(409, str(e), "invalid_request_error")
                except OSError as e:
                    return self._error(500, "保存配置失败，原配置仍然有效：%s" % e)
                log("模型选择已更新：%d 个%s%s" % (
                    len(_selection), "，别名 %d 个" % len(_aliases) if _aliases else "",
                    "，默认 " + _default_model if _default_model else ""))
                return self._json(200, saved)
            if path == "/admin/models/refresh":
                try:
                    a = ensure_auth()
                except Exception as e:  # noqa: BLE001
                    return self._error(400, str(e))
                cat = _fetch_catalog_locked(a)
                return self._json(200, {"models": cat.get("models") or [],
                                        "accountId": account_id(a),
                                        "promotions": cat.get("promotions") or [],
                                        "selected": selection_for_account(a)})
            return self._error(404, "接口不存在：" + path, "invalid_request_error")

        if not body:
            return self._error(400, "请求正文为空，或不是有效的 JSON 对象", "invalid_request_error")
        if not _slots.acquire(timeout=30):
            return self._error(429, "并发请求过多（最多 %d 个），请稍后重试" % MAX_INFLIGHT,
                               retry_after="2")
        with _inflight_lock:
            _inflight += 1
        try:
            self._completion(path, body)
        finally:
            with _inflight_lock:
                _inflight -= 1
            _slots.release()

    def _completion(self, path: str, body: dict) -> None:
        self._last_error = ""
        try:
            validate_request(path, body)
        except ValueError as e:
            return self._error(400, str(e), "invalid_request_error")
        try:
            ensure_auth()
        except Exception as e:  # noqa: BLE001
            msg = "未登录：%s —— 打开控制台或执行 --setup 登录" % e
            return self._error(401, msg, "authentication_error")

        # 账号池：先按当前账号所在的池挑一个可用成员；池内换号只影响本次请求，
        # 不改变控制台上的当前账号。
        session_key = _pool_session_key(self)
        try:
            a = _pool_pick(realm_of(_auth), session_key) or ensure_auth()
        except Exception:  # noqa: BLE001 - 池异常时退回原行为
            a = ensure_auth()

        model, err = resolve_model(body.get("model"), a)
        if err:
            code = 404 if err.startswith("模型 '") else 400
            return self._error(code, err)
        body["model"] = model
        want_stream = body.get("stream") is True
        is_resp = path == "/v1/responses"
        is_anthropic = path == "/v1/messages"
        ctx = None
        if is_resp:
            try:
                body, ctx = responses_to_chat(body, account_id(a))
            except ValueError as e:
                return self._error(400, str(e), "invalid_request_error")
        elif is_anthropic:
            try:
                body = anthropic_to_chat(body)
            except ValueError as e:
                return self._error(400, str(e), "invalid_request_error")
        elif isinstance(body.get("n"), int) and body["n"] > 1:
            return self._error(400, "上游不支持一次生成多个候选结果（n > 1）", "invalid_request_error")

        t0 = time.time()
        acc = ChatAccumulator()
        status, ttft = 200, None
        tried: set[str] = set()
        logical_request_id = _uuid7().hex
        resp = None
        try:
            # --- 换号循环：只在写出任何字节之前换号 ---
            while True:
                payload = normalize_chat_body(dict(body), a)
                payload["model"] = model
                payload["_wb2api_request_id"] = logical_request_id
                try:
                    resp = call_upstream(payload, a)
                except UpstreamError as e:
                    status = e.status
                    if _pool_can_switch(a, e.status, tried):
                        nxt = _pool_switch(a, e.status, e.msg, session_key, tried)
                        if nxt is not None:
                            a = nxt
                            continue
                    return self._error(e.status, e.msg, retry_after=e.retry_after)
                except Exception as e:  # noqa: BLE001
                    status = 502
                    if getattr(e, "wb2api_retry_safe", False) and _pool_can_switch(a, 502, tried):
                        nxt = _pool_switch(a, 502, str(e), session_key, tried)
                        if nxt is not None:
                            a = nxt
                            continue
                    msg = "无法连接上游：%s" % e
                    return self._error(502, msg)

                if resp.status >= 400 or not is_sse(resp):
                    ra = resp.headers.get("Retry-After") or ""
                    status, msg = read_error(resp)
                    log("上游 %s：%s" % (status, msg))
                    if _pool_can_switch(a, status, tried):
                        nxt = _pool_switch(a, status, msg, session_key, tried)
                        if nxt is not None:
                            a = nxt
                            continue
                    return self._error(status, msg, retry_after=ra)
                break

            # 成功拿到可用的上游响应：记健康、绑定粘性
            _pool_mark_ok(a)
            _pool_bind(session_key, account_id(a))

            rid = "resp_" + uuid.uuid4().hex[:24]
            if want_stream:
                ttft = self._stream(path, resp, model, acc, rid, ctx, t0)
                if acc.error:
                    status = acc.error_status or 502
                    self._last_error = acc.error
            else:
                try:
                    aggregate_sse(resp, acc)
                    # Anthropic 与 Responses 都必须拿到 finish_reason，否则无法判断
                    # 文本/工具参数是否被上游截断（chat 路径按既有约定补 "stop"）。
                    if is_anthropic or is_resp:
                        if not acc.finish(0):
                            raise UpstreamError(502, "上游响应流未完整结束（缺少 finish_reason）")
                        if is_anthropic:
                            out = chat_to_anthropic(acc, model, "msg_" + uuid.uuid4().hex[:24])
                except UpstreamError as e:
                    status = e.status
                    return self._error(e.status, e.msg)
                except Exception as e:  # noqa: BLE001
                    status = 502
                    msg = "上游响应流中断：%s" % e
                    return self._error(502, msg)
                finally:
                    resp.close()
                if not is_anthropic:
                    out = chat_to_response(acc, model, rid, ctx) if is_resp else acc.completion(model)
                self._json(200, out)
        finally:
            secs = time.time() - t0
            record_usage(path, model, status, acc.usage, secs, ttft, want_stream, self._last_error, account=a)
            u = acc.usage or {}
            log("接口=%s 模型=%s 状态=%s 耗时=%.1f秒%s%s" % (
                path.rsplit("/", 1)[-1], model, status, secs,
                " 首字=%.2f秒" % ttft if ttft is not None else "",
                " 输入/输出=%s/%s" % (u.get("prompt_tokens", "?"), u.get("completion_tokens", "?"))
                if u else ""))

    # ---- 流式输出 ----
    def _stream(self, path: str, resp, model, acc, rid, ctx, t0) -> float | None:
        """返回首字延迟（秒）。"""
        ttft = None
        self._open_stream()
        try:
            last = [time.time()]

            def ping():
                if time.time() - last[0] >= KEEPALIVE_EVERY:
                    self._chunk(": keepalive\n\n")      # SSE 注释行：客户端会忽略，但连接保持活跃
                    last[0] = time.time()

            if path == "/v1/chat/completions":
                try:
                    for obj in iter_chunks(resp, ping):
                        err = sse_error(obj)
                        if err:
                            raise UpstreamError(502, "上游返回错误：%s" % err)
                        u = obj.get("usage")
                        if u.__class__ is dict:           # chat 流只需 usage（用量记录），不必累加全文
                            acc.usage = u
                        out = normalize_chunk(obj, model)
                        if ttft is None and out.get("choices"):
                            ttft = time.time() - t0
                        self._chunk("data: %s\n\n" % _dumps(out))
                        last[0] = time.time()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise
                except Exception as e:  # noqa: BLE001 - 上游中途断流
                    log("chat 流中断：%s" % e)
                    acc.error = "上游响应流中断：%s" % e
                    acc.error_status = e.status if isinstance(e, UpstreamError) else 502
                    self._chunk("data: %s\n\n" % json.dumps(
                        {"error": {"message": "上游响应流中断：%s" % e,
                                   "type": "upstream_error"}}, ensure_ascii=False))
                self._chunk("data: [DONE]\n\n")
            elif path == "/v1/messages":
                for frame in anthropic_stream(resp, model, acc, ping):
                    if ttft is None and frame.startswith("event: content_block_delta\n"):
                        ttft = time.time() - t0
                    self._chunk(frame)
                    last[0] = time.time()
            else:
                # Responses：真流式事件转换（逐 token 转发，不缓冲）
                for frame in responses_stream(resp, model, rid, ctx, acc, ping):
                    if ttft is None and "output_item.added" in frame[:60]:
                        ttft = time.time() - t0
                    self._chunk(frame)
            self._end_stream()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            acc.error, acc.error_status = "连接在响应完成前断开", 499
            self.close_connection = True
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return ttft


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = os.name != "nt"   # Windows 上会允许端口被重复绑定，不开

    def handle_error(self, request, client_address):
        # 客户端中途断开是常态（Ctrl+C、超时），不打 traceback 刷屏
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# ===========================================================================
# 十一、入口
# ===========================================================================
def do_list_models(auth_path: str) -> None:
    """诊断：逐路打印模型目录，看清每个模型来自哪条探测路。"""
    a = _auth or load_auth(auth_path)
    print()
    print("上游：%s（%s）" % (upstream_base(a), "国际版" if is_global(a) else "国内版"))
    print("说明：/v3/config 对三种 UA 下发不同集合，单路必然不全；下表列出各路的独有条目。")
    print()

    routes = [
        ("/v3/config 桌面端UA", lambda: _probe_models(a, MODELS_PATH, desktop_ua(a))),
        ("/v3/config IDE UA", lambda: _probe_models(a, MODELS_PATH, IDE_UA)),
        ("/v3/config CLI UA", lambda: _probe_models(a, MODELS_PATH, CLI_UA)),
        ("企业端点家族", lambda: _probe_enterprise(a)),
    ]
    seen: set[str] = set()
    for name, fn in routes:
        try:
            entries = fn()
        except Exception as e:  # noqa: BLE001
            print("  %-22s 失败：%s" % (name, e))
            continue
        ids = []
        for m in entries:
            mid = _model_id(m)
            if mid and _is_chat_model(m) and mid not in seen:
                ids.append(mid)
        seen.update(ids)
        print("  %-22s 新增 %2d 条" % (name, len(ids)))
        for i in ids:
            print("        %s" % i)

    print()
    catalog = _fetch_catalog_now(a)        # 诊断要看实时结果，不用缓存
    models = catalog["models"]
    print("并集：%d 个模型" % len(models))
    for m in models:
        extra = []
        if m.get("credits"):
            extra.append("倍率 %s" % m["credits"])
        if m.get("context_length"):
            extra.append("ctx=%s" % m["context_length"])
        if m.get("max_output_tokens"):
            extra.append("max_out=%s" % m["max_output_tokens"])
        print("  %-32s %s" % (m["id"], "  ".join(extra)))
    if catalog["promotions"]:
        print()
        print("限时优惠：")
        for p in catalog["promotions"]:
            print("  %-14s %s  %s" % (p["label"] or "-",
                                      p["discountedCredits"] or "",
                                      ",".join(p["modelIds"])))
    print()
    if _selection:
        print("当前选择：%d 个（/v1/models 只输出这些）" % len(_selection))
    else:
        print("当前未做选择：已禁止所有模型" if _enforce else
              "当前未做选择：/v1/models 输出全部模型")
    print()


def _reload_auth() -> dict:
    """从磁盘重载凭证到内存（登录后 / 启动时统一走这里）。"""
    a = load_accounts()
    if a is None:
        raise ValueError("尚未登录，请先添加账号")
    if a["expiresAt"] and a["expiresAt"] - REFRESH_SKEW <= time.time():
        refresh_auth(a)
    return a


def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    s = s.strip()
    return s if s else default


def _human(n) -> str:
    if not n:
        return "—"
    n = int(n)
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0) if n % 1000000 else "%dM" % (n / 1000000)
    if n >= 1000:
        return "%dK" % round(n / 1000.0)
    return str(n)


def parse_pick(text: str, n: int):
    """解析选择串 → 1-based 索引集合。支持 `1,3,7-9` / 全角逗号 / all / none / 空=全部。
    非法输入返回 None。"""
    t = (text or "").strip().lower()
    if t in ("", "all", "*", "全部"):
        return set(range(1, n + 1))
    if t in ("none", "无"):
        return set()
    out: set[int] = set()
    for part in t.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a.strip()), int(b.strip())
            except ValueError:
                return None
            if lo < 1 or hi > n or lo > hi:
                return None
            out.update(range(lo, hi + 1))
        else:
            try:
                i = int(part)
            except ValueError:
                return None
            if i < 1 or i > n:
                return None
            out.add(i)
    return out


def _promo_index(promotions: list) -> dict:
    idx: dict[str, dict] = {}
    for p in promotions or []:
        for mid in p.get("modelIds") or []:
            idx.setdefault(mid, p)
    return idx


def _print_model_table(models: list, promotions: list) -> None:
    promo = _promo_index(promotions)
    w = min(max([len(m["id"]) for m in models] + [8]), 38)
    print("  %4s  %-*s  %-12s  %8s  %8s" % ("#", w, "模型", "积分倍率", "上下文", "最大输出"))
    print("  " + "-" * (w + 44))
    for i, m in enumerate(models, 1):
        cr = m.get("credits", "—")
        p = promo.get(m["id"])
        if p and _parse_factor(p.get("discountedCredits")) is not None:
            cr = "%s→%s" % (m.get("credits") or "", p["discountedCredits"])
        print("  %4d  %-*s  %-12s  %8s  %8s"
              % (i, w, m["id"][:w], cr,
                 _human(m.get("context_length")), _human(m.get("max_output_tokens"))))


def do_setup(args) -> bool:
    """终端向导：登录 → 看模型 → 选模型 → 打印配置。返回是否继续启动服务。"""
    print()
    print("=" * 62)
    print("  wb2api-lite 设置向导")
    print("=" * 62)

    # ---- 1. 登录 ----
    print()
    if _auth and not args.relogin:
        a = _auth
        exp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(a["expiresAt"]))
               if a.get("expiresAt") else "未知")
        print("[1/3] 账号")
        print("      已登录：%s  到期 %s" % (account_name(a) or "未命名账号", exp))
        if _ask("      要重新登录吗？(y/N): ", "n").lower() in ("y", "yes"):
            do_login(args.realm, AUTH_FILE)
    else:
        print("[1/3] 账号")
        if not os.path.exists(AUTH_FILE):
            print("      尚未登录，开始设备授权…")
        do_login(args.realm, AUTH_FILE)

    try:
        _reload_auth()
        a = _auth
    except Exception as e:  # noqa: BLE001
        print("  登录状态异常：%s" % e)
        return False

    # ---- 2. 模型 ----
    print()
    print("[2/3] 模型目录（%s）" % ("国际版" if is_global(a) else "国内版"))
    print("      正在拉取…", end="", flush=True)
    try:
        cat = fetch_catalog(a)
    except Exception as e:  # noqa: BLE001
        print("\r      拉取失败：%s" % e)
        return False
    models = cat["models"]
    if not models:
        print("\r      上游没有返回任何模型，请稍后重试")
        return False
    print("\r      共 %d 个模型%s"
          % (len(models),
             "，%d 项限时优惠" % len(cat["promotions"]) if cat["promotions"] else ""))
    print()
    _print_model_table(models, cat["promotions"])

    # ---- 3. 选择 ----
    print()
    print("[3/3] 选择要对外暴露的模型")
    cur = list(_selection)
    if cur:
        print("      当前已选 %d 个：%s" % (len(cur), ", ".join(cur[:6]) + ("…" if len(cur) > 6 else "")))
    print("      输入编号，支持逗号与区间，例如  1,3,7-9")
    print("      回车保留已有选择（首次为全选）；none 清空选择")
    print("      强制限制开启时，清空选择会禁止调用；关闭时则返回全部模型")
    pick = _ask("      > ", "")
    if pick == "" and cur:
        ids = cur                                   # 回车保留原选择
    else:
        idx = parse_pick(pick, len(models))
        if idx is None:
            print("      输入无法识别，已跳过（保持原选择不变）")
            ids = cur
        else:
            ids = [models[i - 1]["id"] for i in sorted(idx)]
    # 默认模型：客户端写死的模型名（gpt-4o 之类）不在目录里时自动改用它，开箱即用
    dm = _default_model if _default_model in (ids or [m["id"] for m in models]) else ""
    suggest = dm or (ids[0] if ids else "")
    if suggest:
        got = _ask("      默认模型（未知模型名自动改用它；回车 = %s，输入 none 不设置）: " % suggest,
                   suggest)
        dm = "" if got.lower() == "none" else got
    save_selection(ids, default_model=dm)

    # ---- 输出 ----
    host = "127.0.0.1" if _is_loopback(args.host) else args.host
    base = "http://%s:%d/v1" % (host, args.port)
    key = LOCAL_API_KEY
    print()
    print("=" * 62)
    print("  完成")
    print("=" * 62)
    print("  接口地址 : %s" % base)
    print("  访问密钥 : %s" % key)
    if ids:
        print("  已选模型 : %d 个 —— %s" % (len(ids), ", ".join(ids)))
        print("             （其他工具自动发现模型时只会看到这些）")
    else:
        print("  已选模型 : 无（已禁止所有模型）" if _enforce else
              "  已选模型 : 未选择 —— /v1/models 返回全部 %d 个" % len(models))
    print("  配置文件 : %s" % os.path.abspath(SELECTION_FILE))
    print()
    for line in _client_hint(base, key, _default_model or (ids[0] if ids else "")):
        print(line)
    print()

    return _ask("  现在启动服务？(Y/n): ", "y").lower() not in ("n", "no")


def _client_hint(base: str, key: str, model: str) -> list[str]:
    lines = ["  客户端接入（任选其一）：",
             "    export OPENAI_BASE_URL=%s" % base,
             "    export OPENAI_API_KEY=%s" % key]
    if model:
        lines += ["",
                  "    # Codex：~/.codex/config.toml",
                  "    model = \"%s\"" % model,
                  "    model_provider = \"wb2api\"",
                  "    [model_providers.wb2api]",
                  "    name = \"wb2api\"",
                  "    base_url = \"%s\"" % base,
                  "    env_key = \"WB2API_API_KEY\"      # 再 export WB2API_API_KEY=<API Key>",
                  "    wire_api = \"responses\""]
    return lines


class ChineseArgumentParser(argparse.ArgumentParser):
    """为命令行帮助及常见参数错误提供中文说明。"""
    def format_help(self):
        return super().format_help().replace("usage:", "用法：")

    def format_usage(self):
        return super().format_usage().replace("usage:", "用法：")

    def error(self, message):
        for source, target in (("unrecognized arguments:", "无法识别的参数："),
                               ("invalid int value:", "不是有效的整数："),
                               ("invalid choice:", "无效的选项："),
                               ("choose from", "可选值"),
                               ("expected one argument", "需要提供一个参数值"),
                               ("argument ", "参数 ")):
            message = message.replace(source, target)
        self.print_usage(sys.stderr)
        self.exit(2, "参数错误：%s\n" % message)


def main() -> None:
    global _auth, AUTH_FILE, LOCAL_API_KEY, _BIND_HOST, _PORT, _SERVE_UI, HIDE_REASONING
    global DATA_DIR, API_KEY_FILE, SELECTION_FILE, USAGE_FILE, MODELS_CACHE_FILE
    global _api_key_persisted

    ap = ChineseArgumentParser(description="WorkBuddy → OpenAI / Anthropic 兼容极简代理", add_help=False)
    ap._optionals.title = "可选参数"
    ap.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    ap.add_argument("--host", default=os.environ.get("WB_HOST", "127.0.0.1"), help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=_env_int("WB_PORT", 8787, 1, 65535),
                    help="监听端口，默认 8787")
    ap.add_argument("--data-dir", default="",
                    help="状态文件目录（auth.json / api_key.txt / selection.json 等），默认脚本所在目录")
    ap.add_argument("--auth", default="", help="凭证文件路径（默认 <data-dir>/auth.json）")
    ap.add_argument("--api-key", default=LOCAL_API_KEY,
                    help="本地访问密钥（默认读取/生成 api_key.txt，长期有效）")
    ap.add_argument("--random-api-key", action="store_true",
                    help="每次启动随机生成一次性密钥（不读写 api_key.txt）")
    ap.add_argument("--setup", action="store_true",
                    help="终端向导：登录 → 看模型 → 选模型 → 打印接入信息")
    ap.add_argument("--relogin", action="store_true", help="--setup 时强制重新登录")
    ap.add_argument("--login", action="store_true",
                    help="命令行走设备授权登录并写入凭证文件，然后退出")
    ap.add_argument("--realm", choices=["cn", "global"], default="global",
                    help="--login / --setup 时选择国际版(global，默认) / 国内版(cn)")
    ap.add_argument("--list-models", action="store_true",
                    help="打印模型目录诊断（每条探测路各拿到什么），然后退出")
    ap.add_argument("--no-ui", action="store_true", help="关闭网页控制台及全部管理接口")
    ap.add_argument("--hide-reasoning", action="store_true",
                    help="不向客户端输出模型的推理内容（reasoning_content）")
    ap.add_argument("--debug", action="store_true", help="打印上游请求与前几帧 SSE（排障用）")
    args = ap.parse_args()

    if args.data_dir:
        DATA_DIR = os.path.abspath(os.path.expanduser(args.data_dir))
        os.makedirs(DATA_DIR, exist_ok=True)
        API_KEY_FILE = _data_path("WB_API_KEY_FILE", "api_key.txt")
        SELECTION_FILE = _data_path("WB_SELECTION_FILE", "selection.json")
        USAGE_FILE = _data_path("WB_USAGE_FILE", "usage.jsonl")
        MODELS_CACHE_FILE = _data_path("WB_MODELS_CACHE_FILE", "models_cache.json")
    AUTH_FILE = args.auth or _data_path("WB_AUTH_FILE", "auth.json")
    args.auth = AUTH_FILE
    HIDE_REASONING = args.hide_reasoning
    if args.debug:
        globals()["DEBUG"] = True
    # 必须在向导之前确定：--setup 会把它打印给用户，之后服务也用同一个
    LOCAL_API_KEY, key_src, _api_key_persisted = resolve_api_key(args.api_key or LOCAL_API_KEY,
                                                                   args.random_api_key)
    _BIND_HOST, _PORT = args.host, args.port
    _SERVE_UI = not args.no_ui
    try:
        load_settings()
    except (OSError, ValueError) as e:
        raise SystemExit("读取自动签到设置失败：%s" % e)
    try:
        load_accounts()
    except (OSError, ValueError) as e:
        raise SystemExit("读取账号失败：%s" % e)
    load_selection()

    if args.login:
        do_login(args.realm, args.auth)
        return

    if args.setup:
        if not do_setup(args):
            return
        # 向导确认启动 → 落到下面正常的服务启动流程

    elif args.list_models:
        if _auth is None:
            raise SystemExit("找不到凭证文件 %s，请先执行 --login 或 --setup" % AUTH_FILE)
        do_list_models(args.auth)
        return

    # 先绑定端口：端口被占用时立刻给出可读的提示，而不是一长串 traceback
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        raise SystemExit("无法监听 %s:%d（%s）—— 端口可能已被占用，换一个：--port 8788"
                         % (args.host, args.port, e))

    try:
        load_usage_totals()
    except Exception as e:  # noqa: BLE001 - 统计文件损坏不应阻止服务启动
        log("用量历史回放失败，以空统计启动：%s" % e)
    # 没有凭证也照常启动 —— 网页控制台里可以直接登录
    if _auth:
        log("已加载账号：%s（已保存 %d 个账号）" % (account_name(_auth) or "未命名账号", len(_accounts)))
        if _auth["expiresAt"] and _auth["expiresAt"] - REFRESH_AHEAD <= time.time():
            refresh_auth(_auth)
        try:
            fetch_catalog(_auth, wait=False)      # 预热：读落盘缓存 / 后台探测
        except Exception as e:  # noqa: BLE001 - 目录预热失败不影响启动
            log("模型目录预热失败（不影响使用）：%s" % e)
        threading.Thread(target=POOL.warm, args=(upstream_base(_auth) + _chat_path,),
                         daemon=True).start()     # 预先握手：第一个请求不用等 TLS
    else:
        log("尚未登录（%s 不存在），请打开网页控制台完成登录" % os.path.abspath(AUTH_FILE))
    threading.Thread(target=_refresher_loop, daemon=True, name="token-refresh").start()
    threading.Thread(target=_auto_checkin_loop, daemon=True, name="auto-checkin").start()

    host = "127.0.0.1" if _is_loopback(args.host) or args.host in ("0.0.0.0", "::") \
        else args.host
    base = "http://%s:%d/v1" % (host, args.port)
    log("网页控制台：http://%s:%d/" % (host, args.port))
    log("接口地址：  %s" % base)
    log("访问密钥：  %s（%s）" % (_mask_api_key(LOCAL_API_KEY), key_src))
    log("数据目录：  %s" % DATA_DIR)
    if _selection:
        log("模型选择：  %d 个（/v1/models 只输出这些）" % len(_selection))
    if _aliases:
        log("模型别名：  %s" % ", ".join("%s→%s" % kv for kv in _aliases.items()))
    if _default_model:
        log("默认模型：  %s" % _default_model)
    if not _is_loopback(args.host):
        log("⚠ 已绑定到非回环地址，网页控制台需要 ?pwd=<API Key> 才能打开")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        close_usage()


if __name__ == "__main__":
    main()
