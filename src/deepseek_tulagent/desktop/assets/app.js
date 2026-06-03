const state = {
  boot: null,
  currentAssistant: null,
  attachments: [],
  events: 0,
  running: false,
};

const $ = (id) => document.getElementById(id);

if (!window.pywebview) {
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
      set_runtime: async (data) => ({ ...(await window.pywebview.api.boot()), model: data.model, mode: data.mode, thinking: data.thinking }),
      configure: async () => window.pywebview.api.boot(),
      new_session: async () => ({ ok: true }),
      compact: async () => ({ ok: true, before: 12000, after: 4200, messages: [{ role: "assistant", content: "上下文已压缩，保留最近消息。" }] }),
      save_upload: async (file) => ({ ok: true, name: file.name, path: `/uploads/${file.name}`, size: 128 }),
      send: async ({ prompt }) => {
        setTimeout(() => window.DeepSeekDesktop.onNativeEvent({ event: "turn:start", payload: { prompt, thinking: $("thinking").value } }), 80);
        setTimeout(() => window.DeepSeekDesktop.onNativeEvent({ event: "agent:event", payload: { kind: "tool", name: "read_file", detail: "path=README.md" } }), 260);
        setTimeout(() => window.DeepSeekDesktop.onNativeEvent({ event: "assistant:delta", payload: { text: "这是桌面端预览。工具调用、内部思考和子代理会折叠显示。" } }), 460);
        setTimeout(() => window.DeepSeekDesktop.onNativeEvent({ event: "turn:done", payload: { sessionId: "demo-session-0001", rounds: 2 } }), 700);
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
      state.currentAssistant = addMessage("assistant", "");
      addEvent("thinking", "内部思考", `thinking mode: ${payload.thinking}`);
    }
    if (event === "assistant:delta") {
      if (!state.currentAssistant) state.currentAssistant = addMessage("assistant", "");
      state.currentAssistant.querySelector(".bubble").textContent += payload.text;
      scrollMessages();
    }
    if (event === "agent:event") {
      addEvent(payload.kind, payload.name, payload.detail);
    }
    if (event === "turn:done") {
      state.currentAssistant = null;
      setRunning(false);
      refreshSessions();
      addEvent("done", "完成", `session ${payload.sessionId} · ${payload.rounds} rounds`);
      $("sessionState").textContent = payload.sessionId.slice(0, 8);
      setSaveState("saved", "已保存", payload.sessionId);
      $("composerSession").textContent = payload.sessionId;
    }
    if (event === "turn:error") {
      state.currentAssistant = null;
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
  sessions.slice(0, 18).forEach((session) => {
    const row = document.createElement("div");
    row.className = `sessionItem${session.pinned ? " pinned" : ""}`;
    row.innerHTML = `
      <button class="sessionMain">
        <span>${escapeHtml(session.title || session.session_id.slice(0, 8))}</span>
        <small>${session.pinned ? "置顶 · " : ""}${escapeHtml(session.session_id.slice(0, 8))}</small>
      </button>
      <div class="sessionActions">
        <button title="置顶">${session.pinned ? "●" : "○"}</button>
        <button title="复制 ID">ID</button>
        <button title="改标题">✎</button>
      </div>`;
    row.querySelector(".sessionMain").onclick = async () => {
      const result = await window.pywebview.api.resume(session.session_id);
      $("messages").innerHTML = "";
      result.messages.forEach((message) => addMessage(message.role, message.content));
      $("sessionState").textContent = result.sessionId.slice(0, 8);
      setSaveState("saved", "已恢复", result.sessionId);
    };
    const [pinButton, copyButton, renameButton] = row.querySelectorAll(".sessionActions button");
    pinButton.onclick = async () => {
      await window.pywebview.api.pin_session(session.session_id, !session.pinned);
      await refreshSessions();
    };
    copyButton.onclick = async () => {
      await navigator.clipboard.writeText(session.session_id).catch(() => {});
      addEvent("event", "复制会话 ID", session.session_id);
    };
    renameButton.onclick = async () => {
      const title = prompt("新的会话标题", session.title || "");
      if (!title) return;
      await window.pywebview.api.rename_session(session.session_id, title);
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
    button.innerHTML = `<span>${skill.name}</span><small>${skill.description || ""}</small>`;
    button.onclick = () => {
      const prompt = $("prompt");
      prompt.value = `Use skill ${skill.name}: ` + prompt.value;
      prompt.focus();
    };
    box.append(button);
  });
}

function addMessage(role, content) {
  const empty = document.querySelector(".empty");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "message";
  row.innerHTML = `<div class="role">${role === "user" ? "你" : "助手"}</div><div class="bubble ${role}"></div>`;
  row.querySelector(".bubble").textContent = content;
  $("messages").append(row);
  scrollMessages();
  return row;
}

function addEvent(kind, name, detail) {
  state.events += 1;
  $("eventCount").textContent = String(state.events);
  const details = document.createElement("details");
  details.className = `event ${kind}`;
  const icon = iconFor(kind);
  details.innerHTML = `
    <summary><span class="eventIcon">${icon}</span><span>${labelFor(kind)}</span><strong>${escapeHtml(name || "")}</strong></summary>
    <pre>${escapeHtml(detail || "")}</pre>`;
  $("activity").append(details);
  $("activity").scrollTop = $("activity").scrollHeight;
  const line = `[${labelFor(kind)}] ${name || ""} ${detail || ""}`.trim();
  $("eventMirror").textContent = ($("eventMirror").textContent === "工具、思考和子代理事件会显示在这里。" ? "" : $("eventMirror").textContent + "\n") + line;
  $("eventMirror").scrollTop = $("eventMirror").scrollHeight;
}

function labelFor(kind) {
  return {
    thinking: "内部思考",
    tool: "工具调用",
    done: "工具完成",
    subagent: "子代理",
    compact: "上下文压缩",
    error: "错误",
  }[kind] || "事件";
}

function iconFor(kind) {
  return {
    thinking: "◇",
    tool: "⌘",
    done: "✓",
    subagent: "↳",
    compact: "⇄",
    error: "!",
    skill: "✦",
  }[kind] || "·";
}

function scrollMessages() {
  $("messages").scrollTop = $("messages").scrollHeight;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
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
  $("messages").innerHTML = '<div class="empty"><h1>DeepSeek TuLAgent</h1><p>新对话已创建。</p></div>';
  $("activity").innerHTML = "";
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
