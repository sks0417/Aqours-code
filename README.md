# Aqours_code

`Aqours_code` 是一个面向本地代码仓库的 Coding Agent Harness。它把模型调用、工具执行、上下文压缩、权限拦截、Trace 和 Docker 隔离评测串成一条可检查的工程链路，目标是学习并验证 Coding Agent 的核心设计，而不是提供生产级托管服务。

## 核心能力

- 在当前目录启动交互式 Agent Loop，并让模型读取、修改和测试代码。
- 提供 Bash、文件读写/精确编辑、Glob、Todo 和 Context Compact 工具。
- 对危险操作执行权限检查，对模型瞬时错误进行有界重试和恢复。
- 为每次运行保存脱敏 Trace、时间线、最终输出和运行元数据。
- 在 Docker 中隔离 Agent，并由独立的 trusted grader 对 clean-room workspace 评分。

## 功能状态（v0.1）

Stable 表示默认单 Agent 路径已接线，并有代码和自动化测试支持；Experimental 表示实现仍被保留用于实验，但不构成 v0.1 稳定性承诺。

| 状态 | 功能 | 真实范围 |
| --- | --- | --- |
| Stable | Agent Loop | 交互式与非交互式单 Agent 主循环 |
| Stable | Bash | 前台命令、超时、失败结果与权限边界 |
| Stable | Read / Write / Edit | workspace 内文件读取、写入和精确替换 |
| Stable | Glob / Search | 原生 Glob；文本搜索通过 Bash 中的 `rg`/系统搜索命令完成 |
| Stable | Todo | 可选的轻量任务清单；仅包含稳定 ID、内容和状态 |
| Stable | Context Compact | 原子保留工具调用对、根任务和最近上下文；失败时保留安全历史 |
| Stable | Permission Hooks | 危险操作拦截，非交互执行默认拒绝 |
| Stable | Retry / Recovery | 有界重试；v0.1 不会静默切换到另一个模型 |
| Stable | Trace | 脱敏事件、时间线、状态、运行索引和最小运行元数据 |
| Stable | Docker Eval | 一次性 Agent 容器、clean-room workspace、独立 trusted grader 和可信评分 |
| Experimental | Subagent / Plan Review | 有界临时角色与 Reviewer 流程，不是默认单 Agent 成功的前置条件 |
| Experimental | Persistent Task / Teammate / Message Bus / Multi-Agent | 共享任务和协作原型，保留但不承诺完整生命周期 |
| Experimental | Worktree | 隔离 Worker 修改与显式集成原型 |
| Experimental | Skills / MCP | 动态扩展入口，依赖具体环境和配置 |
| Experimental | Background / Cron | 后台与调度原型，不作为 v0.1 默认使用路径 |

## 架构

```text
Aqours_code CLI
  -> AgentRuntime + Agent Loop
  -> Model adapter
  -> authoritative ToolRegistry
  -> local workspace
  -> .aqours_code/runs/<run_id> Trace

Docker Eval
  -> host Model Broker（凭据留在宿主机）
  -> network-disabled one-shot Agent container
  -> disposable workspace + collected artifacts
  -> separate trusted Grader container
  -> pass/fail + continuous score
```

默认入口只启动单 Agent Coding 流程。Experimental 模块不会为了“看起来完整”而被强制接入主链路。

## Quick Start

官方验证环境是 **Python 3.11**。

```bash
git clone <repository>
cd Aqours_code

python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate

python -m pip install -e .
```

根据示例创建本地配置：

```bash
# Windows PowerShell
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

填写配置后，从要修改的仓库目录运行唯一推荐入口：

```bash
Aqours_code
```

启动信息会显示项目名、当前 workspace、provider 和模型名，不会显示 API Key。默认 workspace 是启动命令所在的当前目录。

## API 配置

`.env.example` 与代码使用同一组权威字段：

```dotenv
AQOURS_CODE_PROVIDER=openai_compatible
AQOURS_CODE_API_KEY=
AQOURS_CODE_BASE_URL=
AQOURS_CODE_MODEL=
# AQOURS_CODE_CONTEXT_LIMIT_TOKENS=64000
```

`AQOURS_CODE_PROVIDER` 可选值为 `openai_compatible`、`openai`、`deepseek` 或 `anthropic`。前三者使用 OpenAI-compatible messages 接口；`anthropic` 使用 Anthropic SDK。Key、Base URL 和模型名没有旧变量别名、provider 专属回退或默认模型。任一必填字段缺失时，CLI 会在进入交互循环前失败并指出字段名。

`.env` 已被 Git 忽略。API Key 和认证 Header 会从 Trace 中移除；Trace 中的 Base URL 也会删除 userinfo 和认证类 query 参数。

Context 只暴露一个可选设置：`AQOURS_CODE_CONTEXT_LIMIT_TOKENS`，默认
`64000`。这是请求上下文的估算 Token 上限；Harness 固定在 85% 时开始
Compact，以保留正常回复和估算误差所需空间。v0.1 不对外暴露第二套字符
上限或 Compact 比例配置。

依赖的权威声明是 `pyproject.toml`。`evals/docker/requirements.lock` 只负责固定 Eval 镜像环境，不是第二份应用依赖清单。

## 执行一个 Coding Task

在目标仓库根目录运行 `Aqours_code`，然后输入具体任务，例如：

```text
Aqours_code >> 修复订单金额计算的舍入错误，并运行相关 pytest；不要修改公开 API。
```

Agent 会在当前 workspace 内读取代码、记录 Todo、执行修改和测试，并将本次运行写入 `.aqours_code/runs/<run_id>/`。危险或越界操作会被权限层拒绝；测试失败不会被转换成成功结果。

## 测试与 Docker Eval

安装开发依赖并执行项目测试：

```bash
python -m pip install -e ".[dev]"
python -m compileall aqours_code
python -m pytest -q
Aqours_code --help
```

最小 Docker Eval smoke 使用确定性的 scripted model，但仍运行真实 Agent 容器和 Grader 容器：

```bash
python evals/run_eval.py --scripted --execution docker --case read_file_basic
```

Docker Eval 会在镜像缺失时自动构建；传入 `--docker-build` 可强制重建镜像。

真实模型 Eval 去掉 `--scripted`，并使用 `.env` 中的同一组 API 配置。Docker 启动或构建失败是硬失败，不会悄悄回退到本地执行。只有开发调试时才显式使用 `--execution local`。

## Trace 示例

每次运行至少生成 `metadata.json`、`trace.jsonl`、`timeline.jsonl`、`timeline.md` 和 `final.md`：

```json
{
  "run_id": "20260803-120000-a1b2c3d4",
  "started_at": "2026-08-03T04:00:00+00:00",
  "project_version": "0.1.0",
  "git_commit": "<commit-or-null>",
  "git_dirty": true,
  "model_provider": "openai_compatible",
  "model": "<configured-model>",
  "base_url": "https://gateway.example/v1",
  "python_version": "3.11.9",
  "platform": "<runtime-platform>",
  "workspace": "<absolute-workspace>"
}
```

Git 不可用或 workspace 不是仓库时，Git 字段可以为空；元数据采集失败只会记录降级信息，不会终止 Agent。

## 已知限制

- v0.1 只承诺 Python 3.11；其他 Python 版本未列为支持环境。
- 稳定入口是本地交互式单 Agent，不包含 Web UI、远程服务或 GitHub PR 自动化。
- Docker Eval 需要可用的 Docker daemon，并受本机镜像构建和资源限制影响。
- Shell 命令行为取决于运行平台；跨平台路径和命令仍需由任务本身约束。
- Experimental 功能有测试覆盖不等于完成产品化接入，不能作为默认 Coding Task 的稳定性依赖。
- 项目不提供长期 Memory、生产级凭据管理、SLA 或多租户隔离。

发布前尚未完成或需要真实环境复核的事项见 [`V0_1_TODO.md`](V0_1_TODO.md)。

## 项目来源与个人工作

Agent Harness 的核心设计主要参考并复现 `learn-claude-code`，包括分阶段构建 Coding Agent Loop、工具调用和 Context Compact 的学习路径。本项目不声称“完全从零自研 Claude Code”。

在此基础上，本项目独立增加并重点建设了 Docker 隔离 Eval、clean-room trusted grader 与可信评分流程、Trace/Timeline 日志，以及围绕权限、失败语义、运行隔离、上下文正确性和测试可靠性的后续工程化修复。作品集目标是用可运行代码和测试验证 Coding Agent Harness 的工程设计取舍。
