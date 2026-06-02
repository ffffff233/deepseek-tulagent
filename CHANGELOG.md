# 更新记录 / Changelog

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

