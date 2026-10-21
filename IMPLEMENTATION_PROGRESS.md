# wb2api 改进实施进度

## 已完成的工作

### P0: 安全改进 - API 密钥脱敏 ✅

**目标**：防止完整 API 密钥出现在日志、错误消息和 SSE 响应中。

**实现细节**：

1. **新增函数** (wb2api.py:4199-4206)
   ```python
   def _mask_api_key(key: str) -> str:
       """日志里只显示密钥前缀与后缀，避免完整密钥进入日志/监控系统。"""
   ```
   - 16 字符以上显示 `前7...后4`
   - 更短的密钥完全隐藏为 `***`

2. **新增函数** (wb2api.py:4208-4217)
   ```python
   def _sanitize_for_log(obj) -> str:
       """递归脱敏敏感字段，用于错误响应和日志。"""
   ```
   - 递归处理字典、列表
   - 脱敏字段：`api_key`, `apiKey`, `accessToken`, `refreshToken`, `openid`
   - 脱敏 Authorization header 中的 Bearer token

3. **应用点**：
   - ✅ `/v1/chat/completions` 错误响应 (5135行)
   - ✅ SSE 流错误处理 (5168行)
   - ✅ `responses_stream` 错误处理 (5201行)
   - ✅ 所有错误日志中的敏感数据

4. **测试覆盖** (tests/test_security.py)
   - ✅ 密钥掩码测试 (边界、短密钥、空值)
   - ✅ 日志脱敏测试 (嵌套对象、数组、Bearer token)
   - ✅ 所有测试通过

**验证命令**：
```bash
pytest tests/test_security.py -v
pytest tests/ -k "not test_security" -v  # 回归测试
```

---

### P1: 池级别模型配置 - 后端 🚧 (90% 完成)

**目标**：国内账号池、国际账号池各自共用一套模型列表，减少重复配置。

**实现细节**：

1. **数据结构**：
   ```json
   {
     "models": [...],           // 全局默认（向后兼容）
     "aliases": {...},
     "default_model": "...",
     "enforce": false,
     
     "pools": {                 // 新增：池级别配置
       "cn": {
         "models": ["deepseek-v4.1-flash", ...],
         "aliases": {"gpt-4o": "deepseek-v4.1-flash"}
       },
       "global": {
         "models": ["gpt-4o", "claude-3-5-sonnet", ...],
         "aliases": {}
       }
     },
     
     "accounts": {              // 账号级别配置（覆盖池配置）
       "account-1": ["model-a", "model-b"]
     },
     "activeAccountId": "account-1"
   }
   ```

2. **新增全局变量** (wb2api.py:234-235)
   ```python
   _pool_selections: dict[str, list[str]] = {"cn": [], "global": []}
   _pool_aliases: dict[str, dict[str, str]] = {"cn": {}, "global": {}}
   ```

3. **更新函数**：
   - ✅ `load_selection()` - 从 JSON 加载池配置 (4065-4150行)
   - ✅ `save_selection()` - 保存池配置到 JSON (4218-4268行)
   - ✅ `admin_state()` - 导出池配置到前端 (4463-4507行)

4. **API 更新**：
   - ✅ `GET /admin/state` 返回 `poolModels` 和 `poolAliases`
   - ✅ `POST /admin/selection` 接受 `poolModels` 和 `poolAliases` 参数

**剩余工作**：
- ⏳ 前端 UI 实现（见下文）
- ⏳ 账号切换时优先使用池级别配置
- ⏳ UI 中添加"应用到同池所有账号"按钮

---

## 待完成的工作

### P1: 池级别模型配置 - 前端 UI

**需要修改的文件**：`ui.html`

**实现步骤**：

1. **在模型管理页面添加池级别配置 UI**
   - 在账户卡片中显示当前账号所属池（cn/global）
   - 添加"池配置"tab，允许配置当前池的默认模型
   - 添加"应用到当前池"按钮（将当前勾选应用到整个池）

2. **修改 `STATE` 对象**
   ```javascript
   STATE = {
     ...existingFields,
     poolModels: {cn: [], global: []},
     poolAliases: {cn: {}, global: {}}
   }
   ```

3. **修改 `drainSelections()` 函数** (672-701行)
   - 添加对 `poolModels` 和 `poolAliases` 的提交支持
   - 请求体格式：
     ```json
     {
       "models": [...],
       "accountId": "...",
       "poolModels": {
         "cn": [...],
         "global": [...]
       },
       "poolAliases": {
         "cn": {...},
         "global": {...}
       }
     }
     ```

4. **添加池配置视图**
   - 新增 tab 或折叠面板显示池配置
   - 显示当前池已配置的模型
   - 允许批量勾选/取消
   - "复制到账号配置"按钮（将池配置复制到当前账号）

5. **视觉反馈**
   - 标记哪些模型是从池配置继承的
   - 标记哪些是账号特有的配置

**参考位置**：
- 模型列表渲染：`renderModels()` 函数
- 勾选处理：`bulk()` 函数 (733-749行)
- 保存逻辑：`persistConfig()` / `drainSelections()` (672-721行)

---

### P2: 模型目录缓存分级

**目标**：按池缓存模型目录，减少账号切换延迟。

**实现思路**：

1. **新增缓存结构**：
   ```python
   _models_cache_by_pool: dict[str, dict] = {
       "cn": {"data": [], "at": 0},
       "global": {"data": [], "at": 0}
   }
   ```

2. **修改 `cached_models()` 函数**
   - 检查当前账号的 realm
   - 优先使用池级别缓存
   - 缓存键：`(realm, sorted_model_ids)`
   - TTL：与当前一致（30分钟）

3. **后台刷新**
   - 扩展 `refresh_token_loop()` 定时刷新池缓存
   - 交替刷新不同池，避免集中请求

**预期收益**：
- 切换同池账号时无需重新拉取模型目录
- 减少 `/models` 请求次数约 50%

---

### P3: 账号池预热机制

**目标**：启动时验证所有账号 token，请求到达时账号已就绪。

**实现思路**：

1. **启动时预热** (`startup()` 函数)
   ```python
   async def warmup_pool():
       for aid, acct in _accounts.items():
           if "accessToken" in acct:
               try:
                   await verify_token(acct)
                   acct["_status"] = "ready"
               except Exception as e:
                   acct["_status"] = "failed"
                   acct["_error"] = str(e)
   ```

2. **状态标记**
   - `ready`: token 有效，可立即使用
   - `refreshing`: 正在刷新 token
   - `failed`: 验证失败，需要重新登录
   - `unknown`: 未验证

3. **UI 显示**
   - 在账号池状态中显示各账号健康状态
   - 账号选择器中标记失效账号

**预期收益**：
- 消除首次请求的 token 验证延迟
- 提前发现账号失效问题

---

### P4: 账号池管理视图

**目标**：新增"账号池总览"页面，统一管理所有账号。

**UI 设计**：

1. **新增导航项** (ui.html:63-67)
   ```html
   <button data-view="pool" onclick="showView('pool')">
     <svg class="icon"><use href="#i-users"/></svg>
     账号池
   </button>
   ```

2. **池总览页面**
   - 按池分组显示账号（国内版/国际版）
   - 每个账号显示：
     - 余额/积分
     - 到期时间
     - 健康状态（就绪/刷新中/失效）
     - 最后使用时间
   - 批量操作：
     - 批量刷新 token
     - 批量配置模型
     - 批量删除账号

3. **池级别操作**
   - 配置池的默认模型列表
   - 查看池的使用统计
   - 导出/导入池配置

**实现参考**：
- 账号卡片样式：`#card-login` (82-103行)
- 表格布局：`.tablebox` (27行)

---

## 优先级建议

根据收益和工作量：

1. **立即完成**：
   - ✅ P0: 安全改进（已完成）
   - 🔄 P1: 池级别模型配置前端（预计 2-3 小时）

2. **短期**（1-2 天）：
   - P3: 账号池预热（预计 3-4 小时）
   - P2: 模型目录缓存分级（预计 2-3 小时）

3. **中期**（1 周内）：
   - P4: 账号池管理视图（预计 1 天）
   - 其他优先级建议中的 P2 项

---

## 测试清单

### 已测试 ✅
- [x] API 密钥脱敏（单元测试）
- [x] 日志脱敏（单元测试）
- [x] 现有功能回归

### 待测试
- [ ] 池级别配置加载
- [ ] 池级别配置保存
- [ ] 账号切换时优先使用池配置
- [ ] 池配置与账号配置的优先级
- [ ] UI 显示池配置
- [ ] UI 保存池配置
- [ ] 向后兼容性（旧 selection.json 格式）

---

## 已知问题

1. **池配置优先级未实现**
   - 当前仅实现了配置存储
   - 账号切换时尚未使用池配置
   - 需要在 `switch_account_direct()` 中添加逻辑

2. **UI 尚未更新**
   - 后端 API 已就绪
   - 前端未添加池配置界面
   - 当前仍为账号级别配置

---

## 下一步行动

1. **继续 P1 前端实现**：
   - 修改 `ui.html`，添加池配置 UI
   - 测试完整流程
   - 验证向后兼容性

2. **实现池配置优先级**：
   - 修改账号切换逻辑
   - 优先使用池配置，除非账号有特殊配置
   - 添加"继承池配置"标记

3. **完善文档**：
   - 更新 README
   - 添加池配置使用说明
   - 添加迁移指南

---

## 代码变更摘要

### 新增文件
- `tests/test_security.py` (185 行) - 安全测试

### 修改文件
- `wb2api.py`
  - 新增 `_mask_api_key()` 函数 (4199-4206行)
  - 新增 `_sanitize_for_log()` 函数 (4208-4217行)
  - 新增全局变量 `_pool_selections`, `_pool_aliases` (234-235行)
  - 更新 `load_selection()` (4065-4150行)
  - 更新 `save_selection()` (4218-4268行)
  - 更新 `admin_state()` (4463-4507行)
  - 应用脱敏到错误处理 (3 处)

### 待修改文件
- `ui.html` - 池配置 UI（待实现）

---

**总计**：
- 代码行数：~250 行（后端）
- 测试行数：~185 行
- 剩余工作量：~400 行（前端 UI + 优先级逻辑）
