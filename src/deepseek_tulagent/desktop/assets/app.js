const state = {
  boot: null,
  currentAssistant: null,
  attachments: [],
};

const $ = (id) => document.getElementById(id);

window.DeepSeekDesktop = {
  onNativeEvent(message) {
    const { event, payload } = message;
    if (event === "turn:start") {
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
      refreshSessions();
      addEvent("done", "完成", `session ${payload.sessionId} · ${payload.rounds} rounds`);
    }
    if (event === "turn:error") {
      state.currentAssistant = null;
      addEvent("error", "错误", payload.error + "\n\n" + payload.trace);
    }
  }
};

async function boot() {
  state.boot = await window.pywebview.api.boot();
  $("version").textContent = `v${state.boot.version}`;
  fillSelect("mode", state.boot.modes, state.boot.mode);
  fillSelect("thinking", state.boot.thinkingModes, state.boot.thinking);
  fillSelect("format", state.boot.compatFormats, state.boot.compatFormats[0]);
  fillSelect("model", [state.boot.model], state.boot.model);
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

async function refreshModels() {
  const result = await window.pywebview.api.models();
  const models = result.models && result.models.length ? result.models : [state.boot.model];
  fillSelect("model", models, state.boot.model);
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
    const button = document.createElement("button");
    button.className = "item";
    button.innerHTML = `<span>${session.session_id.slice(0, 8)}</span><small>${session.updated_at || ""}</small>`;
    button.onclick = async () => {
      const result = await window.pywebview.api.resume(session.session_id);
      $("messages").innerHTML = "";
      result.messages.forEach((message) => addMessage(message.role, message.content));
    };
    box.append(button);
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
  const details = document.createElement("details");
  details.className = `event ${kind}`;
  details.innerHTML = `<summary>${labelFor(kind)} · ${escapeHtml(name || "")}</summary><pre>${escapeHtml(detail || "")}</pre>`;
  $("activity").append(details);
  $("activity").scrollTop = $("activity").scrollHeight;
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
  });
}

$("send").onclick = async () => {
  const prompt = $("prompt").value.trim();
  if (!prompt && !state.attachments.length) return;
  $("prompt").value = "";
  const attachments = state.attachments;
  state.attachments = [];
  renderAttachments();
  await updateRuntime();
  await window.pywebview.api.send({ prompt, attachments });
};

$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("send").click();
  }
});

["model", "thinking", "mode"].forEach((id) => $(id).addEventListener("change", updateRuntime));
$("settingsBtn").onclick = () => $("settingsDialog").showModal();
$("saveSettings").onclick = async (event) => {
  event.preventDefault();
  state.boot = await window.pywebview.api.configure({
    baseUrl: $("baseUrl").value,
    apiKey: $("apiKey").value,
    model: $("defaultModel").value,
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

