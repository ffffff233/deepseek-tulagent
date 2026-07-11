# 桌面端更新记录 / Desktop Changelog

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
