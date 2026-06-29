const state = {
  boot: null,
  currentAssistant: null,
  currentTool: null,
  attachments: [],
  events: 0,
  running: false,
};

const $ = (id) => document.getElementById(id);
const b64 = (s) => btoa(unescape(encodeURIComponent(s)));

if (!window.pywebview) {
  const demoOut = "$ pytest -q\n........                                         [100%]\n8 passed in 0.42s";
  window.pywebview = {
    api: {
      boot: async () => ({
        version: "dev",
        workspace: "D:/deepseek-projects",
        baseUrl: "https://api.deepseek.com",
        model: "deepseek-v4-flash",
        mode: "root",
        thinking: "fast",
        providerFormat: "deepseek",
        modes: ["plan", "review", "agent", "trusted", "yolo", "root"],
        thinkingModes: ["auto", "off", "instant", "fast", "standard", "balanced", "careful", "deep", "deeper", "max", "ultra"],
        modeDescriptions: {
          plan: "只读规划，不写文件，不跑 shell",
          review: "读取和诊断，危险动作需要确认",
          agent: "少量权限，写文件/shell 需要确认",
          trusted: "可信工作区，网络和写入仍有确认",
          yolo: "自动确认受限工具",
          root: "最高权限，直接执行",
        },
        compatFormats: ["deepseek", "openai-compatible"],
        skills: [{ name: "repo-debug", description: "调试仓库时先运行测试" }],
        apiKeySet: true,
      }),
      models: async () => ({ ok: true, models: ["deepseek-v4-flash", "deepseek-v4-pro", "gpt-4o"] }),
      sessions: async () => ([{ session_id: "demo-session-0001", title: "检查项目并修复问题", updated_at: "today", pinned: true }]),
      resume: async (sessionId) => ({ ok: true, sessionId, messages: [{ role: "user", content: "检查项目并修复问题" }, { role: "assistant", content: "已读取项目结构，下一步运行测试。" }] }),
      rename_session: async () => ({ ok: true }),
      pin_session: async () => ({ ok: true }),
      delete_session: async () => ({ ok: true }),
      set_runtime: async (data) => ({ ...(await window.pywebview.api.boot()), model: data.model, mode: data.mode, thinking: data.thinking }),
      configure: async () => window.pywebview.api.boot(),
      new_session: async () => ({ ok: true }),
      compact: async () => ({ ok: true, before: 12000, after: 4200, messages: [{ role: "assistant", content: "上下文已压缩，保留最近消息。" }] }),
      save_upload: async (file) => ({ ok: true, name: file.name, path: `/uploads/${file.name}`, size: 128 }),
      send: async ({ prompt }) => {
        const D = window.DeepSeekDesktop;
        setTimeout(() => D.onNativeEvent({ event: "turn:start", payload: { prompt, thinking: $("thinking").value } }), 60);
        setTimeout(() => D.onNativeEvent({ event: "agent:event", payload: { kind: "tool", name: "run_shell", detail: "cmd=pytest -q" } }), 220);
        setTimeout(() => D.onNativeEvent({ event: "agent:event", payload: { kind: "done", name: "run_shell", detail: demoOut } }), 520);
        setTimeout(() => D.onNativeEvent({ event: "assistant:delta", payload: { text: "测试全部通过，仓库状态正常。" } }), 720);
        setTimeout(() => D.onNativeEvent({ event: "turn:done", payload: { sessionId: "demo-session-0001", rounds: 2 } }), 900);
        return { ok: true };
      },
      cancel: async () => ({ ok: true }),
    },
  };
}

window.DeepSeekDesktop = {
  onNativeEvent(message) {
    const { event, payload } = message;
    if (event === "turn:start") {
      setRunning(true);
      setSaveState("running", "运行中", "正在执行工具和模型");
      addMessage("user", payload.prompt);
      state.currentAssistant = null;
      state.currentTool = null;
      addEvent("thinking", "内部思考", `thinking mode: ${payload.thinking}`);
    }
    if (event === "assistant:delta") {
      if (!state.currentAssistant) state.currentAssistant = addMessage("assistant", "");
      const bubble = state.currentAssistant.querySelector(".bubble");
      bubble.dataset.raw = (bubble.dataset.raw || "") + payload.text;
      renderBubble(bubble);
      scrollMessages();
    }
    if (event === "agent:event") {
      if (payload.kind === "tool") {
        state.currentTool = addToolEvent(payload.name, payload.detail);
      } else if (payload.kind === "done") {
        completeToolEvent(payload.name, payload.detail);
      } else {
        addEvent(payload.kind, payload.name, payload.detail);
      }
    }
    if (event === "turn:done") {
      state.currentAssistant = null;
      state.currentTool = null;
      setRunning(false);
      refreshSessions();
      $("sessionState").textContent = payload.sessionId.slice(0, 8);
      setSaveState("saved", "已保存", payload.sessionId);
      $("composerSession").textContent = payload.sessionId;
    }
    if (event === "turn:error") {
      state.currentAssistant = null;
      state.currentTool = null;
      setRunning(false);
      addEvent("error", "错误", payload.error + "\n\n" + payload.trace);
      setSaveState("error", "出错", "查看事件流");
    }
    if (event === "turn:cancel") {
      addEvent("event", "取消请求", payload.message || "");
      setSaveState("running", "取消中", "等待当前调用返回");
    }
    if (event === "turn:cancelled") {
      state.currentAssistant = null;
      state.currentTool = null;
      setRunning(false);
      addEvent("done", "已取消", payload.message || "");
      setSaveState("idle", "已取消", state.boot?.sessionId || "未保存");
    }
  }
};

async function boot() {
  state.boot = await window.pywebview.api.boot();
  $("version").textContent = `v${state.boot.version}`;
  $("workspace").textContent = state.boot.workspace || "";
  $("apiState").textContent = state.boot.apiKeySet ? "已配置" : "未配置";
  $("topRuntime").textContent = `${state.boot.model} · ${state.boot.mode}/${state.boot.thinking}`;
  setSaveState("idle", "新会话", state.boot.sessionId || "未保存");
  setRunning(Boolean(state.boot.running));
  fillSelect("mode", state.boot.modes, state.boot.mode);
  fillSelect("thinking", state.boot.thinkingModes, state.boot.thinking);
  fillSelect("format", state.boot.compatFormats, state.boot.providerFormat || "deepseek");
  fillSelect("providerFormat", state.boot.compatFormats, state.boot.providerFormat || "deepseek");
  fillSelect("model", [state.boot.model], state.boot.model);
  updateModeHelp();
  $("baseUrl").value = state.boot.baseUrl || "";
  $("defaultModel").value = state.boot.model || "";
  renderSkills(state.boot.skills || []);
  await refreshModels();
  await refreshSessions();
}

function fillSelect(id, values, selected) {
  const element = $(id);
  element.innerHTML = "";
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    option.selected = value === selected;
    element.append(option);
  });
}

function updateModeHelp() {
  const mode = $("mode").value;
  const descriptions = state.boot?.modeDescriptions || {};
  $("modeHelp").textContent = descriptions[mode] || "当前权限模式";
}

function setSaveState(kind, label, detail) {
  const box = document.querySelector(".saveState");
  box.className = `saveState ${kind}`;
  $("saveState").textContent = label;
  $("composerSession").textContent = detail || "";
}

function setRunning(running) {
  state.running = running;
  $("send").hidden = running;
  $("cancel").hidden = !running;
  $("prompt").disabled = running;
  $("attach").disabled = running;
  document.body.classList.toggle("is-running", running);
}

async function refreshModels() {
  const result = await window.pywebview.api.models();
  const models = result.models && result.models.length ? result.models : [state.boot.model];
  fillSelect("model", models, state.boot.model);
  $("apiState").textContent = result.ok ? "模型可用" : "模型列表失败";
}

async function refreshSessions() {
  const sessions = await window.pywebview.api.sessions();
  const box = $("sessions");
  box.innerHTML = "";
  if (!sessions.length) {
    box.textContent = "暂无会话";
    return;
  }
  sessions.slice(0, 40).forEach((session) => {
    const row = document.createElement("div");
    row.className = `sessionItem${session.pinned ? " pinned" : ""}`;
    row.innerHTML = `
      <button class="sessionMain">
        <span>${escapeHtml(session.title || session.session_id.slice(0, 8))}</span>
        <small>${session.pinned ? "置顶 · " : ""}${escapeHtml(session.session_id.slice(0, 8))}</small>
      </button>
      <div class="sessionActions">
        <button title="${session.pinned ? "取消置顶" : "置顶"}" class="actPin">${session.pinned ? "★" : "☆"}</button>
        <button title="改标题" class="actRename">✎</button>
        <button title="删除" class="actDelete">🗑</button>
      </div>`;
    row.querySelector(".sessionMain").onclick = async () => {
      const result = await window.pywebview.api.resume(session.session_id);
      $("messages").innerHTML = "";
      result.messages.forEach((message) => addMessage(message.role, message.content));
      $("sessionState").textContent = result.sessionId.slice(0, 8);
      setSaveState("saved", "已恢复", result.sessionId);
    };
    row.querySelector(".actPin").onclick = async (e) => {
      e.stopPropagation();
      await window.pywebview.api.pin_session(session.session_id, !session.pinned);
      await refreshSessions();
    };
    row.querySelector(".actRename").onclick = async (e) => {
      e.stopPropagation();
      const title = prompt("新的会话标题", session.title || "");
      if (!title) return;
      await window.pywebview.api.rename_session(session.session_id, title);
      await refreshSessions();
    };
    row.querySelector(".actDelete").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除会话「${session.title || session.session_id.slice(0, 8)}」？此操作不可恢复。`)) return;
      await window.pywebview.api.delete_session(session.session_id);
      await refreshSessions();
    };
    box.append(row);
  });
}

function renderSkills(skills) {
  const box = $("skills");
  box.innerHTML = "";
  if (!skills.length) {
    box.textContent = "暂无技能";
    return;
  }
  skills.forEach((skill) => {
    const button = document.createElement("button");
    button.className = "item";
    button.innerHTML = `<span>${escapeHtml(skill.name)}</span><small>${escapeHtml(skill.description || "")}</small>`;
    button.onclick = () => {
      const prompt = $("prompt");
      prompt.value = `Use skill ${skill.name}: ` + prompt.value;
      prompt.focus();
    };
    box.append(button);
  });
}

function addMessage(role, content) {
  const empty = document.querySelector(".empty, .intro");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = `message ${role}`;
  const avatar = role === "user" ? "你" : "F";
  const name = role === "user" ? "You" : "Fathom";
  row.innerHTML = `<div class="msgHead"><span class="avatar ${role}">${avatar}</span><span class="who">${name}</span></div><div class="bubble ${role}"></div>`;
  const bubble = row.querySelector(".bubble");
  bubble.dataset.raw = content || "";
  renderBubble(bubble);
  $("messages").append(row);
  scrollMessages();
  return row;
}

function renderBubble(bubble) {
  const raw = bubble.dataset.raw || "";
  if (bubble.classList.contains("user")) {
    bubble.textContent = raw;
  } else {
    bubble.innerHTML = renderMarkdown(raw);
  }
}

/* ---------- merged tool block: call (args) on top, output below ---------- */
function addToolEvent(name, args) {
  state.events += 1;
  $("eventCount").textContent = String(state.events);
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  const details = document.createElement("details");
  details.className = "threadEvent tool";
  details.innerHTML = `
    <summary><span class="eventIcon">⌘</span><span class="evLabel">工具调用</span><strong>${escapeHtml(name || "")}</strong><span class="evStatus">运行中</span><span class="evChevron">›</span></summary>
    <div class="toolBody">
      <div class="toolSection toolCall"><div class="secLabel">调用</div><pre><code>${highlightCode(String(args || "").trim(), guessLang(name, args))}</code></pre></div>
      <div class="toolSection toolOut" hidden><div class="secLabel">输出</div><pre><code></code></pre></div>
    </div>`;
  $("messages").append(details);
  scrollMessages();
  mirror(`[工具调用] ${name || ""} ${args || ""}`);
  return details;
}

function completeToolEvent(name, output) {
  const block = state.currentTool || lastToolBlock();
  if (!block) {
    addEvent("done", name, output);
    return;
  }
  const status = block.querySelector(".evStatus");
  if (status) { status.textContent = "完成"; status.classList.add("ok"); }
  const out = block.querySelector(".toolOut");
  const code = out.querySelector("code");
  const text = String(output || "").trim();
  code.innerHTML = text ? highlightCode(text, "") : "<span class=\"t-com\">（无输出）</span>";
  out.hidden = false;
  scrollMessages();
  mirror(`[工具完成] ${name || ""} ${(output || "").slice(0, 200)}`);
}

function lastToolBlock() {
  const blocks = $("messages").querySelectorAll(".threadEvent.tool");
  return blocks.length ? blocks[blocks.length - 1] : null;
}

function addEvent(kind, name, detail) {
  state.events += 1;
  $("eventCount").textContent = String(state.events);
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  const details = document.createElement("details");
  details.className = `threadEvent ${kind}`;
  const icon = iconFor(kind);
  details.innerHTML = `
    <summary><span class="eventIcon">${icon}</span><span class="evLabel">${labelFor(kind)}</span><strong>${escapeHtml(name || "")}</strong><span class="evChevron">›</span></summary>
    <pre>${escapeHtml(detail || "")}</pre>`;
  $("messages").append(details);
  scrollMessages();
  mirror(`[${labelFor(kind)}] ${name || ""} ${detail || ""}`.trim());
}

function mirror(line) {
  const def = "工具、思考和子代理事件会显示在这里。";
  $("eventMirror").textContent = ($("eventMirror").textContent === def ? "" : $("eventMirror").textContent + "\n") + line;
  $("eventMirror").scrollTop = $("eventMirror").scrollHeight;
}

function labelFor(kind) {
  return {
    thinking: "内部思考", tool: "工具调用", done: "工具完成",
    subagent: "子代理", compact: "上下文压缩", error: "错误",
  }[kind] || "事件";
}

function iconFor(kind) {
  return {
    thinking: "◇", tool: "⌘", done: "✓", subagent: "↳",
    compact: "⇄", error: "!", skill: "✦",
  }[kind] || "·";
}

function scrollMessages() { $("messages").scrollTop = $("messages").scrollHeight; }

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

/* ---------- markdown + syntax highlight ---------- */
function renderMarkdown(src) {
  src = String(src || "");
  const blocks = [];
  src = src.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const i = blocks.length;
    const clean = code.replace(/\n$/, "");
    blocks.push(`<pre class="code"><div class="codeHead"><span>${escapeHtml(lang || "code")}</span><button class="copyBtn" type="button">复制</button></div><code>${highlightCode(clean, lang)}</code></pre>`);
    return `@@FB${i}@@`;
  });
  const lines = src.split("\n");
  let html = "";
  let list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const line of lines) {
    const ph = line.match(/^@@FB(\d+)@@$/);
    if (ph) { closeList(); html += blocks[+ph[1]]; continue; }
    if (/^\s*$/.test(line)) { closeList(); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); const n = h[1].length; html += `<h${n} class="mdH">${inline(h[2])}</h${n}>`; continue; }
    if (/^\s*>\s?/.test(line)) { closeList(); html += `<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`; continue; }
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ol) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(ol[1])}</li>`; continue; }
    if (ul) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(ul[1])}</li>`; continue; }
    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  closeList();
  return html;

  function inline(t) {
    t = escapeHtml(t);
    t = t.replace(/`([^`]+)`/g, (m, c) => `<code class="inline">${c}</code>`);
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return t;
  }
}

const HL_KEYWORDS = {
  python: ["def","class","return","if","elif","else","for","while","import","from","as","with","try","except","finally","raise","in","not","and","or","is","None","True","False","lambda","yield","async","await","pass","break","continue","global","nonlocal","self","print"],
  javascript: ["const","let","var","function","return","if","else","for","while","class","new","this","import","from","export","default","try","catch","finally","throw","await","async","typeof","instanceof","null","true","false","undefined","switch","case","break","continue","of","in"],
  bash: ["if","then","else","elif","fi","for","in","do","done","while","case","esac","function","return","export","local","echo","cd","exit","set","source"],
  json: ["true","false","null"],
};
const HL_ALIAS = { js: "javascript", ts: "javascript", jsx: "javascript", py: "python", sh: "bash", shell: "bash", zsh: "bash", console: "bash" };

function guessLang(name, args) {
  const n = (name || "").toLowerCase();
  if (n.includes("shell") || n.includes("bash") || n.includes("exec") || n.includes("run")) return "bash";
  if (n.includes("python")) return "python";
  return "";
}

function highlightCode(code, lang) {
  code = String(code);
  lang = (lang || "").toLowerCase();
  lang = HL_ALIAS[lang] || lang;
  const kw = new Set(HL_KEYWORDS[lang] || []);
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const re = /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(\b0x[0-9a-fA-F]+\b|\b\d+\.?\d*\b)|([A-Za-z_$][A-Za-z0-9_$]*)/g;
  let out = "", last = 0, m;
  while ((m = re.exec(code))) {
    out += esc(code.slice(last, m.index));
    const t = m[0];
    if (m[1]) out += `<span class="t-str">${esc(t)}</span>`;
    else if (m[2]) out += `<span class="t-com">${esc(t)}</span>`;
    else if (m[3]) out += `<span class="t-num">${esc(t)}</span>`;
    else if (m[4]) out += kw.has(t) ? `<span class="t-kw">${esc(t)}</span>` : esc(t);
    last = re.lastIndex;
  }
  out += esc(code.slice(last));
  return out;
}

async function updateRuntime() {
  state.boot = await window.pywebview.api.set_runtime({
    model: $("model").value,
    thinking: $("thinking").value,
    mode: $("mode").value,
    providerFormat: $("format").value,
  });
  $("topRuntime").textContent = `${state.boot.model} · ${state.boot.mode}/${state.boot.thinking}`;
  updateModeHelp();
}

$("send").onclick = async () => {
  if (state.running) return;
  const prompt = $("prompt").value.trim();
  if (!prompt && !state.attachments.length) return;
  $("prompt").value = "";
  autoGrow();
  const attachments = state.attachments;
  state.attachments = [];
  renderAttachments();
  await updateRuntime();
  setRunning(true);
  const result = await window.pywebview.api.send({ prompt, attachments });
  if (!result.ok) {
    setRunning(false);
    addEvent("error", "发送失败", result.error || "unknown error");
    $("prompt").value = prompt;
  }
};

function autoGrow() {
  const ta = $("prompt");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
}
$("prompt").addEventListener("input", autoGrow);
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("send").click();
  }
});

$("cancel").onclick = async () => {
  await window.pywebview.api.cancel();
};

["model", "thinking", "mode"].forEach((id) => $(id).addEventListener("change", updateRuntime));
$("mode").addEventListener("change", updateModeHelp);
$("format").addEventListener("change", async () => {
  $("providerFormat").value = $("format").value;
  await window.pywebview.api.configure({ providerFormat: $("format").value });
  state.boot = await window.pywebview.api.boot();
});
$("settingsBtn").onclick = () => $("settingsDialog").showModal();
$("saveSettings").onclick = async (event) => {
  event.preventDefault();
  state.boot = await window.pywebview.api.configure({
    baseUrl: $("baseUrl").value,
    apiKey: $("apiKey").value,
    model: $("defaultModel").value,
    providerFormat: $("providerFormat").value,
    defaultMode: $("mode").value,
    defaultThinking: $("thinking").value,
  });
  $("settingsDialog").close();
  await boot();
};
$("newSession").onclick = async () => {
  await window.pywebview.api.new_session();
  $("messages").innerHTML = '<div class="empty intro"><div class="introMark">Fathom</div><h1>新对话已创建</h1><p>输入任务开始。工具调用与输出会内联展开。</p></div>';
  $("eventMirror").textContent = "工具、思考和子代理事件会显示在这里。";
  $("sessionState").textContent = "新会话";
  setSaveState("idle", "新会话", "未保存");
};
$("refreshSessions").onclick = refreshSessions;
$("insertSubagent").onclick = () => {
  const prompt = $("prompt");
  prompt.value = 'Use delegate_agent with name="researcher" task="';
  prompt.focus();
};
$("manualCompact").onclick = async () => {
  const result = await window.pywebview.api.compact();
  if (!result.ok) {
    addEvent("compact", "手动压缩", result.error || "no active session");
    return;
  }
  $("messages").innerHTML = "";
  result.messages.forEach((message) => addMessage(message.role, message.content));
  addEvent("compact", "手动压缩", `${result.before} -> ${result.after} estimated tokens`);
};
$("attach").onclick = () => $("fileInput").click();
$("fileInput").onchange = async (event) => {
  for (const file of event.target.files) {
    const content = await readFileAsDataUrl(file);
    const saved = await window.pywebview.api.save_upload({ name: file.name, content });
    state.attachments.push(saved);
  }
  event.target.value = "";
  renderAttachments();
};

// collapse sidebar
const toggleCollapse = () => document.querySelector(".app").classList.toggle("sidebarCollapsed");
["toggleSidebar", "toggleSidebarTop"].forEach((id) => { const b = $(id); if (b) b.onclick = toggleCollapse; });

// copy buttons in code blocks (event delegation)
$("messages").addEventListener("click", (e) => {
  const btn = e.target.closest(".copyBtn");
  if (!btn) return;
  const code = btn.closest(".code").querySelector("code");
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = "已复制";
    setTimeout(() => (btn.textContent = "复制"), 1200);
  }).catch(() => {});
});

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  $("attachments").innerHTML = "";
  state.attachments.forEach((file) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = file.name;
    $("attachments").append(chip);
  });
}

boot();
