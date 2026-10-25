# wb2api-lite

> ## ⚠️ 使用前必读
>
> - 本项目是个人自用工具，**不提供任何质量保证或技术支持**。账号异常、封禁、积分损失或违反服务条款的后果由使用者自行承担。
> - 只用你自己的账号，只在你自己的机器上运行（建议仅监听 `127.0.0.1`），不要用于分发、售卖或搭建公共服务。
> - 上游接口和风控规则随时可能变化，本文档描述的是当前代码的行为，不保证对上游一直有效。

`wb2api-lite` 是一个单文件本地代理（`wb2api.py`，仅依赖 Python 标准库）：用 WorkBuddy 的登录态访问上游，对外提供 OpenAI / Anthropic 兼容接口和一个中文网页控制台（`ui.html`）。

## 快速开始

需要 Python 3.10+。

```bash
python wb2api.py
```

浏览器打开 `http://127.0.0.1:8787/`，选择国际版或国内版登录。接口地址与访问密钥在控制台“接入配置”页查看。

客户端接入：

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=<控制台显示的访问密钥>
```

`ui.html` 需与 `wb2api.py` 放在同一目录；缺失时控制台只显示一个简易兜底页面。

## 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI Chat Completions，流式 / 非流式 |
| `POST /v1/responses` | OpenAI Responses（薄适配，转成上游对话请求） |
| `POST /v1/messages` | Anthropic Messages，支持流式、图片和自定义工具 |
| `GET /v1/models`、`GET /v1/models/{id}` | 当前可见模型 |
| `GET /healthz` | 健康状态（含账号池汇总） |
| `GET /` | 网页控制台 |

- 鉴权：`Authorization: Bearer <密钥>` 或 `x-api-key: <密钥>`。
- 路径容错：少写 `/v1`（如 `/chat/completions`）、结尾斜杠、经 HTTP 代理发出的绝对 URL 请求行都能正确路由。
- 请求体限制：必须是带 `Content-Length` 的 JSON 对象，不支持 `Transfer-Encoding: chunked`（返回 411）；`/v1/chat/completions` 的 `n > 1` 会返回 400，因为上游不支持一次生成多个候选。
- Anthropic SDK：`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`，`ANTHROPIC_API_KEY=<密钥>`。不支持 Anthropic 服务端工具、PDF 内容块和 `/v1/messages/count_tokens`。
- Responses 的 `previous_response_id` 历史只存在内存中：最多 256 条、保留 6 小时、总量 32 MiB，重启后无法续接。续接请求会固定回到创建该响应的账号，不参与池内换号；原账号冷却或已移除时会返回 400，而不会把历史发往其他账号。请求体带 `"store": false` 时不写入历史。

## 访问密钥

优先级：`--api-key` / `WB_API_KEY` > 数据目录的 `api_key.txt` > 首次启动自动生成并写入 `api_key.txt`。`--random-api-key` 每次启动生成一次性密钥，不读写文件。启动日志只显示掩码。

## 网页控制台

- 多账号保存与热切换，显示当前账号积分。
- 模型管理：按倍率排序、搜索、勾选；勾选按账号保存。
- 接入配置：复制接口地址和密钥，生成客户端配置。
- 用量统计：今日 / 本月的请求数、错误数、词元和积分。
- 国内版个人账号的手动签到与每日自动签到开关；开机自启开关。
- 账号池状态、受限账号、模型冷却和风控暂停；受限账号可在控制台做一次只读复查。

管理接口都在 `/admin/*` 下：`GET /admin/state`、`/admin/usage`、`/admin/points`、`/admin/startup`、`/admin/pool`、`/admin/models`（`?refresh=1` 强制重新探测）、`/admin/login/poll`；`POST /admin/login`、`/admin/settings`、`/admin/startup`、`/admin/accounts/switch`、`/admin/accounts/recheck`、`/admin/selection`、`/admin/toggle-account-models`、`/admin/models/refresh`、`/admin/points/checkin`。监听回环地址时免密，但会校验 `Host` 为回环名、`Origin` 同源；监听其他地址时必须带密钥，首次用 `http://<地址>:8787/?pwd=<密钥>` 打开，之后改用 HttpOnly cookie。`--no-ui` 会同时关闭控制台和全部管理接口。

## 模型选择、别名与默认模型

`selection.json`（数据目录内，修改后自动热加载）支持：

```json
{
  "models": ["deepseek-v4.1-flash"],
  "aliases": {"gpt-4o": "deepseek-v4.1-flash"},
  "default_model": "deepseek-v4.1-flash",
  "enforce": false
}
```

解析顺序：别名 → 原名 → 默认模型。`enforce: true` 时只放行已勾选的模型。未知模型名只在模型目录完整且该模型未被勾选时才回退到默认模型。

控制台保存后会额外写入 `accounts`（按账号的勾选）、`activeAccountId`、`accountUseOwnModels`（账户级开关）和 `pools`（按地区的模型与别名）。选号优先级是**账户级 > 池级 > 不过滤**；手写文件时保留上述四个键即可，缺省按账户级处理（向后兼容）。

## 账号池

保存多个账号时，国际版与国内版各成一个池，**绝不跨池**。

- **选号**：会话粘性命中 → 当前账号 → 池内其他可用账号。
- **会话粘性**：请求带 `X-Session-Id`、`X-Conversation-Id`、`Session-Id` 或 `X-Claude-Code-Session` 之一时固定到同一账号，有效期 30 分钟（滑动续期）。
- **换号**：只对账号级或传输级失败换号，单请求最多切换 3 次；每次切换前等待 `WB_POOL_SWITCH_DELAY`（默认 5 秒，随机 ±20%）。
- **不换号**：`11128` 渠道校验、`11140` 账号受限、模型级限流、渠道排队和普通请求错误都不会重放到其他账号。
- 换号只影响当次请求用哪个账号，不改动控制台上的当前账号。
- 同一地区 15 分钟内出现 2 个不同账号的 `11140` 时，暂停该地区账号池派发 30 分钟。

| 情况 | 处置 |
| --- | --- |
| `400 / 11128` 渠道校验 | 请求级错误；非流式响应时切到备用路径最多重试 1 次，不惩罚账号、不换号 |
| `403 / 11140` 账号受限 | 标记受限并停止换号；自动签到立即取消本轮剩余账号 |
| `429 / 6000` 模型级限流 | 仅冷却当前模型 15 分钟，账号仍可用其他模型 |
| `429 / 6020、6021` 渠道排队 | 按地区池退避（默认 60 秒；上游给 `Retry-After` 时取较大值），不冻结账号 |
| `429 / 10105` 活跃会话上限 | 不冻结账号，直接返回限流错误 |
| 其他 `429` | 账号软冷却，600 秒起指数退避，封顶 2 小时 |
| `401` | 先 refresh 求证；成功则重试，明确失效冷却 600 秒，无法求证只短冷却 120 秒且不换号 |
| `402` 余额不足 | 冷却 1 小时 |
| 其他 `403` | 冷却 600 秒 |
| `5xx` 连续 3 次 | 熔断 30 分钟 |

账号级受限和风控波次暂停写入 `risk_state.json`，重启后仍保持；渠道排队退避、普通冷却、模型冷却和熔断只在内存中，重启后清空。汇总视图在 `GET /healthz` 的 `pool` 字段，逐账号状态（含最近错误分类、`requestId`、模型级冷却）在 `GET /admin/pool` 或控制台。

受限账号完成官方申诉后，可在控制台点击“复查受限账户”，或调用 `POST /admin/accounts/recheck`（JSON：`{"accountId":"..."}`）。复查只做 refresh 只读探活，不发送对话请求；探活成功也只解除本地标记，不能保证下一次对话一定恢复。

## 出站身份

代理按官方桌面客户端的请求形态构造上游请求头，主要包括：

- UA `WorkBuddy/<版本> workbuddy-ai/<版本> CLI/<CLI 版本>`（国际版）与 `X-IDE-Version` 使用同一确定性版本，不在请求间随机抖动。
- 国际版身份头：`X-Product: SaaS`、`X-Agent-Type: main`、`X-Agent-Intent: craft`、`X-Private-Data: true`。
- 每个请求现生成的会话头（`X-Conversation-ID` 等）和链路头（`traceparent`、`b3`、`X-B3-*`、`X-Trace-ID`）。
- 对话请求不带 `X-Machine-ID` / `X-Session-ID`；计费类请求带按账号 UID 稳定派生的 UUID 形状标识。

### 客户端版本

国内版从官方国内稳定下载归档获取版本，国际版从官方国际更新接口获取版本；环境变量按字段单独覆盖。每个地区独立记录当天是否探测成功；服务每天首次启动时在后台探测，不阻塞启动或请求。探测期间继续使用上次成功缓存或内置回退值。截至 2026-10-23，国内版为 `5.8.3`，国际版官方下载接口返回 `5.8.4.41366630`，指纹使用 `5.8.4`；国际版 CLI 从官方发布页读取最新正式条目 `2.162.0`。

在线获取解析官方 WorkBuddy 稳定下载归档及 CLI 发布页，不读取含灰度版本的更新日志。每个地区只在当天成功解析后记录检查日期；一侧失败不会刷新另一侧的日期。程序运行期间后台线程每 24 小时检查一次，重启时若当天尚未成功探测也会立即重试（`WB_VERSION_AUTO_REFRESH=0` 关闭）。

```bash
python wb2api.py --show-version     # 查看当前版本
python wb2api.py --update-version   # 立即重新获取并写入缓存
```

需要固定版本时用环境变量：`WB_CLIENT_VERSION`、`WB_CLI_VERSION`（国内版），`WB_INTL_CLIENT_VERSION`、`WB_INTL_CLI_VERSION`（国际版）。

### 请求抖动（`WB_FINGERPRINT_ENABLED`，默认开启）

- 每次发往上游的请求前随机等待 0 到 `WB_FINGERPRINT_JITTER` 秒（默认 0.2）。
- 只做网络时序抖动；UA、`X-IDE-Version` 和产品版本保持确定性一致。官方客户端不会在同一会话内随机更换次版本号。

### system 消息处理

上游要求首条消息为 system，客户端没带时补 `WB_DEFAULT_SYSTEM`（默认 `You are a helpful assistant.`）。`developer` 角色会改写为 `system`。之后按以下开关处理 system 消息：

| 环境变量 | 默认 | 行为 |
| --- | --- | --- |
| `WB_SYSTEM_COMPAT` | 空 | 设为 `minimal`（或 `compact`）时启用最小化兼容模式（默认只影响 system 截断） |
| `WB_LIMIT_SYSTEM_LENGTH` | `0` | 超过 `WB_MAX_SYSTEM_LENGTH` 字节的 system 消息被截断并追加 `...`；默认关闭 |
| `WB_MAX_SYSTEM_LENGTH` | `500` | 上述截断阈值（字节） |
| `WB_SYSTEM_TO_USER` | `0` | 首条 system 超长时改为 user 消息，前面补一条默认 system（优先于截断） |
| `WB_REMOVE_SYSTEM` | `0` | 删除全部 system 消息，只保留一条内容为 `OK` 的 system |

system 改写默认全部关闭，因为 Codex、Claude Code 等 Agent 客户端的提示词通常远超 500 字节，截断会丢失工具定义与行为约束。需要兼容旧客户端时再显式设置 `WB_SYSTEM_COMPAT=minimal` 或单独开启相应开关。

## 错误提示

- `400 / code=11128`：提示请求未通过渠道校验；系统不会因此惩罚账号或继续换号。
- `403 / code=11140`：提示账号已被限制使用；系统立即停止自动换号，并记录受限状态。
- `--debug` 下会打印发往上游的请求头（不含 `Authorization`）和上游错误响应的脱敏元数据，不打印对话正文、错误正文回显或凭证。
- 错误响应可能回显提示词或凭证，因此日志与 `usage.jsonl` 只记录 HTTP 状态码、上游错误码等结构化元数据；客户端仍会收到经过脱敏的可读错误信息。

## 签到与积分

- 国内版个人账号支持“签到领积分”；国际版和企业账号跳过签到。
- 上游业务码 `10001`、`14001` 视为“今日已签到”。
- 每日自动签到开启后遍历所有国内版个人账号，按北京时间换日；每日启动时刻在 30 分钟窗口内按日期散列，全局并发为 1，账号之间随机等待 45–180 秒，出现 `11140` 立即取消本轮剩余账号。
- 签到失败后 30 分钟再试；账号受限或地区风控暂停时直接跳过。开关保存在 `settings.json`，需要程序保持运行。

## 开机自启

控制台里的开关：

- **Windows**：写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `wb2api-lite` 项，无需管理员权限；可用时用 `pythonw.exe` 后台运行。移动项目目录后需重新开关一次。
- **Linux**：写入 systemd 用户单元 `~/.config/systemd/user/wb2api-lite.service`。注销后仍要运行需自行执行 `loginctl enable-linger $USER`；无 systemd 的环境（WSL、容器）不支持。

用 `--api-key` 或 `WB_API_KEY` 启动时不允许开启自启（重启后密钥会丢失）。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--host` | 监听地址，默认 `127.0.0.1`（`WB_HOST`） |
| `--port` | 监听端口，默认 `8787`（`WB_PORT`） |
| `--data-dir` | 状态文件目录，默认脚本所在目录（`WB_DATA_DIR`） |
| `--auth` | 凭证文件路径，默认 `<data-dir>/auth.json` |
| `--api-key` / `--random-api-key` | 指定 / 一次性生成访问密钥 |
| `--setup` / `--relogin` | 终端向导：登录 → 选模型 → 打印接入信息；`--relogin` 强制重新登录 |
| `--login --realm global\|cn` | 命令行设备授权登录后退出（`--realm` 对 `--setup` 同样生效） |
| `--list-models` | 打印各探测路的模型目录后退出 |
| `--show-version` / `--update-version` | 查看 / 重新获取客户端版本后退出 |
| `--no-ui` | 关闭网页控制台和管理接口 |
| `--hide-reasoning` | 不向客户端输出 `reasoning_content` |
| `--debug` | 打印上游请求元数据与前几帧 SSE 的结构摘要（`WB_DEBUG=1`） |

## 环境变量

上文已列出的版本、指纹、system 相关变量之外：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB_UPSTREAM_BASE` | 按账号区域 | 强制所有请求走指定上游 |
| `WB_MAX_INFLIGHT` | `8` | 同时在途的对话请求上限，超出返回 429 |
| `WB_MAX_BODY` | 16 MiB | 请求体上限（1–256 MiB） |
| `WB_CLIENT_TIMEOUT` | `120` | 下游连接空闲超时（秒，30–3600） |
| `WB_DRAIN_BODY_MAX` | 1 MiB | chunked 请求体排空上限（最小 8192，最大不超过 `WB_MAX_BODY`） |
| `WB_POOL_IDLE_TTL` | `40` | 上游连接池空闲超时（秒，10–120） |
| `WB_GZIP_MIN` | `16384` | 请求体超过该字节数时 gzip 后发上游，`0` 关闭 |
| `WB_GZIP_DISABLE_TTL` | `1800` | 上游拒绝 gzip 后的禁用时长（秒），`0` 为永久 |
| `WB_POOL` | `1` | `0` 关闭账号池：只用当前账号，既不池内选号也不换号 |
| `WB_POOL_SOFT_COOLDOWN` / `WB_POOL_SOFT_MAX` | `600` / `7200` | 账号级 429 冷却起点 / 上限（秒） |
| `WB_POOL_MODEL_COOLDOWN` | `900` | `429/6000` 单模型冷却（秒） |
| `WB_POOL_QUEUE_COOLDOWN` | `60` | `429/6020、6021` 渠道排队退避（秒） |
| `WB_POOL_AUTH_INCONCLUSIVE` | `120` | `401` 无法求证时的短冷却（秒） |
| `WB_RECHECK_COOLDOWN` | `60` | 同一受限账号手动只读复查的最短间隔（秒） |
| `WB_RISK_WINDOW` / `WB_RISK_THRESHOLD` / `WB_RISK_PAUSE` | `900` / `2` / `1800` | 受限统计窗口、不同账号阈值、暂停派发时长 |
| `WB_CHECKIN_GAP_MIN` / `WB_CHECKIN_GAP_MAX` | `45` / `180` | 自动签到账号间隔范围（秒） |
| `WB_CHECKIN_WINDOW` | `1800` | 每日签到随机启动窗口（秒），`0` 表示零点后立即检查 |
| `WB_POOL_HARD_COOLDOWN` | `0` | 402 冷却时长，`0` 表示 3600 秒 |
| `WB_POOL_FAIL_THRESHOLD` / `WB_POOL_BREAKER` | `3` / `1800` | 5xx 熔断阈值 / 熔断时长（秒） |
| `WB_POOL_STICKY_TTL` / `WB_POOL_STICKY_MAX` | `1800` / `4096` | 会话粘性有效期（秒）/ 最大条数 |
| `WB_POOL_MAX_SWITCH` | `3` | 单请求最多换号次数 |
| `WB_POOL_SWITCH_DELAY` | `5.0` | 换号前等待（秒，随机 ±20%） |
| `WB_IDE_UA` / `WB_CLI_UA` | 内置 | 模型目录探测用的另外两种 UA |
| `WB_AUTH_FILE`、`WB_ACCOUNTS_FILE`、`WB_API_KEY_FILE`、`WB_SELECTION_FILE`、`WB_SETTINGS_FILE`、`WB_USAGE_FILE`、`WB_MODELS_CACHE_FILE`、`WB_RISK_STATE_FILE` | 数据目录内 | 单独指定各状态文件路径 |

## 风控参数

上游风控主要盯请求节奏，而不是单个请求头。多账号集中请求、批量签到、频繁换号重试，比少一两个请求头更容易触发 `11140`。`11140` 是账号级限制，换请求头通常无效，只能申诉；`11128` 是请求级校验失败；`429` 可能只是模型限流或渠道排队。代理按这三类分别处理，相关参数决定它对上游的请求节奏。

这些参数可以在网页控制台「风控参数」页调整，保存后立即生效并写入 `settings.json`，不需要重启：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| 同时在途请求上限 | `8` | 并发对话请求上限（1–64）。**撞到限制时优先调低这一项** |
| 单请求最多换号 | `3` | 一个请求最多换几个账号重试（0–10）；`0` 表示完全不换号 |
| 换号间隔 | `5` 秒 | 每次换号前等待，实际随机 ±20%（0–60） |
| 会话粘性时长 | `1800` 秒 | 同一会话优先落回同一账号的时长 |
| 触发暂停的受限账号数 | `2` | 统计窗口内出现几个不同账号受限后暂停该地区派发（1–20） |
| 受限统计窗口 | `900` 秒 | 统计受限账号所用的时间窗口 |
| 波次暂停时长 | `1800` 秒 | 触发波次暂停后该地区停止派发的时长 |
| 软冷却起点 / 封顶 | `600` / `7200` 秒 | `429` 等可恢复错误的初始冷却与指数退避上限 |
| 模型级冷却 | `900` 秒 | `429/6000` 只冷却当前模型，不冻结整个账号 |
| 渠道排队退避 | `60` 秒 | `429/6020、6021` 的短退避 |
| 熔断失败次数 / 时长 | `3` / `1800` 秒 | 连续失败多少次后熔断该账号、熔断多久 |
| 签到最小 / 最大间隔 | `45` / `180` 秒 | 自动签到处理多个账号时的相邻等待范围 |

每项都可以用同名环境变量（上表 `WB_<KEY>`，例如 `WB_MAX_INFLIGHT`）固定。**环境变量一旦设置，网页端对应项变为只读**——部署配置优先于界面上的临时改动。

减小风控暴露面的顺序建议：先降并发、再把单请求换号次数调到 `0~1`、然后拉长换号间隔；批量签到本身也是风险来源，账号多时优先关掉「自动签到」或拉大签到间隔。


## 本地状态文件

运行时会在数据目录生成：`accounts.json`、`auth.json`、`api_key.txt`、`selection.json`、`settings.json`、`models_cache.json`、`usage.jsonl`、`client_versions.json`、`risk_state.json`。前三个含账号令牌和访问密钥，**不要提交或分享**。仓库的 `.gitignore` 已覆盖这些文件；若使用 `WB_DATA_DIR` 或自定义文件名，请确认其路径仍在忽略规则内，或另加忽略规则。

停止程序后删除这些文件即可恢复到未登录状态。
