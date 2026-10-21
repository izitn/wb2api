# wb2api-lite 安全修复记录

## 2026-10-21 配置增强与错误修复

### 配置可调性改进
- **MAX_BODY 可配置**：新增 `WB_MAX_BODY` 环境变量，默认 16MB，最小 1MB，最大 256MB
  - 解决大上下文请求（长对话、大量 context）触发 413 错误的问题
- **CLIENT_TIMEOUT 可配置**：新增 `WB_CLIENT_TIMEOUT` 环境变量，默认 120 秒，范围 30-3600 秒
- **DRAIN_BODY_MAX 可配置**：新增 `WB_DRAIN_BODY_MAX` 环境变量，控制 chunked 请求体排空上限
- **IDLE_TTL 可配置**：新增 `WB_POOL_IDLE_TTL` 环境变量，控制连接池空闲超时，默认 40 秒，范围 10-120 秒

### gzip 压缩策略改进
- **gzip 禁用 TTL**：新增 `WB_GZIP_DISABLE_TTL` 环境变量，默认 1800 秒（30 分钟）
  - 原行为：一旦被拒绝，永久禁用该上游的 gzip
  - 新行为：禁用有过期时间，过期后自动重新启用
  - 设置为 0 可恢复旧的永久禁用行为
- gzip 被拒绝时会记录禁用时长（永久或秒数）

### 代码质量改进
- **修复重复定义**：删除了重复的 `_drain_chunked` 函数定义（第一个版本），保留更完善的版本
- **异常处理精确化**：JSON 解析异常从 `except Exception` 改为 `except (json.JSONDecodeError, UnicodeDecodeError)`
  - 避免掩盖非 JSON 解析相关的异常

### 环境变量参考
```bash
# 请求体与连接
WB_MAX_BODY=16777216           # 请求体大小上限（字节），默认 16MB
WB_CLIENT_TIMEOUT=120          # 下游连接空闲超时（秒）
WB_DRAIN_BODY_MAX=1048576      # chunked 排空上限（字节）
WB_POOL_IDLE_TTL=40            # 连接池空闲超时（秒）

# gzip 压缩
WB_GZIP_MIN=16384              # gzip 最小阈值（字节），0 = 禁用
WB_GZIP_DISABLE_TTL=1800       # gzip 被拒后禁用时长（秒），0 = 永久
```

## 2026-10-21 管理界面与积分查询体验

- 积分只读查询遇到连接中止时最多重试一次；签到提交不走该重试，避免重复写操作。
- 管理界面调整账户摘要卡片比例与断点布局；非模型页面收起今日请求卡，降低重复信息占位。
- 用量表按模型名左对齐、数值右对齐；侧栏服务开关增加分组标题。

## 2026-10-20 交接审查复核（main）

- 半读响应只有确认到 EOF 才会将连接放回连接池；连接建立失败可重试，HTTP 请求可能已发送后的连接错误和 5xx 不再自动重放，429 重试复用同一组请求 ID。
- gzip 拒绝按响应正文识别，并按上游地址隔离；普通业务 400/415 不会关闭 gzip 或明文重发。
- 放宽 `stream` 的 0/1 与字符串形式、允许零 token 上限并在转换时省略；允许 Responses 仅带 `instructions`。
- Responses 流中工具参数与文本交错时继续累积完整参数，完成事件按 `output_index` 排序。
- 拒绝 chunked 请求前有限排空正文；凭证、配置和模型缓存写入使用唯一临时文件及目标路径锁；无效整数环境变量回落默认值并夹紧下限。
- Anthropic 服务端工具仍明确拒绝：当前转换层无法忠实表达这些工具的执行语义。密钥持久化检查、启动密钥脱敏和单文件账号库已存在，保持原设计。
- 新增离线回归覆盖；真实上游是否按请求 ID 去重及 gzip 错误文案仍无法离线确认。

## 2026-10-20 交接审查迭代（v2.0.0-account-pool）

- 请求写出后的连接异常不再自动重放或触发账号池换号；仅明确发生在建连阶段的异常允许重试。
- 同账号的 5xx 不再自动重放；429 重试复用稳定的请求追踪 ID。
- gzip 拒绝按上游域名隔离，仅在响应明确指出压缩问题时关闭；业务 400/415 保留原错误正文。
- 上游响应只有确认完整读完后才归还连接池；Responses 缺少 `finish_reason` 或中途断流时不发 item 完成事件。
- 私有状态文件采用唯一临时文件和固定分桶锁；gzip 状态快照与更新使用同一把锁。
- 账号池、并发和 gzip 的整数环境设置采用有界解析；异常浮点设置回退默认值。
- 兼容字符串形式的 `stream`、零 token 上限和仅含 `instructions` 的 Responses 输入；明确拒绝无法映射的 Anthropic 服务端工具。
- 增加离线回归覆盖请求重放、gzip 误判、截断连接、并发写入和输入兼容。

## 2026-10-20 账号池 / 跨平台 / 出站身份

### 1. 账号池（新功能）

按区域分成两个互相独立的池（国际版 / 国内版），**池内**自动换号 + 会话粘性。

- 选号顺序：会话粘性 → 当前账号 → 池内其他可用账号。只有一个账号时行为完全不变。
- 只在向客户端写出任何字节**之前**换号；`401/402/403/429/5xx` 触发换号，
  `400/404` 等请求本身错误不换号（否则在池里把同一个错误重跑一遍、白烧积分）。
- 冷却：429 指数退避（600s 起，封顶 2h）；401/403 立即冷却；402 硬冷却 1h；
  5xx 连续 3 次熔断 30min。单请求最多换 3 次。
- 会话粘性：识别 `X-Session-Id` / `X-Conversation-Id` / `Session-Id` /
  `X-Claude-Code-Session`，TTL 30min 滑动续期，粘性账号失效自动解绑。
- **换号不修改控制台的当前账号**（`_auth`）——当前账号是用户选择，池是容错策略。
- 新增 `GET /admin/pool`；`/healthz` 增加 `pool` 字段；控制台侧栏显示池状态。
- 开关：`WB_POOL=0` 可整体关闭，其余阈值均有环境变量。

修复过程中发现并修掉一个自己引入的缺陷：`2 ** softCount` 无上限，
反复命中 429 会让指数增长到 float 溢出，直接把处理线程打挂（OverflowError）。
现在 `softCount` 夹紧到 30。

### 2. Windows / Linux 跨平台（新功能）

- Linux 上"开机自启"写入 **systemd 用户级单元**
  （`~/.config/systemd/user/wb2api-lite.service`），不需要 sudo、不改系统目录。
- 没有 systemd 的环境（WSL / 容器）明确报告"不支持"，不再给出误导性的成功。
- 开启时提示需要 `loginctl enable-linger $USER` 才能在注销后常驻，但不替用户改系统设置。
- Windows 行为完全不变（`HKCU\...\Run`）。
- 新增 `tests/test_crossplatform.py`：单元文件生成、ExecStart 引号转义、
  XDG 路径、开关幂等、平台分派、密钥未持久化时拒绝开启。

### 3. 出站身份对齐 hub 技术路线

参考 `workbuddy2api-hub` 的 `wb_fingerprint.derive_id`：

- `stable_id()` 输出由 36 位**无连字符裸 hex** 改为**带连字符的 UUIDv4 形状**
  （版本位固定 4、变体位固定 a）。裸 hex 是真实客户端不会出现的形态，本身即网关特征。
- 取值仍**按账号 UID 派生**（同账号恒定、账号间互异），而不是照抄官方"按进程取随机值"：
  本程序一个进程代表多个账号，按进程取会让所有账号共用同一 machineId，反而把它们关联起来。
- 盐值字面量保持不变，已保存账号的设备指纹不会漂移。
- 新增 `tests/test_identity.py`（12 项）：形态、幂等、账号隔离、
  头位置（chat 不带 / 计费带）、会话头族一致性、国际版 5.8.4 头组。

### 4. 每日签到健壮性（对齐同类项目实测口径）

对标 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 的
`checkin()` 与 [workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)
的 `DailyCheckin` / `IsAlreadyCheckin`：

- "已签到"判定补齐 `14001`（panel 实测码）；原本只认 `10001`（hub 实测码）。
- 新增瞬时错误**有界重试**：上游偶发 `HTTP 500 / code 10000` 时重试，最多 3 次；
  业务错误（`11101` 参数错、"今日已签到"）不重试。
- 网络层/解析层错误**绝不**判为"已签到"（panel 的注释明确指出：否则停机补签遇到
  抖动会误记 already，账号当天实际没签到却判定正常）。
- 新增 `tests/test_checkin.py`（20 项）。

**这一项需要你实测确认**：签到是否真的到账无法用离线测试证明，
详见 README 与本次交付说明里的建议。

### 测试

**272 项 Python 测试全部通过**（新增 `test_pool.py` 30 项、`test_pool_failover.py` 10 项、
`test_crossplatform.py` 17 项、`test_identity.py` 12 项、`test_checkin.py` 20 项）。
另外做了实时启动冒烟：`/healthz` 正常返回池状态，未授权 `/v1/models` 仍返回 401。

### 兼容性

- 单账号用户：行为与以前一致（当前账号优先，不会产生额外请求）。
- `WB_POOL=0` 可一键退回旧行为。
- Windows 自启行为不变。

---

## 2026-10-19 安全审查与修复

### 修复问题

#### 1. Windows 自启动密钥持久化问题（中危）

**问题**: 使用 `--api-key` 或 `WB_API_KEY` 启动后开启自启动，重启后服务生成新密钥，客户端认证失败。

**修复**:

- 新增全局变量 `_api_key_persisted` 追踪密钥是否可持久化
- `resolve_api_key()` 现在返回三元组 `(key, source, is_persisted)`
- `set_startup()` 在开启自启时验证密钥已持久化，否则拒绝并提示用户移除显式密钥

**影响**: 使用显式密钥时尝试开启自启会收到明确错误提示，必须让程序自动生成并保存密钥文件。

#### 2. 启动日志泄露完整 API key（低危）

**问题**: 启动日志无条件打印完整本地访问密钥，可能泄露到日志文件、监控系统。

**修复**:

- 实现密钥掩码：长密钥显示前缀和后缀（如 `sk-1234...cdef`），短密钥完全掩码（`***`）
- 启动日志仅显示掩码后的密钥

**影响**: 日志中不再显示完整密钥，降低泄露风险。

### 测试

- 原有 124 项测试全部通过
- 新增 7 项测试，覆盖密钥持久化和掩码逻辑
- 验证 4 种启动场景（默认、显式、随机、文件）

### 兼容性

- **无破坏性变更**
- 用户无需迁移，仅启动日志格式改变

### 修改文件

- `wb2api.py` - 核心逻辑
- `tests/test_startup_key.py` - 新增测试

详细报告见 [SECURITY\_REVIEW\_REPORT.md](SECURITY_REVIEW_REPORT.md)

---

## 2026-10-19 独立审查补充修复

### 1. [严重] main() 遗漏 global 声明，导致开机自启完全不可用

上一轮"密钥持久化保护"改动中，`main()` 给 `_api_key_persisted` 赋值却未声明 `global`，  
赋值落到局部变量，模块级标志恒为 `False`。结果 `set_startup(True)` 对所有用户都拒绝  
（包括密钥已持久化的正常场景），且返回误导性提示。

- 修复：`main()` 补 `global _api_key_persisted`
- 测试：新增 AST 静态断言 `MainGlobalDeclarationTests`，已做变异验证

### 2. [低] usage.jsonl 错误摘要遗漏 deviceToken 脱敏

`record_usage()` 的脱敏列表只包含 `accessToken` / `refreshToken` / `LOCAL_API_KEY`，  
`deviceToken` 会明文写入 `usage.jsonl`。

- 修复：脱敏列表补入 `deviceToken`
- 测试：`test_logging.UsageErrorRedactionTests` 验证四类凭证全部被替换为 `[已隐藏]`

### 3. 测试质量

- 将 `main()` 内联的密钥掩码提取为 `_mask_api_key()`，使其可被测试
- 修正 `KeyMaskingTests` 的假测试（原先在测试内自行重算掩码，从不调用产品代码）
- 删除与 `test_startup_key.py` 重复的 `tests/test_startup.py`

### 4. 本轮实测结论

- 135 项 Python 测试通过，`check_ui.cjs` 8 项通过
- 端到端启动冒烟 17 项通过（路由、鉴权、CSRF、请求体边界、安全响应头）
- 8 线程并发混合调用：无死锁、无异常、用量计数一致

---

## 2026-10-19 深度审计修复（Responses API 与模型目录）

两路独立只读审计覆盖此前从未审查的 §8/§9（约 2000 行），确认并修复以下缺陷。

### 高

1. **部分探测路由失败会用残缺并集覆盖完整目录并落盘**  
   `_fetch_catalog_now` 只要 `models` 非空就整体替换缓存，三路 UA/企业端点任一失败，  
   失败路由独有的模型与优惠项会消失 10 分钟，并被写进 `models_cache.json` 带过重启。  
   → 失败时与旧目录按 id/内容合并；残缺结果**不落盘**；缓存标记 `complete=False`。
2. **默认模型会静默改写客户端显式请求的模型**  
   `resolve_model` 只要目录里有内容且请求名不在目录，就无条件改成 `default_model`，  
   目录残缺时会把用户明确勾选的模型换掉（客户端花 A 的钱拿到 B 的回答）。  
   → 仅在「目录完整 + 该模型未被显式勾选」时才回退。

### 中

1. **Responses 端点把「上游未收尾」当成 completed**  
   缺少 `finish_reason` 时仍发 `response.completed` / 200 JSON，文本与工具参数可能已被截断，  
   而 Anthropic 端点对同一情形会报 502。→ 流式发 `response.failed`，非流式返回 502。
2. **`_warned` 去重集合无界增长 + 日志注入**  
   提示消息拼入客户端可控的工具 `type`，逐请求换值可无限堆内存，`\n` 还能伪造日志行。  
   → 上限 256 条、压平控制字符、截断 500 字符。
3. **Responses usage 丢失 cached_tokens**  
   只读 `prompt_tokens_details.cached_tokens`，上游用 `prompt_cache_hit_tokens` 报告时永远显示 0。  
   → 复用 `norm_usage` 做三种键名的归一。
4. **`usage.jsonl` 损坏会让服务启动崩溃**  
   非对象行触发 `AttributeError`、字段类型错触发 `TypeError`，且 `main()` 未兜底。  
   → 逐行类型校验 + 单行 try + 启动时只告警；`_bump` 全部字段做类型归一。
5. **`_models_refreshing` 在线程启动失败时永久残留**  
   `Thread.start()` 抛 `RuntimeError` 后标记不清，该账号再也不会自动刷新目录。  
   → `try/except` 清理标记，`clear_models_cache` 同时清理。

### 中低 / 低

1. `models_cache.json` 坏字段（`at: [1]`、models 混入字符串）会抛未捕获的 `TypeError`  
   → 捕获 `TypeError`、加载时过滤条目、`_credit_factor` 改用 `.get`。
2. 目录为空且探测全失败时无退避，每个请求都会新起一轮探测  
   → 记录 `attemptAt`，30 秒退避（`CATALOG_RETRY_BACKOFF`）。
3. `tool_choice.allowed_tools` 丢弃白名单，模型可调用白名单外工具  
   → 按 allowlist 过滤 `tools`；空白名单等于禁用全部工具。
4. `previous_response_id` 历史写入异常会打断已开始的响应，超限又静默不缓存  
   → `_RespStore.put` 容错（孤立代理字符退回 ASCII 转义）并返回 bool，`_remember` 只记日志。

### 新增测试

`tests/test_catalog.py`（15）、`tests/test_responses.py`（23）、  
`tests/test_usage_resilience.py`（5），全量 **173 项通过**；  
端到端冒烟 9 项通过（含"损坏 usage.jsonl 仍能启动"）。

### 暂未处理（需要产品决策或影响面较大）

- Responses：不支持的 input item / content part 静默丢弃（应为 400 还是告警）
- Responses：流式与非流式的 `model` / `parallel_tool_calls` / 空 message 字段不一致
- Responses：`item.status` 恒为 completed、refusal 输出丢失、transcript 回放消息拆分
- 模型目录：跨午夜 usage 统计竞态、链式别名、`clear_models_cache` 的磁盘语义、  
  usage 内存与文件分叉、切号瞬间旧账号目录落盘
- 上述项均已记录在审计输出中，未在本轮改动，避免一次引入过多行为变化
