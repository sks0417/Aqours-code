# Aqours_code 当前 Agent 功能与真实接线审计

> 审计日期：2026-07-31  
> 审计对象：当前工作树，而非仅依据 README 或历史提交  
> 验证结果：`326 passed, 3 skipped, 1 deselected`

## 1. 执行摘要

Aqours_code 当前是一个由以下部分组成的实验型 Coding Agent Harness：

- 单个 Lead Agent 主循环；
- 31 个内置工具定义；
- 4 种有边界的临时 Subagent；
- 基于线程、Task、Mailbox 和 Git Worktree 的持久 Teammate；
- Context 压缩、运行级 RunKnowledge、Todo/Acceptance；
- 后台 Bash、Cron 和一次性定时任务；
- Trace、Timeline、Host Model Broker；
- 相互隔离的 Docker Agent 与 Docker Grader。

目前相对成熟的是工具循环、文件操作、Context 控制、Trace、模型 Broker 和 Docker Eval。Acceptance、RunKnowledge、多 Agent 和 Worktree 已接入主流程，但正确性闭环仍然不够硬。

当前不存在或没有真正生效的能力包括：

- 完整 Intent 系统；
- 最终回答前自动运行 Reviewer；
- `worker` 临时 Subagent；
- 自动创建、提交和集成 Worker Worktree；
- 真正的 MCP 协议客户端；
- 向量记忆或自动长期记忆；
- 从 checkpoint/transcript 恢复中断运行；
- 跨工具调用事务与回滚。

因此，当前最准确的产品定位是：

> 功能面较广、评测与隔离设计较完整，但最终正确性仍较依赖模型自律的实验型 Coding Agent Harness。

## 2. 总体执行流程

```text
用户任务 / Eval Case
        |
        v
Lead Agent 主循环
        |
        +--> 动态重建 System Prompt
        +--> Context 完整请求计量和压缩
        +--> 调用 DeepSeek / Anthropic / OpenAI-compatible 模型
        |
        v
模型输出
  |             |
  |             +--> 纯文本 --> Acceptance Final Gate --> Final
  |
  +--> Tool Use
          |
          +--> 文件 / Bash / 测试
          +--> Todo / Acceptance
          +--> 临时 Subagent
          +--> Teammate / Task / Worktree
          +--> Cron / Once
          +--> Skill / Mock MCP
          +--> Manual Compact
          |
          v
    Tool Result 返回 Lead 主循环

全过程 --> RunKnowledge + Trace + Timeline
模型调用 --> Host Model Broker --> Provider API
```

主要入口：

- 交互 CLI：`aqours_code/main.py`
- 非交互入口：`aqours_code/agent_loop.py::run_agent_task()`
- Docker Eval 容器入口：`aqours_code/eval_container_entry.py`
- Eval Runner：`evals/run_eval.py`

## 3. 当前模型配置与调用能力

当前实际配置：

| 项目 | 当前值 |
|---|---|
| Provider | `deepseek` |
| Model | `deepseek-v4-flash` |
| Reasoning | `max` |
| 普通输出上限 | 8,000 tokens |
| 截断后升级上限 | 16,000 tokens |
| Context 预算 | 128,000 字符 |
| Fallback Model | 未配置 |

支持的 Provider：

- Anthropic；
- DeepSeek；
- OpenAI；
- OpenAI-compatible。

DeepSeek V4 Flash/Pro 的特殊处理：

- 发送 `thinking={"type":"enabled"}`；
- 发送 `reasoning_effort="max"`；
- 工具轮之间保存和回放 `reasoning_content`；
- Thinking 模式下不发送可能冲突的 `tool_choice=auto`。

恢复机制：

- 429 指数退避重试；
- 529/overloaded 重试；
- 连续过载时支持 fallback model，但当前没有配置 fallback；
- `max_tokens` 后从 8K 升到 16K；
- 再次截断后最多追加两次 continuation；
- Prompt 过长时触发一次 reactive compact。

相关代码：

- `aqours_code/config.py`
- `aqours_code/model_api.py`
- `aqours_code/recovery.py`
- `aqours_code/model_budget.py`

## 4. Lead Agent 主循环

每次 `agent_loop()` 会：

1. 清空本轮 Todo、Acceptance、RunKnowledge、变更文件和 Lead 读取计数；
2. 用启发式判断是否需要 Todo 和 Acceptance；
3. 对复杂任务注入多 Agent 建议，但不强制委派；
4. 注入已完成的后台任务和到期定时任务；
5. 重建包含动态状态的 System Prompt；
6. 对完整请求进行 Context 预算计算；
7. 必要时压缩 Context；
8. 调用模型；
9. 顺序执行模型返回的工具调用；
10. 将 Tool Result 返回模型并继续循环；
11. 模型输出纯文本时运行 Acceptance Final Gate；
12. 保存 Final 和 Trace。

重要语义：

- 同一模型响应内的工具按顺序执行；
- 工具调用不具备事务性；
- 前一个修改成功、后一个调用失败时，不回滚前一个修改；
- Prompt 明确要求只批量执行互不依赖的读取或修改；
- Harness 本身仍是串行 dispatcher，不会真正并行执行同一响应中的工具；
- 复杂度评估只生成 advisory；
- Lead 读取压力只写入 Trace，不会自动启动 Explorer；
- 当前不会自动运行 Reviewer。

主实现：`aqours_code/agent_loop.py`

## 5. 31 个工具的完整清单

### 5.1 文件和命令

#### `bash`

- 通过当前执行后端运行 Shell 命令；
- 默认单命令超时 120 秒；
- 输出最多返回约 50,000 字符；
- 支持显式 `run_in_background=true`；
- pytest、构建、安装等慢命令会自动后台化；
- 删除类命令会被权限层拒绝。

#### `read_file`

- 读取工作区内文件；
- 支持 `offset` 和 `limit` 行范围；
- 读取结果会记录到 RunKnowledge；
- Python 文件会解析顶层类、函数和方法符号。

#### `write_file`

- 创建或整体覆盖工作区内文件；
- 修改前后对工作区做指纹快照；
- 使旧文件版本绑定的证据失效。

#### `edit_file`

- 精确替换第一次出现的 `old_text`；
- 找不到旧文本时返回错误；
- 同样参与 RunKnowledge 修改失效机制。

#### `glob`

- 支持普通 Glob 和递归 `**`；
- 结果限制在工作区内部。

### 5.2 Todo 和 Acceptance

#### `todo_write`

- `kind=plan`：实现步骤；
- `kind=acceptance`：外部可观察验收条件；
- 每条 Acceptance 可带稳定 ID、状态、证据和证据来源；
- 总 Todo 数上限为 32；
- 已取消单独的 20 条 Acceptance 上限；
- 每条 content/evidence 最多 500 字符；
- 每种 evidence source 最多 20 个。

### 5.3 临时 Subagent

#### `task`

- 兼容入口；
- 固定启动 `general-purpose`；
- 不会根据 Intent 自动切换 Explorer/Reviewer/Worker。

#### `delegate_agent`

- 显式支持 `explore`、`plan`、`review`、`general-purpose`；
- 使用新鲜 Context；
- 受角色工具、轮次、读取路径和调用预算限制。

### 5.4 Context 和 Skill

#### `load_skill`

- 扫描运行时 `skills/<name>/SKILL.md`；
- 返回完整 SKILL.md；
- 当前仓库没有 `skills` 目录，因此目前没有可加载 Skill。

#### `compact`

- 手动触发 Context Compact；
- 由 Agent 主循环特殊处理；
- 它没有普通 Registry Handler 是预期行为。

### 5.5 共享 Task

- `create_task`
- `list_tasks`
- `get_task`
- `claim_task`
- `complete_task`

Task 使用 `.tasks/task_*.json`，支持：

- `pending / in_progress / completed`；
- owner；
- `blockedBy` 依赖；
- Worktree 绑定。

### 5.6 定时任务

- `schedule_cron`
- `schedule_once`
- `list_crons`
- `cancel_cron`

支持五字段重复 Cron、秒/分钟延迟的一次性任务和指定本地时间的一次性任务。

### 5.7 持久 Teammate 协议

- `spawn_teammate`
- `send_message`
- `check_inbox`
- `request_shutdown`
- `request_plan`
- `review_plan`
- `submit_plan`：仅 Teammate 可见，由 Teammate 循环特殊处理。

### 5.8 Git Worktree

- `create_worktree`
- `remove_worktree`
- `keep_worktree`
- `integrate_worktree`

### 5.9 MCP

#### `connect_mcp`

- 连接模拟 MCP server；
- 动态生成 `mcp__server__tool` 工具。

权威注册表：`aqours_code/tool_defs.py` 和 `aqours_code/tool_registry.py`。

## 6. Todo 与 Acceptance 的真实行为

Acceptance 并不存储在 Context checkpoint 文件中。

运行时存在三份相关状态：

1. `runtime.state.todos`：本轮 Todo/Acceptance 真正的活动列表；
2. `RunKnowledge.acceptance`：证据来源和有效性账本；
3. 动态 System Prompt：每次模型请求前重新注入 Acceptance。

Context Compact 生成的 `[Context checkpoint]` 只是旧消息的 Markdown 摘要。Acceptance 之所以在 Compact 后仍可见，是因为 Harness 从运行时状态重新注入，而不是因为 Acceptance 被可靠保存到了摘要里。

生命周期限制：

- 每次新 `agent_loop()` 开始时 Todo 会清空；
- RunKnowledge 也会清空；
- 程序重启后不会从 transcript 恢复 Acceptance；
- 因此它不是持久 checkpoint 或断点恢复机制。

当前 Gate：

- 被启发式判定为复杂代码修改的任务，第一次修改前必须创建 Todo；
- 需要 Acceptance 时，没有 Acceptance 条目就不能修改；
- 第一次成功修改后才把 Acceptance 锁定；
- 锁定后，删除的条目会恢复，改写会保留旧措辞；
- 可以通过稳定 ID 更新状态和证据；
- 最终存在未完成条目时，Harness 暂停一次 Final；
- 再次 Final 时仍未完成，则附带警告后允许退出。

关键缺口：

- Acceptance 在第一次成功修改后才锁定，不是创建后立即锁定；
- completed 只强制要求非空 `evidence` 文本；
- `evidence_sources` 在代码层不是必填；
- Final Gate 只检查 Todo `status`；
- Final Gate 不检查 RunKnowledge 的 `evidence_valid`；
- 因此 `unbound` 或 `stale` 的证据仍可能对应 completed 状态并通过 Final Gate。

## 7. RunKnowledge

`RunKnowledge` 是运行级确定性工作记忆，不是长期记忆。

它记录：

- 读取文件的 SHA-256 digest、版本、读取次数和有效性；
- Python 顶层类、函数和方法；
- Explorer 提取的契约；
- 修改路径；
- 最近 5 次测试及结果；
- 测试运行时的工作区指纹；
- Acceptance 的文件、测试和 Reviewer 来源；
- Reviewer findings；
- `verified / stale / unbound` 证据状态。

以下操作使用修改前后快照进行实际变更检测：

- `write_file`；
- `edit_file`；
- 前台 Bash；
- 后台 Bash；
- Worktree 集成。

修改文件后，所有绑定到旧文件版本的契约、Acceptance、Reviewer finding 和测试状态会按路径失效。

但 RunKnowledge 目前没有形成最终执行闭环：

- `as_dict()` 没有主流程消费者；
- 不会整体注入模型 Context；
- Context Compact 不读取或合并它；
- 不写入可恢复 checkpoint；
- Final Gate 不读取 `evidence_valid`；
- 它能识别证据已失效，却不能阻止 Lead 宣布完成。

实现：`aqours_code/knowledge.py`

## 8. Context Compact、Memory 和 Skill

### 8.1 Context Compact

当前策略：

- 完整请求超过 128,000 字符的 80% 时自动 Compact；
- 使用 3 字符/token 的保守估算；
- 单个 Tool Result 超过约 8,000 tokens 时替换为占位符；
- 保留最新真实用户消息；
- 最多保留 4 个完整 Tool exchange；
- 原始最近尾部最多约 20,000 tokens；
- 更早历史总结成一个累计 Markdown checkpoint；
- 后续 Compact 会替换旧 checkpoint；
- 一次 Compact 最多使用一次总结模型调用；
- 失败时保留原始历史；
- 每次 Compact 尝试保存 transcript；
- 相同历史上的失败自动 Compact 不会无限重试。

当前没有：

- transcript 自动恢复；
- 大 Tool Result 的归档读取工具；
- checkpoint 文件重新加载；
- 向量检索；
- 每文件语义卡片注入；
- 跨进程 continuation。

### 8.2 Memory

- 每轮最多读取 `.memory/MEMORY.md` 前 2,000 字符；
- 将其作为静态 Memory Context 注入；
- 没有 Memory 写入工具；
- 没有 embedding 或相似度检索；
- 当前仓库不存在 `.memory` 目录。

### 8.3 Skill

- 只扫描运行时 `skills` 目录的直接子目录；
- 每个 Skill 需要一个 `SKILL.md`；
- `load_skill` 返回全文；
- 当前仓库没有已安装 Skill。

实现：

- `aqours_code/compact.py`
- `aqours_code/context.py`
- `aqours_code/skills.py`

## 9. 临时 Subagent

| 角色 | 权限 | 最大工具轮 | 最大唯一路径 | 最大工具调用 | 最大输出 |
|---|---|---:|---:|---:|---:|
| `general-purpose` | 读写、Bash | 6 | 20 | 40 | 8K |
| `explore` | 只读 | 1 | 8 | 10 | 4K |
| `plan` | 只读、Glob | 3 | 12 | 18 | 5K |
| `review` | 只读 | 2 | 16 | 20 | 8K |

共同特征：

- 新鲜 Context；
- 共享模型调用预算和 Case 截止时间；
- 角色权限不能默认扩大父 Runtime 权限；
- 重复读取相同文件范围会被拒绝并要求复用证据；
- 到达工具轮上限后进行一次 tool-free synthesis；
- 输出要求结构化 JSON；
- 预算不足时返回 `budget_reserved`，不继续启动可选委派。

缓存策略：

- 一个 Lead 运行内 Explorer 只实际运行一次；
- 后续 Explorer 请求复用缓存；
- 同一 `mutation_revision` 的 Reviewer 只实际运行一次；
- 文件再次变化后可以运行新 Reviewer。

### 当前没有自动 Reviewer

Reviewer 只有模型显式调用 `delegate_agent(role="review")` 时才运行。Harness 不会在第一次 Final 前自动运行 Reviewer。

单元测试 `test_complex_task_does_not_start_an_automatic_reviewer` 明确验证了这一行为。

实现：

- `aqours_code/agent_profiles.py`
- `aqours_code/subagent.py`
- `tests/test_multiagent_roles.py`

## 10. 持久 Teammate、Task、Mailbox 与 Plan Approval

Teammate 是后台 daemon thread，具备：

- Bash；
- 读取、写入和编辑文件；
- Glob；
- 共享 Task 列表；
- claim/complete Task；
- 消息发送；
- `submit_plan`。

行为：

- 优先读取 Mailbox；
- 空闲时自动认领第一个无依赖 pending Task；
- Task 绑定 Worktree 时自动切换文件工具工作目录；
- `submit_plan` 后进入硬等待，直到 Lead approve/reject；
- 完成一个 Task 后继续等待新 Task；
- 直到收到 shutdown 或 Runtime 清理。

“持久”的边界：

- 交互 CLI 中可跨多个 Task 存活；
- 非交互 Eval 结束时会清理线程；
- 程序重启后不会恢复 Teammate 的模型 Context。

并发可靠性缺口：

- Task claim 是 load-check-save，没有文件锁或 compare-and-swap；
- 多 Teammate 同时 claim 时存在竞争；
- Mailbox 是 JSONL append、read、unlink，没有可靠的跨进程原子协议；
- Task ID 和 Teammate 名称的路径级校验不足；
- 适合实验性协作，不适合作为生产级调度器。

实现：

- `aqours_code/teammate.py`
- `aqours_code/task_system.py`
- `aqours_code/message_bus.py`
- `aqours_code/protocol.py`
- `aqours_code/autonomous.py`

## 11. Git Worktree

支持：

- 从 HEAD 创建 `wt/<name>` 分支；
- 将 Worktree 绑定 Task；
- Teammate claim Task 后在对应 Worktree 中操作；
- 检查未提交文件和 Worker 提交数量；
- 保留 Worktree 供人工检查；
- 集成前确认 Worktree 干净；
- 检查 Lead 未提交文件是否与 Worker 变更重叠；
- 无重叠时进行 `git merge --no-ff`；
- Merge 失败时自动 abort；
- 成功后可清理 Worktree。

限制：

- `finalize_worktree()` 存在，但没有暴露为 Agent 工具；
- Teammate 必须通过 Bash 自己执行 `git add/commit`；
- 没有 `worker` 临时 Agent；
- 没有自动 Worker Worktree 提交流程；
- `general-purpose` 临时 Agent 直接修改 Lead 工作区，不使用 Worktree。

实现：`aqours_code/worktree_system.py`

## 12. Intent 功能现状

当前工作树没有 `aqours_code/intent.py`。

只剩 `classify_delegation_intent()`，它是关键词启发式分类器：

- 修改意图；
- Review 意图；
- Plan 意图；
- Explore 意图。

主流程主要用它判断 `task` 或 `general-purpose` 委派是否可能修改代码，从而决定是否触发 Todo Gate。

它不负责：

- 全局任务理解；
- 自动任务分解；
- 自动工具选择；
- 自动角色路由；
- 持久意图状态；
- 从对话历史恢复用户目标。

`task` 仍然固定进入 `general-purpose`，不会根据分类结果切换角色。

另外，当前部分中文关键词已经乱码，因此中文自然语言的 Todo、Acceptance、Complexity、Intent 和一次性定时语义识别可能不可靠。

## 13. 后台任务和定时任务

### 13.1 后台 Bash

自动识别的慢命令包括：

- pytest / unittest；
- pip install；
- npm/pnpm/yarn test、install、build；
- cargo test/build；
- Docker build；
- Go、Maven、Gradle、Make 构建测试。

后台完成后注入 XML 风格的 `<task_notification>`。大输出保留头尾，总计约 4,000 字符。

交互 CLI 中，Lead 可以先结束当前轮次，结果在后续轮次注入；有 Case Deadline 的 Eval 会在 Final 前等待后台任务。

### 13.2 Cron 和 Once

持久文件：

- `.scheduled_tasks.json`
- `.scheduled_once_tasks.json`

支持：

- 五字段 Cron；
- 一次性延迟；
- 本地 ISO 时间；
- recurring/durable；
- 到期后重新进入同一个 Agent Loop。

限制：

- 程序关闭期间错过的一次性任务不会补跑；
- `list_crons` 当前只显示重复 Cron，不显示一次性任务；
- `cancel_cron` 可以取消 `once_*`。

实现：

- `aqours_code/background.py`
- `aqours_code/cron.py`

## 14. MCP 的真实状态

当前 MCP 是教学 Mock，不是真正的 MCP Client。

只有两个模拟 server：

### `docs`

- `search`
- `get_version`

### `deploy`

- `trigger`
- `status`

连接后会动态生成：

```text
mcp__docs__search
mcp__docs__get_version
mcp__deploy__trigger
mcp__deploy__status
```

当前没有：

- MCP stdio transport；
- SSE/HTTP transport；
- 外部 MCP server process；
- capability negotiation；
- resources；
- prompts；
- 真正的外部系统调用。

权限层只要 MCP 工具名包含 `deploy` 就要求交互批准，因此非交互 Eval 中连只读 `mcp__deploy__status` 也会被拒绝。

实现：`aqours_code/mcp.py`

## 15. 安全模型

### 15.1 文件工具

`read_file`、`write_file`、`edit_file` 和 `glob` 会验证解析后的路径仍在当前工作区或 Teammate Worktree 内。

### 15.2 本地 Bash

Bash 会拒绝常见删除和高风险命令，包括：

- `rm`、`rmdir`、`del`、`Remove-Item` 等删除程序；
- `sudo`；
- shutdown/reboot；
- mkfs；
- `dd if=`；
- 部分高风险重定向和权限修改。

但本地 CLI Bash 不是沙箱。除这些规则外，它仍可能：

- 访问工作区外路径；
- 执行宿主机程序；
- 发起网络请求；
- 修改非工作区文件；
- 启动其他进程。

### 15.3 Docker Eval

`--execution docker` 使用更强的隔离：

- Agent 运行在一次性容器；
- `--network none`；
- 根文件系统只读；
- 非 root 用户；
- drop all capabilities；
- `no-new-privileges`；
- 默认 1 GB 内存、1 CPU、128 PID；
- `/tmp` 为 noexec tmpfs；
- 只挂载 disposable workspace、state、runtime 和 Broker IPC；
- API key、宿主项目、Docker socket、trusted grader 不进入 Agent 容器。

Agent 和 Grader 使用两个独立容器。Agent 结束后，Host 只把 `allowed_changes` 应用到干净的 grading workspace，再由独立 Grader 验证。

## 16. Host Model Broker

Model Broker 用于让 API key 留在宿主机。

主要能力：

- 基于文件的原子 JSON IPC；
- nonce 和 request ID 校验；
- 固定单 Case 模型白名单；
- 每次最多 16K tokens；
- 每 Case 模型调用预算；
- 总 requested-token 预算；
- Provider 超时和有限重试；
- 实际 input/output/cache token 统计；
- Case Deadline；
- Broker stats 只读挂载给 Agent 容器。

尾部预算策略：

- 保留模型调用预算的 20%；
- reserve 最少 4 次、最多 8 次；
- reserve 内禁止启动新的可选 Subagent；
- reserve 内避免模型生成的可选 Compact；
- 仅剩一次调用时移除所有工具，强制生成 Final；
- 预算耗尽前阻止再发一个超预算请求。

实现：`aqours_code/model_broker.py`

## 17. Trace、Timeline 和运行记录

每次运行保存：

- `trace.jsonl`
- `timeline.jsonl`
- `timeline.md`
- `metadata.json`
- `final.md`
- 运行索引

Trace 记录：

- 用户 Prompt；
- LLM request/response；
- Provider usage；
- Tool use/result；
- Hook 允许/拒绝；
- 后台路由和结果；
- Context Compact；
- Acceptance Gate；
- Subagent；
- 错误；
- Final。

默认保留策略：

- 7 天；
- 100 个 Run；
- 总计 300 MB；
- 单 Run 20 MB；
- 支持 `.keep` pinned run。

安全处理：

- Trace 对常见 API key、token、password、authorization 做脱敏；
- Tool Result 在 Trace 中会被截断；
- Compact transcript 没有复用完整 Trace 脱敏流程，可能保存用户消息或文件输出里的敏感内容。

实现：

- `aqours_code/trace.py`
- `aqours_code/trace_analysis.py`

## 18. Eval Harness

当前仓库共有 18 个自建 Eval Case，支持：

- scripted 离线模型；
- real-model；
- local compatibility mode；
- Docker Agent；
- Docker Grader；
- trusted input tamper 检测；
- allowed/forbidden change manifest；
- 独立 pass/fail 与连续 100 分评分；
- Provider 实际 token 统计；
- 运行时、工具调用、模型调用和可靠性指标。

评分权重：

| 维度 | 权重 |
|---|---:|
| Functional correctness | 50 |
| Code quality | 20 |
| Runtime efficiency | 15 |
| Token cost | 15 |

运行时和 token 得分会乘以 functional correctness gate，因此快速空提交无法只靠成本低获得高分。

相关代码：

- `evals/run_eval.py`
- `evals/docker_sandbox.py`
- `evals/scoring.py`
- `aqours_code/eval_container_entry.py`

## 19. 当前状态的持久化边界

| 状态 | 保存位置 | 跨 Compact | 跨用户轮次 | 跨进程 |
|---|---|---:|---:|---:|
| 普通消息历史 | 内存 | 部分，旧消息会摘要 | CLI 中是 | 否 |
| Acceptance Todo | RunState 内存 | 是，动态重注入 | 否 | 否 |
| RunKnowledge | RunState 内存 | 是 | 否 | 否 |
| `.memory/MEMORY.md` | 文件 | 是 | 是 | 是 |
| Skill | 文件 | 是 | 是 | 是 |
| Shared Task | `.tasks/*.json` | 是 | 是 | 是 |
| Mailbox 消息 | `.mailboxes/*.jsonl` | 是 | 读取后删除 | 未读消息可保留 |
| Worktree | Git + `.worktrees` | 是 | 是 | 是 |
| Cron | JSON 文件 | 是 | 是 | 是 |
| Teammate Model Context | 线程内存 | 是 | CLI 进程内是 | 否 |
| MCP connection | 进程内存 | 是 | CLI 进程内是 | 否 |
| Trace | `.aqours_code/runs` | 不适用 | 是 | 是 |
| Compact transcript | `.transcripts` | 不适用 | 是 | 是，但不会自动恢复 |

## 20. README 与当前实现的主要偏差

当前 README 中以下描述已经过期或强于实际实现：

1. “最终修改后自动运行 Reviewer”——当前不会自动运行；
2. “存在 `worker` 临时角色”——当前没有该角色；
3. “Worker 自动创建和提交 Worktree”——当前没有；
4. “`task` 根据 Intent 自动路由角色”——当前固定 general-purpose；
5. “Agent performs one bounded review before finalizing”——只有模型显式调用 Review 时成立；
6. “Reviewer skipped_budget 自动状态”——没有自动 Reviewer 路径；
7. “临时 Worker 的修改由 Harness 自动提交”——当前需要 Teammate 自己通过 Bash commit。

`ARCHITECTURE.md` 中关于 RunKnowledge 和 Runtime 迁移状态的描述更接近当前实现。

## 21. 主要技术缺口和风险排序

### P0：最终正确性闭环

- Acceptance Final Gate 不检查 `evidence_valid`；
- 仅有 evidence 文本也能将 Acceptance 标为 completed；
- stale/unbound 证据不能阻止最终完成；
- Reviewer 不是自动步骤。

### P1：配置和请求原子性

- 多工具调用没有事务和回滚；
- Task claim 没有原子 compare-and-swap；
- Mailbox 读写缺少可靠锁；
- Acceptance 在第一次成功修改前仍可被替换。

### P1：多 Agent 与 Worktree 闭环

- 没有 Worker Subagent；
- 没有自动 Worker Worktree；
- `finalize_worktree()` 未暴露；
- general-purpose 直接修改 Lead 工作区；
- Teammate 线程模型和状态不能跨进程恢复。

### P1：Context 与恢复

- transcript 仅记录，不恢复；
- RunKnowledge 不持久化；
- Acceptance 不跨运行；
- 没有真正 checkpoint resume。

### P2：功能名与真实能力不一致

- MCP 实际是 Mock；
- Memory 是静态 2,000 字符注入；
- Intent 只是关键词分类器；
- README 描述与实现不一致；
- 中文关键词存在乱码。

### P2：本地安全

- 本地 Bash 不是沙箱；
- 主要依赖删除命令和少量 deny-list；
- Task/Teammate 名称和部分持久化路径校验不足；
- Compact transcript 的脱敏弱于 Trace。

## 22. 最终能力分级

### 已真实接入且相对扎实

- Lead 工具循环；
- 文件读写与 Bash；
- DeepSeek V4 Flash max reasoning；
- Provider 错误恢复；
- Context 计量与 Compact；
- 后台 Bash；
- Trace/Timeline；
- Host Model Broker；
- Docker Agent/Grader 隔离；
- 四种有边界的临时 Subagent；
- 基础 Worktree 集成；
- Eval 和连续评分。

### 已接入但闭环不完整

- Todo/Acceptance；
- RunKnowledge；
- Reviewer findings；
- 持久 Teammate；
- 文件 Task/Mailbox；
- Cron/Once；
- Skill；
- `.memory/MEMORY.md`；
- Worktree 协作。

### 当前不存在或只是名义能力

- 完整 Intent；
- 自动 Reviewer；
- Worker 临时 Agent；
- 自动 Worker Worktree 提交；
- 真正 MCP；
- 向量长期记忆；
- 运行中断恢复；
- Acceptance checkpoint 文件；
- 跨工具事务；
- 可靠的多 Teammate 原子任务领取。

## 23. 给后续评审者的核心问题

如果下一步要继续改进 Agent，建议优先回答以下问题：

1. Final Gate 是否应该强制要求所有 completed Acceptance 的 RunKnowledge 状态为 `verified`？
2. Acceptance 是否应在首次创建后立即锁定，而不是首次修改后才锁定？
3. 是否恢复自动 Reviewer，还是明确保留“模型自选 Review”的设计？
4. 是否仍需要持久 Teammate，还是简化为有 Worktree 隔离的临时 Worker？
5. 是否需要把 Task claim 和 Mailbox 改成真正原子存储？
6. 是否需要真正可恢复的 checkpoint，而不只是 Context 摘要？
7. Mock MCP 是否应删除、改名，或替换成正式 MCP Client？
8. README 是否应按照当前真实实现重写，避免后续评测和设计判断基于过期架构？

