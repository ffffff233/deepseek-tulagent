const state = {
  boot: null,
  currentAssistant: null,
  currentTool: null,
  attachments: [],
  images: [],
  events: 0,
  running: false,
  stickToBottom: true,
  skills: [],
  slash: { open: false, items: [], index: 0 },
  editing: false,
  editSrc: null,
  pendingVersions: null,
  pendingVersionMarker: null,
  pendingVersionUser: null,
  models: [],
  currentSessionId: "",
  activeTurnId: "",
  pendingOutbound: false,
  pendingOutboundId: "",
  cancelPromise: null,
  goalCollapsed: false,
  goalsBySession: {},
  dismissedGoalSnapshots: {},
  goalDraftId: `__draft__:${Date.now()}-${Math.random().toString(16).slice(2)}`,
  katexRenderToString: null,
  katexLoadPromise: null,
  activeGoal: "",
};

const $ = (id) => document.getElementById(id);
// null-safe text setter — inspector elements were removed, callers must not crash on them
const setText = (id, value) => { const el = $(id); if (el) el.textContent = value; };
const b64 = (s) => btoa(unescape(encodeURIComponent(s)));

/* ---------- line-style SVG icons (Lucide-ish), replacing all emoji ---------- */
const ICONS = {
  pin: '<path d="M9 4h6l-1 6 4 3v2H6v-2l4-3-1-6z"/><path d="M12 15v5"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  terminal: '<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>',
  dots: '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
  loader: '<path d="M12 2v4"/><path d="M12 18v4"/><path d="m4.93 4.93 2.83 2.83"/><path d="m16.24 16.24 2.83 2.83"/><path d="M2 12h4"/><path d="M18 12h4"/><path d="m4.93 19.07 2.83-2.83"/><path d="m16.24 7.76 2.83-2.83"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  branch: '<path d="M4 4v8a3 3 0 0 0 3 3h13"/><path d="M16 11l4 4-4 4"/>',
  compact: '<path d="M17 11l-5-5-5 5"/><path d="M17 13l-5 5-5-5"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
};
function icon(name, size = 14) {
  return `<svg class="ic" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

function installDemoApi() {
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
        modes: ["plan", "agent", "root"],
        modeLabels: { plan: "只读", agent: "受限", root: "完全访问" },
        thinkingModes: ["instant", "fast", "balanced", "deep", "ultra"],
        thinkingLabels: { instant: "Minimal", fast: "Low", balanced: "Medium", deep: "High", ultra: "Extra High" },
        modeDescriptions: {
          plan: "只读：可以阅读文件和回答，不写文件、不执行命令",
          agent: "受限：危险操作会弹出批准请求，同意后才执行",
          root: "完全访问：不受限制地执行命令、读写文件和访问网络",
        },
        compatFormats: ["deepseek", "openai", "openai-responses", "gemini", "anthropic"],
        formatLabels: { deepseek: "DeepSeek", openai: "OpenAI (Chat)", "openai-responses": "OpenAI (Responses·最新)", gemini: "Google Gemini", anthropic: "Anthropic Claude" },
        context: { ok: true, tokens: 4200, inputTokens: 3000, outputTokens: 1200, cachedTokens: 1800, cachePercent: 60, limit: 1000000, threshold: 920000, percent: 0.4, source: "upstream", measure: "上游 usage", accurate: true, model: "deepseek-v4-flash", autoCompact: true },
        skills: [
          { name: "repo-debug", description: "调试仓库时先运行测试" },
          { name: "code-review", description: "审阅改动，找出缺陷" },
        ],
        apiKeySet: true,
      }),
      models: async () => ({ ok: true, models: ["deepseek-v4-flash", "deepseek-v4-pro", "gpt-4o"] }),
      sessions: async () => ([{ session_id: "demo-session-0001", title: "检查项目并修复问题", updated_at: "today", pinned: true }]),
      resume: async (sessionId) => ({ ok: true, sessionId, context: { ok: true, tokens: 4200, inputTokens: 3000, outputTokens: 1200, cachedTokens: 1800, cachePercent: 60, limit: 1000000, threshold: 920000, percent: 0.4, source: "upstream", measure: "上游 usage", accurate: true, model: $("model")?.value || "deepseek-v4-flash", autoCompact: true }, messages: [
        { role: "user", content: "检查项目并修复问题" },
        { role: "tool", name: "run_shell", detail: "cmd=pytest -q", output: "8 passed in 0.42s" },
        { role: "assistant", content: "测试通过，仓库状态正常。" },
      ] }),
      rename_session: async () => ({ ok: true }),
      pin_session: async () => ({ ok: true }),
      delete_session: async () => ({ ok: true }),
      set_runtime: async (data) => ({ ...(await window.pywebview.api.boot()), model: data.model, mode: data.mode, thinking: data.thinking }),
      configure: async () => window.pywebview.api.boot(),
      new_session: async () => ({ ok: true, sessionId: null, messages: [], context: { ok: true, tokens: 0, inputTokens: 0, outputTokens: 0, cachedTokens: 0, cachePercent: 0, limit: 1000000, threshold: 920000, percent: 0, source: "deepseek", measure: "本地估算", accurate: false, model: $("model")?.value || "deepseek-v4-flash", autoCompact: true } }),
      compact: async () => ({ ok: true, before: 12000, after: 4200, context: { ok: true, tokens: 4200, inputTokens: 3000, outputTokens: 1200, cachedTokens: 1800, cachePercent: 60, limit: 1000000, threshold: 920000, percent: 0.4, source: "upstream", measure: "上游 usage", accurate: true, model: "deepseek-v4-flash", autoCompact: true }, messages: [{ role: "assistant", content: "上下文已压缩，保留最近消息。" }] }),
      context_status: async () => ({ ok: true, tokens: 4200, inputTokens: 3000, outputTokens: 1200, cachedTokens: 1800, cachePercent: 60, limit: 1000000, threshold: 920000, percent: 0.4, source: "upstream", measure: "上游 usage", accurate: true, model: $("model")?.value || "deepseek-v4-flash", autoCompact: true }),
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
      resolve_approval: async () => ({ ok: true }),
      test_connection: async () => ({ ok: true, reply: "ok", model: "deepseek-v4-flash", thinking: "deep", reasoning: { reasoning_effort: "high" }, resolved: "https://api.deepseek.com/v1" }),
      branch: async () => ({ ok: true, sessionId: "branch-0001", messages: [
        { role: "user", content: "检查项目并修复问题" }, { role: "assistant", content: "已读取项目结构，下一步运行测试。" },
      ] }),
      retry: async () => {
        const D = window.DeepSeekDesktop;
        setTimeout(() => D.onNativeEvent({ event: "turn:start", payload: { prompt: "检查项目并修复问题", thinking: $("thinking").value } }), 40);
        setTimeout(() => D.onNativeEvent({ event: "assistant:final", payload: { text: "这是重试后的新回答。" } }), 320);
        setTimeout(() => D.onNativeEvent({ event: "turn:done", payload: { sessionId: "demo-session-0001", rounds: 1 } }), 420);
        return { ok: true };
      },
      edit_resend: async ({ prompt }) => {
        const D = window.DeepSeekDesktop;
        setTimeout(() => D.onNativeEvent({ event: "turn:start", payload: { prompt, thinking: $("thinking").value } }), 40);
        setTimeout(() => D.onNativeEvent({ event: "assistant:final", payload: { text: "已按编辑后的问题重新回答。" } }), 320);
        setTimeout(() => D.onNativeEvent({ event: "turn:done", payload: { sessionId: "demo-session-0001", rounds: 1 } }), 420);
        return { ok: true };
      },
    },
  };
}

window.DeepSeekDesktop = {
  onNativeEvent(message) {
    const { event, payload } = message;
    const sid = String((payload && payload.sessionId) || "");
    const tid = String((payload && payload.turnId) || "");
    const activeSid = currentSessionId();
    const scopedToOtherSession = Boolean(
      (sid && activeSid && sid !== activeSid) ||
      (sid && !activeSid && !state.pendingOutbound && (!state.activeTurnId || tid !== state.activeTurnId)) ||
      (tid && state.activeTurnId && tid !== state.activeTurnId)
    );
    // after a user cancel, ignore this turn's late stream/tool events until it ends
    if (state.suppressStream && event !== "turn:done" && event !== "turn:cancelled" && event !== "turn:error" && event !== "turn:start") {
      return;
    }
    if (scopedToOtherSession) {
      if (event === "turn:done" || event === "turn:error" || event === "turn:cancelled") {
        // A background turn finished in another conversation. Keep its transcript
        // bound to its own session; only refresh chrome/global availability here.
        refreshSessions();
        if (tid && tid === state.activeTurnId) {
          state.activeTurnId = "";
          setRunning(false);
        } else if (!state.activeTurnId) {
          setRunning(false);
        }
      }
      return;
    }
    if (event === "turn:start") {
      updateContextBadge({ status: "active", label: "运行中" });
      if (tid) state.activeTurnId = tid;
      state.pendingOutbound = false;
      if (sid) {
        state.currentSessionId = sid;
        if (state.boot) state.boot.sessionId = sid;
        setText("sessionState", sid.slice(0, 8));
        migrateDraftGoal(sid);
      }
      state.suppressStream = false;
      setRunning(true);
      markGoalRunning();
      state.stickToBottom = true;
      setSaveState("running", "运行中", "正在执行工具和模型");
      // send() already rendered the user message locally (with image thumbs); only
      // add it here for turns started elsewhere (retry/edit/branch)
      if (state.suppressLocalUserEcho) { state.suppressLocalUserEcho = false; }
      else {
        const userRow = addMessage("user", payload.prompt);
        if (state.pendingVersions) state.pendingVersionUser = userRow;
      }
      state.currentAssistant = null;
      state.currentTool = null;
      // Codex-style: show a loading shimmer immediately, before the first token arrives
      showThinking("思考中");
    }
    if (event === "assistant:delta") {
      hideThinking();
      if (!state.currentAssistant) {
        state.currentAssistant = addMessage("assistant", "");
        state.currentAssistant.classList.add("streaming");
      }
      const bubble = state.currentAssistant.querySelector(".bubble");
      bubble.dataset.raw = (bubble.dataset.raw || "") + payload.text;
      renderBubble(bubble);
      scrollMessages();
    }
    if (event === "assistant:final") {
      // replace the streamed text with the cleaned final answer; empty text means the
      // streamed content was actually a tool call — remove the bubble entirely
      const text = payload.text || "";
      if (!text.trim()) {
        if (state.currentAssistant) {
          const bubble = state.currentAssistant.querySelector(".bubble");
          if ((bubble && (bubble.dataset.raw || "").trim())) {
            state.currentAssistant.classList.remove("streaming");
          } else {
            state.currentAssistant.remove();
          }
          state.currentAssistant = null;
        }
        return;
      }
      hideThinking();
      if (!state.currentAssistant) state.currentAssistant = addMessage("assistant", "");
      state.currentAssistant.classList.remove("streaming");
      const bubble = state.currentAssistant.querySelector(".bubble");
      bubble.dataset.raw = text;
      renderBubble(bubble);
      // a tool call may follow in this same turn — let the next delta open a new bubble
      state.currentAssistant = null;
      scrollMessages();
    }
    if (event === "agent:event") {
      const sub = payload.sub;  // set when this event came from inside a subagent
      if (payload.kind === "toolpending" && !sub) {
        // model is emitting a tool call; its JSON is held back from the chat, so keep
        // the shimmer alive but tell the user what's happening instead of a dead pause
        showThinking("准备调用工具…");
      } else if (payload.kind === "toolpending") {
        // a subagent is preparing a tool — its own card already shows activity
      } else if (payload.kind === "subagentdone") {
        markSubagentDone(payload.name, payload.detail);
      } else if (sub) {
        // nest the subagent's own activity under its group so you can watch it work
        addSubEvent(sub, payload);
      } else if (payload.kind === "todo") {
        applyTodoPayload(payload.detail, sid || currentSessionId());
      } else if (payload.kind === "media") {
        showMediaFrames(payload.detail);
      } else if (payload.kind === "tool") {
        hideThinking();
        // a tool card starts a new visual block — text after it must open a NEW
        // bubble below the card, not append to the bubble above it
        state.currentAssistant = null;
        // any assistant prose that streamed just before this tool call is pre-tool
        // narration, not a standalone reply — demote it so it carries no copy/retry/
        // branch (one turn shows one set of actions, on its final reply)
        markLastAssistantIntermediate();
        state.currentTool = addToolEvent(payload.name, payload.detail);
      } else if (payload.kind === "done") {
        completeToolEvent(payload.name, payload.detail);
        state.currentAssistant = null;
        // another model round follows a tool result — show the shimmer again
        showThinking("思考中");
      } else if (payload.kind === "subagent") {
        showThinking("子代理运行中…");
        addEvent(payload.kind, payload.name, payload.detail);
      } else {
        addEvent(payload.kind, payload.name, payload.detail);
      }
      refreshContextBadge().catch(() => {});
    }
    if (event === "approval:request") {
      hideThinking();
      showApproval(payload);
    }
    if (event === "turn:done") {
      hideThinking();
      state.currentAssistant = null;
      state.currentTool = null;
      const wasSuppressed = state.suppressStream;
      state.suppressStream = false;
      setRunning(false);
      dismissApproval();
      refreshSessions();
      const doneSid = String(payload.sessionId || "");
      state.currentSessionId = doneSid;
      if (state.boot) state.boot.sessionId = doneSid;
      migrateDraftGoal(doneSid);
      if (!tid || tid === state.activeTurnId) state.activeTurnId = "";
      state.pendingOutbound = false;
      setText("sessionState", doneSid ? doneSid.slice(0, 8) : "新会话");
      if (!wasSuppressed) setSaveState("saved", "已保存", doneSid || "未保存");
      markMessageActions();
      // if this turn was a retry, attach the ‹ i/n › version pager to the retried
      // USER message (Codex-style: versions live on your message, not the reply)
      if (!wasSuppressed) attachVersionPager();
      clearVersionInsertMarker();
      refreshContextBadge().catch(() => {});
    }
    if (event === "turn:error") {
      hideThinking();
      state.currentAssistant = null;
      state.currentTool = null;
      state.suppressStream = false;
      if (!tid || tid === state.activeTurnId) state.activeTurnId = "";
      state.pendingOutbound = false;
      setRunning(false);
      dismissApproval();
      const summary = payload.summary || payload.error || "运行失败";
      const detail = payload.trace ? `${summary}\n\n调试详情：\n${payload.trace}` : summary;
      addEvent("error", "错误", detail);
      setSaveState("error", "出错", "查看上方错误卡片");
      toast(summary);
      clearVersionInsertMarker();
    }
    if (event === "turn:cancel") {
      dismissApproval();
    }
    if (event === "turn:cancelled") {
      hideThinking();
      state.currentAssistant = null;
      state.currentTool = null;
      const wasSuppressed = state.suppressStream;
      state.suppressStream = false;
      if (!tid || tid === state.activeTurnId) state.activeTurnId = "";
      state.pendingOutbound = false;
      setRunning(false);
      dismissApproval();
      if (!wasSuppressed) {  // only if not already handled by the instant-cancel path
        addEvent("done", "已取消", payload.message || "");
        setSaveState("idle", "已取消", state.currentSessionId || "未保存");
      }
      clearVersionInsertMarker();
    }
  }
};

async function boot() {
  state.boot = await window.pywebview.api.boot();
  loadKatexRenderer().catch(() => {});
  $("version").textContent = `v${state.boot.version}`;
  $("workspace").textContent = state.boot.workspace || "";
  state.currentSessionId = state.boot.sessionId || "";
  setText("apiState", state.boot.apiKeySet ? "已配置" : "未配置");
  $("topRuntime").textContent = `${state.boot.model} · ${state.boot.mode}/${state.boot.thinking}`;
  updateContextBadge(state.boot.context || null);
  setSaveState("idle", "新会话", state.boot.sessionId || "未保存");
  setRunning(Boolean(state.boot.running));
  const labels = state.boot.formatLabels || {};
  fillSelect("mode", state.boot.modes, state.boot.mode, state.boot.modeLabels || {});
  fillSelect("thinking", state.boot.thinkingModes, state.boot.thinking, state.boot.thinkingLabels || {});
  fillSelect("format", state.boot.compatFormats, state.boot.providerFormat || "deepseek", labels);
  fillSelect("providerFormat", state.boot.compatFormats, state.boot.providerFormat || "deepseek", labels);
  fillSelect("model", ensureIncludes((state.models && state.models.length ? state.models : [state.boot.model]), state.boot.model), state.boot.model);
  updateModeHelp();  $("baseUrl").value = state.boot.baseUrl || "";
  state.skills = state.boot.skills || [];
  loadGoalStore();
  // Don't block the UI on the network model list — the dropdown already shows the saved
  // model; fetch the full list in the background and refresh it when it arrives. Awaiting
  // here made every load wait on a slow GET /models round-trip.
  refreshModels().catch(() => {});
  refreshSessions().catch(() => {});
  refreshContextBadge().catch(() => {});
}

// pywebview may attach method proxies incrementally, so a method can be missing for a
// moment even after boot works. Wait (briefly) for it before calling instead of throwing
// "X is not a function".
async function apiMethod(name, timeoutMs = 8000) {
  const started = Date.now();
  while (!(window.pywebview && window.pywebview.api && typeof window.pywebview.api[name] === "function")) {
    if (Date.now() - started > timeoutMs) throw new Error(`后端接口 ${name} 尚未就绪，请稍候重试`);
    await new Promise((r) => setTimeout(r, 60));
  }
  return window.pywebview.api[name];
}

function fillSelect(id, values, selected, labels) {
  const element = $(id);
  if (!element) return;
  element.innerHTML = "";
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = (labels && labels[value]) || value;
    option.selected = value === selected;
    element.append(option);
  });
}

function updateModeHelp() {
  const help = $("modeHelp");
  if (!help) return;
  const descriptions = state.boot?.modeDescriptions || {};
  help.textContent = descriptions[$("mode").value] || "当前权限模式";
}

function fmtTokens(n) {
  n = Number(n || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K`;
  return String(Math.round(n));
}

function updateContextBadge(ctx) {
  const badge = $("contextBadge");
  if (!badge) return;
  if (!ctx || ctx.ok === false) {
    badge.className = "contextBadge idle";
    setText("ctxPct", "上下文 0%");
    return;
  }
  if (ctx.status === "active") {
    badge.className = "contextBadge active";
    setText("ctxPct", ctx.label || "统计中");
    return;
  }
  const pct = Math.max(0, Math.min(100, Number(ctx.percent || 0)));
  const level = ctx.needsCompact || pct >= 92 ? "danger" : (ctx.nearLimit || pct >= 75 ? "warn" : "ok");
  badge.className = `contextBadge ${level}`;
  setText("ctxPct", `上下文 ${pct.toFixed(pct >= 10 ? 0 : 1)}%`);
  setText("ctxUsage", `${fmtTokens(ctx.tokens)} / ${fmtTokens(ctx.limit)}`);
  setText("ctxThreshold", `${fmtTokens(ctx.threshold)} (${ctx.thresholdPercent || 95}%)`);
  setText("ctxRemaining", fmtTokens(ctx.remainingTokens || 0));
  const limitSource = ctx.customLimit ? "手动窗口" : sourceLabel(ctx.limitSource || ctx.source);
  setText("ctxSource", ctx.accurate ? `上游实测 · ${limitSource}` : limitSource);
  const limitInput = $("ctxLimitInput");
  const thresholdInput = $("ctxThresholdInput");
  if (limitInput && document.activeElement !== limitInput) limitInput.value = ctx.customLimit ? String(ctx.limit || "") : "";
  if (thresholdInput && document.activeElement !== thresholdInput) thresholdInput.value = String(ctx.thresholdPercent || 95);
  const measure = ctx.accurate ? "以上游输入为基准，并校正当前会话增量" : "按当前会话消息本地估算";
  const basis = `${measure}；窗口 ${fmtTokens(ctx.limit)}，阈值 ${ctx.thresholdPercent || 95}%。`;
  setText("ctxHint", ctx.needsCompact ? `已达到自动压缩阈值。${basis}` : basis);
  const bar = $("ctxBarFill");
  if (bar) bar.style.width = `${pct}%`;
  const pop = $("ctxPopover");
  if (pop) pop.className = `ctxPopover ${level}`;
  badge.title = "点击查看上下文窗口和压缩阈值";
}

function sourceLabel(source) {
  const labels = { upstream: "上游 usage", openai: "OpenAI", anthropic: "Claude", google: "Gemini", deepseek: "DeepSeek", qwen: "通义千问", moonshot: "Kimi", zhipu: "智谱 GLM", minimax: "MiniMax", "model-name": "模型名", fallback: "保守估算" };
  return labels[source] || source || "估算";
}

async function refreshContextBadge() {
  try {
    const fn = await apiMethod("context_status", 2000);
    updateContextBadge(await fn());
  } catch (_) {}
}

function setSaveState(kind, label, detail) {
  const box = document.querySelector(".saveState");
  box.className = `saveState ${kind}`;
  $("saveState").textContent = label;
  const session = $("composerSession");
  const text = String(detail || "");
  session.textContent = truncateInline(text, 96);
  session.title = text;
}

function truncateInline(text, max = 120) {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  return compact.length > max ? compact.slice(0, max - 1) + "…" : compact;
}

function setRunning(running) {
  state.running = running;
  $("send").hidden = running;
  $("cancel").hidden = !running;
  // Keep the composer editable while a turn runs (Codex-style: compose your next
  // message meanwhile). Send is already guarded by `if (state.running) return`, so an
  // enabled box can't double-send — and you never get locked out if an event is missed.
  $("prompt").disabled = false;
  $("attach").disabled = false;
  document.body.classList.toggle("is-running", running);
}

function goalStorageKey() {
  const workspace = state.boot && state.boot.workspace ? state.boot.workspace : "default";
  return `deepseekTul.goals.v1:${workspace}`;
}

function loadGoalStore() {
  try {
    const raw = localStorage.getItem(goalStorageKey());
    const parsed = raw ? JSON.parse(raw) : {};
    state.goalsBySession = parsed && typeof parsed === "object" ? parsed : {};
    delete state.goalsBySession.__draft__;
  } catch (_) {
    state.goalsBySession = {};
  }
  syncActiveGoalFromStore();
  renderGoalDock();
}

function saveGoalStore() {
  try { localStorage.setItem(goalStorageKey(), JSON.stringify(state.goalsBySession)); } catch (_) {}
}

function goalSessionKey() {
  return currentSessionId() || state.goalDraftId;
}

function currentGoalTodos() {
  const value = state.goalsBySession[goalSessionKey()];
  return Array.isArray(value) ? value : [];
}

function goalSnapshot(todos) {
  return JSON.stringify((todos || []).map((todo) => ({
    content: todo.content || "",
    status: todo.status || "pending",
  })));
}

function normalizeTodos(raw) {
  const items = Array.isArray(raw) ? raw : [];
  let seenInProgress = false;
  return items.map((item, index) => {
    const content = String((item && (item.content || item.text || item.title)) || "").trim();
    let status = String((item && item.status) || "pending");
    if (!["pending", "in_progress", "completed", "cancelled"].includes(status)) status = "pending";
    if (status === "in_progress") {
      if (seenInProgress) status = "pending";
      seenInProgress = true;
    }
    return { id: String((item && item.id) || `todo-${index + 1}`), content, status };
  }).filter((item) => item.content);
}

function applyTodoPayload(detail, sessionId) {
  let data = detail;
  if (typeof detail === "string") {
    try { data = JSON.parse(detail); } catch (_) { data = {}; }
  }
  data = unwrapTodoPayload(data);
  const todos = normalizeTodos(data && data.todos ? data.todos : data);
  const key = sessionId || goalSessionKey();
  if (todos.length) state.goalsBySession[key] = todos;
  else delete state.goalsBySession[key];
  delete state.dismissedGoalSnapshots[key];
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
}

function syncActiveGoalFromStore() {
  state.activeGoal = currentGoalTodos().filter((todo) => todo.status !== "completed").map((todo) => todo.content).join("\n");
}

function parseGoalTodos(goal) {
  const text = String(goal || "").trim();
  if (!text) return [];
  let parts = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (parts.length === 1 && /[;；]/.test(parts[0])) {
    parts = parts[0].split(/[;；]/).map((line) => line.trim()).filter(Boolean);
  }
  return parts.map((line, index) => {
    const content = line
      .replace(/^[-*]\s+\[[ xX]\]\s*/, "")
      .replace(/^[-*]\s+/, "")
      .replace(/^\d+[.)、]\s*/, "")
      .trim();
    return {
      id: `goal-${Date.now()}-${index}-${Math.random().toString(16).slice(2)}`,
      content: content || line,
      status: "pending",
    };
  });
}

function migrateDraftGoal(sessionId) {
  if (!sessionId) return;
  const draftKey = state.goalDraftId;
  if (!state.goalsBySession[draftKey]) return;
  if (!state.goalsBySession[sessionId]) state.goalsBySession[sessionId] = state.goalsBySession[draftKey];
  delete state.goalsBySession[draftKey];
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
}

function setGoalStatus(index, status) {
  const key = goalSessionKey();
  const todos = currentGoalTodos().slice();
  if (!todos[index]) return;
  todos[index] = { ...todos[index], status };
  state.goalsBySession[key] = todos;
  delete state.dismissedGoalSnapshots[key];
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
}

function markGoalRunning() {
  const key = goalSessionKey();
  const todos = currentGoalTodos().slice();
  if (!todos.length) return;
  const active = todos.findIndex((todo) => todo.status === "in_progress");
  if (active >= 0) return;
  const next = todos.findIndex((todo) => todo.status !== "completed");
  if (next < 0) return;
  todos[next] = { ...todos[next], status: "in_progress" };
  state.goalsBySession[key] = todos;
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
}

function renderGoalDock() {
  const dock = $("goalDock");
  if (!dock) return;
  const todos = currentGoalTodos();
  const key = goalSessionKey();
  if (!todos.length || state.dismissedGoalSnapshots[key] === goalSnapshot(todos)) {
    dock.hidden = true;
    dock.innerHTML = "";
    return;
  }
  const done = todos.filter((todo) => todo.status === "completed").length;
  const total = todos.length;
  const active = todos.find((todo) => todo.status === "in_progress") || todos.find((todo) => todo.status !== "completed") || todos[todos.length - 1];
  const pct = total ? Math.round((done / total) * 100) : 0;
  dock.hidden = false;
  dock.className = `goalDock${state.goalCollapsed ? " collapsed" : ""}${done === total ? " complete" : ""}`;
  dock.innerHTML = `
    <button class="goalClose" type="button" title="关闭任务目标">${icon("x", 12)}</button>
    <button class="goalHead" type="button" data-action="toggle-goal">
      <span class="goalOrb">${done === total ? icon("check", 13) : icon("loader", 13)}</span>
      <span class="goalMeta">
        <span class="goalTitle">任务目标</span>
        <span class="goalPreview">${escapeHtml(active ? active.content : "")}</span>
      </span>
      <span class="goalCount">${done}/${total}</span>
      <span class="goalChevron">⌄</span>
    </button>
    <div class="goalProgress"><span style="width:${pct}%"></span></div>
    <div class="goalList">
      ${todos.map((todo, index) => `
        <div class="goalItem" data-goal-index="${index}" data-state="${escapeHtml(todo.status)}">
          <span class="goalCheck"></span>
          <span class="goalText">${escapeHtml(todo.content)}</span>
          <span class="goalState">${goalStatusLabel(todo.status)}</span>
        </div>
      `).join("")}
    </div>`;
  const toggle = dock.querySelector('[data-action="toggle-goal"]');
  if (toggle) toggle.onclick = () => {
    state.goalCollapsed = !state.goalCollapsed;
    renderGoalDock();
  };
  const close = dock.querySelector(".goalClose");
  if (close) close.onclick = (event) => {
    event.stopPropagation();
    state.dismissedGoalSnapshots[goalSessionKey()] = goalSnapshot(currentGoalTodos());
    renderGoalDock();
  };
}

function goalStatusLabel(status) {
  if (status === "completed") return "完成";
  if (status === "in_progress") return "进行中";
  if (status === "cancelled") return "取消";
  return "等待";
}

async function refreshModels() {
  const models = await apiMethod("models");
  const result = await models({
    baseUrl: $("baseUrl") ? $("baseUrl").value : undefined,
    apiKey: $("apiKey") ? $("apiKey").value : undefined,
    providerFormat: $("providerFormat") ? $("providerFormat").value : undefined,
    model: $("model") ? $("model").value : undefined,
  });
  const fetched = result.ok && result.models && result.models.length ? result.models : [];
  // cache the last good full list so it never collapses to a single fallback item
  if (fetched.length) state.models = fetched;
  const list = (state.models && state.models.length) ? state.models.slice() : [state.boot.model];
  let current = $("model").value || state.boot.model;
  if (fetched.length && !fetched.includes(current)) {
    // model isn't offered by this provider — pick the provider's first, then persist
    current = fetched[0];
    fillSelect("model", ensureIncludes(list, current), current);
    setText("apiState", "模型可用");
    await updateRuntime();
    toast(`模型已切换为 ${current}`);
    return;
  }
  fillSelect("model", ensureIncludes(list, current), current);
  setText("apiState", result.ok ? "模型可用" : "模型列表暂不可用（沿用上次列表）");
}

// keep the current model in the option list even if the fetched list omits it
function ensureIncludes(list, value) {
  return value && !list.includes(value) ? [value].concat(list) : list;
}

async function refreshSessions() {
  let sessions;
  try {
    const fn = await apiMethod("sessions", 3000);
    sessions = await fn();
  } catch (_) {
    return;  // api not ready yet — a later poll will populate the list
  }
  if (!Array.isArray(sessions)) return;
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
        <small>${escapeHtml(session.session_id.slice(0, 8))}</small>
      </button>
      <div class="sessionActions">
        <button title="${session.pinned ? "取消置顶" : "置顶"}" class="actPin${session.pinned ? " on" : ""}">${icon("pin")}</button>
        <button title="改标题" class="actRename">${icon("edit")}</button>
        <button title="删除" class="actDelete">${icon("trash")}</button>
      </div>`;
    row.querySelector(".sessionMain").onclick = async () => {
      const result = await window.pywebview.api.resume(session.session_id);
      state.currentAssistant = null;
      state.currentTool = null;
      state.activeTurnId = "";
      state.pendingOutbound = false;
      state.pendingOutboundId = "";
      state.suppressLocalUserEcho = false;
      state.stickToBottom = true;
      $("messages").innerHTML = "";
      result.messages.forEach(replayMessage);
      markMessageActions();
      scrollMessages(true);
      state.currentSessionId = result.sessionId;
      if (state.boot) state.boot.sessionId = result.sessionId;
      setText("sessionState", result.sessionId.slice(0, 8));
      setSaveState("saved", "已恢复", result.sessionId);
      updateContextBadge(result.context || null);
      syncActiveGoalFromStore();
      renderGoalDock();
    };
    row.querySelector(".actPin").onclick = async (e) => {
      e.stopPropagation();
      await window.pywebview.api.pin_session(session.session_id, !session.pinned);
      await refreshSessions();
    };
    row.querySelector(".actRename").onclick = async (e) => {
      e.stopPropagation();
      const title = await uiPrompt("新的会话标题", session.title || "");
      if (!title) return;
      await window.pywebview.api.rename_session(session.session_id, title);
      await refreshSessions();
    };
    row.querySelector(".actDelete").onclick = async (e) => {
      e.stopPropagation();
      const ok = await uiConfirm(`删除会话「${session.title || session.session_id.slice(0, 8)}」？此操作不可恢复。`);
      if (!ok) return;
      await window.pywebview.api.delete_session(session.session_id);
      await refreshSessions();
    };
    box.append(row);
  });
}

// replay a serialized transcript entry: text bubble or a completed tool card
function replayMessage(entry) {
  if (entry.role === "tool") {
    if (entry.name === "todo_write") applyTodoPayload(entry.output || "", currentSessionId());
    const card = addToolEvent(entry.name, entry.detail);
    if (card) {
      card.dataset.done = "1";
      const status = card.querySelector(".evStatus");
      if (status) { status.textContent = "完成"; status.classList.add("ok"); }
      const out = card.querySelector(".toolOut");
      const code = out.querySelector("code");
      code.innerHTML = entry.name === "todo_write"
        ? todoSnapshotHtml(entry.output)
        : (truncateForDisplay(String(entry.output || "").trim()) ? highlightCode(truncateForDisplay(String(entry.output || "").trim()), "") : '<span class="t-com">（无输出）</span>');
      out.hidden = false;
    }
    state.currentTool = null;
    return;
  }
  const row = addMessage(entry.role, entry.content, entry.srcIndex);
  if (entry.intermediate && row) row.classList.add("intermediate");
  return row;
}

function appendTranscriptNode(node) {
  const box = $("messages");
  const marker = state.pendingVersionMarker;
  if (marker && marker.isConnected && marker.parentNode === box) {
    box.insertBefore(node, marker);
  } else {
    box.append(node);
  }
}

function bumpEventCount() {
  state.events += 1;
  setText("eventCount", String(state.events));
}

function addMessage(role, content, srcIndex) {
  const empty = document.querySelector(".empty, .intro");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = `message ${role}`;
  if (srcIndex !== undefined && srcIndex !== null) row.dataset.src = String(srcIndex);
  const avatar = role === "user" ? "你" : "F";
  const name = role === "user" ? "You" : "DeepSeekFathom";
  row.innerHTML = `<div class="msgHead"><span class="avatar ${role}">${avatar}</span><span class="who">${name}</span></div><div class="bubble ${role}"></div>` +
    `<div class="msgActions">` +
    `<button class="msgAct copy" title="复制">${icon("copy", 13)}</button>` +
    (role === "assistant" ? `<button class="msgAct retry" title="重试（重新生成，丢弃其后内容）">${icon("refresh", 13)}</button><button class="msgAct branch" title="从这里开分支">${icon("branch", 13)}</button>` : "") +
    (role === "user" ? `<button class="msgAct edit" title="编辑并重发（分支）">${icon("edit", 13)}</button>` : "") +
    `</div>`;
  const bubble = row.querySelector(".bubble");
  bubble.dataset.raw = content || "";
  renderBubble(bubble);
  appendTranscriptNode(row);
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
  bumpEventCount();
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  const details = document.createElement("details");
  details.className = "threadEvent tool";
  details.dataset.tool = name || "";
  details.innerHTML = `
    <summary><span class="eventIcon">${icon("terminal")}</span><span class="evLabel">工具调用</span><strong>${escapeHtml(name || "")}</strong><span class="evStatus">运行中</span><span class="evChevron">${icon("chevron", 13)}</span></summary>
    <div class="toolBody">
      <div class="toolSection toolCall"><div class="secLabel">调用</div><pre><code>${String(args || "").trim() ? highlightCode(String(args).trim(), guessLang(name, args)) : '<span class="t-com">（无参数）</span>'}</code></pre></div>
      <div class="toolSection toolOut" hidden><div class="secLabel">输出</div><pre><code></code></pre></div>
    </div>`;
  appendTranscriptNode(details);
  scrollMessages();
  mirror(`[工具调用] ${name || ""} ${args || ""}`);
  return details;
}

function completeToolEvent(name, output) {
  let block = state.currentTool;
  if (!block || block.dataset.done || (name && block.dataset.tool && block.dataset.tool !== name)) {
    const blocks = Array.from($("messages").querySelectorAll(".threadEvent.tool")).reverse();
    block = blocks.find((b) => !b.dataset.done && b.dataset.tool === name)
      || blocks.find((b) => !b.dataset.done)
      || null;
  }
  if (!block) {
    addEvent("done", name, output);
    return;
  }
  block.dataset.done = "1";
  state.currentTool = null;
  const status = block.querySelector(".evStatus");
  if (status) { status.textContent = "完成"; status.classList.add("ok"); }
  const out = block.querySelector(".toolOut");
  const code = out.querySelector("code");
  const text = truncateForDisplay(String(output || "").trim());
  code.innerHTML = name === "todo_write" ? todoSnapshotHtml(output) : (text ? highlightCode(text, "") : "<span class=\"t-com\">（无输出）</span>");
  out.hidden = false;
  scrollMessages();
  mirror(`[工具完成] ${name || ""} ${(output || "").slice(0, 200)}`);
}

function todoSnapshotHtml(output) {
  let data = {};
  try { data = JSON.parse(String(output || "")); } catch (_) {}
  data = unwrapTodoPayload(data);
  const todos = normalizeTodos(data && data.todos ? data.todos : data);
  if (!todos.length) return '<span class="t-com">0 个任务</span>';
  const mark = (status) => status === "completed" ? "[✓]" : status === "in_progress" ? "[•]" : status === "cancelled" ? "[×]" : "[ ]";
  return todos.map((todo) => `${mark(todo.status)} ${escapeHtml(todo.content)}`).join("\n");
}

function unwrapTodoPayload(data) {
  if (!data || typeof data !== "object") return data;
  if (data.todos || Array.isArray(data)) return data;
  if (typeof data.output === "string") {
    try {
      const inner = JSON.parse(data.output);
      if (inner && typeof inner === "object") return inner;
    } catch (_) {}
  }
  return data;
}

function showMediaFrames(detail) {
  let data = {};
  try { data = JSON.parse(String(detail || "")); } catch (_) {}
  const images = Array.isArray(data.images) ? data.images : [];
  if (!images.length) return;
  const block = addEvent("media", "截图/视频帧", `已查看 ${images.length} 张画面`);
  const pre = block && block.querySelector("pre");
  if (!pre) return;
  pre.innerHTML = "";
  const strip = document.createElement("div");
  strip.className = "msgImages mediaFrames";
  images.forEach((url) => {
    const img = document.createElement("img");
    img.src = url;
    strip.append(img);
  });
  pre.append(strip);
}

const MAX_DISPLAY_CHARS = 40000;
function truncateForDisplay(text) {
  if (text.length <= MAX_DISPLAY_CHARS) return text;
  return text.slice(0, MAX_DISPLAY_CHARS) + `\n…（输出过长，已截断 ${text.length - MAX_DISPLAY_CHARS} 字符）`;
}

function addEvent(kind, name, detail) {
  bumpEventCount();
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  const details = document.createElement("details");
  details.className = `threadEvent ${kind}`;
  const icon_ = iconFor(kind);
  details.innerHTML = `
    <summary><span class="eventIcon">${icon_}</span><span class="evLabel">${labelFor(kind)}</span><strong>${escapeHtml(name || "")}</strong><span class="evChevron">${icon("chevron", 13)}</span></summary>
    <pre>${escapeHtml(truncateForDisplay(String(detail || "")))}</pre>`;
  appendTranscriptNode(details);
  scrollMessages();
  mirror(`[${labelFor(kind)}] ${name || ""} ${detail || ""}`.trim());
  return details;
}

/* ---------- subagent group: an expandable card that shows the subagent working ---------- */
function subagentCard(name) {
  let card = $("messages").querySelector(`.subagentCard[data-sub="${cssEscape(name)}"]:not(.done)`);
  if (card) return card;
  bumpEventCount();
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  card = document.createElement("details");
  card.className = "threadEvent subagent subagentCard";
  card.dataset.sub = name;
  card.open = true;
  card.innerHTML =
    `<summary><span class="eventIcon">${icon("branch")}</span><span class="evLabel">子代理</span>` +
    `<strong>${escapeHtml(name)}</strong><span class="evStatus">运行中</span>` +
    `<span class="evChevron">${icon("chevron", 13)}</span></summary>` +
    `<div class="subBody"></div>`;
  appendTranscriptNode(card);
  scrollMessages();
  return card;
}

function addSubEvent(name, payload) {
  const body = subagentCard(name).querySelector(".subBody");
  const row = document.createElement("div");
  row.className = `subRow ${payload.kind}`;
  const label = payload.kind === "tool" ? `⌘ ${payload.name || ""}`
    : payload.kind === "done" ? `✓ ${payload.name || ""}`
    : payload.kind === "subanswer" ? "↳ 输出"
    : `${labelFor(payload.kind)} ${payload.name || ""}`;
  row.innerHTML = `<span class="subLabel">${escapeHtml(label.trim())}</span>` +
    (payload.detail ? `<pre class="subDetail">${escapeHtml(truncateForDisplay(String(payload.detail)))}</pre>` : "");
  body.append(row);
  scrollMessages();
  mirror(`[子代理:${name}] ${label} ${payload.detail || ""}`.trim());
}

function markSubagentDone(name, summary) {
  const card = $("messages").querySelector(`.subagentCard[data-sub="${cssEscape(name)}"]:not(.done)`);
  if (!card) return;
  // append the subagent's final result so the card carries its complete output, not
  // just the tool trace
  if (summary && String(summary).trim()) {
    const body = card.querySelector(".subBody");
    const row = document.createElement("div");
    row.className = "subRow subanswer";
    row.innerHTML = `<span class="subLabel">↳ 结果</span>` +
      `<pre class="subDetail">${escapeHtml(truncateForDisplay(String(summary).trim()))}</pre>`;
    body.append(row);
  }
  card.classList.add("done");
  const status = card.querySelector(".evStatus");
  if (status) { status.textContent = "完成"; status.classList.add("ok"); }
  card.open = false;  // collapse when the subagent finishes; click to reopen
}

function cssEscape(s) {
  return String(s).replace(/["\\\]]/g, "\\$&");
}

function mirror(line) {
  const box = $("eventMirror");
  if (!box) return;
  const def = "工具、思考和子代理事件会显示在这里。";
  const current = box.textContent === def ? "" : box.textContent;
  const lines = (current ? current + "\n" : "").concat(line).split("\n");
  box.textContent = lines.slice(-600).join("\n");
  box.scrollTop = box.scrollHeight;
}

function labelFor(kind) {
  return {
    thinking: "内部思考", tool: "工具调用", done: "工具完成",
    subagent: "子代理", compact: "上下文压缩", error: "错误",
  }[kind] || "事件";
}

function iconFor(kind) {
  return icon(({
    thinking: "dots", tool: "terminal", done: "check", subagent: "branch",
    compact: "compact", error: "alert", skill: "sparkle",
  }[kind]) || "terminal");
}

function scrollMessages(force = false) {
  const box = $("messages");
  if (force || state.stickToBottom) box.scrollTop = box.scrollHeight;
}

/* ---------- thinking shimmer: shown immediately on send, hidden once content flows ---------- */
function showThinking(message) {
  let el = $("messages").querySelector(".thinkingShimmer");
  if (!el) {
    const intro = document.querySelector(".empty, .intro");
    if (intro) intro.remove();
    el = document.createElement("div");
    el.className = "thinkingShimmer";
    el.innerHTML = `<span class="shimmerDots"><i></i><i></i><i></i></span><span class="shimmerText"></span>`;
    appendTranscriptNode(el);
  }
  el.querySelector(".shimmerText").textContent = message || "思考中";
  // keep the shimmer as the last child so it always sits below the newest content
  appendTranscriptNode(el);
  scrollMessages();
}

function hideThinking() {
  const el = $("messages").querySelector(".thinkingShimmer");
  if (el) el.remove();
}
$("messages").addEventListener("scroll", () => {
  const box = $("messages");
  state.stickToBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
});

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function hasMathDelimiters(text) {
  return /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^\n$]+?\$/.test(String(text || ""));
}

function loadKatexRenderer() {
  if (state.katexRenderToString) return Promise.resolve(state.katexRenderToString);
  if (state.katexLoadPromise) return state.katexLoadPromise;
  state.katexLoadPromise = import("./katex-CBSAILhF.js")
    .then((module) => {
      state.katexRenderToString = module.renderToString || (module.default && module.default.renderToString) || null;
      if (!state.katexRenderToString) throw new Error("KaTeX renderToString missing");
      rerenderMathBubbles();
      return state.katexRenderToString;
    })
    .catch((error) => {
      console.warn("KaTeX unavailable, using lightweight math fallback.", error);
      state.katexLoadPromise = null;
      return null;
    });
  return state.katexLoadPromise;
}

function rerenderMathBubbles() {
  document.querySelectorAll(".bubble.assistant").forEach((bubble) => {
    if (hasMathDelimiters(bubble.dataset.raw || "")) renderBubble(bubble);
  });
}

function renderLatex(tex, displayMode) {
  const source = String(tex || "").trim();
  if (!source) return "";
  if (state.katexRenderToString) {
    try {
      return state.katexRenderToString(source, {
        displayMode,
        throwOnError: false,
        strict: "ignore",
        trust: false,
        output: "htmlAndMathml",
        maxSize: 12,
        maxExpand: 1000,
      });
    } catch (_) {
      // Keep rendering resilient while streaming partially complete model output.
    }
  } else {
    loadKatexRenderer().catch(() => {});
  }
  return renderMathFallback(source);
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
  // Protect math ($$…$$, \[…\], $…$, \(…\)) before markdown parsing so
  // underscores, asterisks and pipes inside formulas are not mistaken for markdown.
  src = src.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g, (m, a, b) => {
    const i = blocks.length;
    blocks.push(`<div class="mathBlock">${renderLatex(a != null ? a : b, true)}</div>`);
    return `@@FB${i}@@`;
  });
  src = src.replace(/\$([^\n$]+?)\$|\\\(([\s\S]+?)\\\)/g, (m, a, b) => {
    const inner = a != null ? a : b;
    // $…$ is ambiguous with currency. Treat it as math when it has LaTeX commands,
    // super/subscripts, or variable/operator structure (x+y, a=b, x^2), but not plain
    // money/ranges like $5 or $5 和 $10.
    if (a != null && !looksLikeMath(inner)) return m;
    const i = blocks.length;
    blocks.push(`<span class="mathInline">${renderLatex(inner, false)}</span>`);
    return `@@FB${i}@@`;
  });
  const lines = src.split("\n");
  let html = "";
  let list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const isTableRow = (l) => typeof l === "string" && /\|/.test(l) && l.trim() !== "";
  const isTableSep = (l) => typeof l === "string" && /^\s*\|?(\s*:?-{2,}:?\s*\|)*\s*:?-{2,}:?\s*\|?\s*$/.test(l);
  const splitCells = (l) => {
    let s = l.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const ph = line.match(/^@@FB(\d+)@@$/);
    if (ph) { closeList(); html += blocks[+ph[1]]; continue; }
    if (/^\s*$/.test(line)) { closeList(); continue; }
    // markdown table: header row + separator row + body rows
    if (isTableRow(line) && isTableSep(lines[i + 1])) {
      closeList();
      const head = splitCells(line);
      let body = [];
      let j = i + 2;
      while (j < lines.length && isTableRow(lines[j]) && !isTableSep(lines[j])) {
        body.push(splitCells(lines[j]));
        j++;
      }
      html += '<div class="mdTableWrap"><button class="tableCopyBtn" type="button" title="复制表格">' + icon("copy", 13) + '</button><table class="mdTable"><thead><tr>' +
        head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        body.map((row) => "<tr>" + head.map((_, k) => `<td>${inline(row[k] || "")}</td>`).join("") + "</tr>").join("") +
        "</tbody></table></div>";
      i = j - 1;
      continue;
    }
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
  // restore protected code/math placeholders wherever they appear (including inline math
  // embedded inside a paragraph or table cell), not only when a line is exactly @@FBn@@.
  return html.replace(/@@FB(\d+)@@/g, (m, i) => blocks[Number(i)] || "");

  function inline(t) {
    t = escapeHtml(t);
    t = t.replace(/`([^`]+)`/g, (m, c) => `<code class="inline">${c}</code>`);
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, href) =>
      /^https?:\/\//i.test(href) ? `<a href="${href}" target="_blank" rel="noreferrer">${label}</a>` : m);
    return t;
  }
}

// Lightweight fallback used only before/if KaTeX cannot load. It keeps formulas
// readable but intentionally stays behind the real KaTeX renderer.
const MATH_SYMBOLS = {
  "\\alpha":"α","\\beta":"β","\\gamma":"γ","\\delta":"δ","\\epsilon":"ε","\\varepsilon":"ε","\\zeta":"ζ","\\eta":"η","\\theta":"θ","\\iota":"ι","\\kappa":"κ","\\lambda":"λ","\\mu":"μ","\\nu":"ν","\\xi":"ξ","\\pi":"π","\\rho":"ρ","\\sigma":"σ","\\tau":"τ","\\phi":"φ","\\varphi":"φ","\\chi":"χ","\\psi":"ψ","\\omega":"ω",
  "\\Gamma":"Γ","\\Delta":"Δ","\\Theta":"Θ","\\Lambda":"Λ","\\Xi":"Ξ","\\Pi":"Π","\\Sigma":"Σ","\\Phi":"Φ","\\Psi":"Ψ","\\Omega":"Ω",
  "\\times":"×","\\cdot":"·","\\div":"÷","\\pm":"±","\\mp":"∓","\\leq":"≤","\\le":"≤","\\geq":"≥","\\ge":"≥","\\neq":"≠","\\ne":"≠","\\approx":"≈","\\equiv":"≡","\\sim":"∼","\\propto":"∝",
  "\\infty":"∞","\\partial":"∂","\\nabla":"∇","\\sum":"∑","\\prod":"∏","\\int":"∫","\\oint":"∮","\\sqrt":"√",
  "\\rightarrow":"→","\\to":"→","\\leftarrow":"←","\\Rightarrow":"⇒","\\Leftarrow":"⇐","\\leftrightarrow":"↔","\\Leftrightarrow":"⇔","\\mapsto":"↦",
  "\\in":"∈","\\notin":"∉","\\subset":"⊂","\\subseteq":"⊆","\\supset":"⊃","\\cup":"∪","\\cap":"∩","\\emptyset":"∅","\\forall":"∀","\\exists":"∃","\\neg":"¬","\\wedge":"∧","\\vee":"∨",
  "\\ldots":"…","\\cdots":"⋯","\\dots":"…","\\angle":"∠","\\degree":"°","\\prime":"′",
};
function looksLikeMath(s) {
  s = String(s || "").trim();
  if (!s) return false;
  if (/^[¥€£]?\s*\d+(?:[.,]\d+)?\s*$/.test(s)) return false;  // plain money/number
  if (/\\[A-Za-z]+|[\^_{}]|[=+*/<>≤≥√∑∫∞≈≠→←×÷±]|\b(sin|cos|tan|log|ln|lim|max|min)\b/.test(s)) return true;
  // single-letter variables next to digits/operators are math; plain words are not
  return /\b[a-zA-Z]\b/.test(s) && /\d|[+\-*/=()]/.test(s);
}

function renderMathFallback(tex) {
  let s = String(tex || "").trim();
  // tolerate nested wrappers like \($x$\) from model output
  if (s.startsWith("$") && s.endsWith("$") && s.length > 1) s = s.slice(1, -1).trim();
  s = s.replace(/\\(?:left|right|displaystyle)\b|\\!|\\,|\\;|\\:|\\quad\b|\\qquad\b/g, " ");
  // \frac{a}{b} -> (a)/(b), \sqrt{x} -> √(x)
  s = s.replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
  s = s.replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)");
  s = s.replace(/\\text\s*\{([^{}]*)\}/g, "$1");
  for (const [k, v] of Object.entries(MATH_SYMBOLS)) s = s.split(k).join(v);
  s = escapeHtml(s);
  // superscripts / subscripts: ^{...} ^x  _{...} _x
  s = s.replace(/\^\{([^{}]*)\}/g, (m, g) => `<sup>${g}</sup>`);
  s = s.replace(/\^(\w)/g, (m, g) => `<sup>${g}</sup>`);
  s = s.replace(/_\{([^{}]*)\}/g, (m, g) => `<sub>${g}</sub>`);
  s = s.replace(/_(\w)/g, (m, g) => `<sub>${g}</sub>`);
  s = s.replace(/[{}]/g, "");  // drop leftover grouping braces
  return s;
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
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  if (code.length > 60000) return esc(code);
  lang = (lang || "").toLowerCase();
  lang = HL_ALIAS[lang] || lang;
  const kw = new Set(HL_KEYWORDS[lang] || []);
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
  const setRuntime = await apiMethod("set_runtime");
  state.boot = await setRuntime({
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
  const raw = $("prompt").value.trim();
  if (!raw && !state.attachments.length && !state.images.length) return;
  let outgoing = raw;
  if (raw) {
    const cmd = interpretPrompt(raw);
    if (cmd.handled) { $("prompt").value = ""; autoGrow(); closeSlash(); return; }
    if (cmd.unknown) { closeSlash(); toast(`未知命令 /${cmd.unknown} —— 输入 / 查看可用命令`); return; }
    outgoing = cmd.send;
  }
  $("prompt").value = "";
  autoGrow();
  closeSlash();
  const attachments = state.attachments;
  const images = state.images;
  state.attachments = [];
  state.images = [];
  renderAttachments();
  // show the user's message (with image thumbnails) immediately
  addUserMessageWithImages(outgoing, images, attachments);
  state.suppressLocalUserEcho = true;  // turn:start would double-add it
  state.stickToBottom = true;
  setRunning(true);
  state.pendingOutbound = true;
  const outboundId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  state.pendingOutboundId = outboundId;
  try {
    if (state.cancelPromise) {
      await state.cancelPromise.catch(() => {});
      state.cancelPromise = null;
    }
    await updateRuntime();
    const sendFn = await apiMethod("send");
    const result = await sendFn({ prompt: outgoing, attachments, images: images.map((i) => i.url), goal: state.activeGoal || undefined });
    if (!result.ok) throw new Error(result.error || "unknown error");
    const stillVisibleOutbound = state.pendingOutboundId === outboundId;
    if (stillVisibleOutbound) state.pendingOutbound = false;
    if (result.sessionId) {
      if (stillVisibleOutbound) {
        state.currentSessionId = result.sessionId;
        if (state.boot) state.boot.sessionId = result.sessionId;
        migrateDraftGoal(result.sessionId);
        setText("sessionState", String(result.sessionId).slice(0, 8));
      }
    }
    if (result.turnId && stillVisibleOutbound) state.activeTurnId = result.turnId;
  } catch (error) {
    if (state.pendingOutboundId === outboundId) {
      state.pendingOutbound = false;
      state.pendingOutboundId = "";
      setRunning(false);
      addEvent("error", "发送失败", String(error.message || error));
      $("prompt").value = raw;
      state.attachments = attachments;
      state.images = images;
      renderAttachments();
      autoGrow();
    }
  }
};

function addUserMessageWithImages(text, images, attachments) {
  const row = addMessage("user", text || "");
  const bubble = row.querySelector(".bubble");
  if (images && images.length) {
    const strip = document.createElement("div");
    strip.className = "msgImages";
    images.forEach((img) => {
      const el = document.createElement("img");
      el.src = img.url;
      strip.append(el);
    });
    bubble.append(strip);
  }
  if (attachments && attachments.length) {
    const list = document.createElement("div");
    list.className = "msgFiles";
    attachments.forEach((file) => {
      const item = document.createElement("div");
      item.className = "msgFile";
      const extra = file.kind === "video" && file.frameCount ? ` · 已抽 ${file.frameCount} 帧` : "";
      const size = Number.isFinite(Number(file.size)) ? ` · ${fmtTokens(Number(file.size))}B` : "";
      const showPath = ["folder", "folder_file", "video"].includes(file.kind || "");
      item.textContent = showPath ? `${file.name || "file"} · ${file.path || ""}${extra}` : `${file.name || "file"}${size}${extra}`;
      list.append(item);
      if (file.frames && file.frames.length) {
        const frames = document.createElement("div");
        frames.className = "msgImages mediaFrames";
        file.frames.forEach((url) => {
          const el = document.createElement("img");
          el.src = url;
          frames.append(el);
        });
        list.append(frames);
      }
    });
    bubble.append(list);
  }
  return row;
}

function autoGrow() {
  const ta = $("prompt");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
}

/* ---------- slash command menu: small command palette, not a prompt-template dump ---------- */
const SLASH_COMMANDS = [
  { key: "/goal", desc: "查看当前目标", run: () => showGoal() },
  { key: "/goal <text>", desc: "设置持续目标", insert: "/goal " },
  { key: "/goal clear", desc: "清除持续目标", run: () => setGoal("") },
  { key: "/compact", desc: "压缩当前上下文", run: () => $("manualCompact").click() },
  { key: "/new", desc: "开始新对话", run: () => $("newSession").click() },
  { key: "/settings", desc: "打开 API 设置", run: () => $("settingsBtn").click() },
  { key: "/copyid", desc: "复制当前会话 ID", run: () => { const s = currentSessionId(); if (s) { copyToClipboard(s, null, null, null); toast("已复制会话 ID"); } else toast("暂无会话 ID"); } },
];

function slashCandidates(query) {
  const q = query.toLowerCase();
  const skills = (state.skills || []).map((s) => ({
    key: "/skill " + s.name, desc: s.description || "技能", insert: `Use skill ${s.name}: `,
  }));
  return SLASH_COMMANDS.concat(skills).filter((it) => it.key.toLowerCase().includes(q));
}

function updateSlash() {
  const match = $("prompt").value.match(/^\/([\w-]*)$/);
  if (!match) { closeSlash(); return; }
  state.slash = { open: true, items: slashCandidates(match[1]), index: 0 };
  renderSlash();
}

function renderSlash() {
  const menu = $("slashMenu");
  if (!menu || !state.slash.open) { closeSlash(); return; }
  menu.hidden = false;
  if (!state.slash.items.length) {
    menu.innerHTML = '<div class="slashEmpty">无匹配命令</div>';
    return;
  }
  menu.innerHTML = state.slash.items.map((it, i) =>
    `<div class="slashItem${i === state.slash.index ? " active" : ""}" data-i="${i}">` +
    `<span class="slashName">${escapeHtml(it.key)}</span>` +
    `<span class="slashDesc">${escapeHtml(it.desc)}</span></div>`
  ).join("");
}

function moveSlash(step) {
  const n = state.slash.items.length;
  if (!n) return;
  state.slash.index = (state.slash.index + step + n) % n;
  renderSlash();
  const active = $("slashMenu").querySelector(".slashItem.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function selectSlash(i) {
  const it = state.slash.items[i];
  closeSlash();
  if (!it) return;
  if (it.insert !== undefined) {
    $("prompt").value = it.insert;
    $("prompt").focus();
    autoGrow();
  } else if (it.run) {
    $("prompt").value = "";
    autoGrow();
    it.run();
  }
}

function closeSlash() {
  state.slash.open = false;
  const menu = $("slashMenu");
  if (menu) menu.hidden = true;
}

function setGoal(goal) {
  const key = goalSessionKey();
  const todos = parseGoalTodos(goal);
  if (todos.length) state.goalsBySession[key] = todos;
  else delete state.goalsBySession[key];
  delete state.dismissedGoalSnapshots[key];
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
  toast(state.activeGoal ? `目标已设置：${state.activeGoal}` : "目标已清除");
  setSaveState(
    state.running ? "running" : (currentSessionId() ? "saved" : "idle"),
    state.running ? "运行中" : (currentSessionId() ? "已保存" : "新会话"),
    state.activeGoal ? `Goal: ${state.activeGoal}` : (currentSessionId() || "未保存")
  );
}

function showGoal() {
  toast(state.activeGoal ? `当前目标：${state.activeGoal}` : "当前没有持续目标");
}

/* Route a leading-slash message: run app commands locally, set modes, or expand
   explicit skills. Unknown slash commands are blocked instead of being sent raw. */
function interpretPrompt(text) {
  const match = text.match(/^\/([a-zA-Z][\w-]*)(?:\s+([\s\S]*))?$/);
  if (!match) return { send: text };
  const name = match[1].toLowerCase();
  const rest = (match[2] || "").trim();
  if (name === "goal") {
    if (!rest) { showGoal(); return { handled: true }; }
    if (["clear", "off", "none"].includes(rest.toLowerCase())) setGoal("");
    else setGoal(rest);
    return { handled: true };
  }
  if (name === "compact") { $("manualCompact").click(); return { handled: true }; }
  if (name === "new") { $("newSession").click(); return { handled: true }; }
  if (name === "settings") { $("settingsBtn").click(); return { handled: true }; }
  if (name === "skill") {
    const [skillName, ...tail] = rest.split(/\s+/);
    const skill = (state.skills || []).find((s) => (s.name || "").toLowerCase() === String(skillName || "").toLowerCase());
    if (skill) return { send: tail.length ? `Use skill ${skill.name}: ${tail.join(" ")}` : `Use skill ${skill.name}: ` };
    return { unknown: rest ? `skill ${skillName}` : "skill" };
  }
  // generic: any run-command from the slash menu (typed + Enter) runs locally
  const cmd = SLASH_COMMANDS.find((c) => c.key.toLowerCase() === "/" + name);
  if (cmd) {
    if (cmd.run && !rest) { cmd.run(); return { handled: true }; }
    if (cmd.insert !== undefined) return { send: rest ? cmd.insert + rest : cmd.insert };
    if (cmd.run) { cmd.run(); return { handled: true }; }
  }
  return { unknown: match[1] };
}

function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = truncateInline(message, 160);
  el.title = String(message || "");
  document.body.append(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 200); }, 2600);
}

$("prompt").addEventListener("input", () => { autoGrow(); updateSlash(); });
// paste an image directly into the composer
$("prompt").addEventListener("paste", async (e) => {
  const items = e.clipboardData && e.clipboardData.items ? Array.from(e.clipboardData.items) : [];
  const imgs = items.filter((it) => it.kind === "file" && (it.type || "").startsWith("image/"));
  if (!imgs.length) return;
  e.preventDefault();
  for (const it of imgs) {
    const file = it.getAsFile();
    if (file) await uploadFile(file, `粘贴图片-${Date.now()}.png`);
  }
  renderAttachments();
});
$("prompt").addEventListener("blur", () => setTimeout(closeSlash, 150));
$("slashMenu").addEventListener("mousedown", (e) => {
  const item = e.target.closest(".slashItem");
  if (!item) return;
  e.preventDefault();
  selectSlash(Number(item.dataset.i));
});
$("prompt").addEventListener("keydown", (event) => {
  // isComposing / keyCode 229: IME (中文输入法) 候选确认，不能当作发送/命令
  if (event.isComposing || event.keyCode === 229) return;
  if (state.slash.open) {
    if (event.key === "ArrowDown") { event.preventDefault(); moveSlash(1); return; }
    if (event.key === "ArrowUp") { event.preventDefault(); moveSlash(-1); return; }
    if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      state.slash.items.length ? selectSlash(state.slash.index) : closeSlash();
      return;
    }
    if (event.key === "Escape") { event.preventDefault(); closeSlash(); return; }
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("send").click();
  }
});

$("cancel").onclick = async () => {
  // respond instantly: free the UI now, ignore late events from this turn, let the
  // backend wind down the in-flight request/tool in the background
  state.suppressStream = true;
  setRunning(false);
  hideThinking();
  setSaveState("idle", "已中断", state.currentSessionId || "未保存");
  addEvent("done", "已中断", "已停止当前回复");
  state.currentAssistant = null;
  state.currentTool = null;
  // keep copy/retry/branch available on whatever was produced before the interrupt
  markMessageActions();
  const turnId = state.activeTurnId || "";
  state.cancelPromise = window.pywebview.api.cancel({ turnId });
  try { await state.cancelPromise; } catch (_) {}
};

["thinking", "mode"].forEach((id) => $(id).addEventListener("change", updateRuntime));
$("model").addEventListener("change", updateRuntime);
$("mode").addEventListener("change", updateModeHelp);
$("format").addEventListener("change", async () => {
  $("providerFormat").value = $("format").value;
  const configure = await apiMethod("configure");
  await configure({ providerFormat: $("format").value });
  const bootFn = await apiMethod("boot");
  state.boot = await bootFn();
  // the new provider serves a different model list — refresh it
  await refreshModels().catch(() => {});
});
$("settingsBtn").onclick = () => $("settingsDialog").showModal();
$("ctxSave").onclick = async () => {
  const configureContext = await apiMethod("configure_context");
  const result = await configureContext({
    contextWindowTokens: $("ctxLimitInput").value,
    compactThresholdPercent: $("ctxThresholdInput").value,
  });
  if (result.ok) {
    if (result.boot) state.boot = result.boot;
    updateContextBadge(result.context || (result.boot && result.boot.context) || null);
    toast("上下文设置已保存");
  }
};
$("testConn").onclick = async () => {
  const box = $("testResult");
  box.hidden = false;
  box.className = "testResult testing";
  box.textContent = "正在获取上游模型列表…";
  const fmtReasoning = (r) => {
    if (!r || !Object.keys(r).length) return "无（思考关闭）";
    try { return JSON.stringify(r); } catch (_) { return String(r); }
  };
  try {
    const probe = {
      baseUrl: $("baseUrl").value,
      apiKey: $("apiKey").value,
      providerFormat: $("providerFormat").value,
      model: $("model") ? $("model").value : undefined,
    };
    const modelsFn = await apiMethod("models");
    const listed = await modelsFn(probe);
    const fetched = listed.ok && Array.isArray(listed.models) ? listed.models.filter(Boolean) : [];
    if (!fetched.length) throw new Error(listed.error || "上游没有返回可选模型");
    state.models = fetched;
    const current = fetched.includes(probe.model) ? probe.model : fetched[0];
    fillSelect("model", ensureIncludes(fetched, current), current);
    const selectedModel = await uiSelect("选择用于测试连接的模型", fetched, current);
    if (!selectedModel) {
      box.className = "testResult";
      box.textContent = "已取消测试连接";
      return;
    }
    $("model").value = selectedModel;
    box.className = "testResult testing";
    box.textContent = `正在使用 ${selectedModel} 发送真实测试请求…`;
    const testConnection = await apiMethod("test_connection");
    const r = await testConnection({
      ...probe,
      model: selectedModel,
    });
    if (r.ok) {
      box.className = "testResult ok";
      box.textContent =
        `连接成功 · 模型 ${r.model} · ${r.resolved}\n` +
        `思考档位：${r.thinking} · 上游 reasoning 参数：${fmtReasoning(r.reasoning)}\n` +
        `模型回复：${r.reply || "（空）"}`;
    } else {
      box.className = "testResult err";
      box.textContent =
        `连接失败：${r.error || "未知错误"}\n` +
        `本次尝试发送的 reasoning 参数：${fmtReasoning(r.reasoning)}` +
        (r.resolved ? `\n端点：${r.resolved}` : "");
    }
  } catch (e) {
    box.className = "testResult err";
    box.textContent = `连接失败：${String(e.message || e)}`;
  }
};
$("saveSettings").onclick = async (event) => {
  event.preventDefault();
  const configure = await apiMethod("configure");
  state.boot = await configure({
    baseUrl: $("baseUrl").value,
    apiKey: $("apiKey").value,
    providerFormat: $("providerFormat").value,
    defaultMode: $("mode").value,
    defaultThinking: $("thinking").value,
  });
  $("settingsDialog").close();
  await boot();
};
$("newSession").onclick = async () => {
  const result = await window.pywebview.api.new_session();
  state.currentAssistant = null;
  state.currentTool = null;
  state.currentSessionId = "";
  state.activeTurnId = "";
  state.pendingOutbound = false;
  state.pendingOutboundId = "";
  state.suppressLocalUserEcho = false;
  delete state.goalsBySession[state.goalDraftId];
  delete state.dismissedGoalSnapshots[state.goalDraftId];
  state.goalDraftId = `__draft__:${Date.now()}-${Math.random().toString(16).slice(2)}`;
  saveGoalStore();
  syncActiveGoalFromStore();
  renderGoalDock();
  if (state.boot) state.boot.sessionId = null;
  state.events = 0;
  state.stickToBottom = true;
  setText("eventCount", "0");
  $("messages").innerHTML = '<div class="empty intro"><div class="introMark">DeepSeekFathom</div><h1>新对话已创建</h1><p>输入任务开始，输入 <kbd>/</kbd> 调出命令。工具调用与输出会内联展开。</p></div>';
  setText("eventMirror", "工具、思考和子代理事件会显示在这里。");
  setText("sessionState", "新会话");
  setSaveState("idle", "新会话", "未保存");
  updateContextBadge(result.context || null);
  refreshSessions();
};
$("refreshSessions").onclick = refreshSessions;
$("contextBadge").onclick = (e) => {
  e.stopPropagation();
  const pop = $("ctxPopover");
  if (pop) pop.hidden = !pop.hidden;
};
document.addEventListener("click", (e) => {
  const pop = $("ctxPopover");
  if (pop && !pop.hidden && !e.target.closest("#ctxPopover") && !e.target.closest("#contextBadge")) pop.hidden = true;
});
// keep the sidebar list fresh without a manual click
window.addEventListener("focus", () => { if (!state.running) refreshSessions(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.running) refreshSessions(); });
// several quick polls right after launch (api may attach late), then a steady beat
[600, 1500, 3000].forEach((t) => setTimeout(() => refreshSessions(), t));
setInterval(() => { if (!state.running && !document.hidden) refreshSessions(); }, 5000);

/* ---------- conversation menu (top-right ⋮ — copy ID / rename / branch / new / delete) ---------- */
function currentSessionId() {
  return state.currentSessionId || (state.boot && state.boot.sessionId) || "";
}
const convMenu = $("convMenu");
$("convMenuBtn").onclick = (e) => {
  e.stopPropagation();
  convMenu.hidden = !convMenu.hidden;
};
document.addEventListener("click", (e) => {
  if (!convMenu.hidden && !e.target.closest(".convMenuWrap")) convMenu.hidden = true;
});
convMenu.addEventListener("click", async (e) => {
  const item = e.target.closest(".convItem");
  if (!item) return;
  convMenu.hidden = true;
  const act = item.dataset.act;
  const sid = currentSessionId();
  if (act === "copyId") {
    if (!sid) { toast("当前还没有会话 ID（先发一条消息）"); return; }
    copyToClipboard(sid, null, null, null);
    toast(`已复制会话 ID：${sid.slice(0, 8)}…`);
  } else if (act === "rename") {
    if (!sid) { toast("当前还没有会话"); return; }
    const title = await uiPrompt("新的会话标题", "");
    if (title) { await window.pywebview.api.rename_session(sid, title); await refreshSessions(); }
  } else if (act === "branch") {
    doBranch();
  } else if (act === "new") {
    $("newSession").click();
  } else if (act === "delete") {
    if (!sid) { toast("当前还没有会话"); return; }
    const ok = await uiConfirm("删除当前对话？此操作不可恢复。");
    if (!ok) return;
    const result = await window.pywebview.api.delete_session(sid);
    $("newSession").click();
    updateContextBadge(result.context || null);
    await refreshSessions();
  }
});
$("manualCompact").onclick = async () => {
  updateContextBadge({ status: "active", label: "压缩中" });
  const result = await window.pywebview.api.compact();
  if (!result.ok) {
    addEvent("compact", "手动压缩", result.error || "no active session");
    return;
  }
  $("messages").innerHTML = "";
  result.messages.forEach(replayMessage);
  addEvent("compact", "手动压缩", `${result.before} -> ${result.after} estimated tokens`);
  updateContextBadge(result.context || null);
};
$("attach").onclick = () => $("fileInput").click();
$("fileInput").onchange = async (event) => {
  for (const file of event.target.files) await uploadFile(file);
  event.target.value = "";
  renderAttachments();
};

async function uploadFile(file, relPath) {
  try {
    const content = await readFileAsDataUrl(file);
    // images ride along as vision input (kept as data URL); other files are saved to disk
    if ((file.type || "").startsWith("image/")) {
      state.images.push({ name: relPath || file.name || "image", url: content });
      return;
    }
    const saved = await window.pywebview.api.save_upload({ name: relPath || file.name, content });
    if (saved && saved.ok) {
      state.attachments.push(saved);
      if (saved.kind === "video" && Array.isArray(saved.frames)) {
        saved.frames.forEach((url, index) => state.images.push({ name: `${saved.name || "video"} frame ${index + 1}`, url }));
      }
    }
  } catch (_) { /* skip unreadable file */ }
}

// walk a dropped FileSystemEntry (file or directory) and upload every file
function collectEntry(entry, prefix) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file(async (file) => { await uploadFile(file, (prefix || "") + entry.name); resolve(); }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const dir = (prefix || "") + entry.name + "/";
      const readBatch = () => reader.readEntries(async (entries) => {
        if (!entries.length) { resolve(); return; }
        for (const child of entries) await collectEntry(child, dir);
        readBatch(); // directories may return entries in batches
      }, () => resolve());
      readBatch();
    } else { resolve(); }
  });
}

// drag files / folders from the OS onto the composer
const composeCard = document.querySelector(".composeCard");
["dragenter", "dragover"].forEach((ev) => composeCard.addEventListener(ev, (e) => {
  e.preventDefault();
  composeCard.classList.add("dropping");
}));
composeCard.addEventListener("dragleave", (e) => {
  if (e.relatedTarget && composeCard.contains(e.relatedTarget)) return;
  composeCard.classList.remove("dropping");
});
composeCard.addEventListener("drop", async (e) => {
  e.preventDefault();
  composeCard.classList.remove("dropping");
  const dt = e.dataTransfer;
  const items = dt && dt.items ? Array.from(dt.items) : [];
  const entries = items.map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  if (entries.length) {
    for (const entry of entries) await collectEntry(entry, "");
  } else if (dt && dt.files) {
    for (const file of dt.files) await uploadFile(file);
  }
  renderAttachments();
});

// collapse sidebar
const toggleCollapse = () => document.querySelector(".app").classList.toggle("sidebarCollapsed");
["toggleSidebar", "toggleSidebarTop"].forEach((id) => { const b = $(id); if (b) b.onclick = toggleCollapse; });

// copy buttons in code blocks (event delegation)
$("messages").addEventListener("click", (e) => {
  const copyBtn = e.target.closest(".copyBtn");
  if (copyBtn) {
    const code = copyBtn.closest(".code").querySelector("code");
    copyToClipboard(code.textContent, copyBtn, "已复制", "复制");
    return;
  }
  const tableCopyBtn = e.target.closest(".tableCopyBtn");
  if (tableCopyBtn) {
    const table = tableCopyBtn.closest(".mdTableWrap").querySelector("table");
    copyToClipboard(tableToMarkdown(table), tableCopyBtn, null, null);
    return;
  }
  const act = e.target.closest(".msgAct");
  if (!act) return;
  const msg = act.closest(".message");
  const raw = msg.querySelector(".bubble").dataset.raw || "";
  const src = msg.dataset.src !== undefined ? Number(msg.dataset.src) : null;
  if (act.classList.contains("copy")) copyToClipboard(raw, act, null, null);
  else if (act.classList.contains("retry")) doRetry(src);
  else if (act.classList.contains("branch")) doBranch(src);
  else if (act.classList.contains("edit")) doEdit(raw, src, msg);
});

function tableToMarkdown(table) {
  const rowText = (tr) => "| " + [...tr.children].map((c) => c.textContent.trim()).join(" | ") + " |";
  const head = table.querySelector("thead tr");
  const lines = [];
  if (head) {
    lines.push(rowText(head));
    lines.push("| " + [...head.children].map(() => "---").join(" | ") + " |");
  }
  table.querySelectorAll("tbody tr").forEach((tr) => lines.push(rowText(tr)));
  return lines.join("\n");
}

function copyToClipboard(text, btn, okLabel, resetLabel) {
  navigator.clipboard.writeText(text).then(() => {
    if (!btn) return;
    if (okLabel && resetLabel) {
      btn.textContent = okLabel;
      setTimeout(() => (btn.textContent = resetLabel), 1200);
    } else {
      btn.classList.add("copied");
      setTimeout(() => btn.classList.remove("copied"), 900);
    }
  }).catch(() => {});
}

// remove the last user message and everything after it from the transcript view
function removeLastExchange() {
  const box = $("messages");
  const users = box.querySelectorAll(".message.user");
  if (!users.length) return;
  let node = users[users.length - 1];
  while (node) { const next = node.nextSibling; node.remove(); node = next; }
  state.currentAssistant = null;
  state.currentTool = null;
}

// Snapshot only this user's answer/tool tail. Stop before the next user turn so
// edit/retry version arrows never delete later messages.
function tailHTMLFrom(anchorUserEl) {
  let html = "";
  let n = anchorUserEl.nextElementSibling;
  while (n && !(n.classList && n.classList.contains("message") && n.classList.contains("user"))) {
    html += n.outerHTML;
    n = n.nextElementSibling;
  }
  return html;
}

// Replace only this user's answer/tool tail; preserve later turns.
function applyVersion(anchorUserEl, version) {
  const bubble = anchorUserEl.querySelector(".bubble");
  if (bubble && version.prompt != null) { bubble.dataset.raw = version.prompt; renderBubble(bubble); }
  let n = anchorUserEl.nextElementSibling;
  while (n && !(n.classList && n.classList.contains("message") && n.classList.contains("user"))) {
    const nx = n.nextElementSibling;
    n.remove();
    n = nx;
  }
  const safeTail = currentTurnHTMLOnly(version.tailHTML || "");
  if (safeTail) anchorUserEl.insertAdjacentHTML("afterend", safeTail);
}

function currentTurnHTMLOnly(html) {
  if (!html) return "";
  const template = document.createElement("template");
  template.innerHTML = html;
  let out = "";
  for (const node of Array.from(template.content.children)) {
    if (node.classList && node.classList.contains("message") && node.classList.contains("user")) break;
    out += node.outerHTML;
  }
  return out;
}

/* ---------- approval request card (Codex approvalRequestCard: 受限模式弹批准) ---------- */
function showApproval(payload) {
  dismissApproval();
  const intro = document.querySelector(".empty, .intro");
  if (intro) intro.remove();
  const card = document.createElement("div");
  card.className = "approvalCard";
  card.dataset.approvalId = payload.id || "";
  card.innerHTML =
    `<div class="apHead">${icon("alert", 14)}<span>请求批准</span><strong>${escapeHtml(payload.tool || "")}</strong></div>` +
    `<pre class="apArgs">${escapeHtml(payload.summary || "")}</pre>` +
    `<div class="apActions"><button class="ghost apDeny">拒绝</button><button class="primary apAllow">批准</button></div>`;
  card.querySelector(".apAllow").onclick = () => resolveApproval(card, true);
  card.querySelector(".apDeny").onclick = () => resolveApproval(card, false);
  appendTranscriptNode(card);
  state.stickToBottom = true;
  scrollMessages(true);
}

async function resolveApproval(card, approved) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try { await window.pywebview.api.resolve_approval({ id: card.dataset.approvalId, approved }); } catch (_) {}
  card.classList.add(approved ? "allowed" : "denied");
  const actions = card.querySelector(".apActions");
  actions.innerHTML = `<span class="apResult">${approved ? "已批准" : "已拒绝"}</span>`;
}

function dismissApproval() {
  document.querySelectorAll(".approvalCard").forEach((card) => {
    if (!card.classList.contains("allowed") && !card.classList.contains("denied")) card.remove();
  });
}

// Remove an element and following siblings until the next user turn. Version/edit
// operations must never delete adjacent conversations.
function removeTurnNodes(el) {
  let node = el;
  while (node) {
    const next = node.nextSibling;
    node.remove();
    if (next && next.classList && next.classList.contains("message") && next.classList.contains("user")) break;
    node = next;
  }
}

function nextUserAfterTurn(el) {
  let node = el ? el.nextElementSibling : null;
  while (node) {
    if (node.classList && node.classList.contains("message") && node.classList.contains("user")) return node;
    node = node.nextElementSibling;
  }
  return null;
}

function setVersionInsertMarker(nextUserEl) {
  clearVersionInsertMarker();
  const marker = document.createElement("span");
  marker.className = "versionInsertMarker";
  marker.hidden = true;
  const box = $("messages");
  if (nextUserEl && nextUserEl.parentNode === box) box.insertBefore(marker, nextUserEl);
  else box.append(marker);
  state.pendingVersionMarker = marker;
  state.pendingVersionUser = null;
}

function clearVersionInsertMarker() {
  if (state.pendingVersionMarker && state.pendingVersionMarker.isConnected) state.pendingVersionMarker.remove();
  state.pendingVersionMarker = null;
  state.pendingVersionUser = null;
}

// For retrying/editing an assistant/user message: strip only that turn. Do not remove
// later user turns; those are separate conversation branches in the visible transcript.
function removeTurnFrom(msg) {
  let anchor = msg;
  if (msg.classList.contains("assistant")) {
    let p = msg.previousElementSibling;
    while (p && !(p.classList && p.classList.contains("message") && p.classList.contains("user"))) p = p.previousElementSibling;
    if (p) anchor = p;
  }
  const nextUser = nextUserAfterTurn(anchor);
  removeTurnNodes(anchor);
  state.currentAssistant = null;
  state.currentTool = null;
  return nextUser;
}

async function doRetry(src) {
  if (state.running) return;
  const box = $("messages");
  const target = src != null ? box.querySelector(`.message.assistant[data-src="${src}"]`)
                             : [...box.querySelectorAll(".message.assistant")].pop();
  if (!target) return;
  // find the user message that starts this turn, and snapshot the FULL tail being
  // replaced (old answer + tool cards + any later turns) so the version pager can bring
  // all of it back — not just one answer bubble's text.
  let userEl = target.previousElementSibling;
  while (userEl && !(userEl.classList && userEl.classList.contains("message") && userEl.classList.contains("user"))) userEl = userEl.previousElementSibling;
  const priorVersions = (userEl && userEl.__versions) ? userEl.__versions.slice() : [];
  const replacedVersion = {
    prompt: userEl ? (userEl.querySelector(".bubble").dataset.raw || "") : "",
    tailHTML: userEl ? tailHTMLFrom(userEl) : "",
  };
  state.pendingVersions = {
    versions: priorVersions.length ? priorVersions : [replacedVersion],
    newPrompt: replacedVersion.prompt,  // retry keeps the same prompt
  };
  const nextUser = removeTurnFrom(target);
  setVersionInsertMarker(nextUser);
  state.stickToBottom = true;
  setRunning(true);
  try {
    await updateRuntime();
    const retry = await apiMethod("retry");
    const result = await retry(src != null ? { srcIndex: src } : {});
    if (!result.ok) throw new Error(result.error || "unknown error");
  } catch (error) {
    setRunning(false);
    state.pendingVersions = null;
    addEvent("error", "重试失败", String(error.message || error));
  }
}

/* Codex-style response versions: after a retry/edit, the ‹ i/n › arrows live on the USER
   message; each version is a full snapshot of the turn's prompt AND its entire tail
   (answer, tool cards, later turns), so flipping restores everything — never leaves a
   dangling half-conversation. */
function attachVersionPager() {
  const snap = state.pendingVersions;
  state.pendingVersions = null;
  if (!snap) return;
  const box = $("messages");
  // Prefer the user row created at the edit/retry insertion point. Falling back to
  // the last user is only for older/demo event paths.
  const userEl = (state.pendingVersionUser && state.pendingVersionUser.isConnected)
    ? state.pendingVersionUser
    : [...box.querySelectorAll(".message.user")].pop();
  if (!userEl) return;
  const newVersion = {
    prompt: snap.newPrompt != null ? snap.newPrompt : (userEl.querySelector(".bubble").dataset.raw || ""),
    tailHTML: tailHTMLFrom(userEl),
  };
  const versions = (snap.versions || []).concat([newVersion]);
  userEl.__versions = versions;
  let index = versions.length - 1;

  let pager = userEl.querySelector(".versionPager");
  if (!pager) {
    pager = document.createElement("span");
    pager.className = "versionPager";
    userEl.querySelector(".msgActions").prepend(pager);
  }
  const total = versions.length;
  const render = () => {
    pager.innerHTML =
      `<button class="vBtn prev" title="上一版本"${index === 0 ? " disabled" : ""}>${icon("chevron", 12)}</button>` +
      `<span class="vCount">${index + 1}/${total}</span>` +
      `<button class="vBtn next" title="下一版本"${index === total - 1 ? " disabled" : ""}>${icon("chevron", 12)}</button>`;
  };
  pager.onclick = (e) => {
    const btn = e.target.closest(".vBtn");
    if (!btn || btn.disabled) return;
    index += btn.classList.contains("prev") ? -1 : 1;
    applyVersion(userEl, versions[index]);
    userEl.__versions = versions;  // survive the tail swap
    render();
    markMessageActions();
  };
  render();
}

async function doBranch(src) {
  if (state.running) return;
  try {
    const result = await window.pywebview.api.branch(src != null ? { srcIndex: src } : {});
    if (!result.ok) throw new Error(result.error || "unknown error");
    state.currentAssistant = null;
    state.currentTool = null;
    state.stickToBottom = true;
    $("messages").innerHTML = "";
    (result.messages || []).forEach(replayMessage);
    markMessageActions();
    scrollMessages(true);
    setText("sessionState", String(result.sessionId || "").slice(0, 8));
    setSaveState("saved", "已开分支", result.sessionId || "");
    await refreshSessions();
    toast("已从该回复开出新分支");
  } catch (error) {
    addEvent("error", "开分支失败", String(error.message || error));
  }
}

// Codex-style inline edit: the user message becomes an editable box with 取消/保存.
// Nothing is deleted until you save; Cancel restores the original.
function doEdit(text, src, msg) {
  if (state.running || !msg || msg.querySelector(".editBox")) return;
  const bubble = msg.querySelector(".bubble");
  const actions = msg.querySelector(".msgActions");
  bubble.style.display = "none";
  if (actions) actions.style.display = "none";
  const box = document.createElement("div");
  box.className = "editBox";
  box.innerHTML =
    `<textarea class="editArea"></textarea>` +
    `<div class="editBtns"><button class="ghost editCancel">取消</button>` +
    `<button class="primary editSave">保存并重发</button></div>`;
  const area = box.querySelector(".editArea");
  area.value = text;
  msg.append(box);
  const grow = () => { area.style.height = "auto"; area.style.height = Math.min(area.scrollHeight, 240) + "px"; };
  area.addEventListener("input", grow);
  const restore = () => { box.remove(); bubble.style.display = ""; if (actions) actions.style.display = ""; };
  box.querySelector(".editCancel").onclick = restore;
  box.querySelector(".editSave").onclick = async () => {
    const next = area.value.trim();
    if (!next) return;
    restore();
    // snapshot the full prior turn (old prompt + its whole tail) so the ‹ i/n › pager on
    // the new user message can flip back to the original question AND everything under it
    const priorVersions = (msg.__versions) ? msg.__versions.slice() : [];
    const replacedVersion = { prompt: bubble.dataset.raw || "", tailHTML: tailHTMLFrom(msg) };
    state.pendingVersions = {
      versions: priorVersions.length ? priorVersions : [replacedVersion],
      newPrompt: next,
    };
    const nextUser = removeTurnFrom(msg);
    setVersionInsertMarker(nextUser);
    state.stickToBottom = true;
    setRunning(true);
    try {
      await updateRuntime();
      const editResend = await apiMethod("edit_resend");
      const result = await editResend(
        src != null ? { prompt: next, srcIndex: src } : { prompt: next });
      if (!result.ok) throw new Error(result.error || "unknown error");
    } catch (error) {
      state.pendingVersions = null;
      clearVersionInsertMarker();
      setRunning(false);
      addEvent("error", "编辑重发失败", String(error.message || error));
    }
  };
  area.focus();
  grow();
  area.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); restore(); }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); box.querySelector(".editSave").click(); }
  });
}

// mark the latest turn so its actions stay visible without hover (others hover-reveal)
function markMessageActions() {
  const box = $("messages");
  box.querySelectorAll(".canRetry, .canEdit").forEach((m) => m.classList.remove("canRetry", "canEdit"));
  // only real replies are actionable — skip pre-tool narration bubbles
  const assistants = box.querySelectorAll(".message.assistant:not(.intermediate):not(.streaming)");
  const usersList = box.querySelectorAll(".message.user");
  if (assistants.length) assistants[assistants.length - 1].classList.add("canRetry");
  if (usersList.length) usersList[usersList.length - 1].classList.add("canEdit");
}

// Demote the most recent assistant bubble to pre-tool narration: it's part of this
// turn, not a standalone reply, so it should carry no copy/retry/branch actions.
function markLastAssistantIntermediate() {
  const box = $("messages");
  const assistants = box.querySelectorAll(".message.assistant:not(.intermediate)");
  if (!assistants.length) return;
  const last = assistants[assistants.length - 1];
  // only demote narration from THIS turn: the bubble must sit after the latest user
  // message. A reply before it is the PREVIOUS turn's final answer — demoting that was
  // why earlier replies lost retry/branch when the next turn began with a tool call.
  const users = box.querySelectorAll(".message.user");
  const lastUser = users.length ? users[users.length - 1] : null;
  if (lastUser && !(lastUser.compareDocumentPosition(last) & Node.DOCUMENT_POSITION_FOLLOWING)) return;
  last.classList.add("intermediate");
  last.classList.remove("streaming");
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  const box = $("attachments");
  box.innerHTML = "";
  state.images.forEach((img, i) => {
    const chip = document.createElement("span");
    chip.className = "chip imgChip";
    chip.innerHTML = `<img src="${img.url}" alt=""><button class="chipX" title="移除">${icon("trash", 11)}</button>`;
    chip.querySelector(".chipX").onclick = () => { state.images.splice(i, 1); renderAttachments(); };
    box.append(chip);
  });
  state.attachments.forEach((file, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span>${escapeHtml(file.name)}</span><button class="chipX" title="移除">${icon("trash", 11)}</button>`;
    chip.querySelector(".chipX").onclick = () => { state.attachments.splice(i, 1); renderAttachments(); };
    box.append(chip);
  });
}

/* ---------- in-app modal (window.prompt/confirm 在 pywebview 多数后端不可用) ---------- */
function uiModal({ title, withInput, defaultValue }) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "miniModal";
    dialog.innerHTML = `
      <form method="dialog">
        <p>${escapeHtml(title)}</p>
        ${withInput ? '<input type="text" autofocus>' : ""}
        <menu>
          <button value="cancel" class="ghost">取消</button>
          <button value="ok" class="primary">确定</button>
        </menu>
      </form>`;
    document.body.append(dialog);
    const input = dialog.querySelector("input");
    if (input) input.value = defaultValue || "";
    dialog.addEventListener("close", () => {
      const ok = dialog.returnValue === "ok";
      resolve(withInput ? (ok ? (input.value || "").trim() : null) : ok);
      dialog.remove();
    });
    dialog.showModal();
    if (input) { input.focus(); input.select(); }
  });
}
const uiPrompt = (title, defaultValue) => uiModal({ title, withInput: true, defaultValue });
const uiConfirm = (title) => uiModal({ title, withInput: false });

function uiSelect(title, values, selected) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "miniModal modelPickModal";
    dialog.innerHTML = `
      <form method="dialog">
        <p>${escapeHtml(title)}</p>
        <select></select>
        <menu>
          <button value="cancel" class="ghost">取消</button>
          <button value="ok" class="primary">测试</button>
        </menu>
      </form>`;
    const select = dialog.querySelector("select");
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = value === selected;
      select.append(option);
    });
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok" ? select.value : null);
      dialog.remove();
    });
    dialog.showModal();
    select.focus();
  });
}

/* ---------- startup: 等待 pywebview 注入 api，浏览器预览时回退到演示数据 ---------- */
// pywebview creates window.pywebview.api as an object BEFORE attaching each method
// proxy, so `api && api.boot` can be an object with no boot yet. Only start once
// boot is actually callable.
function apiReady() {
  return !!(window.pywebview && window.pywebview.api && typeof window.pywebview.api.boot === "function");
}

function start() {
  if (window.__fathomBooting || window.__fathomBooted) return;
  window.__fathomBooting = true;
  boot().then(() => {
    window.__fathomBooted = true;  // only mark done on SUCCESS
  }).catch((error) => {
    // a transient failure (e.g. an api method not attached yet) must not lock startup
    // forever — release the flags so the poller retries instead of "crashes then works"
    window.__fathomBooting = false;
    setSaveState("error", "启动中…", String(error.message || error));
  });
}

// Poll until boot is a real function (covers the window where api exists but its
// method proxies haven't been attached), then start. pywebviewready also triggers it.
let __bootPolls = 0;
function tryStart() {
  if (window.__fathomBooted) return;
  if (apiReady()) { start(); return; }
  if (++__bootPolls > 100) {  // ~10s: no real backend — fall back to the demo API
    if (!window.pywebview) installDemoApi();
    if (apiReady()) start();
    return;
  }
  setTimeout(tryStart, 100);
}
window.addEventListener("pywebviewready", tryStart);
tryStart();
