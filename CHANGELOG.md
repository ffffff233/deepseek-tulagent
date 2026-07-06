# 更新记录 / Changelog

## v0.1.70

中文（继续对齐 Codex）：

- **重试的左右箭头挪到「你发的消息」上**：以前 ‹ 1/2 › 放在 AI 回复那排（错的），现在放在**你发送的
  消息**下方——像 Codex 一样，第 1 版 / 第 2 版切换的是你这条消息的不同回答，翻页时对应的 AI 回复也跟着换。
- **中断立即生效**：点中断后 UI 立刻停转、恢复输入、标为「已中断」，并忽略这个已取消回合后续迟到的
  流式/工具事件（后端在后台收尾），不用再干等。
- **侧栏对话列表自动刷新**：新建对话后、窗口重新聚焦时、以及每 8 秒空闲时自动刷新，往期对话不用再手动点刷新。

English (further Codex alignment):

- **Retry version arrows moved onto YOUR message**: the ‹ 1/2 › pager used to sit on the AI reply (wrong);
  it now lives under the **message you sent** — like Codex, it flips between the different answers to that
  message, swapping the reply in place.
- **Instant interrupt**: clicking stop frees the UI immediately (spinner off, input restored, marked
  "已中断") and ignores this cancelled turn's late stream/tool events while the backend winds down — no
  more waiting.
- **Sidebar conversation list auto-refreshes**: after creating a conversation, on window focus, and every
  8s while idle — past conversations no longer need a manual refresh.

## v0.1.69

中文（继续对齐 Codex）：

- **编辑消息改成 Codex 那样的内联编辑**：点用户消息的编辑，消息**原地变成可编辑框 + 取消 / 保存并重发**，
  不再一点就把后面全删、也不再塞回输入框。**取消**可放弃恢复原样；**保存**才截断并重跑（Cmd/Ctrl+Enter
  保存、Esc 取消）。不再莫名其妙多开会话。
- **设置里新增「测试连接」按钮**：用当前填写的 Base URL / Key / 接口格式实测拉一次模型列表（不落盘），
  成功显示模型数量和实际请求地址，失败显示具体错误。已对第三方接口实测可用。

English (further Codex alignment):

- **Message editing is now Codex-style inline editing**: clicking edit turns the message into an
  in-place editor with 取消 / 保存并重发 — it no longer deletes everything after on click, nor dumps the
  text back into the composer. Cancel restores the original; only Save truncates and re-runs
  (Cmd/Ctrl+Enter to save, Esc to cancel). No more spurious extra conversations.
- **Added a "测试连接" button in Settings**: probes the currently-entered Base URL / key / format by
  fetching the model list (without saving), showing model count + resolved URL on success or the exact
  error on failure. Verified against a third-party endpoint.

## v0.1.68

中文：

- **修复：第三方接口老「空返回」**。根因是 Base URL 只填了域名（如 `https://api.hanhegufei.online`）
  没带 `/v1`，网关返回的是**网页 HTML**（200），流式解析器找不到 `data:` 行就啥都不返回，空结果被
  当成正常完成。两处修复：① **OpenAI 系（含 DeepSeek）自动补 `/v1`**——只填域名时自动加，不用你手动补；
  ② 万一仍返回 HTML/非 API 响应，**直接报错提示**（"上游返回的是网页而不是 API 响应…"），不再空返回当成功。
  已用你给的接口（chat 协议）实测：模型列表、流式、非流式全部正常。
- **兼容返回 `reasoning_content` 的网关**：非流式回复缺 `content` 键时回退读 `reasoning_content`，不再报错。
- **顶栏 ⋮ 对话菜单（学 Codex）**：右上角三点菜单——**复制会话 ID**、重命名对话、从最新回复开分支、
  新建对话、删除对话。

English:

- **Fixed: third-party endpoints kept returning empty.** Root cause: a bare-host Base URL (no `/v1`)
  hit the gateway's HTML homepage (200); the SSE parser found no `data:` lines and the empty result was
  treated as a successful empty answer. Two fixes: (1) **auto-append `/v1` for OpenAI-family (incl.
  DeepSeek)** when only a host is given — no manual `/v1` needed; (2) if an HTML/non-API response still
  comes back, **raise a clear error** instead of silently returning empty. Verified end-to-end against
  the provided endpoint (chat protocol): models, streaming, and non-streaming all work.
- **Tolerate gateways that return `reasoning_content`**: non-stream replies missing `content` fall back
  to `reasoning_content` instead of erroring.
- **Top-bar ⋮ conversation menu (Codex-style)**: copy conversation ID, rename, branch from latest reply,
  new conversation, delete.

## v0.1.67

中文：

- **修复：模型用一次后列表全没、老默认回 deepseek-v4-flash**。根因有二：① 切换接口 / 保存设置时
  `configure()` 会 `get_settings()` 重新读文件，把当前模型重置成文件默认（v4-flash）——现在
  configure 会保留你当前选的模型。② 前端每次拉不到模型列表就把下拉塌成一个当前项——现在**缓存
  上一次成功的完整列表**，拉取失败时沿用旧列表、不再清空，当前模型也始终保留在选项里。
  （把上一版把下拉改成输入框的处理已还原成下拉。）
- **复制 / 重试 / 开分支 现在每条消息都能用**（之前只有最新那条有）：每条助手消息都能重试、开分支，
  每条用户消息都能编辑分支；后端按消息在会话中的真实位置（srcIndex）截断，可从任意一条开分支或重来。

English:

- **Fixed: model list vanished after one use and kept defaulting to deepseek-v4-flash.** Two causes:
  (1) `configure()` reloaded settings from file on provider switch / save, resetting the model to the
  file default — it now preserves the currently selected model; (2) the dropdown collapsed to a single
  item whenever a model fetch failed — it now **caches the last good full list**, reuses it on failure,
  and always keeps the current model in the options. (The stopgap combobox is reverted to a dropdown.)
- **Copy / retry / branch now work on every message** (previously only the latest): every assistant
  message can be retried or branched, every user message edited-and-branched; the backend truncates by
  each message's real transcript position (srcIndex), so you can fork or redo from any point.

## v0.1.66

中文：

- **修复（最严重）：切回旧对话后工具调用全变成一串参数**。之前 resume 把「工具调用的助手消息」
  原样当文本渲染，你看到的就是那段 JSON 参数。现在 `serialize_messages` 会把工具调用还原成
  **工具卡片**（名称 + 参数 + 配对的输出），并把周围正文拆出来单独显示。
- **修复：串上下文**。一轮回复结束时，只有当你还停在发起该轮的对话时才把会话指针切过去；
  如果你中途开了新对话或切到别的对话，旧轮不再把上下文灌进当前对话。
- **修复：切回对话要点两下才出现「复制/重试/开分支」**。最新一轮的操作按钮改为常显（不再依赖
  悬停），切回对话立即可见。

English:

- **Fixed (most severe): tool calls turned into a blob of arguments after switching back to a
  conversation.** Resume rendered the tool-call assistant message as plain text. `serialize_messages`
  now rebuilds tool calls as **tool cards** (name + args + paired output), splitting any surrounding
  prose into its own bubble.
- **Fixed: context bleeding across chats.** When a turn finishes, the session pointer only adopts
  the result if you're still on the conversation that started it — opening/switching to another
  chat mid-turn no longer leaks the old turn into the current chat.
- **Fixed: copy/retry/branch needed two clicks after switching back.** The latest turn's actions are
  now always visible (no hover), so they appear immediately on resume.

## v0.1.65

中文：

- **去掉设置里的「默认模型」输入框**——它总把上一个接口的旧模型名自动填进去、保存后换了接口就报
  「模型不存在」。现在模型只从底部下拉选择，设置弹窗不再填模型。
- **过期模型自动纠正**：切换接口格式后，如果当前模型不在新接口的模型列表里，自动切到该接口的
  第一个可用模型并提示「模型已切换为 …」，不再拿旧模型名去请求然后报错。

English:

- **Removed the "default model" input from settings** — it kept auto-filling the previous
  provider's model name, which then errored ("model not found") after switching providers.
  The model is now chosen only from the composer dropdown.
- **Stale-model auto-correction**: after switching provider format, if the current model isn't in
  the new provider's model list, the first available model is selected automatically (with a
  "模型已切换为 …" toast) instead of sending a name the API will reject.

## v0.1.64

中文（对照 Codex 的 composer.permissionsDropdown / approvalRequestCard）：

- **权限模式改成 Codex 那样的三档**：**只读 / 受限 / 完全访问**（内部映射 plan/agent/root，
  旧的 review/trusted/yolo 自动归入受限或完全访问）。下拉直接显示中文档名。
- **「受限」现在会真的弹批准**：受限模式下，写文件 / 执行命令 / 联网等危险操作会在对话里弹出
  **批准请求卡片**（显示工具名和参数），点「批准」才执行、「拒绝」则不执行——之前是直接拦掉不问，
  现在跟 Codex 一样人为确认。后端用阻塞式 approve 回调 + `resolve_approval` 桥接，取消会自动放行结束。

English (against Codex's composer.permissionsDropdown / approvalRequestCard):

- **Permission modes are now Codex's three tiers**: **Read-only / Restricted / Full access**
  (mapped to plan/agent/root internally; legacy review/trusted/yolo fold into restricted or full).
  The dropdown shows the tier names directly.
- **"Restricted" now actually prompts**: in restricted mode, dangerous actions (write file / run
  command / network) raise an in-conversation **approval request card** (tool name + arguments);
  the tool runs only after you click Approve, and is skipped on Deny — previously these were just
  blocked without asking. Backend uses a blocking approve callback bridged by `resolve_approval`;
  cancelling releases any pending approval.

## v0.1.63

中文（对照 Codex 桌面端源码实现）：

- **修复：工具调用卡片总在最底下**。工具事件后 `currentAssistant` 未复位，后续文字全续写进
  工具卡上方的旧气泡，导致工具卡永远垫底。现在工具卡出现后文字开新气泡，时间顺序与 Codex 一致：
  正文 → 工具卡 → 后续正文。
- **回复版本箭头（Codex 的 ‹ i/n ›）**：重试不再丢弃旧回答——新回答生成后消息下方出现
  ‹ 1/2 › 分页箭头，可来回翻看每一版（对应 Codex 的 previousResponse/currentVersion）。
- **从回复开分支（Codex 的 onForkTurn）**：助手消息上新增分支按钮，把到该回复为止的历史
  fork 成一个新会话（标题自动加「· 分支」），原会话不动。
- **思考等级学 Codex**：桌面端从 11 档精简为 Codex 的 5 档 reasoning effort——
  Minimal / Low / Medium / High / Extra High（内部映射 instant/fast/balanced/deep/ultra，CLI 不变）。
- **Markdown 表格解析**：模型输出的 `| a | b |` 表格现在渲染成真正的表格（表头、斑马线、横向滚动）。

English (implemented against the Codex desktop source):

- **Fixed: tool-call cards always stuck at the bottom.** `currentAssistant` wasn't reset after a
  tool event, so post-tool text kept appending to the bubble above the card. Text now opens a new
  bubble after each tool card — chronological like Codex: text → tool card → more text.
- **Response version arrows (Codex's ‹ i/n ›)**: retry keeps the old answer — the regenerated
  message gets ‹ 1/2 › arrows to flip between versions (Codex's previousResponse/currentVersion).
- **Branch from a reply (Codex's onForkTurn)**: new branch action on assistant messages forks the
  history up to that reply into a new session (title suffixed "· 分支"); the original stays intact.
- **Thinking levels follow Codex**: the desktop trims 11 modes down to Codex's 5 reasoning-effort
  tiers — Minimal / Low / Medium / High / Extra High (mapped to instant/fast/balanced/deep/ultra
  internally; CLI unchanged).
- **Markdown table parsing**: `| a | b |` tables from the model now render as real tables
  (header, row dividers, horizontal scroll).

## v0.1.62

- 修复流式上游错误解析（`ResponseNotRead` 吞错误；现按各家格式提取 `error.message`）。
  Fixed streamed upstream-error parsing (ResponseNotRead masked errors; provider error JSON now parsed).
- 工具调用 JSON 不再泄露进聊天正文（安全边界扣留 + `assistant:final` 替换；空调用显示占位）。
  Tool-call JSON no longer leaks into streamed chat text (safe-boundary holdback + final replace).

## v0.1.61

中文：

- **消息悬停操作（学 Codex）**：每条消息 hover 出现操作按钮——**复制**（所有消息）、
  **重试**（最新的助手回复，重新生成）、**编辑并重发 / 分支**（最新的用户消息，改完重发）。
- **重试** 会把最近一轮的旧回答换成新回答,不再重复堆消息:后端新增 `Session.rewrite()` 把会话
  日志截断到最后一条用户消息,再重新跑一轮(`DesktopApi.retry`)。
- **编辑分支**：点用户消息的编辑,原文进输入框、该轮从视图移除,发送时走 `edit_resend`
  截断并用新内容重跑,相当于从这条消息开分支。
- **拖拽文件 / 文件夹到输入框**：从桌面把文件或整个目录拖到输入卡片即可添加为附件;目录会递归
  遍历上传每个文件;拖动时输入框高亮并提示「松开以添加文件 / 文件夹」。

English:

- **Per-message hover actions (Codex-style)**: each message reveals actions on hover — **copy**
  (all messages), **retry** (regenerate the latest assistant reply), and **edit & resend / branch**
  (edit the latest user message and re-run).
- **Retry** replaces the last answer instead of stacking duplicates: new backend
  `Session.rewrite()` truncates the log to the last user message, then re-runs the turn
  (`DesktopApi.retry`).
- **Edit-branch**: clicking edit on a user message drops its text into the composer, removes that
  turn from view, and on send routes through `edit_resend`, which truncates and re-runs with the
  new text — effectively branching from that message.
- **Drag & drop files / folders onto the composer**: drop files or whole directories from the OS
  onto the input card to attach them; directories are walked recursively and each file uploaded;
  the card highlights with a "松开以添加文件 / 文件夹" prompt while dragging.

## v0.1.60

中文：

- **去掉所有 emoji，换成 Codex 那样的线性 SVG 图标**。会话行的置顶/改名/删除（★☆✎🗑）、
  事件图标（工具 ⌘、思考 ◇、完成 ✓、子代理 ↳、压缩 ⇄、错误 !、技能 ✦）、折叠 «、侧栏 ☰、
  新对话 +、关闭 ×、以及展开箭头 ›，全部改为统一的 Lucide 风格线性图标（`stroke=currentColor`，
  随主题着色）。置顶后 pin 图标高亮填充。
- **修复：切换接口格式后模型列表没刷新**。选到别的 provider（OpenAI/Gemini/Claude）时会重新拉取
  该 provider 的模型列表，而不是继续显示上一个 provider 的模型。

English:

- **Removed all emoji, replaced with Codex-style line SVG icons.** Session row pin/rename/delete
  (★☆✎🗑), event icons (tool ⌘, thinking ◇, done ✓, subagent ↳, compact ⇄, error !, skill ✦),
  the collapse «, sidebar ☰, new-chat +, close ×, and the disclosure chevron › are now a single
  set of Lucide-style line icons (`stroke=currentColor`, themed). The pin icon fills/highlights
  when a session is pinned.
- **Fixed: model list not refreshed after switching provider format.** Selecting another provider
  (OpenAI/Gemini/Claude) now re-fetches that provider's models instead of keeping the previous
  provider's list.

## v0.1.59

中文：

- **修复：`/` 菜单显示不全、无法滚动**。菜单从输入框向上弹出，之前固定 `max-height:300px`，
  在较矮的窗口里顶部会溢出到视口之外、够不到、也滚不动。改为
  `max-height: min(340px, calc(100vh - 130px))`，始终留在窗口内并内部滚动到全部命令/技能。
- **会话的置顶 / 改标题 / 删除更易发现**：这三个操作一直都在（每条会话右侧 ★ 置顶、✎ 改名、
  🗑 删除），但之前要 hover 才显示。现在默认半透明常显、hover 变亮。
- 说明：**对话标题是自动生成的**——首条消息发送后由 `session_title_from_text` 从内容提取，
  也可随时用 ✎ 手动改名（改名对话框预填当前标题）。

English:

- **Fixed: `/` menu was cut off and could not scroll.** The menu pops up above the composer;
  its fixed `max-height:300px` overflowed off the top of the viewport on shorter windows, where
  the top items were unreachable and unscrollable. Now `max-height: min(340px, calc(100vh - 130px))`
  keeps it inside the window and scrolls internally through every command/skill.
- **Session pin / rename / delete are easier to find**: these actions always existed (★ pin, ✎
  rename, 🗑 delete on the right of each session row) but only appeared on hover. They are now
  faintly visible by default and brighten on hover.
- Note: **conversation titles are auto-generated** — `session_title_from_text` derives one from the
  first message after it's sent, and ✎ lets you rename anytime (the dialog pre-fills the current
  title).

## v0.1.58

中文：

- **修复：对话区不能上下滚动 / 内容超出视口**。`.messages` 用了 `flex:1` 却没设 `min-height:0`，
  flex 子项撑到内容高度而非内部滚动。现给 `.chatPane` 与 `.messages` 加 `min-height:0`、
  `.chatPane` 固定 `100vh` 且 `overflow:hidden`、`.composer` 加 `flex-shrink:0`——对话历史正常滚动，
  输入区始终可见。
- **修复：回复不是流式**。`agent.run()` 的流式循环原先只把 token 收集起来，最后一次性回传，
  所以前端一次拿到整段。现在按 `should_hold_stream_output` 判定不再像工具调用后即增量回调
  `on_delta`；新增 `on_final` 回调在结束时用清洗后的最终文本替换。桌面端发 `assistant:delta`
  逐字追加、`assistant:final` 收尾替换，真正逐字流式；CLI/TUI 行为不变（未传 `on_final`）。

English:

- **Fixed: chat area could not scroll / content overflowed the viewport.** `.messages` used
  `flex:1` without `min-height:0`, so the flex child grew to content height instead of scrolling.
  Added `min-height:0` to `.chatPane` and `.messages`, a fixed `100vh` + `overflow:hidden` on
  `.chatPane`, and `flex-shrink:0` on `.composer` — history scrolls normally and the composer
  stays visible.
- **Fixed: replies were not streamed.** `agent.run()`'s streaming loop only accumulated tokens and
  flushed once at the end, so the UI received the whole answer at once. It now calls `on_delta`
  incrementally once `should_hold_stream_output` clears the tool-call guard, plus a new `on_final`
  callback that replaces the streamed text with the cleaned answer at the end. Desktop streams via
  `assistant:delta` (append) and finishes with `assistant:final` (replace) for true token streaming;
  CLI/TUI behavior is unchanged (they pass no `on_final`).

## v0.1.57

中文：

- **`/` 命令不再被当作普通消息发给模型**：发送时以 `/` 开头的输入先经命令解析——`/compact`、
  `/new`、`/settings` 在本地执行（不发送）；`/subagent`、技能名展开为提示模板后再发送；
  **未知命令（如 `/goal ...`）被拦截并提示，绝不原样发给模型**（避免在 root/yolo 权限下被误执行）。
  形如 `/etc/hosts` 的路径、中文开头等非命令文本照常发送，不会误拦。
- **兼容 OpenAI 最新 Responses 接口**：接口格式新增 **OpenAI (Responses·最新)**，与旧的
  **OpenAI (Chat)** 并存。Responses 走 `POST /responses`，消息放 `input`、system 放
  `instructions`，流式解析 `response.output_text.delta`；Chat 仍走 `/chat/completions`。
  现共支持 DeepSeek / OpenAI Chat / OpenAI Responses / Google Gemini / Anthropic Claude 五种。
- **非 DeepSeek 格式统一给 max_tokens 封顶**（32000），避免 OpenAI/Gemini/Claude 因 DeepSeek
  思考模式的超大输出预算而 400。
- **桌面端无需再构建 exe**：README 改为主推 `pip install "deepseek-tulagent[desktop]"` 后直接
  `deepseekTulDesktop` 运行（用系统 WebView，无编译步骤）。
- **修 exe 构建报错**：`build_windows_exe.ps1` 改用 `--collect-all`（打包 assets 与子模块）+
  pywebview Windows 后端的 hidden-import（clr/proxy_tools/bottle/edgechromium 等），修掉最常见的
  「module not found / 空白窗口」问题；CI 直接复用该脚本。

English:

- **`/` commands are no longer sent to the model as plain messages**: a leading-slash input is
  routed on send — `/compact`, `/new`, `/settings` run locally (never sent); `/subagent` and skill
  names expand to a prompt template then send; **unknown commands (e.g. `/goal ...`) are blocked
  with a notice and never sent raw** (so they can't be acted on under root/yolo). Path-like text
  (`/etc/hosts`) and non-command input still send normally — no false blocking.
- **OpenAI Responses API support**: added an **OpenAI (Responses·newest)** format alongside the
  classic **OpenAI (Chat)**. Responses uses `POST /responses` with `input` + `instructions` and
  streams `response.output_text.delta`; Chat still uses `/chat/completions`. Five formats now:
  DeepSeek / OpenAI Chat / OpenAI Responses / Google Gemini / Anthropic Claude.
- **Cap max_tokens for non-DeepSeek formats** (32000) so OpenAI/Gemini/Claude don't 400 on the
  huge output budgets DeepSeek thinking modes request.
- **Desktop no longer needs an exe build**: README now leads with `pip install
  "deepseek-tulagent[desktop]"` + `deepseekTulDesktop` (uses the system WebView; no compile step).
- **Fixed exe build errors**: `build_windows_exe.ps1` now uses `--collect-all` (bundles the
  desktop assets and submodules) plus hidden-imports for pywebview's Windows backend
  (clr/proxy_tools/bottle/edgechromium/…), fixing the common "module not found / blank window"
  failures; CI reuses the same script.

## v0.1.56

中文：

- **多 Provider 兼容**：接口格式从 DeepSeek/OpenAI 扩展为 **DeepSeek、OpenAI、Google Gemini、
  Anthropic Claude** 四种。`provider.py` 按格式分发端点/鉴权/请求体/SSE 解析——OpenAI 系走
  `/chat/completions` + Bearer；Anthropic 走 `/v1/messages` + `x-api-key`，system 抽到顶层、
  流式解析 `content_block_delta`，max_tokens 封顶避免 400；Gemini 走
  `:generateContent`/`:streamGenerateContent?alt=sse`，role 映射 assistant→model、
  system→systemInstruction。各格式在 base_url 留空时自动选用默认域名。
- **修复：自定义 API 保存后不生效 / 无法发送** —— `config.get_settings()` 原先环境变量优先于
  配置文件，桌面端点「保存」写的是配置文件，一旦启动环境里设了 `DEEPSEEK_API_KEY/BASE_URL/MODEL`
  保存的值永远被覆盖。现改为 GUI 字段（api_key/base_url/model/provider_format）**配置文件优先**，
  环境变量退为回退。
- **Codex 式 `/` 命令菜单**：在输入框输入 `/` 弹出命令与技能菜单，支持前缀过滤、↑/↓ 选择、
  Enter/Tab 确认、Esc 关闭、点击选中；内置 `/compact`、`/subagent`、`/new`、`/settings`，
  技能自动列入。中文输入法（`isComposing`）下不误触发。
- **删除左侧「技能」栏**：技能改由 `/` 菜单暴露（贴近输入框，Codex 风格）。
- **删除右侧 inspector 面板**（运行状态/能力/权限/事件流），腾出阅读空间；「压缩上下文」移到顶栏。
  所有对已删元素的 DOM 赋值改为空判断，避免 `null.textContent` 报错。

English:

- **Multi-provider support**: interface formats expand from DeepSeek/OpenAI to **DeepSeek,
  OpenAI, Google Gemini, Anthropic Claude**. `provider.py` dispatches endpoint/auth/body/SSE
  per format — OpenAI-family uses `/chat/completions` + Bearer; Anthropic uses `/v1/messages`
  + `x-api-key` (system hoisted to the top level, streaming parses `content_block_delta`,
  max_tokens capped to avoid 400s); Gemini uses `:generateContent` /
  `:streamGenerateContent?alt=sse` with assistant→model, system→systemInstruction. Each format
  falls back to its default host when base_url is left blank.
- **Fixed: saved custom API not taking effect / cannot send** — `config.get_settings()` used to
  rank env vars above the config file, but the desktop Save writes the config file, so a leftover
  `DEEPSEEK_API_KEY/BASE_URL/MODEL` in the launch env silently shadowed it forever. GUI fields
  (api_key/base_url/model/provider_format) now prefer the config file, env as fallback.
- **Codex-style `/` command menu**: typing `/` in the composer opens a command+skill menu with
  prefix filtering, ↑/↓ navigation, Enter/Tab to confirm, Esc to close, click to select; built-in
  `/compact`, `/subagent`, `/new`, `/settings`, plus every skill. Does not misfire under a Chinese
  IME (`isComposing`).
- **Removed the left Skills panel**: skills are now surfaced through the `/` menu next to the
  composer (Codex style).
- **Removed the right inspector panel** (status / capabilities / permissions / event mirror) for
  more reading room; "compact context" moved to the toolbar. Every DOM write to now-removed
  elements is null-guarded to avoid `null.textContent` crashes.

## v0.1.55

中文：

- **视觉全面对齐 Codex 桌面端设计体系**（研究了官方 Codex Desktop 前端后重写）：单一石墨灰 `#181818` 底色，浮层 `#212121`；文字用白色 100%/70%/50%/32% 四档透明度分层；边框全部改为 8%/12% 白色发丝线；暗色主按钮改为白底黑字；去掉蓝色渐变 logo、大蓝按钮、彩色状态胶囊和工具卡彩色左边条等所有彩色噪音。
- **消息布局重做**：用户消息改为右对齐圆角气泡，助手消息为通栏正文，去掉头像行；工具调用折叠行改为 30px 紧凑行高。
- **输入区重做**：模型/思考/权限/接口选择器、附件、发送/中断按钮全部收进一张浮起的圆角输入卡片（Codex composer 样式）。
- **修复：中文输入法回车误发送** —— 拼音候选未上屏时按 Enter 不再直接发送（检查 `isComposing`/keyCode 229）。
- **修复：启动时序竞态** —— 现在等待 `pywebviewready` 事件后再初始化，pywebview 注入 api 慢时不再白屏；浏览器预览超时后才回退演示数据。
- **修复：会话改名/删除失效** —— `window.prompt/confirm` 在 pywebview 多数后端不可用，改为应用内置对话框。
- **修复：工具输出串卡** —— 工具完成事件按名称匹配未完成的卡片且完成后复位指针，连续/交错的工具调用不再把输出写进错误的卡片。
- **修复：流式输出强制滚底** —— 向上翻阅历史时不再被拽回底部，只有停留在底部附近才跟随滚动。
- **修复：`hidden` 属性被 CSS `display` 覆盖**，中断按钮不再在空闲时显示。
- **修复：模型输出含 U+2028/U+2029 时事件丢失**（后端 `evaluate_js` JSON 注入转义）。
- **修复：超长工具输出卡死界面** —— 展示截断至 4 万字符、跳过超大文本的语法高亮，输出区限高滚动。
- **修复其余小问题**：新对话/恢复会话不重置事件计数与流式状态、`turn:done` 空 sessionId 崩溃、markdown 链接允许 `javascript:`、发送失败不恢复附件、事件流面板无限增长等。

English:

- **Visual system realigned with Codex Desktop** (rewritten after studying the official frontend): single graphite `#181818` background with `#212121` elevated surfaces; text in white at 100/70/50/32% opacity tiers; all borders replaced with 8%/12% white hairlines; dark-mode primary buttons are now white-on-black; removed the blue gradient logo, big blue buttons, colored status pills and colored tool-card edge bars.
- **Message layout redone**: user messages are right-aligned rounded bubbles, assistant messages full-width prose, avatar rows removed; tool rows use a compact 30px height.
- **Composer redone**: model/thinking/mode/format selectors, attach, and send/stop now live inside one elevated rounded composer card (Codex style).
- **Fixed: IME Enter mis-send** — pressing Enter while composing Chinese no longer sends (checks `isComposing`/keyCode 229).
- **Fixed: startup race** — boot now waits for `pywebviewready`; no more blank UI when api injection is slow; demo data only after a browser-preview timeout.
- **Fixed: session rename/delete dead** — `window.prompt/confirm` are unavailable in most pywebview backends; replaced with in-app dialogs.
- **Fixed: tool output landing in the wrong card** — completion events match pending cards by name and reset the pointer afterwards.
- **Fixed: forced auto-scroll during streaming** — scrolling up is respected; the view only follows when near the bottom.
- **Fixed: `hidden` attribute overridden by CSS `display`** — the stop button no longer shows while idle.
- **Fixed: events lost when model output contains U+2028/U+2029** (escaped in the backend `evaluate_js` JSON bridge).
- **Fixed: huge tool outputs freezing the UI** — display truncated at 40k chars, syntax highlighting skipped for oversized text, output pane capped with its own scroll.
- **Other fixes**: event counter/stream state now reset on new/resumed sessions, null-sessionId crash in `turn:done`, `javascript:` links blocked in markdown, attachments restored on failed send, event mirror capped at 300 lines.

## v0.1.54

中文：

- **工具调用与输出合并到同一卡片分层显示**：调用参数（蓝色左边条「调用」）在上，系统返回的输出（绿色左边条「输出」）在下，同属一个折叠块，不再拆成两条独立事件，可一眼看清「做了什么」与「返回了什么」。
- **代码语法高亮**：代码块按语言（Python / JS / Bash / JSON）着色关键字、字符串、注释、数字，采用 VS Code Dark+ 配色；代码块带语言标签与「复制」按钮。
- **侧栏会话可置顶、改名、删除**：每条会话 hover 显示 ☆ 置顶 / ✎ 改名 / 🗑 删除（删除二次确认，后端新增 `delete_session`）。
- **侧栏可收起**：顶栏 ☰ 与品牌区 « 一键折叠/展开对话侧栏，腾出阅读空间。
- **输入区按钮重设计**：胶囊式输入框内嵌圆形「＋ 添加文件」与圆形蓝色「发送」按钮；发送后发送键就地切换为红色「中断」键，点击即强行中断当前生成。

English:

- **Tool call and its output now share one layered card**: the invocation (blue-edged “调用”) sits on top, the returned system output (green-edged “输出”) below, inside a single collapsible block instead of two separate events — what was run and what came back at a glance.
- **Code syntax highlighting**: code blocks colorize keywords/strings/comments/numbers per language (Python / JS / Bash / JSON) using a VS Code Dark+ palette; blocks carry a language label and a Copy button.
- **Sidebar sessions can be pinned, renamed, and deleted**: each row reveals ☆ pin / ✎ rename / 🗑 delete on hover (delete is confirmed; new backend `delete_session`).
- **Collapsible sidebar**: a ☰ in the toolbar and « in the brand area fold/expand the conversation sidebar.
- **Redesigned composer buttons**: a pill input box with a round “＋ attach” and a round blue Send button; after sending, Send morphs in place into a red Stop button that force-interrupts the current generation.

## v0.1.53

中文：

- 桌面端视觉对齐 **Codex / VS Code**：中性深灰底色、VS Code 蓝（#3794ff）强调色、扁平小圆角、IDE 级信息密度，替换上一版的深海主题。
- 重构对话布局为 **扁平全宽消息**（头像+名字在上、内容在下），不再用左右气泡，阅读动线更接近 Copilot Chat / Codex。
- **工具调用内联进对话流**：用户消息 → 工具/思考折叠步骤 → 助手回答，按时间顺序排成一条线；移除原先悬在对话区与输入框之间、会被截断的独立事件条。
- 代码块改为编辑器样式（语言头 + #1e1e1e 正文），行内代码用 VS Code 橙色字符串色。
- 侧栏改为 hover 高亮的扁平列表，输入区与设置弹窗统一 VS Code 控件风格。

English:

- Desktop visuals realigned to **Codex / VS Code**: neutral dark-grey base, VS Code focus blue (#3794ff) accent, flat small radii and IDE-level density, replacing the previous deep-sea theme.
- Reworked the conversation into **flat full-width messages** (avatar + name on top, content below) instead of left/right bubbles, closer to Copilot Chat / Codex.
- **Tool calls render inline in the thread**: user message → collapsible tool/thinking steps → assistant answer, in chronological order; removed the detached, clipped event strip between the chat and composer.
- Code blocks now use an editor-style chrome (language header + #1e1e1e body); inline code uses the VS Code orange string color.
- Sidebar is a flat hover-highlight list; composer and settings dialog use unified VS Code-style controls.

## v0.1.52

中文：

- 品牌更名：桌面端由 “DeepSeek TuLAgent” 更名为 **Fathom**（深海主题，寓意“深入每一寻”）；包名与 CLI 入口（dstul / deepseek-tulagent / deepseekTul / deepseekTulDesktop）保持不变，升级无需改动脚本。
- 桌面端全面重做视觉：深海青绿主题、渐变品牌标识、头像气泡、卡片式三栏布局、脉冲状态点、自定义滚动条与对话框美化，整体观感对齐 Claude / Codex 桌面端。
- 助手消息支持 **Markdown 渲染**：标题、列表、引用、粗体/斜体、行内代码与带语言标签的代码块，流式输出实时渲染。
- 修复开场白无法清除的 Bug：`addMessage` 之前只匹配 `.empty`，与实际的 `.intro` 容器不一致，导致首条消息后欢迎语残留。
- 新会话占位、窗口标题、侧栏与检查器文案同步更新为 Fathom。

English:

- Rebrand: the desktop app is renamed from “DeepSeek TuLAgent” to **Fathom** (deep-sea theme). Package name and CLI entry points (dstul / deepseek-tulagent / deepseekTul / deepseekTulDesktop) are unchanged, so upgrades need no script edits.
- Full desktop visual redesign: deep-sea teal theme, gradient brand mark, message avatars, card-based three-pane layout, pulse status dot, custom scrollbars and a polished settings dialog — on par with the Claude / Codex desktop clients.
- Assistant messages now render **Markdown**: headings, lists, blockquotes, bold/italic, inline code and fenced code blocks with a language label, rendered live during streaming.
- Fixed the welcome-screen bug: `addMessage` matched only `.empty` while the intro container used `.intro`, leaving the welcome block stuck after the first message.
- New-session placeholder, window title, sidebar and inspector copy updated to Fathom.

## v0.1.51

中文：

- 改进终端输入提示，把 `agent/fast` 改为 `mode=agent think=fast`，避免把权限模式误看成“进入子代理模式”。
- `/cancel` 提示现在明确显示已回普通输入，并保留当前 `mode` / `think` 状态。
- 同步 README 安装链接到 `v0.1.51`。

English:

- Clarified the terminal prompt from `agent/fast` to `mode=agent think=fast`, so permission mode is not confused with a subagent mode.
- `/cancel` now explicitly reports that normal input is restored while keeping the current `mode` / `think` state.
- Updated README install links to `v0.1.51`.

## v0.1.50

中文：

- 修复终端主输入处 `Ctrl-C` 直接退出程序的问题；现在 `Ctrl-C` 会执行 `/cancel`，清理当前目标/子代理提示状态并回到普通输入。
- 真正退出终端会话仍使用 `Ctrl-D`、`/exit` 或 `/quit`。
- 新增 `/cancel` / `/stop` 命令，用于从误触或残留的委派/子代理状态回到普通聊天。
- 同步 README 安装链接到 `v0.1.50`。

English:

- Fixed terminal main-input `Ctrl-C` exiting the program directly; it now runs `/cancel`, clears active goal/subagent prompt state, and returns to normal input.
- Exiting the terminal session still uses `Ctrl-D`, `/exit`, or `/quit`.
- Added `/cancel` / `/stop` commands to recover from accidental or stale delegation/subagent state.
- Updated README install links to `v0.1.50`.

## v0.1.49

中文：

- 修复终端 `/` 快捷命令面板中“子代理”入口容易让用户误进入委派链路的问题；快捷面板不再展示 `/subagents`，避免误触后看起来像进入无法退出的子代理模式。
- 手动输入 `/subagents` 仍只显示子代理能力说明，不会切换会话模式或拦截后续用户消息。
- 终端命令面板现在支持 `Ctrl-C` / `Ctrl-D` 取消并返回主输入，同时底部提示明确列出退出键。
- `delegate_agent` 执行链路增加取消检查；上层交互请求取消时会传播到子代理循环，避免子代理执行时主会话长时间无法释放。
- 同步 README 安装链接到 `v0.1.49`。

English:

- Fixed the terminal `/` quick command palette exposing a subagent entry that could make users accidentally enter a delegation flow that looked like an unescapable subagent mode.
- Manually typing `/subagents` still only prints capability help and does not switch modes or intercept later user messages.
- The terminal command palette now supports `Ctrl-C` / `Ctrl-D` cancellation back to the main input, and its footer documents the cancel keys.
- Added cancellation checks through the `delegate_agent` path so parent interactive cancellation can propagate into subagent loops.
- Updated README install links to `v0.1.49`.

## v0.1.48

中文：

- 修复工具失败后的自动恢复提示被保存成 `user` 消息，导致恢复会话时看起来像用户自己说了“previous tool failed...”的问题。
- 自动恢复提示现在只作为临时模型上下文使用，不再写入 session；旧 session 中已有的内部提示也会在恢复上下文和 recent 历史中被过滤。
- 修复流式模式下模型先输出自然语言前言再输出工具 JSON 时，前言和工具 JSON 被打印到可见正文的问题。
- 恢复历史现在会隐藏“带前言的工具调用 assistant 消息”，避免出现“你说得对...```json`”这类噪音。
- 同步 README 安装链接到 `v0.1.48`。

English:

- Fixed automatic recovery prompts after failed tools being persisted as `user` messages, which made resumed sessions look like the user had said "previous tool failed...".
- Recovery prompts are now temporary model context only and are no longer written to session history; existing internal prompts in old sessions are filtered from resumed context and recent history.
- Fixed streamed responses where the model emits natural-language preface text before tool-call JSON, causing both the preface and JSON to appear in visible output.
- Resume history now hides assistant messages that contain tool calls even when they include a preface such as "you are right...".
- Updated README install links to `v0.1.48`.

## v0.1.47

中文：

- 改进 Windows 终端适配：Windows 默认使用 ASCII/plain UI，避免 Unicode 线框、特殊符号和宽度计算差异导致乱码、重叠或排版错乱。
- 启动动画、工具事件、信息框、输入提示和截断符在 plain UI 下改为保守单行文本。
- `DSTUL_PLAIN_UI=1` 可在任意平台强制启用 Windows 同款保守排版。
- 增加回归测试覆盖 plain UI 的 box、事件、prompt 和截断显示。
- 同步 README 安装链接到 `v0.1.47`。

English:

- Improved Windows terminal compatibility: Windows now defaults to an ASCII/plain UI to avoid garbled box drawing, symbol width mismatches, overlap, and layout drift.
- Startup output, tool events, boxes, prompts, and clipping markers use conservative single-line text in plain UI mode.
- `DSTUL_PLAIN_UI=1` can force the same conservative layout on any platform.
- Added regression tests for plain UI boxes, events, prompts, and clipping.
- Updated README install links to `v0.1.47`.

## v0.1.46

中文：

- 修复终端 composer 粘贴大量多行内容时，换行逐行刷屏导致输入区显示混乱的问题。
- 多行粘贴现在会在输入区压缩显示为单行摘要，例如 `[pasted 3 lines] ...`，实际提交内容仍保留完整换行。
- 增加回归测试覆盖 bracketed paste、多行显示摘要和长尾截断。
- 同步 README 安装链接到 `v0.1.46`。

English:

- Fixed terminal composer redraw noise when pasting large multi-line clipboard content.
- Multi-line pasted input is now rendered as a single-line summary such as `[pasted 3 lines] ...`, while preserving the full submitted newlines.
- Added regression tests for bracketed paste, multi-line display summaries, and long-tail clipping.
- Updated README install links to `v0.1.46`.

## v0.1.45

中文：

- `delegate_agent` 支持 `agents=[{name, task, mode?, think?, max_rounds?}, ...]`，一次工具调用可委派多个隔离子代理任务。
- 修复模型把 `fast`、`careful` 等思考模式误填到子代理 `mode` 字段时导致 CLI 崩溃的问题；现在会自动识别为 `think`。
- 工具执行层现在会把工具参数校验错误转成失败结果返回给主代理，避免单个工具异常中断整个会话。
- 同步 README 安装链接到 `v0.1.45`。

English:

- `delegate_agent` now accepts `agents=[{name, task, mode?, think?, max_rounds?}, ...]`, allowing multiple isolated subagent tasks in one tool call.
- Fixed crashes when the model put thinking modes such as `fast` or `careful` into the subagent `mode` field; they are now treated as `think`.
- Tool argument validation errors are now returned as failed tool results instead of terminating the CLI process.
- Updated README install links to `v0.1.45`.

## v0.1.44

中文：

- 修复流式模式下模型输出工具调用 JSON 时，原始 `{"tool":...}` / fenced JSON 会先被打印到正文的问题。
- 现在疑似工具调用的流式开头会先缓冲；如果完整消息解析为工具调用，只显示工具事件，不显示原始 JSON。
- 增加回归测试覆盖普通 JSON 工具调用和 fenced JSON 工具调用的流式过滤。

English:

- Fixed streamed tool-call JSON leaking into visible assistant text before being rendered as a tool event.
- Streamed output that starts like a tool call is now buffered first; if the full message parses as a tool call, only the tool event is shown.
- Added regression tests for both plain JSON and fenced JSON streamed tool-call filtering.

## v0.1.43

中文：

- 新增 `ask_user` 交互工具：模型可以返回结构化问题和选项，由终端渲染为可选择列表，并支持手动填写自定义答案。
- 修复 `/` 命令面板中选择 `/goal` 时直接提交的问题；现在会回填 `/goal ` 到输入框，方便继续输入目标。
- 同步 README 安装链接到 `v0.1.43`。

English:

- Added the `ask_user` interaction tool so the model can return structured questions/options, rendered by the terminal as a selectable list with manual custom input support.
- Fixed selecting `/goal` from the `/` command palette submitting immediately; it now inserts `/goal ` into the composer for continued typing.
- Updated README install links to `v0.1.43`.

## v0.1.42

中文：

- 修复 `ThinkingSpinner.stop()` 非幂等导致流式回复结束时再次清行，从而吞掉最后一行/部分回复的问题。

English:

- Fixed non-idempotent `ThinkingSpinner.stop()` clearing the terminal again at the end of streaming, which could erase the last line or part of the response.

## v0.1.41

中文：

- 修复流式模式首个 token 后再次清行动画，导致部分终端吞掉回复开头的问题。

English:

- Fixed an extra spinner line clear after the first streamed token, which could erase the beginning of the assistant response in some terminals.

## v0.1.40

中文：

- 恢复流式模式下的思考动画：等待首个模型 token 时显示 spinner。
- 首个流式 token 到来后自动清除动画，避免动画覆盖正文。
- `ThinkingSpinner` 增加显式 `start()` / `stop()`，便于流式路径精确控制动画生命周期。

English:

- Restored the thinking spinner for streaming mode while waiting for the first model token.
- The spinner now clears as soon as the first streamed token arrives so it does not cover assistant text.
- Added explicit `start()` / `stop()` controls to `ThinkingSpinner` for precise streaming lifecycle handling.

## v0.1.39

中文：

- 普通 `deepseekTul start` 行输入模式改为默认流式输出，不再等整段模型回复结束才显示。
- `deepseekTul run` 现在默认流式输出；`--json` 仍保持非流式，保证机器可读 JSON 完整。
- 流式输出增加兜底：如果后端没有发 delta，会打印最终 answer，避免空输出。

English:

- Plain `deepseekTul start` line-mode chat now streams assistant output by default instead of waiting for the full response.
- `deepseekTul run` now streams by default; `--json` remains non-streaming to keep machine-readable JSON intact.
- Added a streaming fallback: if no delta arrives, the final answer is printed instead of producing empty output.

## v0.1.38

中文：

- 桌面端新增运行中防重复发送，避免长文本或误触导致同一条消息提交两次。
- 桌面端新增停止按钮和取消状态；执行中取消不会直接退出整个对话。
- 桌面端工具/思考/子代理事件改成更清晰的折叠事件卡片，减少流水文本噪音。
- 兼容模型输出的 `Tool: ...` / `Arguments: {...}` 工具调用格式，避免仓库拉取等工具调用被当成普通文本后中断。
- 继续保持 `clone_repo` 对 `repo/url`、Windows 路径和 GitHub URL 规范化的兼容。

English:

- Added desktop in-flight send guarding to prevent duplicate submissions from long text or accidental repeated sends.
- Added a desktop stop button and cancellation state; cancelling a running turn no longer exits the whole conversation.
- Reworked desktop tool/thinking/subagent events into clearer collapsible event cards.
- Added parser support for model outputs like `Tool: ...` / `Arguments: {...}` so repository cloning and similar calls do not stall as plain text.
- Kept `clone_repo` compatibility for `repo/url`, Windows paths, and normalized GitHub URLs.

## v0.1.37

中文：

- 修复 `clone_repo` 对模型输出的兼容性：支持错误键名 `repo/url` 和 `repository`。
- `clone_repo` 现在允许 workspace 内的绝对路径，避免 `/root/...` 这类路径误判为逃逸。
- GitHub 仓库 URL 会规范化为 `.git` 形式，工具调用日志更稳定。
- 更新提示词，避免诱导模型把参数名写成 `repo/url`。

English:

- Made `clone_repo` more tolerant of model output: accepts `repo/url` and `repository` aliases.
- `clone_repo` now accepts absolute paths that are still inside the configured workspace.
- GitHub repository URLs are normalized to `.git` form for stable tool logs.
- Updated prompting to avoid suggesting `repo/url` as a literal argument name.

## v0.1.36

中文：

- 桌面设置面板改成右侧抽屉式结构，更接近成熟开发者桌面端。
- 修复桌面启动时权限说明初始化顺序问题。
- 新增 `NOTICE`，明确桌面 UI 参考 Reasonix Desktop，并保留 MIT attribution。

English:

- Reworked desktop settings into a right-side drawer, closer to a mature developer desktop app.
- Fixed permission-help initialization order on desktop startup.
- Added `NOTICE` with Reasonix Desktop MIT attribution for the UI inspiration.

## v0.1.35

中文：

- 重做桌面端视觉结构：改为深色开发者工作台，左侧会话/技能，中间 transcript，右侧运行状态和能力面板。
- 桌面端突出 TuLAgent 自有能力：自动压缩、手动压缩、子代理、技能目录、工具调用和内部思考事件。
- 修复桌面端自定义 API 配置：保存时不再清空旧 API key，第三方 OpenAI 兼容接口可切换 `provider_format`。
- OpenAI-compatible 模式不再发送 DeepSeek 专属 `thinking` / `reasoning_effort` 字段。
- 桌面端新增手动压缩按钮和子代理任务插入按钮。
- 输入框上方新增主控制台，可直接切换模型、思考模式、权限模式和接口格式。
- 新增对话保存状态、会话 ID 展示、置顶会话、复制会话 ID、重命名会话。
- 右侧面板新增权限模式说明，方便只给模型一点权限或切到最高权限。

English:

- Reworked the desktop UI into a dark developer workspace with session/skill navigation, central transcript, and a right-side runtime/capability inspector.
- Surfaced TuLAgent-specific features: auto/manual compaction, subagents, skills, tool calls, and internal-thinking events.
- Fixed desktop custom API configuration so saving no longer clears an existing API key and OpenAI-compatible providers can switch `provider_format`.
- OpenAI-compatible mode no longer sends DeepSeek-only `thinking` / `reasoning_effort` fields.
- Added desktop actions for manual context compaction and subagent task insertion.
- Added a composer-level control console for model, thinking, permission, and provider-format switching.
- Added conversation save state, session id display, pinned sessions, copy-session-id, and rename-session actions.
- Added permission-mode descriptions in the right inspector.

## v0.1.34

中文：

- 修复 Windows desktop workflow：桌面 assets 不再被 Hatch 重复加入 wheel。
- Windows exe 构建脚本改用当前 `python`，避免 GitHub Actions 里 `py -3` 误选其他 Python 版本。

English:

- Fixed the Windows desktop workflow by avoiding duplicate Hatch wheel inclusion for desktop assets.
- The Windows exe build script now uses the current `python` so GitHub Actions does not accidentally select another Python version through `py -3`.

## v0.1.33

中文：

- 新增桌面端入口：`deepseekTul desktop` 和 `deepseekTulDesktop`。
- 桌面端支持聊天、文件上传、技能列表、模型/思考/权限切换、第三方 OpenAI 兼容 API 配置。
- 工具调用、子代理、上下文压缩和内部思考事件在桌面端折叠展示，需要用户点开查看详情。
- 新增 Windows exe 构建脚本 `scripts/build_windows_exe.ps1`。
- 新增 GitHub Actions Windows 构建流水线，tag 发布时生成 `DeepSeekTuLAgent-windows` artifact。

English:

- Added desktop entrypoints: `deepseekTul desktop` and `deepseekTulDesktop`.
- The desktop app supports chat, file uploads, skills, model/thinking/permission switching, and third-party OpenAI-compatible API settings.
- Tool calls, subagents, context compaction, and internal thinking events are shown as collapsible desktop events.
- Added `scripts/build_windows_exe.ps1` for Windows exe builds.
- Added a GitHub Actions Windows build workflow that uploads a `DeepSeekTuLAgent-windows` artifact on tagged releases.

## v0.1.32

中文：

- 增强 Windows 原生兼容：`termios`、`tty`、`curses` 不存在时不再启动崩溃。
- Windows 下高级 TUI 不可用时自动退回普通行输入交互；`run`、`config`、`update`、`sessions` 保持可用。
- 更新器改用当前 Python 解释器执行 pip，不再硬编码 `python3`。
- `clone_repo` 和文件工具兼容用户粘贴的 Windows 风格路径。
- README 增加 PowerShell、CMD、Windows 代理和 Windows 路径示例。

English:

- Improved native Windows compatibility: missing `termios`, `tty`, or `curses` no longer crashes startup.
- The CLI falls back to line-mode chat when the full TUI is unavailable on Windows; `run`, `config`, `update`, and `sessions` remain usable.
- The updater now runs pip through the current Python interpreter instead of hard-coding `python3`.
- `clone_repo` and file tools accept pasted Windows-style paths.
- README now includes PowerShell, CMD, Windows proxy, and Windows path examples.

## v0.1.31

中文：

- 新增 `clone_repo` 工具：拉取 Git/GitHub 仓库时会自动尝试直连、镜像和 GitHub archive fallback。
- 模型提示词现在要求仓库拉取优先使用 `clone_repo`，避免反复手写失败的镜像 `git clone` 命令。
- README 增加仓库拉取兼容说明，并把安装包示例更新到 `v0.1.31`。

English:

- Added the `clone_repo` tool with direct git, mirror, and GitHub archive fallbacks.
- Updated prompting so repository fetch requests prefer `clone_repo` instead of repeated manual mirror `git clone` commands.
- README now documents repository-fetch compatibility and updates tarball examples to `v0.1.31`.

## v0.1.30

中文：

- 更新器不再强依赖 `git+https`：非 git 安装默认使用 GitHub tag tarball。
- 源码树 git 更新失败时，会尝试 pip 安装 tag tarball 作为 fallback。
- README 增加无 git / 代理环境安装说明，以及 `HTTP_PROXY` / `HTTPS_PROXY` 和 git proxy 示例。

English:

- The updater no longer depends on `git+https` for non-git installs; it uses the GitHub tag tarball.
- If a git source-tree update fails, the updater attempts a pip tarball fallback.
- README now documents no-git/proxy-friendly install commands and `HTTP_PROXY` / `HTTPS_PROXY` plus git proxy examples.

## v0.1.29

中文：

- 强化子代理提示词：多分支调查、独立复核、验证、研究、长流程拆分时会更主动考虑 `delegate_agent`。
- 复杂任务的私有执行提示也会提醒模型把适合独立处理的部分交给子代理。

English:

- Strengthened subagent prompting so the model more proactively considers `delegate_agent` for multi-branch investigation, independent review, verification, research, and long workflows.
- Complex-task private hints now also remind the model to delegate focused subtasks when useful.

## v0.1.28

中文：

- 新增子代理能力：主 agent 可调用 `delegate_agent(name, task, mode?, think?, max_rounds?)`。
- 子代理使用隔离上下文执行任务，只把摘要、证据和建议下一步返回给主 agent。
- `/subagents` 可查看子代理能力说明；`/` 面板和工具面板会显示子代理入口。
- 子代理结果使用 `SUBAGENT_RESULT` 格式，恢复对话时会隐藏这类工具噪音。

English:

- Added subagent delegation: the parent agent can call `delegate_agent(name, task, mode?, think?, max_rounds?)`.
- Subagents run in isolated context and return a concise summary, evidence, and recommended next step to the parent agent.
- `/subagents` shows the delegation capability; the slash palette and tool palette expose the entry.
- Subagent results use `SUBAGENT_RESULT` and are hidden from resumed human-visible history noise.

## v0.1.27

中文：

- 调整 `/` 面板里的 `/goal ` 排序，默认优先插入输入框，方便继续输入目标文本。

English:

- Reordered `/goal ` in the slash palette so it is selected as an insertion template first, making it easier to keep typing the objective.

## v0.1.26

中文：

- 新增 `/goal <目标>`：设置持续目标后，agent 不会因为中间回复就主动停下，会继续推进到明确完成或阻塞。
- `/goal` 查看当前目标，`/goal clear` 清除目标。
- `/` 面板选择 `/goal ` 会插入输入框，方便继续补目标文本，不会直接当命令执行。

English:

- Added `/goal <objective>` so the agent keeps working toward an active objective until explicit completion or blockage.
- `/goal` shows the active goal; `/goal clear` clears it.
- Selecting `/goal ` from the slash palette inserts it into the composer so the user can finish typing the goal.

## v0.1.25

中文：

- 多步骤任务会自动加入私有执行提示，促使模型按工具结果连续推进，不只口头说明下一步。
- 工具失败后，如果模型没有尝试恢复路径也没有明确说明阻塞，会自动要求模型基于错误再尝试一个更合适的工具调用。
- 工具结果进入上下文前会裁剪超长输出，保留头尾，降低上下文污染和遗忘概率。

English:

- Multi-step tasks now get a private execution hint so the model keeps progressing through tool-backed steps instead of only describing the next action.
- After a failed tool result, if the model neither retries nor explicitly declares a block, the agent asks it to recover with a better tool call.
- Very large tool outputs are trimmed before entering context, preserving head and tail to reduce context pollution.

## v0.1.24

中文：

- 修复思考动画和工具事件同时输出时互相覆盖、残留在同一行的问题。
- 思考动画清行改用终端整行清除，并在工具事件输出前主动清掉当前动画行。
- 思考动画颜色判断改为使用 stderr 的 TTY 状态。

English:

- Fixed thinking spinner and tool events overwriting or leaving remnants on the same terminal line.
- Spinner cleanup now clears the whole terminal line and tool events clear the active spinner line before printing.
- Spinner color detection now uses stderr TTY state.

## v0.1.23

中文：

- 清理 `v0.1.22` 发布说明中的不必要描述。

English:

- Cleaned up unnecessary wording in the `v0.1.22` release notes.

## v0.1.22

中文：

- 修复部分终端长文本粘贴没有 bracketed paste 标记时，换行被误判为回车提交的问题；高速粘贴中的换行会进入输入框缓冲，不会自动发送。
- 启动动画加入更密集的 signal 流动效果。
- 思考动画和工具事件输出改成更清晰的彩色分段：tool start、tool done、thinking pass、compact 都有独立样式。

English:

- Fixed long pasted text being submitted accidentally on terminals that do not emit bracketed paste markers; high-speed pasted newlines stay in the input buffer.
- Added denser signal-flow startup animation.
- Thinking and tool events now use clearer colored segments for tool start, tool done, thinking passes, and compaction.

## v0.1.21

中文：

- 提高 DeepSeek 前缀缓存命中：大段固定系统提示保持为第一条 system，技能目录拆到独立后置 system，避免技能变化破坏主要前缀。
- 工具结果消息改成稳定前缀 `TOOL_RESULT name=...`，减少自然语言包装变化。
- 恢复会话时的 resume note 改为 user 消息，避免在历史中插入额外 system 破坏系统前缀。
- DeepSeek HTTP 客户端复用连接，减少重复 TLS/连接开销。

English:

- Improved DeepSeek prefix cache hits by keeping the large fixed system prompt as the first system message and moving skill context into a separate later system message.
- Tool result messages now use the stable `TOOL_RESULT name=...` prefix.
- Resume notes now use user messages instead of inserting extra system messages into resumed history.
- The DeepSeek HTTP client reuses its connection client to reduce repeated TLS/connection overhead.

## v0.1.20

中文：

- 默认工具轮数从 8 提高到 256，复杂任务不会轻易打满。
- 工具轮数真的打满时，不再输出 `Paused after tool execution...`；会让模型基于已有工具结果做一次最终总结。
- 系统提示加入公网服务验证建议：公网 IP 获取优先使用 `api.ipify.org`、`ifconfig.me`、`checkip.amazonaws.com`，并配合 `ss`、本地 `curl`、防火墙状态检查。

English:

- Raised the default tool round limit from 8 to 256 for longer automation tasks.
- When the tool round limit is actually reached, the agent no longer prints `Paused after tool execution...`; it asks the model to summarize the completed and remaining state.
- Added public service verification guidance using `api.ipify.org`, `ifconfig.me`, `checkip.amazonaws.com`, plus `ss`, local `curl`, and firewall checks.

## v0.1.19

中文：

- 修复工具执行后模型只说“接下来继续检查/启动/验证”但没有真正继续调用工具，导致对话提前回到输入的问题。
- Agent 会识别这类未完成承诺，并自动要求模型继续返回下一个工具 JSON 或给出最终结论。

English:

- Fixed turns stopping early after a tool result when the model only said it would continue checking/starting/verifying but did not request the next tool.
- The agent now detects these unfinished promises and asks the model to continue with the next tool JSON or provide the final answer.

## v0.1.18

中文：

- 修复模型输出工具 JSON 后面混入代码围栏尾巴时没有执行工具的问题。
- 兼容模型把 `timeout`、`max_results` 等参数放在工具 JSON 顶层的情况。
- TUI 中空闲状态按 `Ctrl+C` 仍会退出；正在思考/执行状态下收到中断只取消当前回合并回到输入。

English:

- Fixed tool JSON not being executed when the model leaves trailing code-fence noise after the JSON object.
- Accepts top-level tool options such as `timeout` and `max_results` when the model emits them outside `arguments`.
- In the TUI, `Ctrl+C` still exits while idle; during thinking/execution, an interrupt cancels only the current turn and returns to input.

## v0.1.17

中文：

- 修复 `web_search` 用 Bing 搜索中文内容时结果为空、跑偏或只说“换个搜索”但没有继续返回总结的问题。
- Bing 搜索默认带中文地区和语言参数，并把 Bing 跳转链接清洗成真实目标链接。
- 搜索结果失败时会把查询词写入工具结果，方便模型继续改写查询并重试。
- 默认最大输出从 2048 提高到 8192；用户自己的 `DSTUL_MAX_TOKENS` 或配置文件值不会被覆盖。

English:

- Fixed `web_search` Bing searches for Chinese queries returning empty/off-topic results or stopping after saying it would search again.
- Bing searches now send Chinese market/language parameters and normalize Bing redirect links to real target URLs.
- Failed search results include the query so the model can retry with a better query.
- Raised the default max output from 2048 to 8192 without overriding user `DSTUL_MAX_TOKENS` or config values.

## v0.1.16

中文：

- 自动上下文压缩默认开启，但触发阈值提高到约 92% 上下文窗口，减少过早压缩。
- 如需关闭自动压缩，可设置 `DSTUL_AUTO_COMPACT=0`。

English:

- Automatic context compaction is enabled by default, with a higher trigger threshold around 92% of the context window.
- Set `DSTUL_AUTO_COMPACT=0` to disable automatic compaction.

## v0.1.15

中文：

- 支持 bracketed paste，粘贴长文本或多行文本时，粘贴内容中的换行不会自动提交。
- 增加 1 秒内相同输入的重复提交保护，避免同一段长文本被发送两次。
- 自动上下文压缩保持可配置。
- 手动 `/compact` 仍然可用。

English:

- Added bracketed paste support so pasted long or multiline text does not submit on embedded newlines.
- Added duplicate-submit protection for identical prompts within one second.
- Automatic context compaction remains configurable.
- Manual `/compact` remains available.

## v0.1.14

中文：

- 输入太长时改成固定单行窗口，只显示末尾内容，左侧用 `…` 表示前面还有文本。
- 修复长输入换行后整行重画导致历史内容重复很多次的问题。
- 单行窗口按显示宽度处理中文宽字符。

English:

- Long composer input now uses a fixed single-line viewport, showing the tail with `…` for hidden prefix text.
- Fixed repeated visual echoes caused by full-line redraw after long input wrapped.
- The viewport accounts for wide CJK characters.

## v0.1.13

中文：

- 输入框每次输入、删除、插入技能后都会整行清理并重画，修复中文宽字符残留、第二个中文字删不掉的问题。
- 启动信息明确显示 `DeepSeek TuLAgent <version>`，并显示更新检查状态。

English:

- Composer now clears and redraws the full line after typing, deleting, or skill insertion, fixing wide-character residue and undeletable second CJK characters.
- Startup now clearly prints `DeepSeek TuLAgent <version>` and update-check status.

## v0.1.12

中文：

- `/models` 当前模型标记改成 `(current)`，不再使用星号。

English:

- `/models` now marks the current model with `(current)` instead of an asterisk.

## v0.1.11

中文：

- 纯问号消息仍交给模型理解，但禁止把它解释成“继续执行任务”并触发工具调用。
- 增加规则：`?`、`？`、连续问号只应询问用户具体想问什么。

English:

- Question-mark-only messages still go to the model, but tool calls are ignored so they cannot be treated as “continue the task”.
- Added rules for `?`, `？`, and repeated question marks to ask for clarification instead of inferring work.

## v0.1.10

中文：

- 最终回答显示前会清理装饰性星号：`**加粗**` 会去掉星号，`* 列表` 会改成 `- 列表`。
- 代码块里的星号不处理，避免破坏 shell glob、正则、代码。
- `/models` 当前模型标记从 `*` 改成 `(current)`。

English:

- Final answers now strip decorative asterisks: `**bold**` loses the markers, `* bullets` become `- bullets`.
- Code blocks are preserved so shell globs, regex, and code stay intact.
- `/models` now marks the current model with `(current)` instead of `*`.

## v0.1.9

中文：

- 修复 raw 输入只能读单字节导致中文输入不显示的问题。
- raw 输入现在会读取完整 UTF-8 字符，同时保留方向键 ESC 序列处理。
- CLI 启动和退出时会强制恢复终端 sane 状态、显示光标、退出 alternate screen。

English:

- Fixed Chinese/non-ASCII input being dropped because raw input read only one byte at a time.
- Raw input now reads complete UTF-8 characters while keeping arrow-key escape handling.
- CLI now forces terminal sane state, visible cursor, and alternate-screen exit on startup and exit.

## v0.1.8

中文：

- 文档把思考模式表的“路由”改为“推荐模型”，避免误解成切思考模式会强制换模型。
- 测试假模型名改成 `deepseek-v4-flash`，避免误导。
- 伪终端验证 `/think max` 后能继续输入并退出，且模型保持 `deepseek-v4-flash`。

English:

- Renamed the thinking-mode table column from route to recommended model to clarify that thinking changes do not force model switches.
- Replaced the test-only fake model with `deepseek-v4-flash`.
- Verified through a pseudo-terminal that `/think max` returns to input and keeps `deepseek-v4-flash`.

## v0.1.7

中文：

- 思考模式不再强制切换模型；`/think max` 只改思考参数和输出预算，保留当前模型。
- `/model`、`/think`、`/mode` 会写入本地默认配置，下次启动继续使用。
- 进入输入框前强制显示光标，减少从选择器返回后输入不可见的问题。

English:

- Thinking modes no longer force model changes; `/think max` changes thinking controls and output budget while keeping the current model.
- `/model`, `/think`, and `/mode` persist local defaults for the next session.
- Composer now forces cursor visibility before input to reduce invisible-input issues after pickers.

## v0.1.6

中文：

- 所有思考模式最大输出上限统一提升到 384K，包括 `off`、`instant`、`fast`。
- 新增 `auto` 思考模式：由模型先判断任务难度，再自动选择具体思考档位。
- 选择器退出后终端立即恢复，降低从 `/think` 或 `/model` 回来后输入不显示的问题。
- 系统提示默认要求少用星号、少用花哨 Markdown。

English:

- Raised every thinking mode to a 384K max output cap, including `off`, `instant`, and `fast`.
- Added `auto` thinking mode, where the model chooses the concrete thinking depth for the task.
- Restores terminal mode immediately after pickers to reduce input-not-showing issues after `/think` or `/model`.
- Prompt now asks the model to avoid decorative asterisks and heavy Markdown by default.

## v0.1.5

中文：

- 取消小输出预算限制，思考模式最大输出提升到 384K。
- 接入 DeepSeek Chat API 的真实 `thinking` 参数。
- 接入 `reasoning_effort`，深度模式使用 `high` 或 `max`。
- 文档标明每个思考模式的最大输出、API thinking 和内部思考轮数。

English:

- Removed small output-budget limits; thinking modes now scale up to 384K max output.
- Added real DeepSeek Chat API `thinking` controls.
- Added `reasoning_effort`; deeper modes use `high` or `max`.
- Documented max output, API thinking, and internal passes for every thinking mode.

## v0.1.4

中文：

- 新增更细的思考模式：`off`、`instant`、`fast`、`standard`、`balanced`、`careful`、`deep`、`deeper`、`max`、`ultra`。
- 新增真实内部思考轮次：`balanced` 及以上会先进行额外模型调用生成私有规划，再把规划作为本轮回答上下文使用。
- 修复深度思考预算被默认 `2048` max tokens 压住的问题，选择深度模式会真实切换模型和输出预算。
- 新增自动上下文压缩：接近模型上下文窗口时，保留系统提示和最近消息，把旧消息压成摘要上下文。
- 新增手动压缩命令：输入 `/` 后选择 `/compact`，可立即压缩旧上下文。
- 修复 `/` 面板滚动：选项超过可见数量时，下键会滚动，选中项始终可见。

English:

- Added finer thinking modes: `off`, `instant`, `fast`, `standard`, `balanced`, `careful`, `deep`, `deeper`, `max`, `ultra`.
- Added real internal deliberation passes: `balanced` and deeper modes make extra model calls for private planning before the final answer.
- Fixed deep thinking budgets being capped by the old default `2048` max tokens.
- Added automatic context compaction near the model context limit.
- Added manual `/compact` command from the slash palette.
- Fixed slash palette scrolling so selection remains visible.

## v0.1.3

中文：

- 修复 raw 终端下 `/` 面板斜着排版的问题：输出改用 CRLF，强制每行回到行首。
- 按键读取改成原始字节读取，避免方向键 ESC 序列被文本缓冲吞掉。
- 终端宽度裁剪严格使用真实列数，覆盖 20、42、100 列测试。

English:

- Fixed diagonal slash palette rendering in raw terminal mode by using CRLF.
- Switched key reads to raw bytes so arrow-key escape sequences are not swallowed by text buffering.
- Width clipping now uses the real terminal column count, tested at 20, 42, and 100 columns.

## v0.1.2

中文：

- `/` 面板改成左对齐竖排列表。
- 方向键解析更宽容，并支持 `j/k` 上下选择。

English:

- Reworked the slash palette into a plain left-aligned vertical list.
- Made arrow-key parsing more tolerant and added `j/k` selection.

## v0.1.1

中文：

- 新增 `version`、`update`、`update --check` 命令。
- 启动时自动检查 GitHub 最新 tag。
- 更新不会覆盖用户 API key、模型配置、技能目录、会话目录。
- 如果源码有未提交改动，更新会停止，避免覆盖用户修改。

English:

- Added `version`, `update`, and `update --check` commands.
- Added startup update checks against GitHub tags.
- Updater does not overwrite API keys, model config, skills, or sessions.
- Updater stops when local source changes exist.

## v0.1.0

中文：

- 初始开源版本。
- 支持 DeepSeek 配置、工具调用、权限模式、思考模式、技能目录、会话恢复和中英文文档。

English:

- Initial open source release.
- Added DeepSeek config, tools, permission modes, thinking modes, skills, session resume, and bilingual docs.
