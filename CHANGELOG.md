# 更新记录 / Changelog

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
