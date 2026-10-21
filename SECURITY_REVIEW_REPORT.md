# wb2api.py 代码审查与交接报告

**审查日期**：2026-10-21
**审查范围**：wb2api.py 全文件；重点检查 HTTP 管理接口、凭证与密钥、上游请求/重试、账号池、Responses 历史、日志与并发控制。测试文件用于核对回归覆盖。
**基线提交**：0594b67；工作区 wb2api.py 与该提交一致。
**总体结论**：发现 1 项中危日志泄露风险、1 项低危测试失配；其余已检查的核心路径未发现可确认的高危问题。合入前建议修复日志风险，并修正测试夹具。

## 发现

### [P2 / 中危] 上游错误文本未脱敏即写入日志

**位置**：wb2api.py:2600-2623、wb2api.py:5057-5066、wb2api.py:5140-5146

read_error() 将上游响应正文解析为错误摘要并返回；在无法解析时会把整个响应正文作为 msg。调用方随后通过 log() 记录完整错误。流式处理中，上游错误帧中的 message/msg 也被拼进日志和返回客户端的错误文本。

上游错误正文可能回显请求内容、令牌或其他敏感信息；如果其中含 CR/LF，还可能伪造多行日志。record_usage() 对最终错误摘要做了凭证替换，但它发生在另一条路径，不能保护这里的 stderr 日志。

**建议**：日志只记录状态码、上游业务码和经长度限制、控制字符压平及敏感字段脱敏后的摘要；不要写入原始错误正文。客户端错误可保留必要诊断信息，但应做长度上限和凭证脱敏。增加覆盖 read_error()、SSE 错误帧与日志输出的测试，断言用户正文、账户 token、device token、本地 API key 和换行载荷不会出现在日志中。

### [P3 / 低危] gzip TTL 实现与两组回归测试的数据结构不匹配

**位置**：wb2api.py:140-160；tests/test_handoff_fixes.py:90-91；tests/test_handoff_v2.py:50-56

生产代码把 _gzip_disabled_bases 定义为 dict[str, float]，用于存储禁用到期时间；但两组测试仍调用集合专用的 discard(base)。在当前工作区运行 tests.test_handoff_v2 时，7 个 UpstreamSafetyTests 在 setUp() 直接报 AttributeError: dict 没有 discard 方法。tests.test_handoff_fixes 的 gzip 业务错误用例也使用同一旧接口。

这不会直接影响正常请求，但使 gzip 拒绝回退、重试与重放安全相关测试无法验证当前实现。

**建议**：测试夹具按字典语义清理，例如在 _gzip_stats_lock 下调用 pop(base, None)；验证正常响应、TTL 到期、永久禁用（TTL 为 0）和非 gzip 业务 400 不触发降级重放。

## 已核查的安全与正确性控制

- **管理面鉴权**：回环绑定时校验 Host 并拒绝不匹配或 null Origin；非回环绑定时要求 API key、Bearer 或已验证的面板 cookie。/admin/state 返回本地 API key，因此这些检查是敏感边界。首次 ?pwd= 验证后重定向到干净 URL，cookie 设置 HttpOnly、SameSite=Strict；面板响应设置 no-store、no-referrer 和禁止被 frame 嵌入。
- **请求体边界**：限制 Content-Length，拒绝冲突长度与不支持的 chunked JSON；短读或边界不可信时关闭连接。拒绝请求时对可接受范围内的 chunked 正文做有界排空并设置 socket 超时。
- **凭证持久化**：账号库原子替换写入；_write_private() 尝试设置仅当前用户可读的文件权限。登录切换先持久化再替换内存态。刷新锁按账号隔离，并避免旧刷新响应覆盖重新登录后的新凭证。
- **上游重试与账号池**：只重试明确安全的连接建立失败和有限 429；对结果不确定的断连不自动重放。账号池按 realm 隔离，只在下游响应开始前换号，并限制尝试数。
- **Responses 历史**：会话缓存按 owner 隔离，限制条数、TTL 和序列化字节总量；客户端请求正文不能直接指定缓存 owner。
- **密钥与用量日志**：启动日志使用密钥掩码；用量日志不记录请求正文，并在错误摘要中移除已保存的本地/上游凭证。发现的日志风险见上方 P2。

## 验证记录

将 TEMP/TMP 指向仓库内可写临时目录后，完成以下检查：

- 通过：python -B -m py_compile wb2api.py
- 通过：tests.test_http，29 项
- 通过：tests.test_auth 与 tests.test_startup_key，30 项
- 通过：tests.test_pool 与 tests.test_pool_failover，41 项
- 通过：node tests/check_ui.cjs
- 未通过：tests.test_handoff_v2，17 项中 7 项因 gzip 集合/字典夹具不匹配失败
- 未完成：tests.test_handoff_fixes 首项 chunked socketpair 测试在此 Windows 沙箱中长时间阻塞；测试运行被停止。Python 全量测试也在进入该项后停滞，因此不能报告全量套件通过。

针对性 HTTP、凭证、自启动与账号池测试通过，不代表上游错误日志脱敏问题已有覆盖；目前缺少对应安全回归测试。

## 接手顺序

1. 先修复 read_error() 和 SSE 错误路径的日志清洗，并增加敏感值、CR/LF 回归测试。
2. 将两组 gzip 测试清理逻辑更新为字典操作，再分别运行两份交接测试。
3. 在可稳定运行 socketpair 用例的环境完成 python -B -m unittest discover -s tests -v，保存最终通过/失败总数和失败详情。
4. 若服务需要绑定非回环地址，部署侧应使用可靠的网络隔离与访问控制；管理 API 会暴露本地 API key 和账户摘要，不能将其当作面向公网的服务接口。

## 工作区交接状态

本轮删除了旧的 SECURITY_REVIEW_REPORT.md 内容，并以本报告替换；上一轮新增的 HANDOFF_REVIEW.md 已删除。ui.html 的既有未提交改动保留。审查没有修改 wb2api.py 或测试代码。
