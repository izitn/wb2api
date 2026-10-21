# wb2api-lite

> ## ⚠️ 使用前必读（作者自述）
>
> **本项目纯属个人自用的小玩具。**
>
> - **作者不是程序员**，一行代码也看不懂。整个项目是在 AI 辅助下"做着玩"的产物，代码的交付目标是"在我自己的机器上能跑"，而不是"在别人机器上一定可靠"。
> - **不提供任何质量保证、兼容性承诺或技术支持。** 任何 bug、数据丢失、账号异常、积分/额度损失、上游封禁或违反服务条款的后果，**全部由使用者自行承担**。
> - **这不是给第三方分发、打包售卖或搭建公共服务用的。** 请只用你自己的账号、只在你自己的机器上自用（理想情况仅监听 `127.0.0.1`）。
> - 上游接口随时可能变更，签名/校验/签到/积分等逻辑**可能随时失效**，作者不保证跟进修复。
> - 仓库里的说明文档同样是 AI 生成的，**可能过期、夸大或出错**，请以实际代码行为为准。
> - 继续使用即表示你已理解并接受以上全部内容。**如果你看不懂上面在说什么，请不要使用本项目。**

`wb2api-lite` 是一个本地代理：它使用 WorkBuddy 登录态访问上游服务，并提供 OpenAI / Anthropic 兼容接口和中文网页控制台。

账号、令牌、访问密钥、模型缓存和历史用量保存在本机数据目录。首次运行会生成所需文件；分享项目时不要包含这些运行时文件。

## 快速开始

需要 Python 3.10 或更高版本。

```powershell
python wb2api.py
```

打开 `http://127.0.0.1:8787/`，在网页中选择国际版或国内版登录。登录完成后，顶部显示当前用户名、账号版本和积分余额；接口地址和密钥在“接入配置”中查看、复制。

国内 WorkBuddy 与 CodeBuddy 使用同一账号体系，国内版积分共享，不需要分别保存两套账号。

## 主要功能

- 支持国际版和国内版登录，重新登录时可以重新选择版本。
- 支持保存多个账号，下拉选择后立即热切换，自动刷新该账号的积分和模型，无需再点切换按钮。
- **账号池**：国际版与国内版各自成一个池，池内自动换号 + 会话粘性（见下节）。
- 显示当前用户名和当前账户积分余额。
- 模型按倍率升序或降序排列。
- 勾选模型按账户分别保存，切换账户时自动恢复对应选择，重启后仍保留。
- 模型目录、导出配置和客户端模型列表只展示倍率明确的模型，保留零倍率和有效优惠项。
- “低倍率 ≤1”包含有效倍率为 `0` 的模型，搜索支持模型 ID 和显示名称。
- 国内版个人账号支持手动“签到领积分”和每日自动签到。
- Windows 当前用户登录后可后台启动本项目的 `wb2api.py`，无需管理员权限。
- 自动签到开关位于“可用积分”卡片内，wb2api 自启开关位于侧栏底部。
- 支持 `/v1/chat/completions`、`/v1/responses`、Anthropic `/v1/messages` 和 `/v1/models`。

## 账号池（池内自动换号 + 会话粘性）

保存了多个账号时，程序会按区域把它们分成**两个互相独立的池**：国际版池、国内版池。
**绝不跨池**——两个区域的令牌、上游域名和出站身份都不同，混用会打到错误的上游。

**选号顺序**：会话粘性命中 → 当前账号 → 池内其他可用账号。
只有**一个账号**时行为与以前完全一致，不会产生任何额外请求。

**自动换号**：请求在向客户端写出任何字节之前，如果遇到 `401 / 402 / 403 / 429 / 5xx`，
会把该账号放进冷却，并在同池内换一个账号重试（单请求最多换 3 次）。
`400 / 404` 这类请求本身的错误**不换号**，避免在池里把同一个错误跑一遍、白烧积分。

**冷却与熔断**：

| 情况 | 处置 |
| --- | --- |
| `429` 限流 | 软冷却，从 600 秒起指数退避，封顶 2 小时 |
| `401 / 403` 授权失效 | 立即冷却 600 秒，等令牌刷新或重新登录 |
| `402` 余额/积分不足 | 硬冷却 1 小时，避免反复烧请求 |
| `5xx` 连续 3 次 | 熔断 30 分钟 |

**会话粘性**：客户端带下列任一请求头时，同一会话会被固定在同一个账号上，
避免多轮上下文在两个账号间跳变：

- `X-Session-Id`
- `X-Conversation-Id`
- `Session-Id`
- `X-Claude-Code-Session`

粘性有效期 30 分钟（滑动续期），粘性账号进入冷却时会自动解绑并改投他人。
不带这些请求头时**不做粘性**，退化为"当前账号优先"。

> 换号只改变"这一次请求用哪个账号"，**不会**改动控制台上的当前账号。
> 当前账号是你在界面上的选择，账号池是容错策略，两者互不覆盖。

状态查看：`GET /healthz` 里的 `pool` 字段，或控制台接口 `GET /admin/pool`。

所有参数都能用环境变量调整：`WB_POOL`（`0` 关闭账号池）、`WB_POOL_SOFT_COOLDOWN`、
`WB_POOL_SOFT_MAX`、`WB_POOL_HARD_COOLDOWN`、`WB_POOL_FAIL_THRESHOLD`、`WB_POOL_BREAKER`、
`WB_POOL_STICKY_TTL`、`WB_POOL_MAX_SWITCH`。

### 其他可配置参数

代理层面的性能与容量也支持通过环境变量调整：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WB_MAX_BODY` | `16777216` (16MB) | 请求体大小上限（字节），最小 1MB，最大 256MB |
| `WB_CLIENT_TIMEOUT` | `120` | 下游连接空闲超时（秒），范围 30-3600 |
| `WB_DRAIN_BODY_MAX` | `1048576` (1MB) | chunked 请求体排空上限（字节） |
| `WB_POOL_IDLE_TTL` | `40` | 连接池空闲超时（秒），范围 10-120 |
| `WB_GZIP_MIN` | `16384` (16KB) | gzip 压缩阈值（字节），`0` 禁用压缩 |
| `WB_GZIP_DISABLE_TTL` | `1800` (30分钟) | gzip 被拒后禁用时长（秒），`0` 永久禁用 |

**常见场景**：

- **长上下文对话遇到 413 错误**：提高 `WB_MAX_BODY` 到 `67108864`（64MB）或更高
- **网络不稳定**：延长 `WB_CLIENT_TIMEOUT` 到 `180` 或 `300`
- **高并发场景**：延长 `WB_POOL_IDLE_TTL` 到 `60`，增加连接复用率

## 网页控制台

控制台按用途分成三个页面，顶部始终展示当前账户和余额。手机上，wb2api 自启开关位于顶部导航下方。

| 页面   | 用途                                |
| ---- | --------------------------------- |
| 模型管理 | 搜索倍率明确的模型、按倍率排序、快速勾选，修改后自动保存到当前账户 |
| 接入配置 | 复制接口地址和密钥，生成客户端配置；密钥默认遮挡，可点击显示    |
| 用量统计 | 按今日或本月查看代理请求数、错误数、词元和积分消耗         |

点击“添加账户”可重新选择国际版或国内版并授权。已保存账户在下拉菜单选择后立即成为当前账户；切换失败时恢复原选择，并显示失败原因。切换后会自动请求积分和模型，查询过程中显示加载状态，旧账户的延迟响应不会覆盖当前账户积分。查询缓慢时仍可继续切换其他账户。

每个账户单独保存模型勾选，新账户初始未勾选。旧版全局勾选会归到迁移时的当前账户，不会复制给其他账户。保存失败的修改与重试也按账户隔离；另一个标签页切换账号后，旧页面的保存请求会被拒绝，避免写到错误账户。

连续勾选在 200 毫秒窗口内合并保存，请求在途时只保留最新待保存结果。切换账户或开始登录时会立即提交并等待这些修改；关闭页面前请确认显示“已保存到当前账户”。单次勾选仅更新对应行，倍率和优惠索引在目录更新时重建。同一账户重叠的目录刷新、积分查询共用在途请求，不同账户独立处理。

控制台支持窄屏和系统深色模式，不需要加载外部字体或图标。

## 签到与积分

“签到领积分”先向账号所属官网查询今日状态，未签到时提交实际的每日签到请求。今日已签到会直接显示完成；上游返回错误时显示失败，不会把访问成长中心视为领取成功。国际版和企业账号跳过每日签到。

**对"已签到"的判定**（与同类项目对齐实测口径）：上游用业务码表示"今天已经签过"，
本项目把 `10001`（hub 实测）与 `14001`（panel 实测）都按"已签到"处理，而不是显示失败。
另外，上游偶发的 `HTTP 500 / code 10000` 抖动会做**有界重试**（最多 3 次、共 1.5s/3s 间隔）；
`11101` 之类的参数错误以及"今日已签到"**不重试**——重试只会原样再失败一次。
网络层/解析层错误**绝不**当成"已签到"，否则停机补签遇到抖动会误记成功、账号当天实际没签到。

开启“可用积分”卡片中的“每日自动签到”后，程序会检查所有已保存的国内版个人账号。按北京时间换日，成功后当日不重复提交，失败后间隔 30 分钟重试。开启开关后会立即唤醒检查；重启后会重新核对服务端状态。该功能需要 wb2api 保持运行。

自动签到开关单独保存在数据目录的 `settings.json`，与 Windows 自启设置互不影响。

积分请求使用与登录账号匹配的官网域，国内版优先读取资源汇总，必要时查询资源包明细；国际版使用其资源列表接口。余额兼容字符串精确值、零余额及嵌套响应，不会把套餐总量、已用量或错误码当作余额。查询失败会保留上次余额并标注错误和上次成功更新时间。手动签到成功后会刷新余额；到账情况以服务端返回为准。

## wb2api 开机自启（Windows / Linux）

### Windows

在侧栏底部开启“wb2api 开机自启”（手机上位于顶部导航下方）。程序仅写入当前 Windows 用户的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `wb2api-lite` 项，不调用任务计划服务，也不需要提权。

当前用户登录 Windows 后，该启动项会运行当前 Python 和本项目脚本，并保留监听地址、端口、数据目录及凭证路径。可用时使用 `pythonw.exe` 在后台运行。设置完成后请保留 Python 和项目原路径；移动项目后，在新位置关闭再开启自启。

### Linux

同一个开关在 Linux 上写入 **systemd 用户级单元**（`~/.config/systemd/user/wb2api-lite.service`），
不需要 `sudo`，也不改动系统目录：

```bash
python3 wb2api.py                 # 前台运行
python3 wb2api.py --port 8788     # 换端口
```

- 开启后执行 `systemctl --user enable wb2api-lite.service`，可用 `systemctl --user status wb2api-lite` 查看状态。
- **注销后仍要常驻**，需要执行一次 `loginctl enable-linger $USER`。程序不会替你改这项系统设置，只在提示里告诉你怎么做。
- 没有 systemd 的环境（WSL、部分容器）会明确提示"不支持"，请改用 `tmux` / `nohup` / 容器编排自行常驻。
- 与 Windows 一样，使用 `--api-key` 或 `WB_API_KEY` 启动时不允许开启自启（重启后密钥会变）。

关闭开关会删除此启动项，重复关闭也会成功。此设置仅启动 wb2api 代理自身。

## 常用参数

```powershell
python wb2api.py --port 8788
python wb2api.py --data-dir .\data
python wb2api.py --no-ui
python wb2api.py --login --realm cn
```

| 参数                              | 说明                  |
| ------------------------------- | ------------------- |
| `--host`                        | 监听地址，默认 `127.0.0.1` |
| `--port`                        | 监听端口，默认 `8787`      |
| `--realm global` / `--realm cn` | 命令行登录版本             |
| `--data-dir`                    | 状态文件目录              |
| `--no-ui`                       | 关闭网页控制台和管理接口        |

## 接口

| 方法与路径                       | 用途                               |
| --------------------------- | -------------------------------- |
| `POST /v1/chat/completions` | Chat Completions                 |
| `POST /v1/responses`        | Responses                        |
| `POST /v1/messages`         | Anthropic Messages（支持流式、图片和工具调用） |
| `GET /v1/models`            | 当前可见模型                           |
| `GET /healthz`              | 服务健康状态                           |
| `GET /`                     | 中文网页控制台                          |

默认地址为 `http://127.0.0.1:8787/v1`，接口密钥在网页控制台显示。

Anthropic SDK 接入时，将 `ANTHROPIC_BASE_URL` 设置为 `http://127.0.0.1:8787`（SDK 会自动请求 `/v1/messages`），将 `ANTHROPIC_API_KEY` 设置为网页控制台显示的本地访问密钥；也可以直接使用 `x-api-key` 请求头。代理会把 Anthropic Messages 请求转换为上游对话请求，再将响应转换回 Anthropic 格式。

模型填写控制台中已启用的实际模型 ID，沿用当前账户的模型选择、别名和默认模型配置。控制台“接入配置 → Anthropic”可生成包含 `ANTHROPIC_MODEL` 的配置；使用 Claude Code 时可合并到 `~/.claude/settings.json`。

当前支持文本、URL/Base64 图片、自定义工具及多轮工具结果。图片是否可用取决于所选上游模型。暂不提供 Anthropic 服务端工具、PDF 内容块、原生扩展思考签名和 `/v1/messages/count_tokens`；缓存控制标记不会启用原生 Anthropic 提示缓存。

Responses 的 `previous_response_id` 历史保存在内存中，最多 256 条、保留 6 小时，序列化历史总容量最多 32 MiB（不是进程总内存上限）。超过条数或容量时淘汰最早存入的历史，单条超过容量上限则不缓存。访问缓存及后台每 30 秒的维护会清理过期记录。重启、过期或淘汰后，相应 ID 无法续接，需要重新提供上下文。

## Claude Code 使用说明

### 兼容性注意

由于 WorkBuddy 的渠道校验策略，**Claude Code 使用 Anthropic Messages 接口（`/v1/messages`）可能遇到 `code=11128` 错误**。这是 WorkBuddy 在检测到非官方客户端身份特征后主动拒绝请求，与 wb2api 的格式转换无关。

### 推荐方案：使用 OpenAI 接口

**在 Claude Code 配置中使用 `/v1/chat/completions` 接口**（`~/.claude/settings.json`）：

```json
{
  "openai": {
    "baseURL": "http://127.0.0.1:8787/v1",
    "apiKey": "wb-your-api-key",
    "model": "claude-sonnet-4-20250514"
  }
}
```

或者配置为 Anthropic 提供商但使用 OpenAI 格式：

```json
{
  "anthropic": {
    "apiProvider": "openai",
    "openai": {
      "baseURL": "http://127.0.0.1:8787/v1",
      "apiKey": "wb-your-api-key",
      "model": "claude-sonnet-4-20250514"
    }
  }
}
```

**要点**：
- 使用 `/v1/chat/completions` 接口，wb2api 提供完整支持
- `model` 填写控制台中已启用的实际模型 ID
- `apiKey` 从网页控制台的"接入配置"中获取

### 技术背景

WorkBuddy 的渠道校验不仅检查 HTTP 请求头，还会分析 system prompt 内容。Claude Code 的默认 system prompt 包含身份标识（如 "You are Claude Code, Anthropic's official CLI"），会被检测为非官方客户端而拒绝。使用 OpenAI 接口可以避开这一检测机制。

## 国际版出站身份（渠道校验）

上游会校验请求是否"像官方客户端发出的"，不通过时返回 `400 / code 11128`（`request illegal`，`displayTips` 提示"这次调用没通过渠道校验，可能不是官方客户端发出的"）。2026-10-19 起该校验对国际版明显收紧，只补 `system` 首条消息已不足以通过。

代理按对 WorkBuddy 5.8.4 的抓包对齐了国际版出站身份，主要包括：

- 版本与 UA：`X-IDE-Version: 5.8.4`，UA `WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0`（可用 `WB_INTL_CLIENT_VERSION` / `WB_INTL_CLI_VERSION` 覆盖）。
- 身份头：`X-Product: SaaS`、`X-Agent-Type: main`、`X-Agent-Intent: craft`、`X-Private-Data: true`。
- 会话头族：`X-Conversation-ID`（带连字符 UUID，每请求现生成）、`X-Conversation-Request-ID` 与 `X-Root-Request-ID` 同值、`X-Conversation-Message-ID` 与 `X-Request-ID` 同值，后四者为 32 位 hex。
- 链路头族：`X-Trace-ID`、`traceparent`、`b3`、`X-B3-TraceId`、`X-B3-SpanId`、`X-B3-Sampled`。
- 对话请求不再发送 `X-Machine-ID` / `X-Session-ID`：官方只在反馈上报里发，恒定的派生值反而是网关特征。
- 国际版不发送 `stream_options`（上游不认，国内版仍依赖它取 usage）。

**设备/会话标识按账号稳定派生**（技术路线参考同类项目
[workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 的 `wb_fingerprint.derive_id`）：
计费/上报类请求携带的 `X-Machine-ID`、`X-Session-ID` 现在输出**带连字符的 UUIDv4 形状**，
而不是以前 36 位无连字符的裸 hex——裸 hex 是上游见不到的形态，本身就是一处网关特征。

取值仍**按账号 UID 派生**（同一账号跨重启恒定、不同账号互异），而不是像官方客户端那样
按进程取一个随机值：本程序是一个长驻进程同时代表多个账号，若按进程取值，所有账号会共用
同一个 `machineId`，反而把它们关联到同一台机器上。身份模块的回归测试在 `tests/test_identity.py`。

国内版没有对应抓包且当前正常，出站身份维持原样。若上游再次调整，优先核对官方客户端版本与抓包字段，再改这里。

## 离线回归测试

测试使用模拟账户和上游事件流，不访问真实账户、上游服务或 Windows 自启设置。

```powershell
python -B -m unittest discover -s tests -v
node tests/check_ui.cjs
```

Python 测试覆盖 Anthropic 转换、OpenAI 请求体归一、HTTP 路由与鉴权、请求正文边界、凭证与账号库读写、积分解析、模型勾选竞态、模型目录缓存合并、Responses 协议收尾、用量文件抗损坏、日志脱敏、国际版出站身份（会话头族、ID 形态、5.8.4 版本/UA 与国内版不受影响）、**账号池（双池隔离、换号、会话粘性、冷却熔断、端到端 failover）**、**跨平台自启（Linux systemd 单元生成与开关）**、**身份标识形态** 和 **每日签到（已签到码、瞬时重试、状态预检查）**。Node.js 检查验证网页脚本语法、无外链资源、DOM 引用完整性，以及配置生成（Anthropic 使用根地址、实际模型 ID 及访问密钥，Codex 使用 Responses 接口）；不包含浏览器布局测试。

当前共 **272 项 Python 测试**。

## 已知限制

这些是当前版本有意保留的行为，不是缺陷：

- **Claude Code 与 Anthropic Messages**：由于 WorkBuddy 的渠道校验策略，Claude Code 使用 `/v1/messages` 接口可能遇到 `code=11128` 错误。推荐使用 `/v1/chat/completions` 接口（OpenAI 格式），wb2api 对该接口提供完整支持。详见"Claude Code 使用说明"章节。
- **Responses 端点**：不支持的内容块（`input_file`、`input_audio`、`file_id` 图片、`item_reference`）会被忽略而不是报错；模型拒答（refusal）不会转成 `refusal` 内容块；`finish_reason=length` 时 `response.status` 为 `incomplete`，但 `item.status` 仍是 `completed`。
- **流式与非流式差异**：`model` 字段来源不同（非流式优先用上游回显，流式用请求值），`parallel_tool_calls` 只在非流式响应里出现。
- **默认模型回退**：只有当模型目录完整（三路 `/v3/config` 全部成功）且该模型未被勾选时，未知模型名才会回退到默认模型。目录残缺时宁可让上游返回错误，也不静默换模型。
- **模型目录**：部分路由失败时与上次缓存取并集，因此上游已下线的模型可能多保留一段时间。
- **用量统计**：`usage.jsonl` 只回放尾部 4MB；跨午夜并发记录存在极窄的归日竞态；多账号共用同一份汇总，不按账号分组。
- **启动日志中的访问密钥只显示掩码**；`--setup` 向导会为了让你复制而打印完整密钥。

## 本地状态文件

运行时可能产生 `accounts.json`、`auth.json`、`api_key.txt`、`selection.json`、`models_cache.json`、`usage.jsonl` 和 `settings.json`。这些文件包含本机账号、令牌、密钥、历史数据或个人设置，不应提交或分享。要恢复未登录状态，停止程序后删除这些运行时文件。

签到和积分接口参考以下项目核对，并结合本项目的账号隔离与令牌刷新机制实现：

- [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)：`wb_accounts.py`
- [changexbc/workbuddy-switch](https://github.com/changexbc/workbuddy-switch)：`checkin.rs`、`credits.rs`、`variant.rs`
