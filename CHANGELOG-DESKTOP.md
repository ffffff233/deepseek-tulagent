# 桌面端更新记录 / Desktop Changelog

## v0.1.17

中文：

- **新增 6 个可启停的官方原生插件**：代码审查、测试诊断、安全审查、提交助手、更新记录和项目体检会动态进入 `/` 命令列表；插件提示词由后端可信生成，每条命令声明的权限模式和思考等级只作用于当前一轮，重试和编辑重发也不会退回全权限。
- **代码审查改为独立只读会话**：冻结上一轮或当前工作区改动，使用分页、哈希校验和失效检测读取完整差异；审查不会修改 Git 索引或工作区，未读完、快照变化或页数超限时会明确失败，不会伪造“审查完成”。
- **命令输入与消息存储重新接线**：本地斜杠命令只保存一次、在会话中可见但不进入模型上下文；官方命令、重试、取消和排队回合按 `sessionId + turnId` 隔离，避免重复回答和跨会话串线。
- **桌面界面支持完整中英文切换**：设置、扩展、动态状态、命令菜单和错误提示均可切换语言；用户消息、模型内容、代码、路径和工具输出保持原文。输入区改为克制的边框式命令输入，桌面与 390px 窄屏均无横向溢出。
- **扩展可见性补齐**：官方插件和用户插件分来源展示并独立持久化；插件提供的 Skills 会进入真实运行时和 `/` 菜单，并在插件启停或重新加载后同步刷新。`/mcp` 只连接已经配置、启用且可信的服务器，不会顺带改配置、打开设置或授予项目权限。
- **所有对外品牌统一为 DeepSeekFathom**：安装包、pip 分发、命令、界面和默认数据目录不再显示旧名称；首次启动会把历史配置、会话、Skills 和插件只补缺失地迁移到 `.deepseekfathom`。
- **升级时保留并找回历史会话**：侧边栏和加载统一兼容工作区与用户目录的新旧会话位置，同一会话选择最新有效副本；当前配置优先，旧配置只补缺失项，删除会话不会因兼容副本再次出现。
- **文件修改差异继续按真实行号展示**：删除在新增之前，删除行红色、新增行绿色、未变化上下文不着色；长差异可滚动，完整增删计数与省略状态保持可见。
- **新增 `Max` 思考等级**：位于 `Ultra` 之后，向上游原样发送 `reasoning_effort=max` 并使用最高本地推理轮次；全局设置不会被一次性插件命令覆盖。
- **构建与许可证闸门**：桌面版本由单一 Python 常量生成 EXE 属性、前端缓存标记和安装包名；构建会拒绝旧包目录、错版本前端或缺失许可证。安装包携带本项目 MIT、ReasoniX MIT、安装器翻译 MIT、Python 和打包依赖的完整许可证清单。
- CLI 同步提升到 `0.1.109`，其独立改动见 `CHANGELOG-CLI.md`。

English:

- Added six toggleable native plugins with dynamic slash commands and backend-owned prompts. Each command now enforces its declared permission mode and thinking level for that turn, including retries and edited resends.
- Added isolated, read-only AI review sessions backed by frozen, paginated, hash-verified diffs with stale detection and fail-closed completion rules.
- Persisted local slash commands exactly once while keeping them out of model context, and scoped queued/retried turn state by session and turn IDs.
- Added complete Chinese/English UI switching for static and dynamic controls while preserving user content, model output, code, paths, and tool output verbatim.
- Refined the composer, mobile layout, plugin/Skill refresh behavior, and connection-only `/mcp` semantics.
- Preserved sessions and settings across the current and compatibility data locations without overwriting newer user-owned data.
- Kept file changes as real-line-number diffs with deletions before additions, red/green changed rows, neutral context rows, scrolling, and complete counts.
- Added `Max` after `Ultra`, sending `reasoning_effort=max` upstream unchanged as the strongest deliberation tier.
- Generated all desktop version metadata from one source and added build-time checks plus complete bundled license notices.

## v0.1.16

中文：

- **MCP 已接入真实对话运行时**：同时支持本地 stdio 与 MCP `2025-06-18` Streamable HTTP，覆盖 JSON / SSE 响应、会话与协议版本请求头、会话失效单次自动重建、分页工具发现、动态参数 schema、并发调用、单工具超时、取消、重连、断开和多服务故障隔离；MCP 工具与内置工具共用原生工具调用协议和权限门控。
- **新增插件包运行时**：兼容 DeepSeekFathom、Codex、Claude 与 ReasoniX manifest，已启用插件可以提供技能、指令、MCP 服务和 Hooks；所有相对路径都经过目录逃逸与符号链接检查，安装和更新采用 staging + 原子替换，用户会话、API 配置、用户技能和其他插件不会被覆盖。
- **Hooks 生命周期可执行**：支持 SessionStart / SessionEnd、UserPromptSubmit、PreToolUse、PostToolUse、PermissionRequest、Stop、SubagentStop、PreCompact 与 PostLLMCall；项目 Hooks 默认不运行，必须显式信任；阻断事件、匹配器、超时、输出上限和失败开放边界均有测试覆盖。
- **设置页新增中文扩展管理**：MCP 页可直接新增、编辑、重命名和删除用户服务；远程模式填写名称与 URL，请求头按需逐行添加且值默认隐藏，本地模式填写命令与逐行参数，不再要求手改源码或 JSON。项目 MCP 与项目 Hooks 都必须独立显式授权；同时支持连接/重连/断开、插件启停、项目插件安装和按稳定 ID 启停单条 Hook。
- **修复无输入却重复回答**：同一批工具部分成功、部分失败时不再错误触发第二轮内部恢复；临时失败回答会在显示和保存前撤回。不同请求编号也必须原子争用同一个回合状态，WebView 重放、双击或并发桥接只能启动一个回合。
- **修复旧会话 `6e739390-d1cb-4587-bc1a-cb78a93b9658` 的重复重放**：原始 JSONL 保持不动，但界面重放和后续模型上下文会过滤旧版本留下的相邻恢复回答，不再重复显示“复制 / 重试 / 开始分支”。
- **修复工具调用时闪出 Windows 终端**：命令、Git、更新器、FFmpeg、后台服务、MCP 和 Hooks 统一使用无窗口启动；裸 `npx` 会先解析为实际 `.cmd` / `.bat` 入口，再通过隐藏 COMSPEC 运行，并清除会抵消 `CREATE_NO_WINDOW` 的冲突标志。升级时清理旧版 `DeepSeekTuLAgent` 桌面和开始菜单入口，避免误启动旧 EXE。
- **收紧 Hook 输出边界**：`PostLLMCall` 仍会执行并报告状态，但普通 stdout 不再替换模型回答；回复生成期间也不能断开 MCP 或改写插件、Hook 状态。
- **修复重试版本状态串到其他对话**：版本快照、插入标记和用户消息绑定 `sessionId + turnId`；失败、取消、完成、切换会话和新建会话都会清理对应状态，附件发送的幂等编号也在所有终止路径释放。
- **继续保留 ReasoniX MIT 版权证明**：MCP 生命周期、插件清单归一化、Hook 信任/超时模型的参考来源与提交号写入 `NOTICE`，安装包继续携带完整 MIT 文本；实现针对 DeepSeekFathom 接口独立重写。
- CLI 发行版本继续保持 `0.1.108`，本次只提升桌面端到 `0.1.16`。

English:

- Integrated local stdio and MCP 2025-06-18 Streamable HTTP runtimes with JSON/SSE responses, bounded session recovery, dynamic native tool contracts, pagination, concurrent calls, timeouts, reconnect/disconnect, isolation, redacted diagnostics, and process-tree cleanup.
- Added safe plugin discovery, manifest compatibility, atomic install/update, and runtime contributions for skills, instructions, MCP servers, and Hooks without touching user sessions or settings.
- Added trusted lifecycle Hooks with blocking boundaries, matchers, timeouts, bounded output, hidden Windows execution, and temporary SessionStart context.
- Added Chinese MCP/plugin/Hook management views, including in-app user MCP create/edit/rename/delete, remote URL and repeatable secret-header fields, local command arguments, separate explicit project trust, stable per-Hook controls, and runtime mutation guards.
- Prevented unsolicited duplicate answers with atomic turn claims, made sends idempotent, filtered legacy adjacent recovery replies during replay/context construction, and scoped retry-version state to session and turn IDs.
- Eliminated visible Windows child consoles across tools, services, media probes, updates, MCP, and Hooks, including bare `npx` batch resolution, conflicting creation-flag removal, and stale legacy-shortcut cleanup.
- Kept `PostLLMCall` observable without allowing ordinary Hook stdout to replace the assistant answer.
- Preserved the full ReasoniX MIT attribution and provenance in `NOTICE` and the Windows installer.

## v0.1.15

中文：

- **OpenAI / DeepSeek 原生工具调用**：Chat Completions 请求会发送当前权限允许的工具 schema，支持流式和非流式 `tool_calls`，同一轮可顺序执行多个工具；不支持原生工具的接口继续使用文本协议兜底，原始参数不会先作为普通回复显示。
- **技能现在会真正进入工作流**：新增只读 `list_skills` 与 `read_skill`；固定前缀只保留有字符预算的名称/描述索引，正文、`references/*.md` 和脚本清单仅在调用时加载。技能发现兼容 `.deepseek-tulagent`、`.agents`、`.agent`、`.claude` 和项目 `skills` 目录。
- **自动加载项目指令**：用户级、Git 项目层级和本地覆盖的 `REASONIX.md`、`AGENTS.md`、`CLAUDE.md` 会按确定顺序加载，按物理文件去重并限制单文件/总提示词体积；诊断页展示真实加载状态和 token 成本，不再报“尚未加载”。
- **旧对话同步到当前运行规则**：继续旧会话时会刷新当前系统提示、项目指令和技能索引；上下文压缩会保留这些稳定前缀与已加载技能，不再只保留第一条 system 消息。自动压缩改用上游返回的完整上下文快照，并按完整工具调用组切分，短消息列表也能在真实 token 超限时触发压缩。
- **修复快速切换会话串线**：前后端共同使用递增导航序号，过期的慢请求不能覆盖最后点击的会话，也不能让下一次发送写入错误对话。
- **长回复与侧边栏明显减负**：后端合并高频流式片段后再送到界面，前端按浏览器帧批量更新纯文本，最终只做一次 Markdown/高亮/公式渲染；会话列表使用轻量索引和文件签名缓存，不再每 5 秒反复解析全部 JSONL 与图片 Base64；恢复长对话只滚动一次。
- **侧栏滚动条按真实内容工作**：会话行固定高度且不再被布局压缩，自定义滑块与原生滚动位置双向同步，长对话目录可以一直滚到真正的最后一项。
- **权限和失败状态更严格**：子代理只能继承或降低父代理权限，不能从 `plan` / `agent` 提升到 `root`；失败的 `apply_patch` 不再携带绿色成功差异，界面明确显示“修改失败”。
- **大差异与上下文统计更准确**：超长 diff 保留首尾并展示完整增删计数和省略行数；补充 Gemini `cachedContentTokenCount`，当前窗口优先采用包含推理 token 的上游总量。
- CLI 发行版本继续保持 `0.1.108`，本次只提升桌面端到 `0.1.15`。

English:

- Added native OpenAI/DeepSeek tool calls, streaming assembly, and multiple calls per model round with text fallback.
- Added on-demand `list_skills` / `read_skill`, broader skill conventions, reference loading, and script discovery.
- Loaded hierarchical project instruction files with deduplication, safety budgets, diagnostics, and legacy-session refresh.
- Preserved runtime instructions and loaded skills across compaction, using upstream context snapshots and complete tool-call groups.
- Prevented stale session navigation, subagent permission escalation, and false-success patch diffs.
- Reduced streaming and sidebar costs with backend chunk batching, two-phase rendering, lightweight session indexes, and a correctly mapped long-list scrollbar.
- Added balanced large-diff truncation and Gemini cache/total-token accounting.

## v0.1.14

中文：

- **设置页新增只读能力诊断**：静态读取当前工作区的技能、技能搜索路径和工具契约，不联网、不启动外部进程，也不修改配置；报告中的路径会脱敏为 `<workspace>`、`~` 或 `<external>`。
- **同名技能不再静默消失**：诊断页会同时展示生效技能与所有被覆盖候选，并明确生效文件路径和搜索优先级。
- **创建技能禁止覆盖已有文件**：`skills new` 或后续界面创建技能时，已有 `SKILL.md` 会直接拒绝写入，用户自己添加的技能不会被重新创建或更新流程覆盖。
- **新增确定性工具契约快照**：17 个当前工具按固定名称顺序展示参数 JSON schema、只读状态、审批/禁用门控，以及 schema 与固定提示词的 token 成本估算；测试会阻止运行时、提示词和契约列表漂移。
- **明确未接入能力**：MCP、插件包和 Hooks 当前显示“尚未集成”，不会伪装成健康；检测到 `AGENTS.md` / `REASONIX.md` / `CLAUDE.md` 时也会提示当前运行时尚未自动加载。
- **继续保留 Reasonix MIT 版权证明**：`NOTICE` 增加技能覆盖、工具契约、路径脱敏和能力诊断设计的参考说明，实现仍为针对本项目接口的独立重写。
- CLI 发行版本继续保持 `0.1.108`，本次只提升桌面端到 `0.1.14`，不新增 CLI 命令。

English:

- Added static, read-only capability diagnostics to Desktop Settings.
- Reported skill winners, shadowed candidates, discovery roots, and priority instead of silently dropping duplicates.
- Refused to overwrite an existing user `SKILL.md` when creating a skill.
- Added deterministic contracts for all 17 current tools, including schemas, permission gates, and prompt-cost estimates.
- Reported MCP, plugin packages, Hooks, and unloaded instruction documents explicitly instead of fabricating healthy states.
- Extended Reasonix MIT provenance while keeping the implementation independently written.

## v0.1.13

中文：

- **上下文占用改为上游完整快照**：当前窗口使用最后一次执行模型的真实 `prompt + completion`，不再只显示输入，也不会拿会话累计 token 冒充当前上下文；旧版 v2 快照会自动补上真实输出 token 后迁移。
- **缓存判断拆分为本次与会话平均**：统一解析 DeepSeek 顶层 hit/miss、OpenAI `cached_tokens`、Anthropic cache read/create 等格式；缓存率严格按 `hit / (hit + miss)` 计算，上游未提供缓存字段时显示“未上报”，不再伪造 0%。
- **修复 14 万缓存提示词被显示成 1.2K 的兼容网关形态**：当网关把 `prompt_tokens` 只返回为新增部分、另把缓存前缀单独返回时，会重建完整输入为 `cached + new`；只返回 hit 没返回 miss 时也会可靠推导新增 token。
- **本地增量使用真实 tokenizer 校准**：完成一次有可靠 usage 的请求后，记录上游输入与本地估算的比例；用户继续追加尚未发送的消息时，按该模型最近一次比例估算，而不是固定字符公式。
- **上下文面板新增输出、缓存命中/新增和会话平均缓存**：缓存数值使用绝对 token 与两位小数命中率，弹层支持滚动和长数字换行，不会在小窗口重叠。
- **保留 Reasonix 版权与来源证明**：上下文/缓存遥测模型参考 `esengine/DeepSeek-Reasonix` 的 MIT 设计，`NOTICE` 记录仓库、参考提交 `78e9e265...`、完整 MIT 文本与独立重写说明，并随 Windows 安装包分发为 `NOTICE.txt`。

English:

- **Changed context usage to the full upstream prompt-plus-completion snapshot**, keeping current-window occupancy separate from cumulative session usage and migrating v2 metadata safely.
- **Separated per-request and session-average prompt-cache telemetry**, normalizing DeepSeek, OpenAI-compatible, Anthropic, and Gemini usage while leaving cache rate unknown when the provider reports no cache fields.
- **Reconstructed cache-heavy gateway usage correctly** when prompt tokens contain only the fresh tail or only cache hits are reported.
- **Calibrated unsent local context deltas against the latest reliable upstream tokenizer ratio** instead of relying on a fixed character heuristic.
- **Expanded the context panel** with exact output, cache hit/new splits, session-average cache rate, responsive wrapping, and scrolling.
- **Preserved Reasonix MIT attribution and provenance** in the repository and installed `NOTICE.txt`.

## v0.1.12

中文：

- **普通 Markdown 不再误触发工具状态**：反引号、代码围栏和未闭合的普通 JSON 只作为临时流式边界；确认不是工具后会继续实时显示，不再从第一段行内代码开始卡住并显示“准备调用工具”。
- **普通配置 JSON 不再被 `name` 字段误判**：只有 `tool` / `name` 的值是已知工具名，或出现明确的结构化工具协议时，才会永久扣留并创建工具状态；`package.json`、应用配置和代码示例可以正常流式展示。
- **扩大本机操作证据覆盖**：“做一个”“生成”“制作”以及 `build` / `make` 等自然表达也必须等真实工具成功后才能宣称完成；同时识别“创建成功/生成完成/写入成功”等更多完成说法，并正确区分“未创建成功”。

English:

- **Stopped ordinary Markdown from falsely entering tool-pending state**, keeping backticks, code fences, and ambiguous JSON boundaries provisional until classified.
- **Stopped normal configuration JSON from being treated as a tool merely because it starts with `name`**, requiring a known tool name or an explicit structured protocol marker before locking the stream.
- **Expanded local-action evidence checks** to natural create/build wording and more completion phrases while preserving negative statements such as “not created successfully”.

## v0.1.11

中文：

- **本机操作必须有真实工具结果才能宣称完成**：明确要求在桌面、本机或具体文件路径创建、写入、修改、删除、下载、安装或运行时，模型的无依据“已创建/已完成”不会再显示或写入会话；客户端会自动要求模型立即调用工具，连续不执行时改为明确报告未执行。
- **工具参数从协议起点开始锁定扣留**：流式输出一旦出现 JSON、DSML、工具标签或工具代码围栏边界，本轮后续内容永久停在该边界，直到最终确认；带说明前缀、超长 HTML、逐字符分片和整段分片都不会先显示参数、再迟到替换成工具卡。
- **加入真实故障形态回归测试**：使用“桌面创建小游戏 HTML”的虚假完成回复和超长 `write_file` 参数，验证工具卡在执行前出现、文件真实写入后才允许确认，且聊天增量和最终气泡均不包含原始工具参数。

English:

- **Required successful tool evidence before reporting local actions as complete**, suppressing unsupported create/write/modify/delete/download/install/run claims and automatically recovering into a real tool call.
- **Locked streaming output at the first detected tool-protocol boundary**, preventing prefaced or very large JSON/DSML arguments from flashing before the tool card for both character-split and single-chunk responses.
- **Added a regression matching the reported desktop HTML creation failure**, verifying real file creation, event order, and complete protocol redaction.

## v0.1.10

中文：

- **新增 Markdown 会话导出**：对话菜单加入“导出 Markdown”，使用 Windows 原生另存为窗口并原子写入文件；导出内容包含标题、会话 ID、创建时间、用户/助手消息、工具参数摘要、执行状态和结果。
- **导出内容遵循界面可见边界**：系统提示词、原始 DSML / JSON 工具协议、内部 `TOOL_RESULT` 以及图片 Base64 数据不会写入文件；图片只记录数量，旧版遗留的无结果工具调用明确标记为未执行。
- **加固导出文件与并发边界**：自动清理 Windows 文件名非法字符和保留名，支持内容本身包含 Markdown 围栏；取消另存为不会创建文件，正在生成的目标会话会拒绝导出，避免保存半截回复。

English:

- **Added native Markdown conversation export** with an atomic Save-dialog workflow covering visible user/assistant messages, tool summaries, statuses, and results.
- **Kept exports inside the visible transcript boundary**, excluding system prompts, raw DSML/JSON protocol, internal tool-result records, and Base64 image payloads while retaining image counts.
- **Hardened filename and concurrency handling** for Windows reserved names, embedded Markdown fences, cancelled dialogs, and active generations.

## v0.1.9

中文：

- **修复旧会话继续污染模型上下文**：历史版本遗留的未执行 DSML / JSON 工具调用和孤立 `TOOL_RESULT` 不再重复发送给模型；原始会话文件保持不变，界面会把缺少结果的工具卡明确标成“没有执行结果，已按未执行处理”。
- **运行时选择会在重启后保留**：模型、权限模式和思考强度切换后立即原子合并到用户配置，不再每次启动恢复旧默认值。
- **修正停止按钮与真实请求的时序**：只有后端返回有效 `turnId` 后才显示停止按钮；取消、切换或新建会话会清理旧请求标识，避免出现点了却无法停止的假按钮。
- **Windows 桌面端改为单实例**：重复启动会唤醒已有 DeepSeekFathom 窗口，不再同时运行多个进程争用会话和设置文件。
- **统一 Windows 版本信息**：界面、安装器、EXE 文字版本与固定数字版本全部更新为 `0.1.9`，修复固定版本元组长期停留在 `0.1.5` 造成的属性页版本异常。

English:

- **Stopped damaged legacy tool records from poisoning future model context** while preserving source JSONL files and marking result-less tool cards as not executed.
- **Persisted runtime model, permission, and thinking selections** through atomic configuration merges so restarts keep the user's choices.
- **Tied the Stop control to a real backend turn id**, clearing stale ids on cancellation and session changes.
- **Made the Windows desktop app single-instance**, focusing the existing DeepSeekFathom window on duplicate launches.
- **Unified UI, installer, string, and fixed numeric Windows versions at `0.1.9`**, correcting the stale `0.1.5` executable tuple.

## v0.1.8

中文：

- **修复取消后排队回复的会话串线**：旧请求结束时不再把后端当前会话强行切回排队会话；即使连续切换多个对话，排队回复仍写入原目标会话，后续发送也不会误落到别的对话。
- **排队中的回复现在可以真正停止**：停止按钮会按 `turnId` 移除尚未启动的回复，并阻止删除仍有回复排队的会话；迟到的旧请求事件按请求隔离，不再重复显示取消或污染当前界面。
- **设置保存改为并发安全的原子合并**：API、模型、上下文等设置同时保存时不再互相覆盖，临时文件使用唯一名称并在失败后清理。
- **支持恢复自动 API 地址与上下文窗口**：清空基础地址会使用当前接口格式的默认域名，清空模型上下文会恢复自动识别，清空压缩阈值会恢复 95%；保存这些设置不再重置当前选择的模型。
- **侧栏跳过非法旧会话文件**：会话目录中的异常文件名不会再导致整个对话列表加载失败。

English:

- **Prevented queued turns from stealing the active conversation after cancellation and multiple session switches**, keeping every queued reply bound to its original session.
- **Made queued replies genuinely cancellable by turn id**, blocked deletion while a reply is queued, and isolated late terminal events to prevent duplicate cancellation UI.
- **Made configuration merges atomic and concurrency-safe** with unique temporary files and cleanup on failure.
- **Allowed clearing custom API and context settings to restore automatic defaults** without resetting the currently selected model.
- **Skipped invalid legacy session filenames** instead of failing the entire sidebar conversation list.

## v0.1.7

中文：

- **重试与编辑重发改为可回滚事务**：原会话不再提前截断，替换回复先在内存会话中生成，成功后才一次性原子写回；模型报错、用户取消或进程中断时，原始 JSONL 保持不变，界面会立即恢复原问题、回复和工具卡片。
- **重试和分支完整保留图片上下文**：带截图或图片的用户消息重试时会把原图重新发送给模型；从任意回复创建分支时，历史消息中的图片数据也会一起复制，不再出现文字还在但视觉上下文消失。
- **阻止生成期间删除或压缩活动会话**：侧栏和会话菜单现在会显示后端拒绝原因，避免后台写入与删除竞争造成“删掉后又出现”或会话文件被重建。
- **加固会话存储边界与元数据并发更新**：会话 ID 只允许安全文件名字符，不能通过路径分隔符逃出 `sessions` 目录；标题、置顶、上下文 usage 等并发更新使用进程内锁合并，避免字段互相覆盖。

English:

- **Made retry and edit-resend transactional and rollback-safe**: replacement turns run in memory and atomically replace JSONL only after success. Provider errors, cancellation, or interruption leave the original conversation untouched and restore it in the UI.
- **Preserved image context across retries and branches**, resending original user images to the model and copying image payloads into forked conversation history.
- **Blocked deletion and manual compaction of an actively generated conversation**, preventing write/delete races and exposing the backend rejection in both delete entry points.
- **Hardened session storage boundaries and metadata concurrency** with safe session ID validation and locked read-merge-write updates for titles, pins, context usage, and other metadata.

## v0.1.6

中文：

- **完整恢复长会话并容忍局部损坏**：桌面端不再只显示最后 320 条消息；JSONL 中单行损坏时会跳过该行并保留其余对话，会话重写和元数据更新均使用唯一临时文件并保证清理，降低并发或异常退出造成的丢失风险。
- **附件保存不再覆盖已有文件**：浏览器兼容上传严格校验 Base64 并限制为 32 MB；同名附件自动编号，网络附件采用临时文件原子落盘、优先使用服务端文件名，并保留原始 URL；失败或超限不会删除旧文件。
- **改进 Windows 命令与终端兼容性**：模型生成 POSIX 命令时优先交给 Git Bash，没有 Git Bash 时使用 PowerShell 兼容层；原生 PowerShell 命令保持原语义，CLI/TUI 的管道和键盘输入等待在 Windows 上不再依赖不支持的 `select` 行为。
- **加固文件写入工具**：支持创建空文件，拒绝把目录当文件覆盖，使用唯一临时文件并在失败后清理；`"..."` 和 `"…"` 这类示例占位路径不会再被 Windows 解析成用户工作目录。系统提示同时明确正文格式偏好不得篡改 CSS `*` 选择器、glob 或代码语法。

English:

- **Restored complete long conversations and tolerated isolated JSONL corruption** by removing the 320-message transcript cutoff, skipping only malformed rows, and using uniquely named, cleaned-up temporary files for session and metadata rewrites.
- **Prevented attachment overwrites** with strict Base64 validation, a 32 MB browser-upload cap, automatic same-name numbering, atomic network downloads, server-provided filenames, source URL retention, and failure cleanup that preserves existing files.
- **Improved Windows command and terminal compatibility** by routing POSIX commands through Git Bash or a PowerShell compatibility layer while preserving native PowerShell behavior and supporting pipe/console input without unsupported `select` calls.
- **Hardened file writes** with empty-file support, directory-target rejection, unique atomic temporary files, and explicit rejection of ellipsis placeholder paths that Windows can otherwise resolve to the user's working directory. Prose formatting guidance can no longer alter CSS `*` selectors, globs, or code syntax.

## v0.1.5

中文：

- **修复工具参数偶尔作为 Markdown JSON 泄露到对话**：流式解析现在会缓存逐字符到达的反引号和未完整的 `json` 围栏；后端确认内容为工具调用后，界面会无条件移除临时气泡，只保留真实工具卡片。
- **支持 DeepSeek 原生 DSML 工具调用并阻止示例误执行**：`DSML tool_calls / invoke / parameter` 会转换为真实工具卡片并正常执行，多行 HTML 等参数不再整段露出；带“正确格式、比如、示例”等解释语境的 JSON 代码块只展示，不会再把 `"..."` 当路径误写文件。
- **修复模型承诺读取附件后停住**：对“让我先读取附件”这类明确要执行本机操作却未发出工具调用的短回复，自动隐藏中间话并续跑一次，不再要求用户再催一句。
- **修复从 Windows 桌面拖入文件时丢失原路径**：接入 pywebview/WebView2 的原生拖放路径桥，直接引用本机原文件，不再因为浏览器 `File` 对象缺少路径而复制到上传目录；浏览器兼容回退会等待原生路径结果，避免重复附件和错误引用。

English:

- **Prevented tool arguments from leaking as Markdown JSON into chat** by buffering character-split fence prefixes and always removing the temporary streamed bubble once the backend confirms a tool call.
- **Added native DeepSeek DSML tool-call support and blocked explanatory examples from executing**, so multiline HTML parameters become real tool calls while JSON shown after phrases such as “correct format” or “for example” remains display-only.
- **Continued automatically after short unfulfilled action promises** such as “let me read the attachment first,” removing the placeholder reply instead of waiting for another user nudge.
- **Preserved original Windows paths for files dragged from Explorer or the desktop** through pywebview's native WebView2 drop bridge, with a coordinated browser fallback that avoids duplicate uploads and incorrect attachment references.

## v0.1.4

中文：

- **修复安装包夹带旧版前端资源**：PyInstaller 现在强制从当前源码目录打包界面，不再从虚拟环境中的旧包收集资源；解决 EXE 属性为 `0.1.4`、界面却回退到 `0.1.2`，并连带看不到当前会话目录的问题。CSS / JS 同时加入版本缓存标记。
- **本机附件改为路径直传**：点击 `+` 使用原生文件选择器，只记录本机绝对路径和大小，不再把文件转成 Base64 后复制到上传目录；拖拽能取得本机路径时同样直传。浏览器拿不到路径的兼容拖拽限制为 32 MB，超限直接提示而不是崩溃；网络 URL 拖入采用最大 100 MB 的流式下载。
- **新增始终可见、可拖动的会话滑块**：不再依赖 WebView 覆盖式系统滚动条；支持拖动滑块、点击轨道跳转，并移除只渲染前 40 条会话的截断，滚动到底可以看到真正的最后一条。
- **修正上下文 usage 的缓存 token 解析**：支持 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`、OpenAI 缓存明细和 Anthropic 缓存输入；例如上游返回 `1,236` 未缓存输入和 `140,000` 缓存输入时，当前上下文会显示约 `141K`，不再误报 `1.2K`。旧版不可信快照不会继续冒充实测值；上游数字小于实际发送内容时会明确标为“上游少报”。
- **思考强度改为四个真实档位**：桌面端只显示 `Low / Medium / High / XHigh`，移除 `None` 和 `Minimal`；OpenAI 分别发送 `low / medium / high / xhigh`，DeepSeek 仅发送其原生思考开关，Anthropic / Gemini 按档位换算原生预算。
- **修复启动卡在“启动中”或窗口未响应**：原生 pywebview 窗口不再作为公开后端属性被递归扫描，避免 WebView2 在桥接初始化时陷入原生对象递归；上游模型列表仍在启动后异步刷新，不阻塞界面初始化。
- **修复取消后并发请求导致卡死**：停止回复会主动关闭当前 HTTP 连接；旧工作线程完全退出前，新消息只进入队列，不会出现两个线程同时写同一会话。
- **缩短并细分接口超时**：默认读取超时从 180 秒调整为 60 秒，连接阶段最多等待 10 秒；设置页可在 15–300 秒范围内调整。
- **修复侧栏滚动条到底但对话未显示完**：底部设置入口改为侧栏真实布局行，不再覆盖会话列表，滚动范围与最后一条对话位置一致。
- **浅色主题对齐 Codex**：使用本机 Codex 实测的 `#F6F6F6` 主背景、白色内容层和中性灰边界，避免大面积纯白刺眼。

English:

- **Fixed installers bundling stale frontend assets** by forcing PyInstaller to package the current checkout instead of an older site-packages copy, eliminating 0.1.4 executables that displayed a 0.1.2 UI and stale conversation behavior. CSS and JS URLs are versioned to invalidate cache.
- **Changed local attachments to path-based selection**: the `+` button uses the native picker without Base64 copying, path-aware OS drops stay local, fallback browser uploads are capped at 32 MB, and dragged web URLs stream to disk with a 100 MB cap.
- **Added an always-visible draggable conversation scrollbar**, including track clicks, and removed the 40-session rendering cap so the final row is genuinely reachable.
- **Fixed cached-token context accounting** for DeepSeek-compatible cache hit/miss fields, OpenAI cached-input details, and Anthropic cache input. Legacy untrusted snapshots are discarded, and under-reported upstream usage is labeled instead of presented as exact.
- **Replaced desktop reasoning choices with four real tiers: `Low / Medium / High / XHigh`**, removing `None` and `Minimal` while translating each tier to the provider's native parameter shape.
- **Fixed startup hangs and unresponsive windows** by hiding the native pywebview Window from recursive JS API exposure while retaining the asynchronous upstream model refresh.
- **Prevented post-cancel request races** by closing the active HTTP client and queueing new turns until the previous worker fully exits.
- **Reduced and split API timeouts**, with a 60-second default read timeout, 10-second connect timeout, and a 15–300 second setting.
- **Fixed the conversation scrollbar ending before the final rows were visible** by giving Settings its own sidebar layout row instead of overlaying the list.
- **Matched Codex's light theme hierarchy** with a measured `#F6F6F6` canvas, white content surfaces, and neutral gray separators.

## v0.1.3

中文：

- **设置入口移到左侧栏底部，并改为完整设置页面**：设置不再弹出模态框，页面顶部和底部均可返回对话；API 格式、Base URL、API Key 和连接测试集中在该页面。
- **新增黑色 / 柔和浅白主题切换**：默认使用黑色主题，浅白主题避免纯白大底刺眼，选择会保存在本机并在下次启动时恢复。
- **修复启动阶段短暂显示 `v0.0.0`**：界面资源直接携带当前桌面版本，后端完成初始化后再同步真实运行信息。
- **文件写入改为专用差异卡片**：使用笔形图标和文件路径替代通用“工具调用”；修改内容按 Codex 统一 diff 的“删除块在上、新增块在下”展示，并显示真实旧 / 新行号。所有行共享同一内容宽度，红绿背景在横向滚动时始终齐平；长差异支持横向和纵向滚动，历史会话恢复后仍可展开查看。
- **修复长会话目录无法滚动**：会话列表拥有独立滚动区域，底部设置入口始终可见。
- **按屏幕 DPI 和可用工作区调整启动窗口高度**：高缩放或低分辨率设备上，左下角设置与底部输入框不再被任务栏遮挡。
- **修复桌面升级后用户数据消失**：打包版默认把会话、配置和用户技能保存在安装目录之外；首次启动会从旧安装目录增量迁移数据，已有用户文件绝不覆盖。
- **区分官方技能与用户技能**：官方技能随程序包更新，用户自建技能保存在用户目录并拥有同名优先级，升级不会覆盖。
- **重新生成透明鲸鱼图标资源**：EXE、ICO 与应用内 PNG 继续只保留鲸鱼本体，周围保持透明。

English:

- **Moved Settings to the bottom of the sidebar and turned it into a full application page**, with back controls at both the top and bottom and all API controls in one place.
- **Added persistent dark/soft-light themes**, with dark remaining the default and the light palette avoiding a harsh pure-white canvas.
- **Removed the misleading `v0.0.0` startup placeholder** by embedding the current desktop version in the initial UI.
- **Added dedicated file-change cards** with a pen icon, file path, replayable real line numbers, Codex-style removed-then-added blocks, equal-width row backgrounds, and independent scrolling for long changes.
- **Made long conversation lists independently scrollable** while keeping Settings anchored at the bottom.
- **Sized the startup window from the display DPI and available work area**, keeping bottom controls above the taskbar.
- **Protected user data across desktop upgrades** by moving packaged-app storage outside the install directory and migrating legacy data without overwriting existing files.
- **Separated bundled and user skills** so bundled skills may update while user-created skills remain untouched and take precedence on name conflicts.
- **Regenerated the transparent whale assets** used by the EXE, ICO, and in-app UI.

## v0.1.2

中文：

- **软件内左上角和新会话空白页改用专属透明鲸鱼图标**，移除旧波浪 SVG、图标底色和文字占位标记，只显示鲸鱼本体。
- **上下文面板新增“当前请求输入”和“会话累计输入”**。当前上下文继续表示最后一次模型请求实际携带的输入，会话累计输入则展示同一会话多轮工具调用产生的累计提示词 token，避免把两者混为一谈。
- **会话累计 usage 写入会话元数据**，重启或切换会话后仍保留；拆分显示可直接看出为什么一次任务累计消耗十几万 token，而最后一次请求上下文可能较小。

English:

- **Replaced the in-app top-left mark and new-session placeholder with the transparent whale icon**, removing the old wave SVG, icon background, and text-only mark.
- **Added separate “current request input” and “session cumulative input” metrics**. Current context remains the latest model-request input, while cumulative input shows prompt tokens spent across all model/tool rounds in the session.
- **Persisted cumulative session usage in metadata**, so both figures survive app restarts and session switches.

## v0.1.1

中文：

- **修复恢复会话后上下文从真实上游输入退回 `1.4K` 本地估算的问题**。最后一次上游 usage 和对应本地消息基线现在会原子写入会话元数据，重启或切换会话后仍能恢复真实输入 token。
- **有新消息但上游暂未返回 usage 时，沿用上次实测基线并按本地消息增量校正**，不再直接丢弃已知的上游输入规模。
- **缺少上游 usage 时不再显示不准确的上下文数字和百分比**。界面直接显示“上下文未知”，并把 `1.4K` 之类的数字单独标为“本地可见消息”，明确不含网关注入提示词；获得实测值后自动切回“上游实测”。

English:

- **Fixed restored sessions falling back from real upstream input usage to a `1.4K` local estimate**. The latest upstream usage and matching local-message baseline are now atomically persisted in session metadata.
- **When a new turn has not returned usage yet, the meter keeps the last measured baseline and adjusts it by the local message delta** instead of discarding known upstream overhead.
- **Missing upstream usage no longer produces a misleading context number or percentage**. The UI shows “context unknown” and labels values such as `1.4K` only as local visible messages that exclude gateway-injected prompts.

## v0.1.0

中文：

- **桌面端建立独立版本线**，从 `0.1.0` 开始，不再延续 CLI 已累计的版本号；安装包名称改为 `DeepSeekFathom-0.1.0-Setup.exe`。
- **软件名称、窗口标题、安装目录、桌面入口、开始菜单和卸载项统一为 `DeepSeekFathom`**。
- **采用透明背景蓝色鲸鱼图标**，EXE、桌面入口和应用内图标保持一致；桌面入口使用标准 Windows `.lnk`。
- **提供简体中文 Windows 安装程序**，按当前用户安装到 `%LOCALAPPDATA%\Programs\DeepSeekFathom`，无需管理员权限。
- **修复上下文占用显示**，优先采用上游输入 token 并按当前会话增量校正，改进中文和图片估算。
- **自动与手动上下文压缩会原子写回 JSONL**，重启或切换会话后保持压缩结果。
- **仓库和支持链接改为 `ffffff233/DeepSeekFathom`**；桌面发布使用独立标签 `desktop-vX.Y.Z`。

English:

- **Started an independent desktop version line at `0.1.0`**, separate from the accumulated CLI version. The installer is now named `DeepSeekFathom-0.1.0-Setup.exe`.
- **Unified the product name** across the window title, install directory, desktop entry, Start menu, and uninstall entry as `DeepSeekFathom`.
- **Added the transparent blue whale icon** consistently to the EXE, desktop entry, and app UI. The desktop entry remains a standard Windows `.lnk`.
- **Added a Simplified Chinese per-user Windows installer** targeting `%LOCALAPPDATA%\Programs\DeepSeekFathom` without requiring administrator privileges.
- **Fixed context usage reporting** using upstream input tokens plus the current-session delta, with better CJK and image estimates.
- **Persisted automatic and manual compaction atomically to JSONL**, so compacted history survives restarts and session switches.
- **Updated repository and support links to `ffffff233/DeepSeekFathom`**. Desktop releases now use independent `desktop-vX.Y.Z` tags.

桌面端在独立版本线之前的开发记录保留在 [历史联合更新记录](CHANGELOG-LEGACY.md) 中，其中桌面入口最早加入于原联合版本 `v0.1.33`。

Desktop development before this independent version line remains in the [legacy combined changelog](CHANGELOG-LEGACY.md), where the desktop entrypoint first appeared in combined release `v0.1.33`.
