# DeepSeek TuLAgent

简体中文 | [English](README.md)

DeepSeek TuLAgent 是一个专门适配 DeepSeek OpenAI 兼容接口的终端编程代理。它支持本地工具、会话恢复、`/` 命令面板、权限模式、思考模式和本地技能目录。

## 功能

- DeepSeek 优先配置：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`
- 默认全局命令：`deepseekTul`
- DeepSeek V4 模型别名：`pro`、`v4-pro`、`flash`、`v4-flash`
- 工具：读写文件、本地搜索、联网搜索、Git 状态、Shell、补丁、下载、后台服务
- 权限模式：`plan`、`review`、`agent`、`trusted`、`yolo`、`root`
- 思考模式：`off`、`instant`、`fast`、`standard`、`balanced`、`careful`、`deep`、`deeper`、`max`、`ultra`
- 本地技能目录：自动发现 `SKILL.md`
- 会话保存和恢复：JSONL 格式

## 快速开始

```bash
git clone https://github.com/ffffff233/deepseek-tulagent.git
cd deepseek-tulagent
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
deepseekTul config set --base-url https://api.deepseek.com --api-key sk-你的key --model deepseek-v4-flash
deepseekTul doctor --live
deepseekTul
```

## 常用命令

```bash
deepseekTul
deepseekTul run --mode root --think fast "检查当前项目"
deepseekTul start --resume <SESSION_ID>
deepseekTul sessions list
deepseekTul sessions show <SESSION_ID>
deepseekTul models
deepseekTul version
deepseekTul update --check
deepseekTul update
deepseekTul skills list
```

## 配置

本地配置文件：

```text
~/.deepseek-tulagent/config.json
```

保存配置：

```bash
deepseekTul config set \
  --base-url https://api.deepseek.com \
  --api-key sk-你的key \
  --model deepseek-v4-flash
```

查看配置，API key 会自动打码：

```bash
deepseekTul config show
```

环境变量也支持覆盖配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名或别名 |
| `DSTUL_WORKSPACE` | 当前目录 | 工作目录 |
| `DSTUL_MAX_TOOL_ROUNDS` | `8` | 最大工具循环次数 |
| `DSTUL_MAX_TOKENS` | `2048` | 最大输出 token |
| `DSTUL_REQUEST_TIMEOUT` | `180` | 请求超时时间 |

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

| 模式 | 路由 | 适用场景 |
| --- | --- | --- |
| `off` | `deepseek-v4-flash` | 最快直接回答 |
| `instant` | `deepseek-v4-flash` | 极快响应 |
| `fast` | `deepseek-v4-flash` | 快速工具任务 |
| `standard` | `deepseek-v4-flash` | 标准任务 |
| `balanced` | `deepseek-v4-pro` | 普通工程任务 |
| `careful` | `deepseek-v4-pro` | 更谨慎验证 |
| `deep` | `deepseek-v4-pro` | 复杂调试和设计 |
| `deeper` | `deepseek-v4-pro` | 更深层复杂任务 |
| `max` | `deepseek-v4-pro` | 最复杂任务 |
| `ultra` | `deepseek-v4-pro` | 最大内部思考预算 |

`balanced` 及以上会进行真实内部思考轮次：客户端会先调用模型生成私有规划，再把规划作为本轮回答上下文使用，不靠动画假装思考。

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

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 上下文压缩

TuLAgent 会估算消息上下文大小。接近模型上下文窗口时，会自动压缩旧消息：

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
deepseekTul version
```

检查更新：

```bash
deepseekTul update --check
```

执行更新：

```bash
deepseekTul update
```

交互启动时也会自动检查 GitHub 最新 tag。如果有新版本，会弹出选择面板：默认第一项是更新，直接回车更新；按下键再回车是不更新。

更新不会修改这些用户数据：

- `~/.deepseek-tulagent/config.json`，包括 API key、base URL、默认模型
- `~/.deepseek-tulagent/skills` 和工作区技能目录
- 会话目录

如果源码目录里有你自己改过但还没提交的文件，更新会停止，避免覆盖你的改动。

拉取老版本：

```bash
git fetch --tags
git checkout v0.1.2
# 或
git checkout v0.1.1
```

## 会话恢复

会话保存在：

```text
<workspace>/.deepseek-tulagent/sessions/<SESSION_ID>.jsonl
```

退出交互时会显示：

```text
[session] <SESSION_ID>
[resume] deepseekTul start --resume <SESSION_ID>
```

恢复会话：

```bash
deepseekTul start --resume <SESSION_ID>
```

列出会话：

```bash
deepseekTul sessions list
```

## 技能目录

TuLAgent 会发现这些目录里的技能：

- `<workspace>/.deepseek-tulagent/skills`
- `<workspace>/.agents/skills`
- `<workspace>/skills`
- `~/.deepseek-tulagent/skills`
- `~/.agents/skills`

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
deepseekTul skills new repo-debug \
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
- `web_search`：联网搜索
- `download_url`：下载 URL 到工作区
- `start_service`：启动后台服务
- `stop_service`：停止后台服务
- `service_status`：查看服务状态

手动执行工具：

```bash
/tool {"tool":"web_search","arguments":{"query":"DeepSeek","max_results":5}}
```

普通 JSON 输入不会被当作工具执行，只有 `/tool ...` 会执行。

如果模型输出“我要检查/执行/获取”并给出一个或多个 `bash` 代码块，程序会把它们兜底转换成一次 `run_shell` 工具调用；真正执行过的操作一定会进入工具结果，避免只在对话里假装执行。

## 安全说明

- 不要提交 `~/.deepseek-tulagent/config.json`，里面可能有 API key。
- 会话日志可能包含命令输出、路径、提示词和工具结果。
- `root` 和 `yolo` 会直接执行受限工具，只建议在可信工作区使用。

## 开源协议

MIT
