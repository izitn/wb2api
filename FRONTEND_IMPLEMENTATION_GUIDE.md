# 池级别配置 - 前端实现指南

## 概览

后端已完成，`POST /admin/selection` 支持 `poolModels` 和 `poolAliases` 参数。
前端需要添加 UI 来配置和显示池级别的模型选择。

---

## 1. 数据流

### 后端 → 前端
`GET /admin/state` 返回：
```json
{
  "poolModels": {
    "cn": ["deepseek-v4.1-flash", "deepseek-chat"],
    "global": ["gpt-4o", "claude-3-5-sonnet"]
  },
  "poolAliases": {
    "cn": {"gpt-4o": "deepseek-v4.1-flash"},
    "global": {}
  },
  ...otherFields
}
```

### 前端 → 后端
`POST /admin/selection` 接受：
```json
{
  "models": [...],           // 当前账号的模型选择
  "accountId": "...",
  "poolModels": {            // 可选：更新池配置
    "cn": [...],
    "global": [...]
  },
  "poolAliases": {           // 可选：更新池别名
    "cn": {...},
    "global": {...}
  }
}
```

---

## 2. UI 设计方案

### 方案 A: 在现有模型管理页添加"池配置"区域

**优点**：无需新增页面，上下文明确
**缺点**：页面可能过长

**布局**：
```
┌─────────────────────────────────────┐
│ 模型管理                             │
├─────────────────────────────────────┤
│ [当前账户] [积分] [用量]             │  ← 现有的 summary-grid
├─────────────────────────────────────┤
│ 🔹 账号级别配置                      │  ← 现有的模型表格
│ ☑️ model-1                           │
│ ☑️ model-2                           │
│                                      │
│ [应用到整个池] [从池配置继承]        │  ← 新增按钮
├─────────────────────────────────────┤
│ 🔹 池级别配置 (国内版)               │  ← 新增区域
│ ☑️ deepseek-v4.1-flash               │
│ ☑️ deepseek-chat                     │
│                                      │
│ 提示：池配置应用于所有国内版账号      │
└─────────────────────────────────────┘
```

### 方案 B: 使用 Tab 切换

**优点**：界面清爽，不占用垂直空间
**缺点**：切换 tab 增加操作步骤

**布局**：
```
┌─────────────────────────────────────┐
│ [ 当前账号 ] [ 池配置：国内版 ]      │  ← Tabs
├─────────────────────────────────────┤
│ ☑️ model-1                           │
│ ☑️ model-2                           │
│ ...                                  │
└─────────────────────────────────────┘
```

**推荐**：方案 A（更直观，符合"共用一套"的语义）

---

## 3. 代码实现步骤

### Step 1: 更新 STATE 对象

找到 `syncState()` 函数中解析 `admin/state` 的部分，添加：

```javascript
STATE = {
  ...existingFields,
  poolModels: d.poolModels || {cn: [], global: []},
  poolAliases: d.poolAliases || {cn: {}, global: {}}
};
```

**参考位置**：搜索 `STATE = {` 或 `STATE.selected =`

---

### Step 2: 添加池配置存储

在全局变量区添加：

```javascript
let POOL_SELECTIONS = {cn: new Set(), global: new Set()};  // 池级别勾选
let POOL_EDITING = false;  // 是否正在编辑池配置
```

---

### Step 3: 渲染池配置区域

在 `#card-models` 表格后添加新 card：

```html
<section class="card" id="card-pool-config" style="display:none;">
  <div class="card-heading">
    <h2>池级别配置</h2>
    <span class="pill" id="pool-realm-label">国内版</span>
  </div>
  <p class="note">
    池配置应用于所有<span id="pool-note-realm">国内版</span>账号。
    切换账号时，如果账号未单独配置模型，将自动使用池配置。
  </p>
  <div class="quick-actions">
    <button type="button" class="sm" onclick="copyAccountToPool()">
      从当前账号复制
    </button>
    <button type="button" class="sm" onclick="copyPoolToAccount()">
      应用到当前账号
    </button>
  </div>
  <div class="tablebox">
    <div class="scroll">
      <table id="pool-models-table">
        <thead>
          <tr>
            <th class="c"><input type="checkbox" id="pool-select-all" onchange="poolBulkSelect()"></th>
            <th>模型</th>
            <th class="c">倍率</th>
          </tr>
        </thead>
        <tbody id="pool-models-body"></tbody>
      </table>
    </div>
  </div>
</section>
```

---

### Step 4: 渲染池配置模型列表

添加函数：

```javascript
function renderPoolModels() {
  if (!STATE || !STATE.loggedIn) {
    $('#card-pool-config').style.display = 'none';
    return;
  }
  
  const realm = STATE.realm;  // 'cn' or 'global'
  const poolModels = STATE.poolModels[realm] || [];
  POOL_SELECTIONS[realm] = new Set(poolModels);
  
  $('#card-pool-config').style.display = 'block';
  $('#pool-realm-label').textContent = realm === 'cn' ? '国内版' : '国际版';
  $('#pool-note-realm').textContent = realm === 'cn' ? '国内版' : '国际版';
  
  const tbody = $('#pool-models-body');
  tbody.innerHTML = '';
  
  const allModels = knownModels();  // 获取所有可用模型
  allModels.forEach(model => {
    const checked = POOL_SELECTIONS[realm].has(model.id);
    const factor = effectiveFactor(model);
    const row = document.createElement('tr');
    row.innerHTML = `
      <td class="c">
        <input type="checkbox" ${checked ? 'checked' : ''} 
               onchange="togglePoolModel('${realm}', '${model.id}')">
      </td>
      <td>
        <div class="model-identity">
          <span class="model-glyph">${model.id[0].toUpperCase()}</span>
          <div>
            <div class="model-name">${model.id}</div>
          </div>
        </div>
      </td>
      <td class="c">${factor !== null ? '×' + factor : '—'}</td>
    `;
    tbody.appendChild(row);
  });
}

function togglePoolModel(realm, modelId) {
  if (POOL_SELECTIONS[realm].has(modelId)) {
    POOL_SELECTIONS[realm].delete(modelId);
  } else {
    POOL_SELECTIONS[realm].add(modelId);
  }
  savePoolSelection(realm);
}

function poolBulkSelect() {
  const checked = $('#pool-select-all').checked;
  const realm = STATE.realm;
  if (checked) {
    knownModels().forEach(m => POOL_SELECTIONS[realm].add(m.id));
  } else {
    POOL_SELECTIONS[realm].clear();
  }
  renderPoolModels();
  savePoolSelection(realm);
}
```

---

### Step 5: 保存池配置

添加函数：

```javascript
function savePoolSelection(realm) {
  if (!STATE || !STATE.loggedIn) return;
  
  const poolModels = {
    cn: STATE.poolModels.cn || [],
    global: STATE.poolModels.global || []
  };
  poolModels[realm] = [...POOL_SELECTIONS[realm]];
  
  const poolAliases = {
    cn: STATE.poolAliases.cn || {},
    global: STATE.poolAliases.global || {}
  };
  
  return api('/admin/selection', {
    method: 'POST',
    body: JSON.stringify({
      models: [...SELECTED],  // 当前账号的选择
      accountId: STATE.activeAccountId,
      poolModels: poolModels,
      poolAliases: poolAliases
    })
  }).then(d => {
    STATE.poolModels = d.poolModels || poolModels;
    STATE.poolAliases = d.poolAliases || poolAliases;
    $('#save-status').textContent = '池配置已保存';
  }).catch(e => {
    alert('保存池配置失败：' + e.message);
  });
}

function copyAccountToPool() {
  const realm = STATE.realm;
  POOL_SELECTIONS[realm] = new Set([...SELECTED]);
  renderPoolModels();
  savePoolSelection(realm);
}

function copyPoolToAccount() {
  const realm = STATE.realm;
  SELECTED = new Set([...POOL_SELECTIONS[realm]]);
  updateSelectionView();
  saveSelection();
}
```

---

### Step 6: 在 `syncState()` 后调用渲染

找到 `syncState()` 函数的末尾，添加：

```javascript
async function syncState() {
  // ... existing code ...
  renderLogin();
  renderModels();
  renderPoolModels();  // ← 新增
  renderOut();
  updateUsage();
}
```

---

## 4. 样式调整

如果需要自定义样式，添加到 `<style>` 块：

```css
#card-pool-config {
  margin-top: 20px;
  border-top: 2px solid var(--accent-line);
}

#card-pool-config .note {
  background: var(--accent-bg);
  padding: 10px 12px;
  border-radius: 6px;
  border-left: 3px solid var(--accent);
}

#pool-realm-label {
  background: var(--accent-bg);
  color: var(--accent);
  font-weight: 600;
}
```

---

## 5. 测试清单

- [ ] 登录后显示池配置区域
- [ ] 勾选/取消勾选池配置中的模型
- [ ] 点击"从当前账号复制"
- [ ] 点击"应用到当前账号"
- [ ] 保存后刷新页面，池配置仍存在
- [ ] 切换账号，池配置根据 realm 自动切换
- [ ] 国内版账号显示国内版池配置
- [ ] 国际版账号显示国际版池配置

---

## 6. 向后兼容性

**关键**：后端已处理向后兼容，`poolModels` 和 `poolAliases` 为可选字段。

- 旧 selection.json 不包含 `pools` 字段 → 后端返回空池配置
- 前端不发送 `poolModels` → 后端不修改池配置
- 仅当用户明确操作池配置时才发送 `poolModels`

---

## 7. 进一步优化

### 自动应用池配置

在账号切换时，如果账号没有特殊配置，自动使用池配置：

```javascript
async function switchAccount() {
  // ... existing code ...
  
  // 如果账号没有自定义配置，使用池配置
  const accountModels = STATE.accounts[aid] || [];
  const poolModels = STATE.poolModels[STATE.realm] || [];
  
  if (accountModels.length === 0 && poolModels.length > 0) {
    SELECTED = new Set(poolModels);
    $('#save-status').textContent = '已应用池配置';
  } else {
    SELECTED = new Set(accountModels);
  }
  
  updateSelectionView();
}
```

### 标记继承自池配置的模型

在模型列表中标记哪些是从池继承的：

```javascript
function renderModels() {
  // ... existing code ...
  
  const poolModels = new Set(STATE.poolModels[STATE.realm] || []);
  const isFromPool = poolModels.has(model.id) && !hasAccountOverride(model.id);
  
  if (isFromPool) {
    row.classList.add('from-pool');  // 添加视觉标记
  }
}
```

添加样式：
```css
tr.from-pool td {
  background: color-mix(in srgb, var(--accent-bg) 30%, transparent);
}
tr.from-pool .model-name:after {
  content: " (池配置)";
  font-size: 10px;
  color: var(--accent);
}
```

---

## 快速集成清单

1. ✅ 后端 API 已就绪
2. ⏳ 添加全局变量 `POOL_SELECTIONS`
3. ⏳ 在 HTML 中添加 `#card-pool-config`
4. ⏳ 实现 `renderPoolModels()`
5. ⏳ 实现 `savePoolSelection()`
6. ⏳ 实现 `copyAccountToPool()` 和 `copyPoolToAccount()`
7. ⏳ 在 `syncState()` 中调用 `renderPoolModels()`
8. ⏳ 测试所有场景

**预计工作量**：2-3 小时

---

## 调试技巧

### 检查后端响应
```javascript
api('/admin/state').then(d => console.log('poolModels:', d.poolModels));
```

### 检查前端状态
```javascript
console.log('STATE.poolModels:', STATE.poolModels);
console.log('POOL_SELECTIONS:', POOL_SELECTIONS);
```

### 模拟保存请求
```javascript
api('/admin/selection', {
  method: 'POST',
  body: JSON.stringify({
    models: ['gpt-4o'],
    accountId: STATE.activeAccountId,
    poolModels: {cn: ['deepseek-chat'], global: ['gpt-4o']}
  })
}).then(console.log);
```

---

## 相关函数位置

- `syncState()` - 同步状态
- `renderModels()` - 渲染模型列表
- `saveSelection()` - 保存账号级别配置 (716行)
- `drainSelections()` - 批量保存队列 (672行)
- `bulk()` - 批量勾选操作 (733行)
- `showView()` - 切换页面视图

---

**需要帮助？** 参考现有的 `renderModels()` 函数实现，照搬逻辑即可。
