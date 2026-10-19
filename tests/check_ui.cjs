#!/usr/bin/env node
/* eslint-env node, es2020 */
"use strict";

/**
 * ui.html 静态检查：语法、无外链、DOM id 齐全、配置生成行为验证。
 * 
 * 不启动浏览器、不测试布局，只验证脚本逻辑与安全约束。
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const UI_PATH = path.join(ROOT, "ui.html");

const failures = [];

function check(name, fn) {
  try {
    fn();
    console.log("  ✓ " + name);
  } catch (e) {
    failures.push(name + "：" + e.message);
    console.log("  ✗ " + name);
  }
}

const html = fs.readFileSync(UI_PATH, "utf8");

// ========== 1) 提取并语法检查脚本 ==========
const scriptBlocks = [];
const scriptRe = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
let match;
while ((match = scriptRe.exec(html)) !== null) {
  const attrs = match[1];
  const code = match[2];
  if (attrs.includes("src=")) {
    failures.push("检测到外链脚本：<script src=...>，不符合单文件要求");
  } else {
    scriptBlocks.push(code);
  }
}

check("提取到至少一个内联脚本块", () => {
  if (scriptBlocks.length === 0) throw new Error("未找到 <script> 标签");
});

const fullScript = scriptBlocks.join("\n");

check("JavaScript 语法无错误", () => {
  new vm.Script(fullScript, { filename: "ui.html" });
});

// ========== 2) 无外部资源引用 ==========
check("无外链样式表", () => {
  if (/<link\b/i.test(html)) throw new Error("检测到 <link> 标签");
  if (/@import\s+url\s*\(/i.test(html)) throw new Error("检测到 @import url(...)");
});

check("无外部字体或图标", () => {
  const extUrlRe = /url\(\s*['"]?https?:/i;
  if (extUrlRe.test(html)) throw new Error("检测到 url(http...) 引用");
});

// ========== 3) DOM id 完整性 ==========
const declaredIds = new Set();
const idRe = /\bid\s*=\s*["']([^"']+)["']/g;
while ((match = idRe.exec(html)) !== null) {
  declaredIds.add(match[1]);
}

const referencedIds = new Set();
const jqIdRe = /\$\(['"]#([A-Za-z0-9_-]+)['"]\)/g;
while ((match = jqIdRe.exec(fullScript)) !== null) {
  referencedIds.add(match[1]);
}

check("所有 $('#id') 引用的 id 都存在于 HTML 中", () => {
  for (const id of referencedIds) {
    if (!declaredIds.has(id)) {
      throw new Error(`脚本引用了不存在的 id="${id}"`);
    }
  }
});

// ========== 4) 安全约束 ==========
check("无 eval 或 new Function", () => {
  if (/\beval\s*\(/.test(fullScript)) throw new Error("检测到 eval(");
  if (/\bnew\s+Function\s*\(/.test(fullScript)) throw new Error("检测到 new Function(");
});

check("存在 escapeHtml 转义函数", () => {
  if (!/function\s+escapeHtml\s*\(/.test(fullScript)) {
    throw new Error("未找到 escapeHtml 函数定义");
  }
});

// ========== 5) 行为验证：模拟配置生成 ==========
function extractFunction(src, name) {
  const fnRe = new RegExp(`function\\s+${name}\\s*\\([^)]*\\)\\s*\\{`);
  const startMatch = fnRe.exec(src);
  if (!startMatch) throw new Error(`未找到函数 ${name}`);
  
  let i = startMatch.index + startMatch[0].length - 1; // '{' 的位置
  let depth = 0;
  const len = src.length;
  
  while (i < len) {
    const ch = src[i];
    
    // 跳过字符串
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      i++;
      while (i < len) {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === quote) { i++; break; }
        i++;
      }
      continue;
    }
    
    // 跳过注释
    if (ch === "/" && src[i + 1] === "/") {
      i = src.indexOf("\n", i);
      if (i === -1) break;
      continue;
    }
    if (ch === "/" && src[i + 1] === "*") {
      i = src.indexOf("*/", i + 2);
      if (i === -1) break;
      i += 2;
      continue;
    }
    
    // 统计大括号深度
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        return src.slice(startMatch.index, i + 1);
      }
    }
    
    i++;
  }
  
  throw new Error(`函数 ${name} 的大括号未闭合`);
}

check("提取 baseUrl、chosen、renderOut 函数并模拟执行", () => {
  const baseUrlSrc = extractFunction(fullScript, "baseUrl");
  const chosenSrc = extractFunction(fullScript, "chosen");
  const renderOutSrc = extractFunction(fullScript, "renderOut");
  
  // 构造沙箱环境
  const outText = { value: "", textContent: "" };
  const outHint = { textContent: "" };
  const elements = new Map([
    ["out-text", outText],
    ["out-hint", outHint]
  ]);
  
  const sandbox = {
    console,
    location: { origin: "http://127.0.0.1:8787" },
    STATE: { apiKey: "sk-test-key", defaultModel: "", enforce: false },
    SELECTED: new Set(),
    CATALOG: {
      models: [
        { id: "model-alpha", name: "Alpha" },
        { id: "model-beta", name: "Beta" }
      ],
      promotions: []
    },
    TAB: "anthropic",
    knownModels: function() { return sandbox.CATALOG.models; },
    $: function(sel) {
      const m = /^#([A-Za-z0-9_-]+)$/.exec(sel);
      if (!m) throw new Error("测试沙箱只支持 id 选择器：" + sel);
      const id = m[1];
      if (!elements.has(id)) {
        elements.set(id, { value: "", textContent: "", innerHTML: "" });
      }
      return elements.get(id);
    },
    JSON,
    String,
    Object,
    Array,
    Math
  };
  
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  
  // 注入函数定义
  vm.runInContext(
    baseUrlSrc + "\n" +
    chosenSrc + "\n" +
    renderOutSrc + "\n" +
    "globalThis.__baseUrl = baseUrl;\n" +
    "globalThis.__chosen = chosen;\n" +
    "globalThis.__renderOut = renderOut;\n",
    sandbox,
    { filename: "ui-functions.js" }
  );
  
  // 验证 baseUrl
  const base = sandbox.__baseUrl();
  if (base !== "http://127.0.0.1:8787/v1") {
    throw new Error(`baseUrl() 应返回 origin + '/v1'，实际：${base}`);
  }
  
  // 验证 Anthropic 配置生成
  sandbox.__renderOut();
  const anthropicOut = JSON.parse(outText.value);
  
  if (anthropicOut.env.ANTHROPIC_BASE_URL !== "http://127.0.0.1:8787") {
    throw new Error("Anthropic BASE_URL 应为 origin（不含 /v1），实际：" + anthropicOut.env.ANTHROPIC_BASE_URL);
  }
  
  if (anthropicOut.env.ANTHROPIC_API_KEY !== "sk-test-key") {
    throw new Error("Anthropic API_KEY 应来自 STATE.apiKey，实际：" + anthropicOut.env.ANTHROPIC_API_KEY);
  }
  
  if (anthropicOut.env.ANTHROPIC_MODEL !== "model-alpha") {
    throw new Error("Anthropic MODEL 应为实际模型 ID，实际：" + anthropicOut.env.ANTHROPIC_MODEL);
  }
  
  // 验证 OpenAI 配置（默认分支输出 shell 变量，不是 JSON）
  sandbox.TAB = "openai";
  sandbox.__renderOut();
  const openaiOut = outText.value;

  if (!openaiOut.includes("export OPENAI_BASE_URL=http://127.0.0.1:8787/v1")) {
    throw new Error("OpenAI BASE_URL 应为 origin + '/v1'");
  }

  if (!openaiOut.includes("export OPENAI_API_KEY=sk-test-key")) {
    throw new Error("OpenAI API_KEY 应来自 STATE.apiKey");
  }

  if (!openaiOut.includes("export ANTHROPIC_BASE_URL=http://127.0.0.1:8787")) {
    throw new Error("Anthropic BASE_URL 应为 origin（不含 /v1）");
  }

  // 验证 Codex 配置（使用 Responses API；密钥通过环境变量传入）
  sandbox.TAB = "codex";
  sandbox.__renderOut();
  const codexOut = outText.value;

  if (!codexOut.includes('model = "model-alpha"')) {
    throw new Error("Codex model 应为实际模型 ID");
  }

  if (!codexOut.includes('base_url = "http://127.0.0.1:8787/v1"')) {
    throw new Error("Codex base_url 应为 origin + '/v1'");
  }

  if (!codexOut.includes('env_key = "WB2API_API_KEY"')) {
    throw new Error("Codex 应通过 WB2API_API_KEY 环境变量读取密钥");
  }

  if (!codexOut.includes("WB2API_API_KEY=sk-test-key")) {
    throw new Error("Codex 密钥应来自 STATE.apiKey");
  }

  if (!codexOut.includes('wire_api = "responses"')) {
    throw new Error("Codex 应使用 Responses API");
  }

  // 验证 JSON 分支可被解析且包含接入信息
  sandbox.TAB = "json";
  sandbox.__renderOut();
  const jsonOut = JSON.parse(outText.value);

  if (jsonOut.baseUrl !== "http://127.0.0.1:8787/v1"
      || jsonOut.apiKey !== "sk-test-key"
      || jsonOut.models[0] !== "model-alpha") {
    throw new Error("JSON 分支的 baseUrl/apiKey/models 不正确");
  }
});

// ========== 汇总结果 ==========
console.log();
if (failures.length === 0) {
  console.log("✅ ui.html 所有检查通过");
  process.exit(0);
} else {
  console.error("❌ 检查失败：");
  failures.forEach(f => console.error("   - " + f));
  process.exit(1);
}
