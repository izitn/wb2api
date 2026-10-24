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
| `GET /healthz` | 健康状态（含账号池状态） |
| `GET /` | 网页控制台 |

- 鉴权：`Authorization: Bearer <密钥>` 或 `x-api-key: <密钥>`。
- 路径容错：少写 `/v1`（如 `/chat/completions`）、结尾斜杠、经 HTTP 代理发出的绝对 URL 请求行都能正确路由。
- Anthropic SDK：`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`，`ANTHROPIC_API_KEY=<密钥>`。不支持 Anthropic 服务端工具、PDF 内容块和 `/v1/messages/count_tokens`。
- Responses 的 `previous_response_id` 历史只存在内存中：最多 256 条、保留 6 小时、总量 32 MiB，重启后无法续接。续接请求会固定回到创建该响应的账号，不参与池内换号；原账号冷却或已移除时会返回 400，而不会把历史发往其他账号。

## 访问密钥

优先级：`--api-key` / `WB_API_KEY` > 数据目录的 `api_key.txt` > 首次启动自动生成并写入 `api_key.txt`。`--random-api-key` 每次启动生成一次性密钥，不读写文件。启动日志只显示掩码。

## 网页控制台

- 多账号保存与热切换，显示当前账号积分。
- 模型管理：按倍率排序、搜索、勾选；勾选按账号保存。
- 接入配置：复制接口地址和密钥，生成客户端配置。
- 用量统计：今日 / 本月的请求数、错误数、词元和积分。
- 国内版个人账号的手动签到与每日自动签到开关；开机自启开关。

管理接口都在 `/admin/*` 下。监听回环地址时免密，但会校验 `Host` 为回环名、`Origin` 同源；监听其他地址时必须带密钥，首次用 `http://<地址>:8787/?pwd=<密钥>` 打开，之后改用 HttpOnly cookie。`--no-ui` 会同时关闭控制台和全部管理接口。

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

解析顺序：别名 → 原名 → 默认模型。`enforce: true` 时只放行已勾选的模型。未知模型名只在模型目录完整且该模型未被勾选时才回退到默认模型。控制台的勾选会写入同一个文件（按账号、按池分别保存）。

## 账号池

保存多个账号时，国际版与国内版各成一个池，**绝不跨池**。

- **选号**：会话粘性命中 → 当前账号 → 池内其他可用账号。
- **会话粘性**：请求带 `X-Session-Id`、`X-Conversation-Id`、`Session-Id` 或 `X-Claude-Code-Session` 之一时固定到同一账号，有效期 30 分钟（滑动续期）。
- **换号**：在向客户端写出任何字节之前遇到 `401 / 402 / 403 / 429 / 5xx` 时换号重试，单请求最多换 3 次；`400 / 404` 不换号。每次换号前等待 `WB_POOL_SWITCH_DELAY`（默认 5 秒，随机 ±20%）。
- 换号只影响当次请求用哪个账号，不改动控制台上的当前账号。

| 情况 | 处置 |
| --- | --- |
| `429` 限流 | 软冷却，600 秒起指数退避，封顶 2 小时 |
| `403` 且错误含 `code=11140`（账号被限制） | 标记为禁用，本进程内不再参与轮询 |
| 其他 `401 / 403` | 冷却 600 秒 |
| `402` 余额不足 | 冷却 1 小时 |
| `5xx` 连续 3 次 | 熔断 30 分钟 |

冷却和禁用状态只保存在内存里，**重启后会恢复参与轮询**。被封禁的账号应从控制台或 `accounts.json` 中移除。状态可在 `GET /healthz` 的 `pool` 字段或 `GET /admin/pool` 查看。

## 出站身份

代理按官方桌面客户端的请求形态构造上游请求头，主要包括：

- UA `WorkBuddy/<版本> workbuddy-ai/<版本> CLI/<CLI 版本>`（国际版）与 `X-IDE-Version`。
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

### 指纹随机化（`WB_FINGERPRINT_ENABLED`，默认开启）

- 每次发往上游的请求前随机等待 0 到 `WB_FINGERPRINT_JITTER` 秒（默认 0.2）。
- UA 中的客户端版本修订号随机 ±1（如 `5.8.4` 可能变成 `5.8.3` 或 `5.8.5`）。注意 `X-IDE-Version` 不做此随机，两者可能不一致；如需保持一致，设 `WB_FINGERPRINT_ENABLED=0`。

### system 消息处理

上游要求首条消息为 system，客户端没带时补 `WB_DEFAULT_SYSTEM`（默认 `You are a helpful assistant.`）。`developer` 角色会改写为 `system`。之后按以下开关处理 system 消息：

| 环境变量 | 默认 | 行为 |
| --- | --- | --- |
| `WB_LIMIT_SYSTEM_LENGTH` | `1` | 超过 `WB_MAX_SYSTEM_LENGTH` 字节的 system 消息被截断并追加 `...` |
| `WB_MAX_SYSTEM_LENGTH` | `500` | 上述截断阈值（字节） |
| `WB_SYSTEM_TO_USER` | `0` | 首条 system 超长时改为 user 消息，前面补一条默认 system（优先于截断） |
| `WB_REMOVE_SYSTEM` | `0` | 删除全部 system 消息，只保留一条内容为 `OK` 的 system |

**默认开启的截断会影响 Agent 类客户端**：Codex、Claude Code 等的 system 提示词通常远超 500 字节，截断后工具定义、行为约束会丢失。这类客户端建议设 `WB_LIMIT_SYSTEM_LENGTH=0`，或改用 `WB_SYSTEM_TO_USER=1`。

## 错误提示

- `403` 且含 `code=11140`：响应里带“异常 / 封禁 / 限制”字样时提示账号已被限制使用；账号池会把该账号移出轮询。
- `--debug` 下会打印发往上游的请求头（不含 `Authorization`）和上游错误响应的脱敏元数据，不打印对话正文、错误正文回显或凭证。
- 错误响应可能回显提示词或凭证，因此日志与 `usage.jsonl` 只记录 HTTP 状态码、上游错误码等结构化元数据；客户端仍会收到经过脱敏的可读错误信息。

## 签到与积分

- 国内版个人账号支持“签到领积分”；国际版和企业账号跳过签到。
- 上游业务码 `10001`、`14001` 视为“今日已签到”。
- 每日自动签到开启后遍历所有国内版个人账号，按北京时间换日，失败后 30 分钟再试。开关保存在 `settings.json`，需要程序保持运行。

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
| `--login --realm global\|cn` | 命令行设备授权登录后退出 |
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
| `WB_DRAIN_BODY_MAX` | 1 MiB | chunked 请求体排空上限 |
| `WB_POOL_IDLE_TTL` | `40` | 上游连接池空闲超时（秒，10–120） |
| `WB_GZIP_MIN` | `16384` | 请求体超过该字节数时 gzip 后发上游，`0` 关闭 |
| `WB_GZIP_DISABLE_TTL` | `1800` | 上游拒绝 gzip 后的禁用时长（秒），`0` 为永久 |
| `WB_POOL` | `1` | `0` 关闭账号池 |
| `WB_POOL_SOFT_COOLDOWN` / `WB_POOL_SOFT_MAX` | `600` / `7200` | 429 冷却起点 / 上限（秒） |
| `WB_POOL_HARD_COOLDOWN` | `0` | 402 冷却时长，`0` 表示 3600 秒 |
| `WB_POOL_FAIL_THRESHOLD` / `WB_POOL_BREAKER` | `3` / `1800` | 5xx 熔断阈值 / 熔断时长（秒） |
| `WB_POOL_STICKY_TTL` / `WB_POOL_STICKY_MAX` | `1800` / `4096` | 会话粘性有效期（秒）/ 最大条数 |
| `WB_POOL_MAX_SWITCH` | `3` | 单请求最多换号次数 |
| `WB_POOL_SWITCH_DELAY` | `5.0` | 换号前等待（秒，随机 ±20%） |
| `WB_IDE_UA` / `WB_CLI_UA` | 内置 | 模型目录探测用的另外两种 UA |
| `WB_AUTH_FILE`、`WB_ACCOUNTS_FILE`、`WB_API_KEY_FILE`、`WB_SELECTION_FILE`、`WB_SETTINGS_FILE`、`WB_USAGE_FILE`、`WB_MODELS_CACHE_FILE` | 数据目录内 | 单独指定各状态文件路径 |

## 本地状态文件

运行时会在数据目录生成：`accounts.json`、`auth.json`、`api_key.txt`、`selection.json`、`settings.json`、`models_cache.json`、`usage.jsonl`、`client_versions.json`。前三个含账号令牌和访问密钥，**不要提交或分享**。仓库的 `.gitignore` 已覆盖这些文件；若使用 `WB_DATA_DIR` 或自定义文件名，请确认其路径仍在忽略规则内，或另加忽略规则。

停止程序后删除这些文件即可恢复到未登录状态。
