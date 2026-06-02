# 更新记录 / Changelog

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
