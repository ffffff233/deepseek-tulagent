from pathlib import Path


ASSET_ROOT = Path(__file__).parents[1] / "src" / "deepseekfathom" / "_core" / "desktop" / "assets"


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_settings_exposes_mcp_plugins_and_hooks_management() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    for tab in ("mcp", "plugins", "hooks"):
        assert f'data-extension-tab="{tab}"' in html
        assert f'data-extension-pane="{tab}"' in html
    assert "当前版本尚未集成 MCP、插件包和 Hooks" not in js
    assert 'apiMethod("extension_status"' not in js  # optional bridge lookup must not block settings for 8 seconds
    assert '"extension_status"' in js
    assert '"refresh_extensions"' in js
    assert '"extension_action"' in js
    assert 'data-extension-action="connect_all"' in html


def test_pending_versions_are_scoped_to_session_and_turn() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    retry = _section(js, "async function doRetry", "/* Codex-style response versions")
    edit = _section(js, "function doEdit", "// mark the latest turn")
    marker = _section(js, "function setVersionInsertMarker", "function clearVersionInsertMarker")
    turn_start = _section(js, 'if (event === "turn:start")', 'if (event === "assistant:delta")')

    for operation in (retry, edit):
        assert 'sessionId: String(currentSessionId() || "")' in operation
        assert 'turnId: ""' in operation
        assert "bindPendingVersionScope(result.sessionId" in operation
    assert "sessionId:" in marker and "turnId:" in marker
    assert "state.pendingVersionUser = {" in turn_start
    assert "sessionId:" in turn_start and "turnId:" in turn_start
    assert "versionScopeMatches(markerState, currentSessionId(), state.activeTurnId)" in js
    assert "resetPendingVersionState();" in retry


def test_outbound_request_id_is_deduplicated_and_cleared() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "clientRequestId: outboundId" in js
    for event, next_event in (
        ("turn:start", "assistant:delta"),
        ("turn:done", "turn:error"),
        ("turn:error", "turn:cancel"),
        ("turn:cancelled", "restoreTranscriptFromEvent"),
    ):
        branch = _section(js, f'if (event === "{event}")', f'if (event === "{next_event}")' if next_event != "restoreTranscriptFromEvent" else "function restoreTranscriptFromEvent")
        assert 'state.pendingOutboundId = "";' in branch
    resume = _section(js, 'row.querySelector(".sessionMain").onclick', "row.querySelector(\".actPin\")")
    new_session = _section(js, '$("newSession").onclick', '$("refreshSessions").onclick')
    assert 'state.pendingOutboundId = "";' in resume
    assert 'state.pendingOutboundId = "";' in new_session


def test_branch_adopts_returned_session_and_blocks_double_submit() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    branch = _section(js, "async function doBranch", "// Codex-style inline edit")

    assert "state.running || state.resuming || state.branching" in branch
    assert "state.branching = true" in branch
    assert "state.currentSessionId = branchSessionId" in branch
    assert "state.boot.sessionId = branchSessionId" in branch
    assert "state.branching = false" in branch
    assert branch.index("state.currentSessionId = branchSessionId") < branch.index("await refreshSessions()")


def test_retry_and_edit_restore_removed_turn_after_sync_failure() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    retry = _section(js, "async function doRetry", "/* Codex-style response versions")
    edit = _section(js, "function doEdit", "// mark the latest turn")
    restore = _section(js, "function restoreRemovedTurn", "async function doRetry")

    assert "removedTurn.nodes.forEach" in restore
    assert "box.insertBefore(node, before)" in restore
    for operation in (retry, edit):
        assert "const removedTurn = removeTurnFrom" in operation
        assert "restoreRemovedTurn(removedTurn)" in operation
        assert operation.index("restoreRemovedTurn(removedTurn)") < operation.index("resetPendingVersionState()")


def test_extension_mutations_are_disabled_during_active_runtime() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    lock = _section(js, "function extensionActionsLocked", "async function loadExtensions")
    action = _section(js, "async function runExtensionAction", "function renderCapabilityDiagnostics")
    controls = _section(js, "function syncRunControls", "function goalStorageKey")

    assert "state.running || state.resuming || state.branching" in lock
    assert "control.disabled = true" in lock
    assert "if (extensionActionsLocked())" in action
    assert "syncExtensionControls()" in controls


def test_project_mcp_trust_and_plugin_hook_controls_are_explicit() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    render = _section(js, "function renderExtensionReport", "async function optionalExtensionMethod")

    assert "mcpReport.projectDefined && !mcpReport.projectTrusted" in render
    assert 'data-extension-kind="mcp" data-extension-action="trust_project"' in render
    assert "项目 MCP 尚未信任" in render
    assert 'const actionName = String(hook.id || "")' in render
    assert 'const pluginManaged = hook.scope === "plugin"' in render
    assert 'pluginManaged ? "随插件启停"' in render
    assert 'pluginManaged ? ""' in render


def test_untrusted_project_hooks_require_explicit_project_trust() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    render = _section(js, "function renderExtensionReport", "async function optionalExtensionMethod")

    assert "hooksReport.projectDefined && !hooksReport.projectTrusted" in render
    assert 'data-extension-kind="hooks" data-extension-action="trust_project"' in render
    assert "信任后会运行当前项目中所有已启用的 Hooks" in render
    assert 'hook.scope === "project" && hooksReport.projectTrusted === false' in render
    assert '${projectUntrusted ? " disabled" : ""}' in render


def test_mcp_user_services_have_structured_editor_and_crud_actions() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="addMcpServer"' in html
    assert 'id="mcpEditor"' in html
    assert 'data-mcp-transport="http" aria-pressed="true"' in html
    assert 'data-mcp-transport="stdio" aria-pressed="false"' in html
    assert "启动参数（每行一个）" in html
    assert '<div id="mcpHeaderRows" class="mcpHeaderRows"></div>' in html
    assert 'id="addMcpHeader"' in html
    assert 'class="mcpHeaderValue" type="password"' in js
    assert "data-mcp-header-reveal" in js
    assert "data-mcp-header-delete" in js
    assert 'requestMcpConfig("get"' in js
    assert 'requestMcpConfig("save"' in js
    assert 'requestMcpConfig("delete"' in js
    assert "originalName: state.mcpEditorOriginalName" in js
    assert "result.warning ? `MCP 服务已保存" in js
    assert "result.warning ? `MCP 服务已删除" in js
    assert 'type: "http", url: rawUrl, headers, enabled' in js
    assert 'type: "stdio", command, args, enabled' in js


def test_mcp_edit_controls_are_user_only_and_validate_headers() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "style.css").read_text(encoding="utf-8")
    render = _section(js, "function renderExtensionReport", "function setMcpEditorError")
    collect = _section(js, "function collectMcpEditorConfig", "async function saveMcpEditor")

    assert '["global", "user"].includes' in render
    assert "&& !server.plugin" in render
    assert "userOwned ?" in render
    assert "data-mcp-edit" in render and "data-mcp-delete" in render
    assert "请求头名称不能为空" in collect
    assert "请求头名称不能重复" in collect
    assert "/^(https?):$/" in collect
    assert ".split(/\\r?\\n/)" in collect
    assert "@media (max-width: 600px)" in css
    assert ".mcpEditorModal { width: calc(100vw - 20px)" in css


def test_mcp_edit_preserves_hidden_advanced_fields_without_cross_transport_leaks() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    populate = _section(js, "function populateMcpEditor", "async function requestMcpConfig")
    collect = _section(js, "function collectMcpEditorConfig", "async function saveMcpEditor")
    reset = _section(js, "function resetMcpEditor", "function populateMcpEditor")

    assert "state.mcpEditorOriginalConfig = JSON.parse(JSON.stringify(value))" in populate
    for field in ("startup_timeout_ms", "call_timeout_ms", "tool_timeout_ms", "trusted_read_only_tools"):
        assert f'"{field}"' in collect
    assert 'originalTransport === "stdio"' in collect
    assert '["env", "cwd", "headers"]' in collect
    assert '{ ...preserved, type: "stdio", command, args, enabled }' in collect
    assert '{ ...preserved, type: "http", url: rawUrl, headers, enabled }' in collect
    assert "state.mcpEditorOriginalConfig = null" in reset


def test_slash_commands_use_a_dedicated_inline_composer_and_replay_ui_kind() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "style.css").read_text(encoding="utf-8")
    parser = _section(js, "function interpretPrompt", "function commandRequestId")
    keyboard = _section(js, '$("prompt").addEventListener("keydown"', '$("cancel").onclick')

    assert 'id="commandChip" class="commandChip" hidden' in html
    assert 'id="promptShell" class="promptShell"' in html
    prompt_shell = _section(html, '<div id="promptShell" class="promptShell">', '<div class="composeRow">')
    assert 'id="commandChip"' in prompt_shell
    assert 'id="prompt"' in prompt_shell
    assert 'id="commandChipText"' in html
    assert 'id="clearCommandChip"' in html
    assert "state.activeCommand ? commandTextFromComposer()" in js
    assert "activateSlashCommand(it);" in js
    assert 'if (name === "mcp")' in parser
    assert 'if (name === "review")' in parser
    assert "const sendExact =" in keyboard
    assert 'if (sendExact) $("send").click();' in keyboard
    assert 'uiKind: cmd.uiKind' in js
    assert 'entry.uiKind === "command"' in js
    assert 'uiKind: entry.uiKind' in js
    assert 'row.className = `message ${role}${isCommand ? " command" : ""}`' in js
    assert ".commandChip {" in css
    command_prefix = _section(css, ".commandChip {", "}")
    assert "background: transparent" in command_prefix
    assert "border: none" in command_prefix
    assert '.promptShell.commandMode' in css
    assert ".message.command .bubble.user {" in css


def test_interface_language_switch_is_persisted_and_covers_dynamic_ui() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-language-value="zh-CN"' in html
    assert 'data-language-value="en"' in html
    assert 'localStorage.getItem("deepseekfathom.language")' in html
    assert 'localStorage.setItem("deepseekfathom.language", state.locale)' in js
    assert 'function setInterfaceLanguage(locale, persist = true)' in js
    assert 'function translateUiText(value)' in js
    assert 'new MutationObserver' in js
    assert 'observer.observe(document.body' in js
    assert '"设置": "Settings"' in js
    assert '"完全访问": "Full access"' in js
    assert '"审查当前工作区改动": "Review current workspace changes"' in js
    assert '".bubble, .sessionMain, pre, code' in js


def test_mcp_slash_command_is_connection_only_and_reports_outcome() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    commands = _section(js, "const SLASH_COMMANDS", "function slashCandidates")
    connect = _section(js, "async function connectAllMcpFromCommand", "async function runLocalSlashCommand")

    assert '{ key: "/mcp", command: "/mcp"' in commands
    assert 'const extensionAction = await apiMethod("extension_action"' in connect
    assert 'extensionAction({ kind: "mcp", action: "connect_all" })' in connect
    assert "settingsBtn" not in connect
    assert "trust_project" not in connect
    assert "const connected =" in connect
    assert "const failed =" in connect
    assert "record_slash_command" in js
    assert 'modelVisible: false' in js


def test_review_command_has_toolbar_action_and_review_bridge() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    commands = _section(js, "const SLASH_COMMANDS", "function slashCandidates")
    review = _section(js, "async function runReviewCommand", "function toast")

    assert 'id="reviewChanges"' in html
    assert '{ key: "/review", command: "/review"' in commands
    assert 'apiMethod("review_changes")' in review
    assert 'uiKind: "command"' in review
    assert 'clientRequestId: outboundId' in review
    assert '$("prompt").value = "/review";' in js
    assert 'review.disabled = state.running || state.resuming || state.branching' in js


def test_enabled_native_plugin_commands_are_real_slash_commands() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    candidates = _section(js, "function nativeSlashCommands", "function slashCandidates")
    parser = _section(js, "function interpretPrompt", "function commandRequestId")

    assert "state.boot.nativeCommands" in candidates
    assert '!builtins.has(String(item.key).toLowerCase())' in candidates
    assert "SLASH_COMMANDS.concat(nativeSlashCommands(), skills)" in js
    assert "state.boot.nativeCommands" in parser
    assert 'native.handler === "review"' in parser
    assert "nativeCommand: String(native.name || name)" in parser
    assert "send: rest" in parser
    assert "String(native.prompt" not in parser
    assert "nativeCommand: cmd.nativeCommand || undefined" in js
    assert "state.boot.nativeCommands = report.nativeCommands" in js


def test_plugin_skills_refresh_the_desktop_slash_catalog() -> None:
    app = (ASSET_ROOT.parent / "app.py").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "SkillStore(self.settings.workspace, extra_roots=roots).list()" in app
    assert 'report["availableSkills"] = self._skill_catalog()' in app
    assert "state.skills = report.availableSkills" in js
    assert "state.boot.skills = report.availableSkills" in js


def test_command_ui_has_mobile_overflow_guards() -> None:
    css = (ASSET_ROOT / "style.css").read_text(encoding="utf-8")
    command_bubble = _section(css, ".message.command .bubble.user {", "}")
    mobile = _section(css, "@media (max-width: 600px) {", "/* ---------- thinking shimmer")

    assert "max-width: 100%" in command_bubble
    assert "min-width: 0" in command_bubble
    assert "overflow-wrap: anywhere" in command_bubble
    assert ".message.command .bubble.user { width: 100%; max-width: 100%; }" in mobile
    assert ".composer { min-width: 0" in mobile
    assert ".toolbarActions .workspace { display: none; }" in mobile
