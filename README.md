# DeepSeek TuLAgent

## 中文用户请点这里

👉 **[查看简体中文文档 / 点击进入中文介绍](README.zh-CN.md)**

这个仓库同时提供中文和英文文档。如果你看不懂英文，直接点上面的 **简体中文文档**，里面有安装、配置、启动命令、权限模式、思考模式、会话恢复、技能目录和工具说明。

---

English

DeepSeek TuLAgent is a terminal coding agent built specifically around DeepSeek's OpenAI-compatible chat API. It provides local tools, session resume, slash commands, permission modes, thinking modes, and installable skills while keeping the implementation independent and compact.
It also includes a desktop entrypoint that can be packaged as a Windows exe.

## Features

- DeepSeek-first provider config: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`
- Native DeepSeek V4 aliases: `pro`, `v4-pro`, `flash`, `v4-flash`
- Live model discovery through `deepseekTul models` and `deepseekTul doctor --live`
- Global `deepseekTul` command for interactive use
- Tool registry: files, local search, web search, git status, shell, patch, downloads, resilient repository cloning, background services
- Subagents: `delegate_agent` supports both one isolated subtask and an `agents=[...]` batch for multiple subagents in one tool call
- Desktop app: chat, file attachments, skill list, collapsible tool calls, collapsible internal thinking, quick model/thinking/permission switching, and third-party OpenAI-compatible API configuration
- Six permission modes: `plan`, `review`, `agent`, `trusted`, `yolo`, `root`
- Five thinking modes: `off`, `fast`, `balanced`, `deep`, `max`
- Local skill directories with `SKILL.md` discovery and skill creation
- JSONL session transcript under `.deepseek-tulagent/sessions`
- One-shot and interactive CLI suitable for automation and later full TUI wrapping
- Doctor command for local config checks

## Quickstart

Linux / macOS:

```bash
git clone https://github.com/ffffff233/deepseek-tulagent.git
cd deepseek-tulagent
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
deepseekTul config set --base-url https://api.deepseek.com --api-key sk-... --model deepseek-v4-flash
deepseekTul doctor --live
deepseekTul
```

Start the desktop app:

```bash
python3 -m pip install --upgrade ".[desktop]"
deepseekTul desktop
```

On Windows after installation:

```powershell
py -3 -m pip install --upgrade "deepseek-tulagent[desktop] @ https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz"
deepseekTulDesktop
```

Native Windows PowerShell:

```powershell
py -3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
deepseekTul config set --base-url https://api.deepseek.com --api-key sk-... --model deepseek-v4-flash
deepseekTul doctor --live
deepseekTul
```

Windows CMD:

```bat
py -3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
deepseekTul version
deepseekTul
```

Native Windows supports `deepseekTul run`, `config`, `update`, `sessions`, and line-mode interactive chat. The Unix-style full TUI depends on `curses`; when it is unavailable on Windows, the CLI falls back to line mode instead of crashing at startup.
The desktop app uses `pywebview` and is suitable for native Windows use.

If `git clone` is blocked by local proxy/git configuration, install directly from the tagged source tarball instead:

```bash
python3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
```

Proxy-compatible examples:

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
python3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
```

Windows PowerShell proxy example:

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:HTTP_PROXY="http://127.0.0.1:7890"
py -3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
```

Windows CMD proxy example:

```bat
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
py -3 -m pip install --upgrade https://github.com/ffffff233/deepseek-tulagent/archive/refs/tags/v0.1.49.tar.gz
```

When asking the agent to fetch another GitHub repository, say something like `clone owner/repo into path`. The agent should use `clone_repo`, which tries direct git, mirror URLs, and GitHub archive download before asking you to configure `HTTP_PROXY`, `HTTPS_PROXY`, or git proxy settings.

Windows paths are accepted, for example:

```text
clone nexu-io/open-design into D:\deepseek-projects\open-design
```

The tool maps Windows-style paths into the configured workspace to avoid writing to an unexpected location. Set `DSTUL_WORKSPACE` first if you want a specific workspace root.

One-shot usage:

```bash
deepseekTul run --mode root --think fast "inspect this repo"
```

Startup commands:

```bash
deepseekTul                                    # default: root + fast + flash
deepseekTul start --mode agent --think balanced
deepseekTul start --mode trusted --think deep --yes
deepseekTul start --mode root --think max
deepseekTul run --mode agent --think fast --yes "run tests and fix failures"
```

## Desktop App and Windows exe

The desktop app includes:

- conversation and skill navigation
- model, thinking mode, permission mode, and compatibility format selectors
- third-party API / OpenAI-compatible Base URL settings
- `+` file uploads
- collapsible tool calls, subagents, context compaction, and internal thinking events

Build the Windows exe locally:

```powershell
git clone https://github.com/ffffff233/deepseek-tulagent.git
cd deepseek-tulagent
.\scripts\build_windows_exe.ps1
```

Output:

```text
dist\DeepSeekTuLAgent\DeepSeekTuLAgent.exe
```

GitHub Actions also builds a `DeepSeekTuLAgent-windows` artifact on tagged releases. This Linux workspace cannot directly produce a real Windows exe; build it on Windows or through the `windows-latest` workflow.

## Conversations

Each conversation is saved under:

```text
<workspace>/.deepseek-tulagent/sessions/<SESSION_ID>.jsonl
```

When you actively leave an interactive conversation with `/exit`, `/quit`, Ctrl-D, or Ctrl-C, the CLI prints the conversation id and ready-to-run commands:

```text
[session] <SESSION_ID>
[resume]  deepseekTul start --resume <SESSION_ID>
```

Session commands:

```bash
deepseekTul sessions list
deepseekTul sessions show <SESSION_ID>
deepseekTul sessions resume <SESSION_ID>
deepseekTul start --resume <SESSION_ID>
deepseekTul version
deepseekTul update --check
deepseekTul update
```

Resume example:

```bash
deepseekTul start --resume 022a00cb-e1cf-49af-9e11-0cbc6b2e3ab8
```

Current local default config lives at `~/.deepseek-tulagent/config.json`. Environment variables still override it.

## Slash Palette

Inside `deepseekTul`, press `/` to open the command palette immediately:

- type letters to filter, for example `m` matches `/model` and `/mode`
- use `↑` / `↓` to select
- press `Enter` to execute the selected command
- press `Esc` to cancel
- press `Backspace` with an empty filter to close the palette and remove `/`

Common commands:

- `/model`
- `/models`
- `/mode root`
- `/think`
- `/think fast`
- `/compact`
- `/doctor`
- `/skills`
- `/skill <name>`
- `/tool <json>`
- `/exit`

Discovered skills are shown in the same palette as `/skill <name>` entries.

`/model` opens the model picker and switches the current session model. `/models` only prints the live model list.
`/think` opens the thinking-mode picker. `/compact` manually compresses older context while keeping recent messages exact.
Selecting a skill from the `/` palette inserts `Use skill <name>: ` into the composer so you can keep typing the task before sending it to the agent.

See [CHANGELOG.md](CHANGELOG.md) for update history.

## Context Compaction

TuLAgent estimates conversation context before model calls. Near the model context limit, it automatically compacts older messages:

- system prompt is preserved
- the most recent 8 messages are kept exactly
- older user, assistant, and tool-result messages become one summary system message

Manual compaction:

```text
/compact
```

This follows the same broad strategy used by terminal agents such as Codex: summarize older context while preserving recent context exactly.

## Versions and Updates

```bash
deepseekTul version
deepseekTul update --check
deepseekTul update
```

Interactive startup checks the latest GitHub tag. If a newer version exists, the update picker opens with `update` selected by default; press Enter to update, or press Down then Enter to skip.

The updater does not touch user configuration, API keys, model defaults, sessions, or skill directories. If the source checkout has local uncommitted changes, the update stops instead of overwriting user edits.

If git update fails because git/proxy syntax is unsupported, `deepseekTul update` falls back to installing the GitHub tag tarball with pip. You can also set `HTTP_PROXY` / `HTTPS_PROXY` for the tarball path, or configure git separately:

```bash
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
```

Install an older version:

```bash
git fetch --tags
git checkout v0.1.2
# or
git checkout v0.1.1
```

## Permission Modes

| Mode | Behavior |
| --- | --- |
| `plan` | Read-only investigation. No shell, writes, or patches. |
| `review` | Read and run non-mutating diagnostics with confirmation-oriented prompts. |
| `agent` | Default coding mode. Reads, shell, writes, and patches inside the workspace. |
| `trusted` | Like agent, with network-capable policy metadata for future tools. |
| `yolo` | Auto-approved trusted workspace mode. |
| `root` | Highest authority mode. No confirmation prompts; tools execute directly. |

Confirmation rules:

- Read-only tools run directly when the mode allows reading.
- Gated tools are `write_file`, `run_shell`, `apply_patch`, `download_url`, `start_service`, `stop_service`.
- `--yes` approves all gated tools for that run/session.
- Without `--yes`, interactive mode asks you to type `yes` for each gated tool.
- `yolo` and `root` behave like all-gated-tools-approved.

## Skills

TuLAgent discovers skills from these directories, in order:

- `<workspace>/.deepseek-tulagent/skills`
- `<workspace>/.agents/skills`
- `<workspace>/skills`
- `~/.deepseek-tulagent/skills`
- `~/.agents/skills`

Each skill is a directory containing `SKILL.md`:

```markdown
---
name: repo-debug
description: Use when debugging this repository.
---

# repo-debug

Run tests first, inspect failures, then patch narrowly.
```

Skill commands:

```bash
deepseekTul skills list
deepseekTul skills show repo-debug
deepseekTul skills new repo-debug --description "Use when debugging this repository." --body "Run tests first."
```

Discovered skill summaries are injected into the agent prompt at startup/run time. The implementation is independent from Codex; it only follows the same useful convention of local `SKILL.md` instruction packs.

## Thinking Modes

| Mode | Recommended model | Max output | API thinking | Internal passes |
| --- | --- | ---: | --- | ---: |
| `auto` | auto-selected | 384K | auto | auto |
| `off` | `deepseek-v4-flash` | 384K | disabled | 0 |
| `instant` | `deepseek-v4-flash` | 384K | disabled | 0 |
| `fast` | `deepseek-v4-flash` | 384K | high | 0 |
| `standard` | `deepseek-v4-flash` | 384K | high | 0 |
| `balanced` | `deepseek-v4-pro` | 384K | high | 1 |
| `careful` | `deepseek-v4-pro` | 384K | high | 1 |
| `deep` | `deepseek-v4-pro` | 384K | high | 2 |
| `deeper` | `deepseek-v4-pro` | 384K | max | 2 |
| `max` | `deepseek-v4-pro` | 384K | max | 3 |
| `ultra` | `deepseek-v4-pro` | 384K | max | 4 |

`fast` and higher modes send real DeepSeek API `thinking` controls. `balanced` and deeper modes also perform client-side internal deliberation passes: the client makes extra model calls for private planning, then uses those notes as context for the final answer.

Changing thinking mode does not force a model change. `/model`, `/think`, and `/mode` selections are saved as local defaults for the next session.

## Configuration

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | required for live calls | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API base URL |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Model name or alias |
| `DSTUL_WORKSPACE` | current directory | Workspace root |
| `DSTUL_MAX_TOOL_ROUNDS` | `256` | Max tool loop iterations |
| `DSTUL_MAX_TOKENS` | `8192` | Max model output tokens |
| `DSTUL_REQUEST_TIMEOUT` | `180` | DeepSeek request timeout seconds |

Model aliases:

| Alias | Resolved model |
| --- | --- |
| `pro`, `v4-pro` | `deepseek-v4-pro` |
| `flash`, `v4-flash` | `deepseek-v4-flash` |

## Tool Protocol

The model is asked to return normal text or a tool request block:

```json
{"tool":"read_file","arguments":{"path":"README.md","max_bytes":12000}}
```

Available tools:

- `list_files`: list workspace files with noisy directories skipped
- `search_text`: search text inside workspace files
- `web_search`: search the web and return result snippets
- `git_status`: show short git status
- `run_shell`: run a command in the workspace
- `read_file`: read a UTF-8 text file
- `write_file`: create or overwrite a file
- `apply_patch`: apply a unified diff through `git apply`
- `download_url`: download a URL into the workspace when network policy allows it
- `clone_repo`: clone a Git/GitHub repository with mirror and archive fallbacks
- `start_service`: launch a background service and store pid/log under `.deepseek-tulagent/services`
- `stop_service`: stop a recorded service
- `service_status`: check a recorded service

If the model says it is about to inspect, fetch, run, or verify something and emits one or more `bash` code blocks, the agent falls back to executing them through one `run_shell` call. Real execution is always recorded as a tool result, so the assistant cannot silently pretend a command ran.

## Design Notes

This project does not copy DeepSeek-TUI source. It implements the same broad class of terminal agent from scratch in Python:

- provider layer is DeepSeek-specific but OpenAI-compatible;
- tool layer is an explicit registry with workspace path checks;
- session state is append-only JSONL;
- approval behavior is mode-driven;
- startup checks verify that the configured DeepSeek model is actually available before interactive use.

## Security Notes

- Do not commit `~/.deepseek-tulagent/config.json`; it may contain your API key.
- Local session logs can contain prompts, tool results, paths, and command output.
- `root` and `yolo` modes execute gated tools without confirmation. Use them only in trusted workspaces.
