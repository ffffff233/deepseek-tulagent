# CLI 更新记录 / CLI Changelog

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
