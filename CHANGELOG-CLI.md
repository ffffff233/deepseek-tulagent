# CLI 更新记录 / CLI Changelog

## v0.1.109

中文：

- **CLI 与桌面端扩展运行时对齐**：同一套 MCP、官方/用户插件、Hooks、插件 Skills 和插件指令会进入终端会话；可从 `/` 菜单查看状态、连接已配置的 MCP，并让动态 MCP 工具遵守当前权限与审批边界。
- **文件写入和补丁直接展示逐行差异**：删除行红色、新增行绿色、未变化上下文行无底色，删除块排在对应新增块之前；显示真实旧/新行号、文件路径、完整增删计数和截断提示，Windows 与 Linux 终端共用同一差异协议。
- **重做 Windows 与 Linux 命令输入**：使用 Prompt Toolkit 提供中文编辑、光标移动、历史、终端缩放、Ctrl+C / Ctrl+D 和紧凑的两列斜杠菜单；初始化异常时保留原生控制台/termios 降级路径，重定向输出统一为 UTF-8。
- **修复真实 Windows 终端中的斜杠输入**：移除会把输入区撑到屏幕底部的状态栏，`/` 命令与说明保持可见；连续输入 `//` 后退格会正确回到 `/` 并恢复菜单，不再卡住。
- **CLI 品牌统一为 DeepSeekFathom**，帮助、标题、更新与恢复提示不再出现旧名称；命令输入保持单一、克制的终端编辑区。
- **pip 分发名统一为 `deepseekfathom`**：只安装 `deepseekfathom` 与 `deepseekfathom-desktop` 两个命令入口；配置、会话、Skills 和插件会安全迁移到 `.deepseekfathom`，迁移只补缺失文件，不覆盖用户内容。
- **安全清理旧 pip 分发**：只在旧版 `0.1.108` 的名称、入口、路径和元数据全部精确匹配时删除旧包与四个旧别名，并离线原子修复两个 DeepSeekFathom 入口；Windows 使用隐藏助手，不再闪出终端，Linux 同步支持。
- **新增 `Max` 思考等级并排在 `Ultra` 之后**：`Max` 向上游原样发送 `reasoning_effort=max` 并使用最高本地推理轮次；`minimal` 与 `none` 不再出现在可选列表。

English:

- Brought MCP, official/user plugins, Hooks, plugin Skills, and plugin instructions into the same CLI runtime used by Desktop, with status commands, connection controls, dynamic tools, and permission enforcement.
- Rendered write/patch operations as direct line-by-line diffs with real old/new line numbers, deletions before additions, red/green changed rows, neutral context, counts, paths, and truncation notices on Windows and Linux.
- Rebuilt Windows and Linux input with Prompt Toolkit for Chinese editing, cursor movement, history, resize handling, and a compact two-column slash menu, while preserving native console/termios fallbacks.
- Fixed real Windows terminal slash editing so `//` can be deleted back to `/`, the command menu returns immediately, and the composer no longer reserves an empty full-screen toolbar area.
- Unified CLI branding as DeepSeekFathom and added `Max` after `Ultra`, sending `reasoning_effort=max` upstream unchanged.
- Renamed the pip distribution to `deepseekfathom`, removed legacy command entry points, and migrated user data safely into `.deepseekfathom` without overwriting existing files.
- Added an exact-match, offline migration that removes the old 0.1.108 distribution and four aliases while atomically preserving the two DeepSeekFathom launchers on Windows and Linux.

## v0.1.108

中文：

- **CLI 主启动命令改为 `deepseekfathom`**，帮助、会话恢复和更新提示统一使用新命令。
- **仓库正式更名为 `ffffff233/DeepSeekFathom`**，CLI 自动检查更新、源码更新和安装文档全部切换到新地址。
- **Python 分发标识 `deepseek-tulagent` 继续保留用于无冲突升级**。旧 CLI 入口暂时作为兼容别名保留，现有 `.deepseek-tulagent` 配置、会话和技能目录继续沿用，升级不会丢失用户数据。
- **CLI 和桌面端版本正式拆分**。CLI 延续原版本线到 `0.1.108`，不再跟随桌面安装包版本变化。
- **修正 OpenAI 推理强度映射**：`fast / balanced / deep / ultra` 分别映射为 `low / medium / high / xhigh`；DeepSeek 不再收到它不支持的 `reasoning_effort` 字段，Anthropic 和 Gemini 继续换算为各自的原生预算参数。

English:

- **Changed the primary CLI command to `deepseekfathom`**, including help, session-resume, and update messages.
- **Renamed the repository to `ffffff233/DeepSeekFathom`**, updating the CLI release checker, source updater, and installation documentation.
- **Preserved the `deepseek-tulagent` Python distribution identifier for conflict-free upgrades**. Legacy CLI aliases remain temporarily for compatibility, while existing `.deepseek-tulagent` config, session, and skill directories are preserved.
- **Separated CLI and desktop versions**. The CLI continues the original line at `0.1.108` and no longer follows desktop installer releases.
- **Corrected OpenAI reasoning-effort mappings** so `fast / balanced / deep / ultra` map to `low / medium / high / xhigh`; DeepSeek no longer receives an unsupported `reasoning_effort` field, while Anthropic and Gemini use native budget parameters.

早期 CLI 与桌面端共用版本号，原始记录见 [历史联合更新记录](CHANGELOG-LEGACY.md)。

Earlier CLI and desktop releases shared one version number. See the [legacy combined changelog](CHANGELOG-LEGACY.md).
