# DeepSeekFathom

简体中文 | [English](README.md)

DeepSeekFathom 是一个专门适配 DeepSeek OpenAI 兼容接口的终端编程代理。它支持本地工具、会话恢复、`/` 命令面板、权限模式、思考模式和本地技能目录。
同时提供桌面端入口，可打包成 Windows exe。

产品、仓库和 CLI 均使用 **DeepSeekFathom**。仅 pip 分发标识与用户数据目录保留 `deepseek-tulagent`，用于兼容旧版本原地升级和保留历史会话。

## 功能

- DeepSeek 优先配置：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`
- 默认全局命令：`deepseekfathom`
- DeepSeek V4 模型别名：`pro`、`v4-pro`、`flash`、`v4-flash`
- 工具：读写文件、本地搜索、联网搜索、任务目标清单、Git 状态、Shell、补丁、下载、仓库拉取、后台服务
- 子代理：`delegate_agent` 支持单个子代理，也支持 `agents=[...]` 一次委派多个隔离子任务
- 桌面端：聊天、文件发送、技能列表、任务目标 dock、工具调用折叠展示、模型/思考/权限切换、第三方 OpenAI 兼容 API 配置、MCP / 插件 / Hooks 管理和只读能力诊断
- 权限模式：`plan`、`review`、`agent`、`trusted`、`yolo`、`root`
- 思考模式：`off`、`instant`、`fast`、`standard`、`balanced`、`careful`、`deep`、`deeper`、`max`、`ultra`
- 本地技能目录：自动发现 `SKILL.md`
- 扩展目录：用户插件保存在 `~/.deepseek-tulagent/plugins`，用户技能、插件、配置和会话不会被桌面端更新覆盖
- 会话保存和恢复：JSONL 格式

## 快速开始

Windows 普通用户直接下载并运行：

**[DeepSeekFathom-0.1.16-Setup.exe](https://github.com/ffffff233/DeepSeekFathom/releases/download/desktop-v0.1.16/DeepSeekFathom-0.1.16-Setup.exe)**

Linux / macOS：

```bash
git clone https://github.com/ffffff233/DeepSeekFathom.git
cd DeepSeekFathom
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
deepseekfathom config set --base-url https://api.deepseek.com --api-key sk-你的key --model deepseek-v4-flash
deepseekfathom doctor --live
deepseekfathom
```

启动桌面端：

```bash
python3 -m pip install --upgrade ".[desktop]"
deepseekfathom desktop
```

Windows 安装后也可以直接运行：

```powershell
py -3 -m pip install --upgrade "deepseek-tulagent[desktop] @ https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz"
deepseekfathom-desktop
```

Windows PowerShell 原生安装：

```powershell
py -3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
deepseekfathom config set --base-url https://api.deepseek.com --api-key sk-你的key --model deepseek-v4-flash
deepseekfathom doctor --live
deepseekfathom
```

Windows CMD：

```bat
py -3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
deepseekfathom version
deepseekfathom
```

Windows 原生可以使用 `deepseekfathom run`、`config`、`update`、`sessions` 和普通行输入交互。高级 Unix TUI 依赖 `curses`；Windows 没有该模块时会自动退回普通行输入，不再启动就崩。
桌面端使用 `pywebview`，适合 Windows 原生使用。

如果用户机器上的 `git clone` 因代理、端口写法或 git 配置失败，可以不依赖 git，直接安装 GitHub tag 源码包：

```bash
python3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
```

代理环境示例：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
python3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
```

Windows PowerShell 代理示例：

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:HTTP_PROXY="http://127.0.0.1:7890"
py -3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
```

Windows CMD 代理示例：

```bat
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
py -3 -m pip install --upgrade https://github.com/ffffff233/DeepSeekFathom/archive/refs/tags/v0.1.108.tar.gz
```

让 agent 拉取其他 GitHub 仓库时，可以直接说“把 `owner/repo` 拉到 `path`”。它会优先使用 `clone_repo` 工具，自动尝试直连、镜像和 GitHub archive 下载，不会反复手写同一批失败的 `git clone` 命令。全部失败后才会提示你配置 `HTTP_PROXY` / `HTTPS_PROXY` 或 git proxy。

Windows 路径也兼容，例如：

```text
把 nexu-io/open-design 拉到 D:\deepseek项目\open-design
```

工具会把 Windows 风格路径映射到当前工作区内，避免误写到未知位置。要指定工作区可以先设置 `DSTUL_WORKSPACE`。

## 常用命令

```bash
deepseekfathom
deepseekfathom run --mode root --think fast "检查当前项目"
deepseekfathom start --resume <SESSION_ID>
deepseekfathom sessions list
deepseekfathom sessions show <SESSION_ID>
deepseekfathom models
deepseekfathom version
deepseekfathom update --check
deepseekfathom update
deepseekfathom skills list
deepseekfathom desktop
```

## 桌面端和 Windows exe

桌面端提供：

- 左侧会话和技能目录
- 设置页能力诊断：已加载项目指令、技能生效/覆盖候选、搜索路径、工具契约、权限门控和提示词成本估算
- OpenAI/DeepSeek 原生工具调用和同轮多工具执行，兼容网关继续保留文本协议兜底
- `list_skills` / `read_skill` 按需搜索、加载本项目、Agents 与 Claude 目录中的技能
- 顶部模型、思考模式、权限模式、兼容接口选择
- 右上角第三方 API / OpenAI-compatible Base URL 配置
- 设置页直接新增、编辑、删除 MCP 服务，支持远程 URL、多个请求头和本地命令
- 底部 `+` 上传文件
- 工具调用、子代理、上下文压缩和内部思考事件折叠展示，必须点开才看详情

Windows 本机打包 exe：

```powershell
git clone https://github.com/ffffff233/DeepSeekFathom.git
cd DeepSeekFathom
.\scripts\build_windows_exe.ps1
```

生成位置：

```text
dist\DeepSeekFathom\DeepSeekFathom.exe
dist\installer\DeepSeekFathom-0.1.16-Setup.exe
```

安装程序会安装到当前用户目录，并自动创建名为 **DeepSeekFathom** 的桌面和开始菜单入口。桌面端使用独立的 `desktop-vX.Y.Z` 标签；GitHub Actions 会在推送该标签时构建 Windows artifact，并把 Setup EXE 上传到对应 Release。
当前仓库所在的 Linux 环境不能直接产出真正 Windows exe；需要在 Windows 或 GitHub Actions 的 `windows-latest` 上构建。

### 桌面端 MCP

进入“设置 -> 扩展 -> MCP”，点击“新增”即可创建服务，不需要编辑源码或 JSON。远程服务填写名称和 `http(s)` URL；只有点击“添加请求头”后才会出现请求头名称和值，并可继续添加多行。本地服务切换到“本地命令”，启动参数每行填写一个。

用户 MCP 配置保存在 `~/.deepseek-tulagent/config.json`，覆盖安装会保留。项目和插件提供的 MCP 会在同一页显示，但由各自来源管理，不能从用户列表误删。

## 配置

本地配置文件：

```text
~/.deepseek-tulagent/config.json
```

保存配置：

```bash
deepseekfathom config set \
  --base-url https://api.deepseek.com \
  --api-key sk-你的key \
  --model deepseek-v4-flash
```

查看配置，API key 会自动打码：

```bash
deepseekfathom config show
```

环境变量也支持覆盖配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名或别名 |
| `DSTUL_WORKSPACE` | 当前目录 | 工作目录 |
| `DSTUL_MAX_TOOL_ROUNDS` | `256` | 最大工具循环次数 |
| `DSTUL_MAX_TOKENS` | `8192` | 最大输出 token |
| `DSTUL_REQUEST_TIMEOUT` | `180` | 请求超时时间 |
| `DSTUL_SEARCH_ENGINES` | `baidu,bing,duckduckgo` | `web_search` 使用的第三方搜索引擎顺序；结果页抓取默认关闭，开启后会检查 robots.txt |

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `plan` | 只读分析，不写文件，不执行 Shell |
| `review` | 读取和诊断，偏审查 |
| `agent` | 默认代理模式，危险工具需要确认 |
| `trusted` | 可信工作区，允许更多工具 |
| `yolo` | 自动确认所有受限工具 |
| `root` | 最高权限，不询问，直接执行 |

默认启动是：

```text
root + fast + deepseek-v4-flash
```

## 思考模式

| 模式 | 推荐模型 | 最大输出 | API thinking | 内部思考 |
| --- | --- | ---: | --- | ---: |
| `auto` | 自动选择 | 384K | 自动 | 自动 |
| `off` | `deepseek-v4-flash` | 384K | 关闭 | 0 |
| `instant` | `deepseek-v4-flash` | 384K | 关闭 | 0 |
| `fast` | `deepseek-v4-flash` | 384K | low | 0 |
| `standard` | `deepseek-v4-flash` | 384K | low | 0 |
| `balanced` | `deepseek-v4-pro` | 384K | medium | 1 |
| `careful` | `deepseek-v4-pro` | 384K | high | 1 |
| `deep` | `deepseek-v4-pro` | 384K | high | 2 |
| `deeper` | `deepseek-v4-pro` | 384K | xhigh | 2 |
| `max` | `deepseek-v4-pro` | 384K | xhigh | 3 |
| `ultra` | `deepseek-v4-pro` | 384K | xhigh | 4 |

`fast` 及以上会向 DeepSeek API 发送真实 `thinking` 开关。表中的强度值用于 OpenAI；Anthropic 和 Gemini 会换算为各自的原生预算参数。`balanced` 及以上还会在客户端进行真实内部思考轮次：先调用模型生成私有规划，再把规划作为本轮回答上下文使用，不靠动画假装思考。

切换思考模式不会强制切换当前模型。`/model`、`/think`、`/mode` 的选择会保存到本地配置，下次启动继续使用。

## `/` 命令面板

在交互界面按 `/` 会立即弹出命令列表：

- 输入字母过滤，例如 `m` 会匹配 `/model`、`/mode`
- 使用 `↑` / `↓` 选择
- 回车执行选中项
- `Esc` 取消
- 没有输入过滤词时按 `Backspace` 会关闭面板并删除 `/`

常见命令：

```text
/model
/models
/mode root
/think
/think fast
/compact
/doctor
/skills
/skill <name>
/tool <json>
/exit
```

`/model` 会打开模型选择面板，回车后切换当前会话模型；`/models` 只打印模型列表。
`/think` 会打开思考模式选择面板；`/compact` 会手动压缩旧上下文，保留最近消息原文。
在 `/` 面板里选中技能时，不会立刻执行命令，而是把 `Use skill <name>: ` 插入输入框，你可以继续补充任务再回车发送给 AI。

更新记录已按产品拆分：[CLI 更新记录](CHANGELOG-CLI.md) | [桌面端更新记录](CHANGELOG-DESKTOP.md) | [历史联合记录](CHANGELOG-LEGACY.md)。

## 上下文压缩

DeepSeekFathom 会估算消息上下文大小。接近模型上下文窗口时，会自动压缩旧消息：

- 保留系统提示
- 保留最近 8 条消息原文
- 把更早的用户、助手、工具结果压成摘要系统消息

手动压缩：

```text
/compact
```

这个策略参考 Codex 类终端代理的上下文压缩方向：旧上下文摘要化，近期关键上下文保留原文。

## 版本和更新

查看当前版本：

```bash
deepseekfathom version
```

检查更新：

```bash
deepseekfathom update --check
```

执行更新：

```bash
deepseekfathom update
```

交互启动时也会自动检查 GitHub 最新 tag。如果有新版本，会弹出选择面板：默认第一项是更新，直接回车更新；按下键再回车是不更新。

如果 git 更新因为代理写法、端口或本机 git 配置失败，`deepseekfathom update` 会回退到 pip 安装 GitHub tag 源码包，不再强依赖 `git+https`。也可以手动设置代理：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
```

如果仍要修 git 自己的代理配置：

```bash
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
```

更新不会修改这些用户数据：

- `~/.deepseek-tulagent/config.json`，包括 API key、base URL、默认模型
- `~/.deepseek-tulagent/skills` 和工作区技能目录
- 会话目录

如果源码目录里有你自己改过但还没提交的文件，更新会停止，避免覆盖你的改动。

拉取老版本时请区分 CLI 与桌面标签：

```bash
git fetch --tags
git checkout v<CLI版本>
# 桌面端：git checkout desktop-v<桌面版本>
```

## 会话恢复

会话保存在：

```text
<workspace>/.deepseek-tulagent/sessions/<SESSION_ID>.jsonl
```

退出交互时会显示：

```text
[session] <SESSION_ID>
[resume] deepseekfathom start --resume <SESSION_ID>
```

恢复会话：

```bash
deepseekfathom start --resume <SESSION_ID>
```

列出会话：

```bash
deepseekfathom sessions list
```

## 技能目录

DeepSeekFathom 会发现这些目录里的技能：

- `<workspace>/.deepseek-tulagent/skills`
- `<workspace>/.agents/skills`
- `<workspace>/skills`
- `~/.deepseek-tulagent/skills`
- `~/.agents/skills`

同名技能按上面的目录顺序取第一个生效。桌面端“设置 → 能力诊断”会同时显示生效文件与所有被覆盖候选；创建技能时如果 `SKILL.md` 已存在会直接拒绝，不会覆盖用户内容。

每个技能是一个包含 `SKILL.md` 的目录：

```markdown
---
name: repo-debug
description: 调试当前仓库时使用。
---

# repo-debug

先运行测试，再根据失败信息进行最小修改。
```

创建技能：

```bash
deepseekfathom skills new repo-debug \
  --description "调试当前仓库时使用。" \
  --body "先运行测试，再根据失败信息进行最小修改。"
```

## 工具

可用工具包括：

- `list_files`：列出文件
- `read_file`：读取文件
- `write_file`：写文件，原子写入
- `apply_patch`：应用补丁
- `run_shell`：执行 Shell 命令
- `git_status`：查看 Git 状态
- `search_text`：本地文本搜索
- `web_search`：通过 Baidu/Bing/DuckDuckGo 联网搜索；支持 `engines`、`language`，可选 `fetch_pages` 会检查 robots.txt 且只返回短摘录
- `todo_write`：创建/更新当前任务目标清单；复杂任务会先列目标，再逐项标记 `in_progress` / `completed`，桌面端会显示为输入框上方的可折叠 dock
- `inspect_media`：查看图片/视频路径；视频会抽帧成截图，截图会同时发给模型和展示在桌面端工具卡里
- `download_url`：下载 URL 到工作区
- `clone_repo`：拉取 Git/GitHub 仓库，支持镜像和 archive fallback
- `start_service`：启动后台服务
- `stop_service`：停止后台服务
- `service_status`：查看服务状态

手动执行工具：

```bash
/tool {"tool":"web_search","arguments":{"query":"DeepSeek","max_results":5,"engines":"baidu,bing,duckduckgo","fetch_pages":2}}
```

普通 JSON 输入不会被当作工具执行，只有 `/tool ...` 会执行。

如果模型输出“我要检查/执行/获取”并给出一个或多个 `bash` 代码块，程序会把它们兜底转换成一次 `run_shell` 工具调用；真正执行过的操作一定会进入工具结果，避免只在对话里假装执行。

## 安全说明

- 不要提交 `~/.deepseek-tulagent/config.json`，里面可能有 API key。
- 会话日志可能包含命令输出、路径、提示词和工具结果。
- `root` 和 `yolo` 会直接执行受限工具，只建议在可信工作区使用。

## 开源协议

MIT
