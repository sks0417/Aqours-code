# Harness 问题台账

本文档记录通过真实 Eval 暴露的 Harness 问题、证据、根因、候选方案和验收标准。
它不是普通功能愿望清单：只有能够由运行结果复现、并会影响 Agent 正确性、评测可信度、
诊断能力或资源使用的问题才进入此处。

## 维护规则

每次压力 Eval 后按以下流程更新：

1. 保存 `summary.json`、`trace.jsonl`、`timeline.md`、`change_manifest.json` 和最终回答。
2. 用稳定 ID 新增或更新问题，不因重复出现而创建第二条。
3. 区分 Agent 自身失败、Harness 放大因素和纯诊断展示问题。
4. 记录可复现证据，不记录 API key、模型凭据或其他敏感内容。
5. 修复后补充测试、验证命令和结果；只有通过原始复现 case 才可标记为 `Validated`。

状态定义：

- `Open`：问题已确认，尚未形成首选方案。
- `Proposed`：已有首选方案，尚未实现。
- `In progress`：正在实现。
- `Fixed`：代码已修改且单元测试通过，尚未通过原始压力 case。
- `Validated`：原始压力 case 已验证修复有效且无明显回归。
- `Won't fix`：明确接受当前行为，并记录原因。

严重性定义：

- `P0`：破坏隔离、安全或评分可信边界。
- `P1`：使复杂任务无法有效完成，或系统性地产生错误评测结果。
- `P2`：显著降低诊断质量、指标可解释性或运维判断。
- `P3`：局部体验或低频可观测性问题。

## 问题总览

| ID | 严重性 | 状态 | 模块 | 摘要 | 首次发现 |
| --- | --- | --- | --- | --- | --- |
| HARN-001 | P1 | In progress | Context / Compact | `micro_compact` 的近期批次保留策略仍会丢失跨文件工作集，诱发重复读取循环 | 2026-07-16 inventory stress Eval |
| HARN-002 | P1 | Validated | Eval Runner / Grading | Agent 终止错误会完全跳过 grader，所有维度归零并丢失可用的部分诊断 | 2026-07-16 inventory stress Eval |
| HARN-003 | P2 | Fixed | Docker lifecycle metrics | grader 未启动时，清理汇总显示失败，容易被误解为容器泄漏 | 2026-07-16 inventory stress Eval |
| HARN-004 | P2 | Validated | Trace analyzers | 分析脚本使用旧 Trace schema，错误地把工具结果显示为长度 0 | 2026-07-16 inventory stress Eval |
| HARN-005 | P1 | Validated | Docker Eval image | Eval 默认静默复用旧镜像，使运行结果与当前 Harness 源码不一致 | 2026-07-18 inventory stress Eval |
| HARN-006 | P1 | Validated | Model Broker timeout | Broker IPC deadline 与 Host HTTP timeout 同值，单次慢请求会以错误层级终止整个 case | 2026-07-18 inventory stress Eval |
| HARN-007 | P2 | Validated | LLM Trace accounting | Compact summary 模型调用未写入 LLM Trace，导致调用数和耗时不完整 | 2026-07-18 inventory stress Eval |
| HARN-008 | P2 | Validated | Docker image build | Harness 源码变更会使依赖安装层失效，瞬时 PyPI TLS 故障阻断镜像刷新 | 2026-07-18 inventory stress Eval |
| HARN-009 | P2 | Fixed | Background task routing | bash 慢操作检测按任意子字符串匹配，使 `cat tests/...` 被错误后台化 | 2026-07-18 inventory stress Eval |
| HARN-010 | P2 | Fixed | Docker execution metrics | Agent 失败时容器结果不返回内部 command metadata，summary 错报 bash 执行数为 0 | 2026-07-18 inventory stress Eval |
| HARN-011 | P2 | Validated | Background task Trace | 完成的后台任务通知会进入模型上下文，但未写入 Trace/Timeline，无法审计测试结果 | 2026-07-19 inventory stress Eval |
| HARN-012 | P1 | Fixed | Todo / Final gate | Todo 只跟踪操作完成，不跟踪外部契约证据，公开测试通过后 Agent 会过早宣布完成 | 2026-07-19 inventory stress Eval |
| HARN-013 | P1 | Validated | Final contract audit | final 审计没有文件范围和读取预算，正确实现完成后仍会重新扫描仓库并耗尽 Broker 调用额度 | 2026-07-19 inventory stress Eval |
| HARN-014 | P1 | Validated | Glob tool | `**` glob 没有启用递归，源码树发现不完整并诱发重复 glob/bash 探索 | 2026-07-19 inventory stress Eval |
| HARN-015 | P1 | Fixed | Multiagent orchestration | Subagent/Worktree 只有孤立工具能力，Leader 不会按复杂度编排角色，也没有安全的 Worker 结果集成闭环 | 2026-07-20 inventory stress 复盘 |
| HARN-016 | P1 | Validated | Call-budget orchestration | Explorer/Reviewer、final audit 和 compact 共享总额度但没有收尾预留，问题在最后被发现后已无调用可供修复 | 2026-07-20 inventory stress Eval |
| HARN-017 | P1 | Fixed | Legacy subagent routing | `task` 仍绕过角色预算、LLM Trace 和 Worker Worktree，在主 workspace 启动通用读写 subagent | 2026-07-20 inventory stress Eval |
| HARN-018 | P1 | Fixed | Reviewer finding / Todo identity | Reviewer finding 以完整文案作为锁定身份，Lead 无法用证据稳定关闭，导致收尾重复审计和 final 状态自相矛盾 | 2026-07-21 inventory stress Eval |
| HARN-019 | P1 | In progress | Benefit-aware role activation | 静态或单次运行时信号都不能证明 Explorer 有替代收益；自动激活保持关闭 | 2026-07-21/22 automatic Explorer 对照实验 |
| HARN-020 | P1 | Validated | Eval scoring / Provider usage | `passed` 与 100 分绑定，宽松过程阈值掩盖耗时和 Token 成本，requested max tokens 又被误当成实际消耗 | 2026-07-22 scoring audit |
| HARN-021 | P1 | Validated | Bash permission parsing | 删除命令按任意子字符串匹配，把 `Confirm raises` 中的 `rm ` 当成删除命令并提前终止 Agent | 2026-07-22 inventory stress Eval |
| HARN-022 | P2 | Validated | Trusted case copy | case 模板中的 `.pytest_cache` 可因本地权限状态导致可信副本复制在 Agent 启动前失败 | 2026-07-22 ledger stress Eval |
| HARN-023 | P1 | Validated | Capability eval coverage | 单一库存 case 已不能区分 Lead 与多组件恢复能力，缺少更难且分组计分的综合 case | 2026-07-22 eval coverage review |
| HARN-024 | P2 | Fixed | Reviewer observability | Reviewer 因尾部预算跳过时仍被记录和注入为 attached/observed | 2026-07-22 ledger stress Eval |

## 首次压力运行基线

- Case：`stress_inventory_reservation_consistency`
- Provider/model：`deepseek / deepseek-chat`
- Run ID：`20260716-150615-9940ba44`
- Eval run directory：`evals/results/runs/20260716-230605/stress_inventory_reservation_consistency`
- 结果：`budget_exhausted`，381.4 秒，最终分数 0
- Broker：40 次成功调用，第 41 次因 call budget 被拒绝
- 工具：196 次，其中 `read_file=148`、`bash=39`、`glob=7`、`todo_write=2`
- 变更与测试：`edit_file=0`、`write_file=0`、pytest 执行 0、workspace 变更 0
- Context：`micro_compact=39`、`snip_compact=23`
- 隔离：无权限拒绝、Agent 容器正常退出并清理、Broker IPC 与 Agent state 均已清理

该运行是以下问题的共同复现基线。修复时应保留这批产物，避免只用小型 synthetic test
证明行为。

---

## HARN-001：当前工具结果批次被 `micro_compact` 拆散

**严重性：** P1  
**状态：** In progress
**模块：** `codepilot_s20/compact.py`、`codepilot_s20/agent_loop.py`、`codepilot_s20/config.py`

### 现象

Agent 在 40 个模型回合内反复读取相同文件，却没有编辑或执行测试：

- `service.py` 被读取 19 次；
- `models.py` 被读取 16 次；
- `state.py`、`errors.py`、`inventory_repository.py` 各被读取 15 次；
- 前 37 条 bash 命令几乎全部是重复 `cat`；
- 模型后期明确表示 “there seems to be caching issues”，随后仍继续重读。

Agent 第二次 todo 更新已经列出原子预留、重复取消和状态转换等正确方向，说明主要阻塞不在
完全无法理解任务，而在无法维持足够的跨文件上下文进入实施阶段。

### 根因

每次 LLM 调用前，`prepare_context()` 都会执行 `micro_compact()`。当前实现收集历史中的
所有 tool results，并只保留全局最近 `KEEP_RECENT_TOOL_RESULTS = 3` 个完整结果：

```python
for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
    if len(str(block.get("content", ""))) > 120:
        block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
```

DeepSeek 经常在一个响应中并行读取 5～11 个文件。这些调用执行完后，下一轮模型请求之前，
同一个最新批次中前面的 2～8 个文件内容已经被替换。模型因此无法同时看到自己刚请求的完整
文件集合，只能再次读取。

首轮读取 README 和 4 个 glob 结果后，第一次 micro compact 立即把上下文从 7420 字节
压缩到 1981 字节；README 是该批次第一个结果，因此在下一次模型调用前已经丢失。这与第二轮
再次读取 README 的行为直接吻合。

`tool_result_budget` 没有触发，因为单个输出小于 30,000 字符且批次总量小于 200,000；
问题不是输出过大，而是按全局结果个数压缩破坏了最新批次的完整性。后续 23 次
`snip_compact` 又删除了更早消息，进一步放大循环。

### 影响

- 中等规模跨文件任务可能退化为无限探索循环；
- 模型调用和 requested-token 预算被无效消耗；
- Agent 无法进入修改、测试和迭代阶段；
- Eval 最终测到的是 Compact 适应失败，而不是预期的业务推理能力；
- 单纯提高 `max_model_calls` 只会延长循环，不能解决根因。

### 首选方案

将 micro compact 改为“压力触发、按工具交换批次压缩”，而不是每轮都按全局 block 数量裁剪：

1. **未达到压力阈值时不压缩。** 例如上下文达到 `CONTEXT_LIMIT` 的 70%～80% 才触发，
   压缩到 55%～65% 后停止，避免 7 KB 上下文也被提前破坏；
2. **永远保护尚未被模型消费的最新批次。** 即使其后插入了 todo reminder 或后台通知，也应
   向后查找最新的 user tool-result message，而不是只检查 `messages[-1]`；
3. **按完整 exchange/batch 处理。** 一次 assistant tool-use message 和对应 user tool-result
   message 作为一个整体；同一批次中的 8 个结果要么保留，要么在已消费后整体降级，不能只保留
   最后 3 个结果；
4. **保留一个受字节预算约束的近期工作集。** 除未消费批次外，从新到旧保留最近的完整批次，
   直到工作集预算耗尽；需要回收空间时从最旧的已消费批次开始，达到目标水位即停止；
5. **超大当前批次先持久化再降级。** 保留 tool-use ID、工具名、关键输入、输出大小、摘要/头尾
   和可读取路径，不能直接替换成鼓励模型重新执行的 `Re-run if needed`；
6. **保持协议配对。** micro compact 只替换 result content，不删除任一侧；`snip_compact` 如需
   删除消息，则必须同时删除对应的 assistant tool-use 与 user tool-result。

`tool_result_budget` 也需要随此修复一起校正：当前默认批次预算为 200,000 字节，而
`CONTEXT_LIMIT` 只有 50,000；并且当总量超标但每个结果都小于 `PERSIST_THRESHOLD=30,000`
时，循环调用 `persist_large_output()` 不会缩小任何结果。应将批次预算绑定到 context limit，
并提供 `force=True`（或独立函数）来持久化多个中等大小的结果，确保聚合预算一定能收敛。

临时把 `KEEP_RECENT_TOOL_RESULTS` 调大只能作为诊断实验，不应作为最终修复，因为批次大小并不
固定，也无法保证 tool-use/result 配对完整。

### 实施记录（2026-07-18）

采用轻量改造保留原有分层压缩骨架，没有引入额外模型调用：

- `CONTEXT_LIMIT` 保持 50,000；新增 `MICRO_COMPACT_TRIGGER=35,000` 和
  `MICRO_COMPACT_TARGET=28,000`；
- `micro_compact` 改为保留最近两个完整 tool-result messages，只在超过软水位后从最旧批次
  开始压缩，并删除会诱发重读的 `Re-run if needed`；
- 最新批次预算从 200,000 调整为 20,000，向后查找最新 tool-result message，不再受 todo
  reminder 或后台通知影响；
- `persist_large_output(force=True)` 支持强制持久化多个中等输出，并按批次数量分配预览字符，
  超宽批次会进一步移除预览但保留完整输出路径；
- 执行顺序调整为 `tool_result_budget -> micro_compact -> snip_compact -> compact_history`；
- `snip_compact` 只有在消息数和上下文压力同时超标时才执行；
- automatic/reactive full compact 保留最近 5 条消息，并在切点处保护 tool-use/result 配对；
- 显式 `compact` 工具在近期 tool-use 被保留时会追加对应的结构化 tool-result。

新增 `tests/test_compact.py`，并在 `tests/test_agent_loop_fake_model.py` 增加真实 context pipeline
集成测试：scripted model 一次读取 8 个文件，下一轮验证第一个和最后一个结果仍可见，再完成编辑。

- 定向测试：`17 passed`；
- 全量测试：`163 passed, 3 skipped, 1 deselected`。

当前代码和单元/集成测试已通过，因此状态为 `Fixed`。原始 Docker 压力 case 尚未复测，暂不标记
为 `Validated`。

2026-07-18 的第一次复测不构成有效验证：运行未使用 `--docker-build`，实际镜像创建于 Harness
修改之前。Trace 仍显示 13,498 字符时触发 micro compact、full compact 后只剩一条摘要等旧版
行为，详见 HARN-005。HARN-001 因此继续保持 `Fixed`，而不是 `Validated`。

### 有效复测与重新打开（2026-07-18）

使用 `--docker-build --request-timeout 60` 成功构建当前 Runtime 后，原始压力 case 再次复现同类
循环。Run directory：
`evals/results/runs/20260718-171607/stress_inventory_reservation_consistency`。

- 40 次 Broker 调用全部成功返回，第 41 次请求被 call budget 拒绝；requested tokens 为
  `320000 / 328000`；
- 40 个 response 全部以 `tool_use` 结束，平均响应 4.69 秒、P95 19.56 秒、最大 20.45 秒，排除
  60 秒 request timeout；
- 127 次工具调用中，`read_file=101`、`bash=17`、`glob=6`、`todo_write=3`；
- 11 个实现文件各读取 8 次，`api.py` 读取 7 次；测试和 README 也被重复读取；
- `edit_file=0`、`write_file=0`、没有执行 pytest，change manifest 没有提交变更；
- `micro_compact` 触发 25 次，累计删除 194,779 字节；`snip_compact` 触发 11 次，累计删除
  18,191 字节。

新实现保护“最近两个 tool-result messages”，但真实工作流中的一个 message 可能只是 todo 结果。
例如第 5 回合读取了 12 个实现文件，第 6 回合读取测试，第 7 回合只更新 todo；此时 todo 和测试
成为最近两个批次，包含完整实现工作集的第 5 回合随即被压缩。第 8 回合模型明确说明
“re-read the actual source files since they were compacted”，之后持续循环。

终止前，第 39 回合再次用 bash 读取 10 个实现文件，第 40 回合继续读取 README 和测试。下一轮
context pipeline 将 63,469 字节压到 58,709，再经 snip 降到 57,370，仍超过
`CONTEXT_LIMIT=50,000`，于是触发 automatic summary。该 summary 正好是第 41 次 Broker 请求，
在调用入口被拒绝，成为最终错误。Broker 限额只是保险丝，单纯提高到更多调用仍会延长重读循环。

因此原轻量修复只能保护单个宽批次，不能保护跨多个回合形成的工作集，HARN-001 从 `Fixed` 重新
打开为 `Open`。下一版仍应保留当前分层骨架，但把回收优先级改为：先压缩相同路径和相同内容的
重复读取，再压缩低价值结果；在字节预算内保留每个近期文件的最新版本。todo/reminder 不能占用
“近期工具结果批次”名额，并增加跨 4～6 个回合、夹有 todo 与测试读取的 scripted 回归用例。

### 第二轮轻量优化（2026-07-18）

先实现用户指定的两个子项，不改变 35,000/28,000 水位和 full compact 流程：

1. `micro_compact` 现在通过 tool-use ID 识别批次来源；纯 `todo_write` 结果不再占用最近两个
   working-result 批次名额。普通 reminder 和 background notification 本身不是 tool-result batch，
   同样不会占用名额；未知工具结果仍按信息批次保护，避免 schema 变化导致静默误删。
2. 达到 micro pressure 后，先扫描 `read_file` 结果。只有规范化路径相同且完整输出逐字相同时，
   才压缩较旧副本，并在 marker 中指向保留完整内容的新 tool-result ID。同一路径但内容不同的读取
   不去重，从而保留编辑前后的不同版本。
3. 重复读取回收发生在普通旧批次回收之前；如果去重后已经达到 target，其他独立证据不会被压缩。

新增回归测试覆盖 todo + reminder 不挤占源码批次、重复读取优先回收，以及同路径不同内容不得去重：

- Compact 定向测试：`9 passed`；
- Compact/Agent loop/Harness 定向测试：`25 passed`；
- 完整测试：`166 passed, 3 skipped, 1 deselected`。

这些是 HARN-001 的部分修复，尚未解决所有跨不同文件批次的工作集保留，因此状态为
`In progress`，需要完成后续策略并重新运行原始压力 case。

### 第二轮优化有效复测（2026-07-18）

使用当前镜像运行原始 case：
`evals/results/runs/20260718-231805/stress_inventory_reservation_consistency`。本轮最终因独立的
HARN-006 timeout 失败，但 context 行为出现实质改善：

- 工具调用从上一轮 127 降到 72，`read_file` 从 101 降到 61；
- `micro_compact` 从 25 次降到 10 次，`snip_compact` 从 11 次降到 0；
- 模型没有耗尽 40 次调用预算，在第 28 次 accepted Broker call 后因网络 timeout 停止；
- Agent 首次进入实现阶段，执行 4 次 `edit_file`，修改
  `inventory_repository.py`、`service.py`、`state.py` 三个允许文件；
- todo 已将四项业务修复标记完成，并把“运行测试”设为 in progress；最终 timeout 发生在请求测试
  命令的前一个模型回合，因此 Trace 中仍没有 pytest。

保存 workspace 的公开测试为 `15 passed, 2 failed`；两处失败都来自 `service.py` 使用
`ReservationStatus` 但缺少 import。可信 grader 的诊断分为 64/100，另发现 request fingerprint
仍未包含 quantity，说明实现尚未完成，但已经不再停留在纯探索阶段。

这证明本轮两个子项方向有效，但同一实现文件仍读取 2～6 次，HARN-001 保持 `In progress`。本轮
不能作为最终 `Validated`，因为独立网络 timeout 在 Agent 运行测试和修正剩余问题前终止了流程。

### 完整执行复测（2026-07-19）

Run directory：`evals/results/runs/20260719-000928/stress_inventory_reservation_consistency`。
本轮 Harness 未提前终止，Agent 在 35 次 LLM 请求内完成修改和两次公开测试，可信 grader 正常启动并
给出 77 分：

- 工具调用从上一轮 72 次保持为 72 次，其中 `read_file=40`、`edit_file=8`、`bash=3`、
  `todo_write=7`；读取比上一轮 61 次进一步下降，并成功修改三个实现文件；
- `tool_result_budget=2`、`micro_compact=13`、`snip_compact=13`，分别回收约 4,648、81,980、
  24,749 字符；同一文件仍读取 2～5 次，但没有重新退化成耗尽 40 次调用的纯探索循环；
- Agent 两次运行公开测试并看到 `17 passed`，随后正常提交最终回答；grader、Agent 容器、Broker、
  IPC 和 state 均正常完成或清理；
- grader 失败来自两个未修复完整的业务根因：库存回滚只捕获 `InsufficientInventory`、没有覆盖
  `UnknownSku`；`request_fingerprint()` 仍遗漏 quantity，使不同数量请求产生相同指纹。

这次结果说明当前 Context 优化已经把 HARN-001 从“阻止任务实施”降为“仍有重复读取和压缩成本”，
但 Agent 明明读取过 README、`serialization.py` 和 `inventory_repository.py`，仍遗漏了两个直接写在契约和
代码里的边界，因此 77 分主要是 Agent 业务推理/验证覆盖不足，不应归因于 Harness 提前终止。
HARN-001 继续保持 `In progress`，后续优化应聚焦跨文件工作集和契约检查，而不是单纯扩大调用预算。

### 第三轮压力复测与路径工作集根因（2026-07-19）

使用重新构建镜像的 `20260719-214544` 在第 41 个 Broker request 被 40 次 call budget 拒绝。失败后
diagnostic grader 正常运行并得到 65 分；Agent 只提交 `inventory_repository.py` 和 `service.py`，
atomic、idempotency conflict、cancellation/state 与 regression 分组仍失败。

本轮 81 次工具调用中有 `read_file=58`、`glob=8`。Final audit 在第 36 次模型请求后才触发，因此额度
主要不是消耗在新 gate：audit 前已有 36 次请求、75 次工具调用和 53 次读取；audit 后只有 5 次请求、
6 次工具调用。Trace 给出了跨批次工作集丢失的直接链路：

- 第 5 回合一次读取 12 个源码文件，第 6 回合读取 3 个测试，第 7 回合单独读取 validation；
- 最近两个 working-result message 随即变成“测试批次 + 单文件批次”，整个 12 文件源码批次失去保护；
- 第 9 回合模型明确写出 “earlier output was compacted”，重新读取 11 个源码文件；
- 建立 Todo 后，第 13～14 回合又读取第三轮核心源码；最终 `service.py` 共读 8 次，其余实现文件读
  3～5 次。

这证明“Todo 不占名额”和“相同路径相同内容优先去重”都有效，但只保护两个 message 仍无法表达一个
跨多回合形成的代码工作集。

### 第三轮轻量优化（2026-07-19）

继续沿用现有确定性 micro-compact，没有增加模型 summary 或常驻 README：

- 新增 `KEEP_RECENT_READ_PATHS=12`；除了最近两个普通工作批次，micro-compact 还从新到旧保护最多
  12 个不同 `read_file` 路径的最新结果；
- 路径数量是软上限，若一个原子批次一次读取多个文件，则保留整个批次，避免再次从批次中间拆散；
- 相同路径、相同内容的较旧副本仍先压缩；同一路径修改前后的不同版本仍保留最新版本语义；
- 该策略只在已有消息中改变回收优先级，不向每轮 system prompt 添加文件摘要；相比提高全局 Context
  上限，它用有限的额外输入保留工作集，以减少整批重读和额外模型回合；
- 新增同构测试：12 文件源码批次之后插入测试批次和单文件复读，源码批次仍保持可见；另验证超过
  路径软上限时最旧结果仍会回收。

聚焦回归 `30 passed`，完整回归 `205 passed, 3 skipped, 1 deselected`。状态保持 `In progress`，等待
重新构建镜像后的原 case 验证第 9、13 回合整批重读是否消失。

### 第三轮优化有效复测（2026-07-19）

`20260719-221750` 正常完成 Agent、final 和 grader，36 次 Provider attempt、57 次工具调用、
`read_file=36`，最终 89 分。相比前一轮调用上限失败，模型调用从 40 降到 36、工具从 81 降到 57、
读取从 58 降到 36、Context compact 事件从 34 降到 6，并且没有 Broker budget、IPC 或 lifecycle
错误。递归 glob 首次就返回嵌套源码，不再回退到 bash `find`；Agent 修改 3 个实现文件并两次完成
公开测试。

新的路径工作集显著减少 micro-compact，但也让上下文两次到达约 50,000 字符并触发 automatic full
compact。两次 summary 后仍出现局部源码复读，说明严重的“每几回合整仓重读”已经消失，但 full
compact 的摘要质量与可观测性成为剩余瓶颈。为保持轻量设计，不继续提高全局 Context 上限；summary
提示改为保留 inspected file/symbol map、`any/all/different/unchanged` 契约、normalized/fingerprint
字段和异常/状态分支，并区分已验证事实与假设。HARN-001 继续保持 `In progress`。

### 辅助保护

在主修复之外，可增加低侵入的重复探索检测：同一路径在没有文件变更、没有测试执行的情况下
重复读取超过阈值时，向 Agent 注入一次“停止重读，基于已有证据采取行动”的恢复提示。该机制
只能作为保险，不能替代正确的上下文保留。

### 验收标准

- 单元测试：最新批次包含 8 个 tool results 时，8 个结果在下一轮全部可见；
- 单元测试：上下文低于触发水位时，历史结果数量超过 3 个也不会触发 micro compact；
- 单元测试：最新结果后存在 reminder/notification 时，仍能正确识别并保护最新批次；
- 单元测试：更早批次的大结果仍会被压缩；
- 单元测试：assistant tool-use 与 user tool-result 不被拆对；
- 单元测试：多个小于 30,000 字节的结果使批次总量超标时，预算逻辑能够实际降到目标值；
- 单元测试：超大最新批次仍遵守字节预算并可从持久化路径读取；
- 集成测试：scripted model 一次读取至少 8 个文件后，能引用第一个和最后一个结果并完成编辑；
- 压力复测：原 inventory case 在预算内至少产生实现改动并运行测试；
- 诊断目标：同一实现文件的重复读取显著下降，grader 能够启动。

---

## HARN-002：Agent 终止错误导致 grader 完全跳过

**严重性：** P1
**状态：** Validated
**模块：** `evals/run_eval.py`

### 现象

本次运行因 `budget_exhausted` 结束后：

- `grader_execution.status = not_started`；
- 五个评分维度全部为 0；
- case grader 的分组测试、代码质量和过程评分均未执行；
- 结果无法区分“已经完成部分正确修改但没来得及结束”和“完全没有动手”。

本次 workspace 恰好没有改动，但复杂任务中 Agent 可能在耗尽预算前已经完成部分有效修复。
当前短路行为会丢失这些重要诊断信息。

### 根因

`run_case()` 在检测到 `agent_error` 后直接构造 failure result，并跳过 clean grading workspace
和独立 grader。后续还会再次把结果覆盖为 score 0。该行为对 sandbox/完整性失败是安全的，
但对 budget exhausted、模型 API 错误或最终回答阶段错误过于粗粒度。

### 第二次实证（2026-07-18）

`20260718-231805` 因 provider SSL handshake timeout 结束时已经提交 3 个允许文件、4 次编辑。
Canonical Eval 仍显示 score 0、五个维度全 0、`grader_execution.status=not_started`。在相同保存
workspace 上手工运行可信 grader 得到诊断分 64/100：

- outcome correctness 19/40；
- constraints 15/15；
- process quality 5/20；
- code quality 15/15；
- efficiency 10/10。

公开测试为 `15 passed, 2 failed`，而不是完全没有实现。这次运行直接证明当前短路会丢失有价值的
部分完成度信息，同时也证明诊断分必须与 canonical pass/fail 分离。

### 第三次实证（2026-07-19）

`20260719-163333` 在 40 次模型调用后因 Broker call budget 结束，提交了 4 个合法源码文件。
Canonical Eval 再次显示 score 0 且 grader 未启动；对保存的同一 workspace 手工运行可信 grader 得到
98/100：outcome 40、constraints 15、process 20、code quality 15、efficiency 8。atomic、retry、conflict、
cancellation、regression 和 API compatibility 全部分组通过，唯一扣分原因是 87 次工具调用。这证明即使
Agent 没能产生正常 final，workspace 也可能已经完整解决任务，诊断评分不能继续被丢弃。

### 首选方案

增加“失败后诊断评分”，同时保持 canonical Eval 仍然失败：

1. 区分不可评分错误与可评分终止错误：
   - 不可评分：可信输入被修改、sandbox 完整性失败、workspace 无法安全快照；
   - 可评分：`budget_exhausted`、模型错误、Agent 最终回答错误，但 workspace 与 manifest 可用。
2. 对可评分错误照常创建 clean grading workspace 并运行可信 grader；
3. 顶层 `passed` 必须保持 `false`，`failure_category` 保留原始 Agent 错误；
4. 将 grader 输出放到独立字段，例如：
   - `diagnostic_score`
   - `diagnostic_breakdown`
   - `diagnostic_grader`
5. 在明确评分政策前，不用诊断分覆盖 canonical `score=0`，避免改变已有基准语义。

### 实施记录（2026-07-19）

- 对 Broker budget、Provider/API、IPC 和普通 Agent 终止错误，在可信输入、manifest 和 sandbox 状态
  安全且仍有 case 时间时，照常创建 clean grading workspace 并启动独立 grader；
- 顶层 `passed=false`、`score=0`、原始 `failure_category` 和失败原因保持不变；真实评分写入
  `diagnostic_score`、`diagnostic_breakdown` 和 `diagnostic_grader`；
- case timeout、sandbox error、trusted-input 修改、unexpected/forbidden manifest 不进入失败后诊断
  路径；
- summary 新增 `diagnostically_graded_cases` 和 `avg_diagnostic_score`，控制台失败行直接显示
  `diagnostic_score`；grader stdout/stderr 和容器 lifecycle 也按实际执行保存；
- 定向回归覆盖“Broker budget 失败但 diagnostic grader 得 100”和“case timeout 不启动 grader”。

### 验收标准

- budget exhausted 且 workspace 有合法改动时，可信 grader 会运行；
- 顶层结果仍为失败，failure category 仍为 `budget_exhausted`；
- 诊断字段能显示已通过和未通过的功能组；
- protected/unexpected changes 仍然 fail closed；
- sandbox、trusted-input 或 manifest 完整性错误绝不触发不安全 grading；
- 现有 clean-room 和双容器信任边界保持不变。

### 真实压力验证（2026-07-19）

`20260719-211240` 再次因 40 次 Broker call budget 结束。此次 canonical 语义保持为
`passed=false`、`score=0`、`failure_category=budget_exhausted`，同时可信 grader 自动启动并得到
`diagnostic_score=100`，所有六类 outcome group、constraints、process、code quality 和 efficiency 均满分。
`grader_execution.status=completed`，grader 容器也成功清理。该结果完整满足失败后诊断评分的验收标准，
HARN-002 标记为 `Validated`。

---

## HARN-003：未启动 grader 被汇总成清理失败

**严重性：** P2  
**状态：** Fixed
**模块：** `evals/run_eval.py`、summary schema

### 现象

本次运行中：

- `agent_container_cleanup_succeeded = true`；
- `agent_phase_cleanup_succeeded = true`；
- `model_broker_stopped = true`；
- `model_broker_ipc_cleaned = true`；
- `agent_state_cleaned = true`；
- grader 因 Agent 失败而从未启动，cleanup 值为 `null`；
- 但 `all_container_cleanup_succeeded` 和顶层 `container_cleanup_succeeded` 显示为 `false`。

从当前字段很容易误判为存在容器泄漏，实际没有任何已启动容器清理失败。

### 根因

聚合逻辑要求 Agent cleanup 和 grader cleanup 都严格为 `true`。当 grader 未启动时，后者为
`null`，整个表达式得到 `false`。当前测试明确固化了这一语义，因此它不是偶发实现错误，而是
指标语义把“流程未完成”和“已创建资源清理失败”混在了一起。

### 首选方案

拆分两个概念：

- `all_started_containers_cleanup_succeeded`：只聚合实际启动过的容器；
- `lifecycle_complete`：Agent 和预期 grader 阶段是否都按流程完成。

保留每个阶段的 started/status/cleanup 三态字段。若必须兼容旧字段，应明确标记 deprecated，
并在 summary 中优先展示新字段。

### 验收标准

- Agent 容器成功清理、grader 未启动时：started-container cleanup 为 `true`；
- 同一场景下 lifecycle complete 为 `false`；
- 任一已启动容器清理失败时 cleanup 聚合为 `false`；
- summary 和文档不会把 `not_started` 描述成资源泄漏；
- 更新现有 lifecycle 测试覆盖三态组合。

### 实施与验证（2026-07-22）

保留旧 `all_container_cleanup_succeeded` 语义以兼容已有消费者，新增
`all_started_containers_cleanup_succeeded` 只聚合实际启动的容器，并用 `lifecycle_complete`
单独表达 Agent 与 grader 是否都完成预期阶段。grader 未启动而 Agent 已清理时，新清理字段为 true、
lifecycle 为 false；任一已启动容器清理失败仍为 false。异常结果、case 结果和顶层 summary 均覆盖新字段。
三态回归已进入 `250 passed, 3 skipped, 1 deselected` 全量测试；真实 Docker smoke 验证正常完整流程两字段
均为 true。尚未制造一次真实模型“Agent 失败、grader 不启动”的容器样本，因此状态为 `Fixed`。

---

## HARN-004：Trace 分析脚本与当前 schema 不一致

**严重性：** P2  
**状态：** Validated
**模块：** `analyze_trace.py`、`analyze_timeline.py`、Trace schema 测试

### 现象

对本次完整 Trace 运行现有分析脚本时，196 个 tool results 几乎全部显示：

```text
Tool Result: | OK | size=0
```

错误事件的正文也为空。这与 `trace.jsonl` 和 `timeline.md` 中存在完整工具输出明显矛盾，导致
排查时必须重新编写临时解析脚本。

### 根因

分析脚本仍按旧字段读取：

- `analyze_trace.py` 读取 `result` 和 `isError`，当前 Trace 使用 `content`；
- `analyze_timeline.py` 期待 `tool_call`、`toolCall`、`result`、`isError`；
- 当前 timeline 使用 `tool_use`、`tool`、`input`、`content`、`status`；
- 两个脚本的说明仍限定为历史上的 “5000-file test”，没有作为当前通用诊断工具维护。

### 候选方案

1. 更新两个脚本以当前 `trace.py` 生成的 schema 为唯一事实来源；
2. 优先复用共享解析函数，避免脚本再次手写字段名；
3. 输出聚合统计：event types、tool counts、重复路径、测试命令、Compact 次数、错误和权限拒绝；
4. 对长内容只显示长度与安全预览，不输出可能含敏感信息的完整正文；
5. 将脚本重命名或改写为通用 run analyzer，移除 “5000-file test” 假设。

### 验收标准

- 用当前 Trace fixture 验证 tool result 长度和状态正确；
- timeline analyzer 能识别当前全部核心事件类型；
- 错误信息、Compact 统计和工具名称不再为空；
- schema 变更时有测试失败，而不是静默输出错误数据；
- 对损坏 JSONL 行保持容错并明确报告解析错误。

### 实施与验证（2026-07-22）

新增共享 `codepilot_s20.trace_analysis`，两个 CLI 不再各自手写 schema。当前字段
`tool/content/status/input` 是主路径，旧字段只作兼容；输出包含 event/tool 计数、重复读取、测试命令、
compact、错误和权限拒绝，预览会折叠长文本并脱敏常见 credential。损坏 JSONL 会报告精确行号并以非零
状态退出。对真实 `20260722-213701` Trace 的分析得到 327 个事件、50 个工具结果、7 个 compact、
1 个权限拒绝，并正确显示重复路径和两次 pytest 命令；不再出现长度 0。状态标记为 `Validated`。

---

## HARN-005：Docker Eval 静默运行旧版 Harness 镜像

**严重性：** P1
**状态：** Validated
**模块：** `evals/run_eval.py`、`evals/docker_sandbox.py`、`evals/docker/Dockerfile`

### 现象

2026-07-18 在 HARN-001 修复后执行了未带 `--docker-build` 的 Docker Eval。命令表面正常启动，
但实际使用的 `codepilot-s20-eval:py311` 镜像已创建约 47 小时，早于 Context 修复。

Trace 明确表现为旧实现：

- `micro_compact` 在 13,498、13,129、32,398 字符时触发，违反新实现的 35,000 触发水位；
- automatic compact 将 50,351 字符、52 条消息压成 1,741 字符、1 条消息，没有保留最近 5 条；
- 完整 compact 后模型重新从 glob 和读取全部源码开始探索；
- 因此该运行不能用于判断 HARN-001 是否有效。

### 根因

Dockerfile 使用 `COPY codepilot_s20 /opt/codepilot-src/codepilot_s20` 将 Runtime 固化进镜像。
`run_eval.py` 只有收到显式 `--docker-build` 时才执行构建；默认路径不会比较当前源码和镜像，
也不会在 summary 中标记源码版本是否匹配。

### 再次复现（2026-07-19）

`20260719-211240` 未带 `--docker-build`。Host 侧新的失败后诊断评分已经生效并返回 100 分，但容器内
仍运行 HARN-013 修复前的 Agent Runtime：`acceptance_gate` 事件缺少新实现必有的
`changed_file_count` 和 `read_budget`；review 后仍执行 22 次工具调用，其中 `read_file=16`、`glob=3`，
又消耗 10 次模型请求直到额度耗尽。该运行再次证明单次 Eval 可以出现“Host runner 是新版、Agent
容器是旧版”的混合状态，也证明它不能用于验证 HARN-013。

### 首选方案

1. 构建镜像时计算 Runtime source fingerprint，并写入 Docker image label；
2. 每次 Docker Eval 前计算当前 fingerprint 并读取 image label；
3. 不一致时默认 fail fast，提示使用 `--docker-build`，不能继续调用真实模型；
4. 如确实需要旧镜像，提供显式 `--allow-stale-image`；
5. summary 记录 image ID、创建时间、镜像 fingerprint、当前 fingerprint 和匹配状态；
6. README 中所有真实 Docker Eval 示例明确包含构建或有效性检查。

### 验收标准

- 当前源码与镜像不匹配时，在第一次模型调用前失败；
- `--docker-build` 后 fingerprint 匹配并正常运行；
- 修改 `codepilot_s20/compact.py` 后旧镜像会被检测为 stale；
- summary 能证明本次结果对应哪一版 Runtime；
- 测试使用 mock Docker inspect/build，不要求真实网络。

---

## HARN-006：Host HTTP timeout 与 Broker IPC deadline 同值导致边界竞态

**严重性：** P1  
**状态：** Validated
**模块：** `codepilot_s20/model_broker.py`、`codepilot_s20/eval_container_entry.py`、`codepilot_s20/recovery.py`、`evals/run_eval.py`

### 现象

同一次复测在第 27 个 Trace LLM request 处结束：

- 请求时间：`1784363946.4219997`；
- Timeout error：`1784363976.5541523`；
- 等待时间约 30.13 秒，与 `request_timeout=30` 精确吻合；
- Broker 仅使用 28/40 次调用和 218,000/328,000 requested tokens；
- 600 秒 case deadline 仍剩约 281 秒；
- 请求仅含 6 条消息，刚经过完整 compact，不是超长上下文；
- 没有权限拒绝、容器超时或 Broker budget rejection。

Agent 因单个慢请求直接失败，grader 再次未启动。

### 60 秒配置再次复现（2026-07-18）

把 `--request-timeout` 提高到 60 秒没有消除竞态，只把失败边界后移：

- 最后一个 Trace request：`1784388196.4935665`；
- Agent error：`1784388256.6544225`，等待约 60.16 秒；
- 容器收到 `[Error] TimeoutError: model broker request exceeded its deadline`；
- Host broker 记录的实际 provider error 是
  `URLError: <urlopen error _ssl.c:993: The handshake operation timed out>`；
- Broker 仅使用 28/40 次调用和 218,000/328,000 requested tokens；
- 600 秒 case deadline 尚未耗尽，容器、IPC 和 Agent state 均正常清理。

该请求发生在 Agent 已完成 4 次编辑、准备运行测试之后。Host 和 IPC 使用相同 60 秒 deadline，
导致容器仍先报告 IPC 层超时，无法获得 Host 已识别出的 SSL handshake 错误，也没有机会执行一次
受控重试。因此“difficulty 5 使用 60 秒”只能缓解普通慢响应，不能替代 deadline 分层。

### 根因

容器内 `BrokerModelClient` 的 IPC 等待 deadline 是 30 秒；Host 侧模型 HTTP timeout 也通过同一个
`MODEL_REQUEST_TIMEOUT=30` 设置。两侧没有交付宽限期：容器可能先抛出
`model broker request exceeded its deadline` 并删除 IPC 文件，Host 还没有机会返回结构化的 HTTP
timeout/error。Runner 随后终止 Broker，因此统计中 `model_broker_error` 仍为空。

当前 recovery 也不会重试普通 `TimeoutError`；一次瞬时延迟会直接结束整个 Agent。

### 首选方案

1. 分离 Host HTTP timeout 和容器 IPC deadline；IPC deadline 至少为 HTTP timeout 加 5 秒交付宽限；
2. 两者继续受 case-wide deadline 约束；
3. 让 Host timeout 先返回结构化错误，并由 Host Broker 在剩余预算内最多重试一次；
4. 重试前确保前一个 Host 请求已经结束，不能产生重叠的重复模型请求；
5. 对 difficulty 5 压力 case 考虑 60 秒 request timeout，但不能代替上述竞态修复；
6. Trace 记录 request ID、Host 开始/结束、IPC 交付和具体 timeout 层级。

### 验收标准

- 模拟 Host 在 30 秒 HTTP timeout 时，容器收到 Host 的结构化 timeout，而不是先发生 IPC timeout；
- 有总体剩余时间时最多重试一次并可继续；
- 已耗尽 case deadline 时不重试；
- timeout 不会留下请求/响应 IPC 文件或孤立 Broker 进程；
- summary 能区分 `provider_http_timeout`、`broker_ipc_timeout` 和 `case_timeout`。

### 实施记录（2026-07-18）

- `--request-timeout` 继续表示单次 Host Provider 请求上限。Docker 容器的 IPC 等待窗口改为
  `provider_timeout × (1 + retries) + retry_delay × retries + delivery_grace`；当前默认值为一次重试、
  1 秒间隔和 5 秒交付宽限，因此 Provider timeout 为 60 秒时，IPC 最多等待 126 秒。两个窗口仍受
  case-wide deadline 截断。
- 重试集中在 Host `ModelBroker` 中执行。只有 timeout、连接、TLS 以及 HTTP
  `408/429/500/502/503/504/529` 会重试，且必须等上一请求明确结束后才能发起下一次调用；401 等
  永久错误不重试。
- 每次 Provider 尝试都占用一次 Broker call budget 和对应 requested-token budget。重试前同时检查
  call、token、case 剩余时间和 Broker 停止状态；预算不足时保留原始 Provider 错误并记录
  `retry_skipped_reason`。
- Host 返回带 `error_kind` 和 request ID 的 `BrokerRemoteError`。该错误及 `BrokerIpcTimeout` 标记为
  Broker 已管理，Agent 的通用 recovery 不会再次重试同一个逻辑请求，避免尝试数被乘倍放大。
- 完整窗口内仍未收到响应时，容器抛出独立的 `broker_ipc_timeout`，清理本次 IPC 文件并停止；容器端
  不盲目重发。`summary.json` 现在可区分 `provider_http_timeout`、`broker_ipc_timeout` 和
  `case_timeout`。
- Broker stats 新增逻辑 request 数、Provider attempt 数、retry 数、Provider error 数、最后错误层级、
  最后 Provider 错误、跳过重试原因以及 Provider/IPC timeout，便于压力 Eval 直接判断失败发生在哪层。
- 定向测试：`30 passed`；完整测试：`178 passed, 3 skipped, 1 deselected`。
- 重新构建当前 Docker 镜像后的 scripted 端到端回归：`read_file_basic` 得分 100；metadata 正确记录
  2 个逻辑 request、2 次 Provider attempt、0 次 retry、`provider_timeout=2`、`ipc_timeout=10`，且
  Broker 已停止、IPC 已清理。

### 真实压力验证（2026-07-19）

原始 `stress_inventory_reservation_consistency` case 使用 `--request-timeout 60 --docker-timeout 600`
完成了完整 Agent 和 grader 流程：

- 35 个逻辑 Broker request 对应 35 次 Provider attempt，未出现 rejection、Provider error 或 IPC timeout；
- metadata 正确记录 `provider_timeout=60`、`ipc_timeout=126`；
- Agent 在 304.2 秒内正常结束，grader 给出 77 分，而不是被 timeout 或 Broker budget 提前终止；
- Broker 已停止，IPC、Agent state 以及 Agent/Grader 容器均清理成功。

本轮没有实际触发网络重试，因此真实重试分支仍由瞬时 timeout/503 的自动化测试覆盖；但原始压力路径已
稳定越过此前 30/60 秒失败点并完成评分，HARN-006 的功能性修复标记为 `Validated`。Provider attempt
级别的 Trace 关联与其他缺失事件继续由 HARN-007、HARN-011 跟踪。

---

## HARN-007：Compact summary 调用未进入 LLM Trace

**严重性：** P2  
**状态：** Validated
**模块：** `codepilot_s20/compact.py`、`codepilot_s20/trace.py`、`codepilot_s20/agent_loop.py`

### 现象

2026-07-18 运行中：

- Broker 统计 `model_broker_calls=28`；
- Trace 只有 27 个 `llm_request` 和 26 个 `llm_response`；
- automatic compact 从 `1784363911.798` 持续到 `1784363934.286`，约 22.5 秒；
- 这次 summary 调用计入 Broker budget，但没有对应的 LLM request/response 事件。

因此仅依赖 Trace 会低估模型调用数、requested-token 消耗和模型等待时间，也无法判断 summary
是否是延迟或预算瓶颈。

`20260718-231805` 再次得到同样差值：Broker accepted calls 为 28，Trace 只有 27 个
`llm_request` 和 26 个 `llm_response`。其中 automatic compact 从 `1784388063.428` 到
`1784388081.983`，约 18.55 秒；恰好有一个未记录的 compact summary 请求。最后一个普通 Agent
request 则只有 request 和 timeout error，没有 response，因此 27/26 的另一处差值是正常失败语义。

`20260719-221750` 再次提供更清晰的计数：Broker 记录 35 个逻辑 request、36 次 Provider attempt 和
1 次 retry，Trace 只有 33 个 `llm_request`。差出的两个逻辑请求恰好对应两次 automatic compact
summary；Provider retry 则是 attempt 层级差值。这证明当前 Trace 无法单独还原 request/attempt/purpose
三层消耗。

`20260720-221357` 中 Broker 成功调用 40 次、第 41 次被拒绝，Trace 只能直接数到
34 个成功 Lead response 和 3 个 Reviewer response。差出的 3 次与 3 个 `compact(kind=automatic)`
一一对应。第三次 compact 正好消耗最后一次成功额度，但在 LLM Trace 中仍没有
request/response，排查时只能通过 Broker 计数反推。

### 根因与首选方案

`summarize_history()` 直接调用 `client.messages.create()`，绕过 `agent_loop.call_llm()` 中的记录逻辑。
应提供统一的受信模型调用包装器，并为每次请求记录 `purpose`，例如：

- `agent_turn`；
- `compact_summary`；
- `subagent_turn`；
- `teammate_turn`。

Trace 同时记录 request ID、max tokens、开始/结束时间、状态和错误；Eval 汇总按 purpose 分组，并与
Broker call count 做一致性校验。

### 验收标准

- 一次 automatic compact 同时产生 `compact_summary` request 和 response；
- summary timeout 产生带相同 request ID 的 error；
- Trace LLM 调用总数与 Broker accepted call count 一致；
- Agent、summary、Subagent、Teammate 消耗可以分开汇总。

### 实施与验证（2026-07-20）

`summarize_history()` 现在与 Lead/role 一样记录 `llm_request` 和 `llm_response`，
`purpose=compact_summary`、`tool_count=0`、`max_tokens=2000`；失败时额外记录
`compact_summary_error`。回归测试验证 request/response purpose 和顺序。代码与完整回归已通过，
状态标记为 `Fixed`；还需要下一次真实 Broker 压力 Trace 确认 logical request 与 Provider
retry 的分层计数后才能标记 `Validated`。

### 真实 Broker 验证（2026-07-21）

Run `evals/results/runs/20260721-230949/stress_inventory_reservation_consistency` 中 Broker 为
40 calls / 40 logical requests / 0 retries，Trace 也有 40 组 `llm_request`/`llm_response`：
35 次 `purpose=lead`、3 次 `purpose=delegate_agent, agent_role=reviewer`、2 次
`purpose=compact_summary`。总数完全对账，compact 与 role 消耗可以按 purpose 分离，原始压力 case
已满足验收条件，状态提升为 `Validated`。

---

## HARN-008：Docker 镜像刷新受 PyPI 瞬时网络故障阻断

**严重性：** P2  
**状态：** Validated
**模块：** `evals/docker/Dockerfile`、`evals/docker_sandbox.py`、Docker build 测试

### 现象

使用 `--docker-build` 刷新 HARN-001 修复后的镜像时，构建在安装 `pytest==8.2.2` 阶段失败：

```text
SSLError: UNEXPECTED_EOF_WHILE_READING
Could not fetch URL https://pypi.org/simple/pytest/
ERROR: No matching distribution found for pytest==8.2.2
```

最后一行是无法访问包索引后的派生错误，不表示该版本不存在。随后使用同一命令再次构建，仍在
同一层、同一 PyPI TLS 握手阶段失败，说明仅靠人工重跑不足以解决 Harness 迭代中的构建脆弱性。
第一次运行在 15.6 秒内 fail fast：

- `container_started=false`；
- `model_broker_calls=0`；
- `command_execution_count=0`；
- 旧的 47 小时镜像仍保留，没有被失败构建覆盖；
- 因此没有消耗模型额度，也没有产生可分析的 Agent Trace。

### 放大因素

修复前 Dockerfile 的顺序是：

1. COPY requirements；
2. COPY `pyproject.toml`、README 和整个 `codepilot_s20`；
3. 在同一个 RUN 中安装第三方依赖和本地 Runtime。

因此任何 Runtime 或 README 修改都会使整个 pip RUN 层失效，即使 `requirements.lock` 没变，也会
重新访问 PyPI。`--no-cache-dir` 又阻止 pip wheel cache 在层重建时提供帮助。

### 首选方案

1. 在 COPY Harness 源码之前，单独安装 `requirements.lock`，形成只受 lockfile 影响的缓存层；
2. COPY 源码后使用 `pip install --no-deps /opt/codepilot-src`，避免第二次解析/下载依赖；
3. 为依赖下载设置有限的重试和合理 timeout，保留 TLS 验证，不能使用不安全的 trusted-host 绕过；
4. 可使用 BuildKit pip cache mount，但必须保持 lockfile 是依赖事实来源；
5. 构建失败时继续保留旧镜像，并明确标记当前镜像仍 stale；
6. 与 HARN-005 的 source fingerprint 配合，只有成功构建的新镜像才能进入真实 Eval。

### 验收标准

- 只修改 `codepilot_s20/compact.py` 时，第三方依赖安装层命中缓存；
- 修改 `requirements.lock` 时依赖层正确重建；
- 本地 Runtime 安装不访问包索引；
- 模拟依赖下载失败时，在模型调用前返回明确的 `docker_build_error`；
- 失败构建不会覆盖已有可用镜像，也不会把旧镜像误标为 current。

### 实施记录（2026-07-18）

- 将 `requirements.lock` 的安装移动到 Harness 源码 COPY 之前，使该镜像层只受 lockfile 变化影响；
- 使用 BuildKit pip cache mount 保存下载缓存，lockfile 变化导致重建时可复用已下载的包；
- 保留 TLS 验证，设置 `PIP_DEFAULT_TIMEOUT=60` 和有限的 `PIP_RETRIES=8`；
- pip 策略位于系统包安装层之后，调整 pip 参数不会额外使稳定的 apt 层失效；
- 本地 Runtime 改为 `pip install --no-cache-dir --no-deps /opt/codepilot-src`，源码更新时不再访问包索引；
- 用户、目录和权限初始化也移出源码安装层，进一步缩小 Harness 迭代时需要重建的范围；
- Dockerfile 结构测试验证依赖安装严格早于源码 COPY、本地安装使用 `--no-deps`，并禁止
  `--trusted-host` 绕过 TLS；
- 针对性测试：`22 passed, 1 deselected`；
- 完整测试：`163 passed, 3 skipped, 1 deselected`。

该修复降低的是“源码频繁变化导致重复访问 PyPI”的放大效应。一个全新的 Docker builder 或首次
安装新 lockfile 时仍然需要成功访问 PyPI。镜像新鲜度和失败后 stale 标记仍由 HARN-005 负责。

### 当前验证阻塞（2026-07-18）

修复后执行了三次不调用模型的纯镜像构建：

1. 初版曾加入 Dockerfile frontend syntax 指令，但它会在解析前额外访问 Docker Hub；该指令已移除，
   避免为 cache mount 新增 registry 网络依赖；
2. pip 参数最初位于 apt 层之前，导致旧 apt 缓存失效；现已移动到 apt 层之后；
3. 最终构建确认 apt 层和 requirements COPY 层均命中缓存，新的独立 requirements 层执行了预期的
   8 次 TLS 重试，但仍无法访问 PyPI。

构建日志还显示 Docker Hub 和 Debian 请求均经由一个拒绝连接的
`127.0.0.1:20036` 转发端点。`docker info` 报告 daemon 的 HTTP/HTTPS proxy 均为
`http.docker.internal:3128`；Windows WinHTTP 为 direct，当前用户 Internet Settings 未配置代理，
宿主机端口 20036 也没有监听进程。用户随后确认 VPN 重启后代理端口会随机变化：本次 VPN 实际端口
已经变为 `16339`，而 Docker Desktop 的手工代理仍指向上一次的 20000+ 端口。因此当前剩余阻塞是
陈旧的手工 VPN 代理端口，而不是 pytest 版本、TLS 证书校验代码或 Agent。

环境侧首选方案是把 VPN 的 HTTP/mixed proxy 配置为固定端口，并让 Docker Desktop 始终使用该端口；
如果 VPN 不支持固定端口，则每次 VPN 重启后都必须同步 Docker Desktop 的 proxy 配置并重启 Docker
Desktop。Harness 不应猜测随机 VPN 端口，但后续可以增加构建网络 preflight，将这类问题明确分类为
`docker_proxy_unreachable`，避免误报成 Python package resolution failure。

用户修正 VPN 端口后，`20260718-171607` 压力运行已成功完成 `--docker-build` 并启动 Agent，证明
当前镜像可构建。尚未用“只改 Harness 源码后再次构建”的日志验证第三方依赖层持续命中缓存，因此
HARN-008 保持 `Fixed`，暂不标记 `Validated`。

### 2026-07-22 复核：`--no-deps` 仍会触发 build isolation

评分 Harness 更新后再次执行 `--docker-build`，源码 COPY 之前的依赖层虽然命中缓存，但本地
`pip install --no-deps /opt/codepilot-src` 仍按 `pyproject.toml` 创建隔离构建环境，并尝试从 PyPI
下载 `setuptools>=68`。失效代理因此再次阻断纯源码刷新。`--no-deps` 只忽略项目运行依赖，并不禁止
PEP 517 build-system 依赖下载，原实施记录对此判断不完整。

Eval 容器只通过 `python -m codepilot_s20.eval_container_entry` 启动，不需要安装 console script 或
wheel metadata。因此 Dockerfile 现在直接保留 `/opt/codepilot-src/codepilot_s20`，设置只读
`PYTHONPATH=/opt/codepilot-src`，并在镜像构建时执行 import check；源码刷新不再运行第二次 pip，也
不会访问包索引。结构测试同时禁止重新引入本地 pip install。针对性测试 `59 passed, 1 deselected`。

本次 Docker Desktop 重启后的冷 apt 层仍被陈旧的 `127.0.0.1:16295` 代理阻断，这属于上文已记录的
环境 preflight 缺口；为了完成评分验证，使用已有依赖镜像只覆盖 Runtime 源码生成了临时验证 tag，
没有把 stale 依赖镜像冒充成新鲜的常规 build。

之后的两次用户 inventory 复测和本轮 `20260722-223236` Docker smoke、`20260722-224825` ledger
压力复测均使用常规 `--docker-build` 成功刷新当前 Runtime；源码变化没有重新触发本地 build isolation
下载，依赖层持续复用。HARN-008 标记为 `Validated`。首次构建或 lockfile 变化仍需要网络属于预期边界。

---

## HARN-009：bash 慢操作检测误把测试路径当作测试命令

**严重性：** P2  
**状态：** Fixed
**模块：** `codepilot_s20/background.py`、background routing 测试

### 现象与根因

有效复测的最后一回合执行了四个普通读取命令：

```text
cat /workspace/README.md
cat /workspace/tests/test_public_reservations.py
cat /workspace/tests/test_public_validation.py
cat /workspace/tests/conftest.py
```

README 立即返回，但三个测试文件命令都得到 `[Background task ... started]`。原因是
`is_slow_operation()` 对整条 command 做任意子字符串匹配，slow keyword 中包含 `test`；因此路径中的
`tests` 和文件名中的 `test_` 也会命中。该问题不是本次 101 次重复读取的主因，但会让模型在关键时刻
拿不到普通文件读取结果，并增加异步通知和 context 噪声。

### 首选方案与验收标准

- 显式 `run_in_background=true` 继续拥有最高优先级；
- 自动后台化只识别命令结构，例如 `pytest`、`python -m pytest`、`npm test`、`cargo test`、明确的
  install/build/deploy 子命令，不扫描普通参数和路径中的任意子字符串；
- `cat tests/test_x.py`、`rg test src/` 和 `echo build` 必须同步执行；
- 为 Windows/Posix 引号、命令前环境变量和常见 shell 前缀增加参数化测试；
- Trace 应记录后台化原因是 explicit 还是 slow-command classifier。

### 实施与验证（2026-07-19）

`is_slow_operation()` 现在先拆分 shell command segment，再识别实际 executable/subcommand：支持
`pytest`、`python -m pytest`、`pip install`、npm/pnpm/yarn test/install/build、cargo、docker build、
go/maven/gradle、make，以及环境变量、`env`/`sudo`/`timeout` 和 `sh/bash -c` 前缀。参数、路径和
heredoc 正文不再参与动词匹配。

后台启动同时写入 `background_routed` 事件，明确区分 `explicit` 与 `slow_command`。参数化回归覆盖了
真实运行中的 `grep ... test_`、`python /tmp/debug_test.py` 和包含 pytest 文本的 heredoc，均保持同步；
真正的 pytest/build 命令仍自动后台化。与 HARN-011、Todo gate 一起执行的聚焦回归为 `40 passed`。
尚待重新构建镜像并用原压力 case 验证，因此状态为 `Fixed`，不是 `Validated`。

---

## HARN-010：Agent 失败时 Docker summary 丢失内部命令执行计数

**严重性：** P2  
**状态：** Fixed
**模块：** `codepilot_s20/eval_container_entry.py`、`evals/run_eval.py`、result schema

### 现象与根因

本次 Trace 有 17 次 `bash` tool use，stdout 也包含实际命令输出，但 case 和顶层 summary 的
`command_execution_count` 都是 0。成功路径会从 `run_agent_task()` 返回值中的 `execution` 读取
`LocalCommandExecutor.execution_metadata()`；异常路径只写入：

```json
{"ok": false, "error": "..."}
```

Host 因而只能把缺失计数默认成 0。这会把“执行了命令但 Agent 最终失败”错误展示成“从未执行命令”，
影响过程诊断和效率指标。

### 首选方案与验收标准

- `eval_container_entry` 在 success/failure 两条路径都写入同一份 execution metadata；
- result schema 将 `execution` 放到顶层，避免只有成功 `run_info` 才能携带；
- Host 对旧 schema 保持兼容，但新 schema 缺失该字段时应标记 unknown，而不是静默填 0；
- 模拟一次 bash 后抛出 Broker 错误，summary 必须报告 `command_execution_count=1`；
- direct、background、timed-out command 的计数语义需要分别定义并测试。

### 实施与验证（2026-07-22）

容器 entrypoint 现在只创建一个 `LocalCommandExecutor`，success/failure 都把其 metadata 写入顶层
`execution`。Host 优先读取新字段并兼容旧 `run_info.execution`，同时新增
`command_execution_count_known` 和 `command_execution_metadata_source`；旧 schema 的缺失仍保留数值 0
兼容，但不再与真实零混淆。回归模拟执行一次命令后抛出异常，result 仍报告 count=1；真实 Docker smoke
显示 `known=true, source=result`。尚待真实模型失败且内部执行过 bash 的完整容器样本，因此状态为 `Fixed`。

---

## HARN-011：后台任务完成通知未写入 Trace/Timeline

**严重性：** P2  
**状态：** Validated  
**模块：** `codepilot_s20/agent_loop.py`、`codepilot_s20/background.py`、`codepilot_s20/trace.py`

### 现象

`20260719-000928` 中，Agent 两次执行 `python -m pytest tests/ -v`，都被正确识别为慢操作并启动为
`bg_0001`/`bg_0002`。Trace 记录了后台启动占位结果和随后立即发生的空 `check_inbox`，但没有记录任何
`task_notification` 或真实 pytest 输出。与此同时：

- 第 34 个 LLM response 明确写出 `All 17 tests pass.`；
- 最终回答继续以该结果为验证依据；
- Agent 正常退出，background lifecycle 测试和现有控制流表明，完成通知会在 LLM 调用前注入 messages。

因此后台结果大概率已经正确回灌给模型，但 Trace/Timeline 无法证明它何时完成、返回了什么、被哪次
LLM request 消费。诊断者只看日志时会误判成 Agent 在空 inbox 后凭空宣称测试通过。

### 根因与首选方案

`inject_background_notifications()` 和 final-answer 前的 `collect_background_results()` 只把通知追加到
`messages`，没有调用 Trace 记录接口。给后台完成通知增加一等事件，例如：

```json
{
  "type": "task_notification",
  "task_id": "bg_0002",
  "status": "completed",
  "command": "python -m pytest tests/ -v",
  "summary_size": 1234,
  "truncated": false
}
```

Timeline 应展示通知摘要和注入位置；如果输出因长度限制被截断，应保留头尾、原始长度以及持久化路径。
通知事件必须在消费它的下一条 `llm_request` 之前写入，使验证结论可以被完整审计。

### 验收标准

- 后台 pytest 完成后，Trace 有一个带 task ID、状态和命令的 `task_notification`；
- Timeline 可看到 pytest 的通过/失败摘要以及通知发生顺序；
- 长输出明确记录截断和原始长度，不泄漏超出既有 Trace 安全边界的数据；
- final-answer 分支等待后台任务时收集的通知同样被记录，不能只覆盖循环顶部注入路径；
- scripted 集成测试证明模型看到的通知内容与 Trace 中记录的摘要一致。

### 实施与验证（2026-07-19）

后台通知保持字符串兼容，同时携带 task id、status、command、summary、原始输出长度和截断状态。
`tool_result_batch`、`loop_start`、`final_wait` 三个消费入口都会先写 `task_notification` Trace，再把同一
通知注入模型上下文。Timeline 展示命令、输出摘要、注入位置和截断信息；Trace 仍复用现有敏感字段
清理边界。

background lifecycle 集成测试实际启动一个受控 pytest 后台任务，证明：模型第三次调用看到了
`2 passed`，Trace 中对应事件的 task id/status/command/summary 一致，且 injection 为 `final_wait`。
与 HARN-009、Todo gate 一起执行的聚焦回归为 `40 passed`。

原压力 case 的 `20260719-161328` 复测产生两组完整事件：`bg_0001` 记录公开测试的 1 个失败，
`bg_0002` 记录修复后的 17 个测试全部通过；两者都有 command、status、summary、原始输出长度、截断
状态和 `tool_result_batch` 注入位置，并且都出现在下一次相关 LLM 判断之前。HARN-011 因而标记为
`Validated`。

---

## HARN-012：Todo 完成状态不能证明外部契约已经满足

**严重性：** P1  
**状态：** Fixed
**模块：** `codepilot_s20/basic_tools.py`、`codepilot_s20/tool_defs.py`、
`codepilot_s20/context.py`、`codepilot_s20/prompts.py`、`codepilot_s20/agent_loop.py`

### 现象与根因

`20260719-000928` 中，Agent 的 Todo 包含“修复库存回滚”“修复幂等冲突”“运行测试”等计划项。它完成
对应编辑并看到公开测试 `17 passed` 后，把全部 Todo 标记完成并输出 final。但可信 grader 仍发现：

- 回滚只覆盖 `InsufficientInventory`，没有覆盖 README 明确要求的 `UnknownSku`；
- Service 增加了指纹比较，但 `request_fingerprint()` 仍遗漏 README 明确要求的 quantity。

旧 Todo 只表示“执行过某项操作”，没有区分计划步骤和外部验收标准；`completed` 不需要代码、测试或
diff 证据。计划内容又由 Agent 自己生成，遗漏的契约不会阻止 final，因此公开测试覆盖不足时容易形成
“编辑完成 = 需求完成”的错误闭环。

### 首次实现的压力复测与二次根因（2026-07-19）

Run `evals/results/runs/20260719-154224/stress_inventory_reservation_consistency` 以 302.9 秒、分数 0
结束，Broker 40 次额度耗尽。Trace 有 39 个普通 LLM response，另一次额度被自动 compact summary
使用；工具共 76 次：`read_file=40`、`glob=18`、`bash=8`、`todo_write=7`、`edit_file=1`。最终只修改
`inventory_repository.py`，尚未进入 acceptance review。

该运行暴露了首次 gate 的两个 Harness 设计错误：

- gate 要求先从 README 提取 acceptance，却在 acceptance 建立前阻止 `read_file(README.md)`；首轮 1 个
  README read 和 7 个 glob 全部被拒绝，Agent 只能猜出“保持 API”“运行测试”两个泛化条件；
- acceptance 按完整文本锁定，Agent 将 `Preserve ...` 改写为语义相同的 `Ensure ...` 后连续收到 3 次
  Error，额外消耗 3 个模型回合。

此外，HARN-009 将包含 `test` 的 grep 和调试脚本错误后台化，HARN-011 又让日志无法直接看到后台
结果；三者共同放大了调用浪费。因此不能把本次失败简单归因于 40 次预算过小，也不应先加预算。

### 二次实现的压力复测与第三根因（2026-07-19）

Run `evals/results/runs/20260719-161328/stress_inventory_reservation_consistency` 正常完成 Agent 和
grader，190.9 秒、28 次模型调用、49 次工具调用、无 compact/Broker/IPC 错误，分数恢复到 77。相较
上一轮，模型调用从 40 降到 28、工具从 76 降到 49，证明 read-only gate、Todo reconciliation、后台
路由和通知 Trace 的效率修复有效。

但 Agent 只提交 `service.py` 和 `state.py`，可信 grader 仍发现：

- `UnknownSku` 发生在已成功扣减的行之后时没有回滚；
- 相同 idempotency key 对不同 normalized request 没有抛出 `IdempotencyConflict`；
- `request_fingerprint()` 仍未包含 quantity。

本次 acceptance gate 有事件，但在第 7 个模型回合、任何编辑之前就触发：初始 Todo 只有一个 plan，
且已标记 completed；其余 7 项全是 acceptance，因此“所有 plan 已完成”错误地被当成 final-ready。
唯一一次 review 被提前消费，真正 final 前不再复核。初始 7 条 acceptance 又漏掉 README 已明确写出的
“UnknownSku 或 InsufficientInventory 均保持所有仓储不变”“different normalized request 必须冲突”
以及 fingerprint 包含每个 `(sku, quantity)`。最终 Todo 用公开测试 17 passed 和局部代码说明将已有项目
全部完成，但无法发现根本没有进入清单的契约。

结论是：验收不能只审核 Agent 自己写出的 pending 项，也不能由 plan 状态猜测 final 时机；必须在模型
真正尝试 final 时，对完整 task/README 做一次新的遗漏审计。

### 实施记录（2026-07-19）

在原有 `todo_write` 骨架上做轻量扩展，没有增加新工具或额外 summary 模型：

- Todo 新增可选 `kind=plan|acceptance`，旧条目默认 `plan`，保持现有调用兼容；
- `acceptance` 表示来自 task/README/API contract 的可观察结果；标记 `completed` 时必须提交非空
  `evidence`；
- 复杂代码任务通过轻量关键词组合识别；README、源码、glob 等只读契约发现可以先执行，第一次
  `write_file`/`edit_file` 前才强制要求 plan 和具体 acceptance，解除先验信息循环；
- acceptance 在第一次文件修改前允许继续精炼；修改开始后进入保护状态。模型改写同位置条目时保留
  稳定原文并接收新的 status/evidence，遗漏条目时自动补回；工具返回提示但不把整个 Todo 更新判为
  Error，因此不会为措辞漂移制造模型重试循环；
- 每轮系统提示只携带简短 acceptance 状态，不携带整个 README 或 plan 列表，因此不受
  micro/snip compact 影响；Todo 最多 20 条、acceptance 最多 12 条，content/evidence 各最多 500
  字符，常驻 Token 成本有明确上界；
- 不再根据 plan 状态提前触发 review。复杂代码任务第一次真正产生 text-only final 时，无论已有
  acceptance 是否都标为 completed，都暂停 final 并注入一次 `<acceptance_review>`；
- final review 明确要求重新 `read_file` task/README、把当前清单视为可能不完整、检查错误路径、
  `any`/`different`/`unchanged` 等边界词以及 normalized data 的每个字段，再对照最终代码和测试；
- review 后必须再次成功调用 `todo_write`，以便遗漏契约重新进入 plan/acceptance/编辑闭环。若模型直接
  final，Harness 追加一次简短 follow-up；仍忽略时才允许结束并标记
  `[Acceptance review incomplete]`，不会无限循环；
- system/tool 提示进一步区分 plan（尚待执行的实现工作）和 acceptance（完整外部契约结果），并明确
  不能只从公开测试失败中提取标准；
- Trace 的 `acceptance_gate` 记录 `review`、`review_followup`、`audit_incomplete_final` 和
  `incomplete_final` 决策事件。

### 第三次实现的压力复测（2026-07-19）

Run `evals/results/runs/20260719-163333/stress_inventory_reservation_consistency` 的 final-triggered audit
正确触发，11 条 acceptance 当时均已完成，但复核仍继续对照完整契约并补齐实现。Agent 最终提交
`inventory_repository.py`、`serialization.py`、`service.py` 和 `state.py`。虽然它随后因 40 次 Broker
call budget 结束，未进入自动 grader，但对保存 workspace 手工运行可信 grader 得到 98/100，所有业务、
回归和 API 分组均通过。这证明 fresh contract audit 已经解决前两次漏掉的 UnknownSku 回滚、不同
normalized request 冲突以及 quantity 指纹要求。

本轮同时暴露出 audit 自身的范围问题：review 之后又发生 15 次模型请求和 42 次工具调用，其中
`read_file=38`、`glob=3`，导致一个已经功能正确的实现无法正常 final。该放大因素拆分为 HARN-013；
因此 HARN-012 暂留 `Fixed`，等待带 HARN-013 范围限制的原 case 正常结束后一起验证。

### 第四次压力复测与证据深度根因（2026-07-19）

`20260719-221750` 在 36 次 Provider attempt 内正常 final，grader 得 89 分。Atomic reservation、retry、
cancellation/state、API、constraints、process、code quality 和 efficiency 均满分，只有
idempotency conflict 与 regression 失败；共同根因是 `request_fingerprint()` 仍只包含 SKU，遗漏
quantity。

Final audit 已受控地读取 README 和 3 个 changed files，并逐条写出“same key + **any different
normalized request** 必须冲突”。但它只验证 `service.py` 是否比较两个 fingerprint，就把该项标记为
完成；没有读取生成 fingerprint 的 `serialization.py`，也没有枚举 README 明确要求的 normalized
order ID 与每个 `(sku, quantity)` 字段。随后 audit Todo 的证据也只写“service raises when fingerprint
differs”。这说明现有 gate 能防止跳过审计，却不能防止“只看消费者、不看派生值生产者”的浅证据。

本轮根因不是 public tests、读取预算或提前终止：audit 尚有两个读取名额，Agent 正常 final，grader
也完整运行。针对该模式继续做轻量提示约束：derived value（fingerprint、normalized key、hash、
serialized payload）必须检查 producer function 并枚举契约字段；只检查 caller/comparison site 不算
证据；`any/all` 必须检查每个命名异常或状态分支。没有新增工具、模型调用或常驻契约文本。相关聚焦
回归 `31 passed`，完整回归 `206 passed, 3 skipped, 1 deselected`。

### 第五次压力复测与虚假证据（2026-07-20）

`20260720-221357` 进入 final audit 前，11 条 acceptance 已全部被 Agent 标记为
`completed`，但其中至少两条证据不成立：

- 幂等冲突证据只说 `service.py` 会比较 fingerprint，没有检查产生者
  `serialization.py`；实际 fingerprint 仍遗漏 quantity；
- “canceled 不能 confirm”的证据一边写出 `CANCELED={CONFIRMED}`，一边声称该路径不存在，
  是可以直接从文本内部检出的矛盾。

后续有界 final audit 成功发现 quantity 问题并新增一条 `in_progress` plan，证明 fresh
contract review 有价值；但 Harness 目前只检查 acceptance 是否有非空 evidence，不检查证据
是否覆盖 producer/契约字段，也不检查证据自身是否矛盾。因此 HARN-012 重新打开为
`In progress`。后续应让 Reviewer 输出可定位的 findings（契约项、严重性、文件和证据），
由 Harness 将未解决 finding 与 acceptance 关联，而不只依赖 Agent 自己填写的 completed/evidence。

### Reviewer finding 与 acceptance 闭环（2026-07-20）

Reviewer 现在最多返回 5 条短 finding，每条包含 severity、requirement、file、symbol 和
evidence。对有 acceptance 契约的任务，Harness 会把本 revision 的 findings 合并为一条
pending acceptance；后续 `todo_write` 不能静默删除它，只能在修复或用代码证据拒绝后
标记 completed。不同 mutation revision 使用不同条目，不会把旧 finding 误当作新评审结果。
这将“Reviewer 提示”从自由文本升级为受现有 acceptance 锁保护的可追踪工作，无需新增
常驻契约系统。定向和完整回归已通过，状态恢复为 `Fixed`。

### 验收标准与测试

- 普通 Todo 无需 `kind`，行为保持兼容；
- completed acceptance 缺少 evidence 时拒绝更新；
- 复杂代码任务可以在 Todo 前读取契约，但没有 plan/acceptance 时文件编辑被 Todo gate 阻止；
- 文件修改后 acceptance 不能被后续列表静默删除或改写，措辞漂移自动校正且不返回 Error；
- 已有 acceptance 即使全部 completed，第一次 final 仍会触发一次新的完整契约审计；
- final 审计后必须再次 todo_write；审计可以新增早期清单遗漏的 acceptance 并重新进入编辑流程；
- acceptance 状态通过 live system prompt 跨 Context 压缩保留，plan 不额外常驻；
- 模型忽略复核时 final 明确标记未验证要求，且不会进入无限模型循环；
- Todo/acceptance 数量和文本长度有上限，不能把整个 README 塞入常驻提示；
- 二次修正后的 Acceptance/Agent/background/Trace 聚焦回归：`40 passed`；
- 二次修正后的完整回归：`198 passed, 3 skipped, 1 deselected`。
- 第三次修正新增“已完成但不完整的清单在 final 审计中补入遗漏契约”的同构回归；完整回归：
  `199 passed, 3 skipped, 1 deselected`。
- 当前镜像重新构建后的 scripted Docker `read_file_basic` 回归通过，得分 100；普通任务未被新 gate
  误拦截。

代码已实现并通过当前定向测试，状态为 `Fixed`。原始压力 case 已证明 final-triggered audit 能补入
UnknownSku、不同请求冲突和 quantity 指纹契约；还需要重新构建包含 HARN-013 限制的镜像并确认它能在
预算内正常 final，才能标记为 `Validated`。

---

## HARN-013：Final contract audit 无范围限制，完成后重新扫描仓库

**严重性：** P1
**状态：** Validated
**模块：** `codepilot_s20/agent_loop.py`

### 现象与根因

`20260719-163333` 中，Agent 在第一次 final 前已经修改 4 个正确文件并两次看到公开测试 `17 passed`。
final gate 正确要求重新对照契约，但旧提示只说“重新阅读 task/README、最终代码和测试”，没有告诉模型
哪些文件发生过改变，也没有限制读取次数。模型于是从局部验收退化成第二次仓库探索：

- review 后 15 次模型请求、14 次响应，最后第 40 次额度用完；
- review 后 42 次工具调用，包括 38 次 `read_file`、3 次 `glob`、1 次 pytest；
- 重读 README 后又顺序读取大部分源码、测试和 bootstrap，随后再次批量读取 12 个源码文件；
- 全程没有新增编辑，手工 grader 已证明进入 review 时的最终 workspace 功能分组全部通过。

根因不是 micro-compact 丢失工作集，而是 final gate 重新打开了一个无边界的探索阶段。Micro-compact
只能减少重复结果占用，不能替 audit 决定“哪些证据已经足够”。

### 实施方案

- Agent loop 记录成功 `write_file`/`edit_file` 的唯一路径；第一次 final 时把明确的 changed-file 列表
  放入 audit 提示；
- audit 只要求 task/README 与 changed files 各读一次，明确复用此前上下文和测试通知，不再 glob、
  重读测试、扫描全仓库或默认展开未修改依赖；
- 读取预算按 `changed_file_count + 3` 计算，并限制在 4 到 8 次之间；同一路径在本次 audit 中只允许
  一次，超过预算或重复读取返回可恢复的 `Tool not run`；
- audit 阶段禁止 `glob`，但不禁止发现问题后的编辑和 Todo 更新；成功 `todo_write` 后解除审计范围，
  允许正常修复新发现的缺口；
- Trace 的 `acceptance_gate` 增加 changed-file count、read budget、重复读取、预算耗尽和 glob 拒绝决策。

### 验收标准与测试

- final review 提示包含本轮真实 changed files 和明确读取预算；
- 同一路径重复读取不执行，glob 不执行，达到预算后额外 read 不执行；
- 允许 README、changed files 以及预算内的必要补充读取；
- audit 后 Todo 可以新增遗漏契约，并可继续编辑修复；
- 不改变普通任务、首次契约发现和 micro-compact 的既有行为；
- Agent-loop 与 Docker remaining-budget 聚焦回归：`32 passed, 1 skipped`；
- 完整回归：`202 passed, 3 skipped, 1 deselected`。

### 首次真实范围验证（2026-07-19）

`20260719-214544` 的 review 事件正确记录 `changed_file_count=2`、`read_budget=4`。audit 后只发生
5 次模型请求和 6 次工具调用，相较旧实现的 15 次请求、42 次工具调用明显收敛；没有执行 glob，且第
5 个不同路径读取被 `audit_read_budget_reached` 拦截。该次 audit 发现 quantity fingerprint 遗漏，但
因为进入 audit 时已经用掉 36 次请求，来不及完成补充编辑。

根据本次具体路径，审计确实需要 README、两个 changed files、`serialization.py` 和
`validation.py`，因此补充依赖余量从 2 调整为 3，整体上限仍为 8。HARN-013 保持 `Fixed`，等待与
HARN-001/HARN-014 一起复测正常 final。

`20260719-221750` 进一步完成正常 final：review 后只有 3 个 Trace Agent request、5 次工具调用，读取
README 和 3 个 changed files 后更新 Todo，没有 glob、重复读取、预算拒绝或二次仓库扫描。虽然语义
审计仍漏掉 fingerprint producer（由 HARN-012 跟踪），但 HARN-013 所定义的“无范围审计导致调用耗尽”
已经通过原始压力 case 验证，状态标记为 `Validated`。

范围限制保持真实压力回归覆盖；后续 semantic audit 增强不重新打开 HARN-013。

---

## HARN-014：Glob 的 `**` 模式没有递归进入源码树

**严重性：** P1  
**状态：** Validated  
**模块：** `codepilot_s20/basic_tools.py`、`codepilot_s20/tool_defs.py`

### 现象与根因

`20260719-214544` 首轮调用 `glob("**/*.py")` 只返回一层深的 `tests/*.py`，没有返回两层深的
`src/inventory_service/*.py`。Agent 随后依次尝试第二次 `**/*.py`、`inventory_service/**/*` 和 bash
`find`，额外消耗 3 个模型回合；同一首轮还盲查 JavaScript、TypeScript、Go、Rust 和 Java 扩展名。

`run_glob()` 使用 Python `glob.glob(pattern, root_dir=base)`，但没有传入 `recursive=True`。Python glob
不会在默认模式下把 `**` 当作任意深度递归，因此工具描述承诺的常见模式与实际行为不一致。

### 实施与验收

- `glob.glob` 启用 `recursive=True` 并对结果排序，仍逐项验证解析路径位于 workspace；
- tool description 明确 `**` 支持递归，并要求已知项目语言后只使用任务相关模式，不探测无关扩展名；
- 新增回归证明根目录 `**/*.py` 同时返回顶层文件和 `src/inventory_service/` 下的嵌套文件；
- 已进入 `205 passed, 3 skipped, 1 deselected` 完整回归。

`20260719-221750` 中首次 `**/*.py` 已返回嵌套实现源码，Agent 没有再使用 bash `find`；后续
`src/inventory_service/**/*.py` 也正确递归。HARN-014 标记为 `Validated`。首轮仍并行探测无关语言
扩展名属于模型在尚未看到 README 结果时的探索选择，不是 glob 递归正确性问题。

---

## HARN-015：Multiagent 能力存在，但没有可复用的角色编排与 Worktree 闭环

**严重性：** P1  
**状态：** Fixed
**模块：** `codepilot_s20/agent_profiles.py`、`codepilot_s20/agent_loop.py`、
`codepilot_s20/subagent.py`、`codepilot_s20/teammate.py`、
`codepilot_s20/worktree_system.py`、`codepilot_s20/tool_defs.py`

### 现象与根因

此前 Harness 已经分别提供通用 `task`、异步 `spawn_teammate`、Task 记录和 Git Worktree 工具，但它们
没有组成 Leader 可以稳定使用的通用工作流：

- 压力 Trace 中没有调用 subagent、teammate、task、worktree 或 skill，复杂仓库探索、实现和最终审计
  全部堆在同一段 Lead context 中；
- teammate 的 `role` 只是自由文本，Explorer、Reviewer 和 Worker 没有不同的工具权限、输出契约或
  生命周期；通用 subagent 也始终获得同一组读写工具；
- Harness 没有依据任务复杂度主动触发委派，是否使用 multiagent 完全依赖模型偶然选择工具；
- Worker 即使在 Worktree 中产生修改，也没有受控的提交/合并工具。Docker change manifest 会忽略
  `.worktrees/**`，因此未合回主 workspace 的实现对 grader 不可见；
- 异步 teammate 与 Lead 并行调用同一个 case-wide Broker，容易在 40-call 压力预算下形成竞争，且
  Lead 可能在结果到达前继续做重复探索。

因此原设计属于“有并发和隔离原语，但没有 orchestration policy”。它既不能稳定减少 Lead context，
也不能保证委派结果进入最终提交。

### 实施方案

新增一套不绑定具体业务 case 的角色编排模式：

1. **稳定角色配置。** `explorer` 仅有 `glob/read_file`，负责契约、代码路径和风险映射；`reviewer`
   同样只读，独立检查最终行为、错误分支、状态、原子性、幂等和兼容性；`worker` 才拥有读写、编辑和
   bash，并且必须在 Worktree 中运行。三个角色都有固定 JSON 结果契约和 5/5/10 回合上限。
2. **Fresh context 同步委派。** 新增 `delegate_agent(role, prompt, ...)`。角色只接收原始 root task、
   Lead 给出的有界子任务和自己的角色说明，不继承 Lead 的长历史。Eval 默认使用该同步路径，避免异步
   teammate 与 Lead 竞速；现有 `task` 和 `spawn_teammate` 保持兼容，命名角色也复用相同权限配置。
3. **自适应 Leader 门禁。** 确定性复杂度评分只依赖任务文本。复杂任务第一次实现修改或 Worker 委派
   前必须先取得 Explorer 的 `complete`；中等任务只有在 Lead 已读取至少 8 个不同路径时才升级。简单
   任务不强制委派，避免为小任务增加模型调用。
4. **最新 revision Reviewer。** 复杂任务真正输出 final 前必须取得 Reviewer 的 `pass`；任何后续
   主 workspace 修改都会让旧 pass 失效。Reviewer pass 代替原先由 Lead 自己执行的 fresh contract
   audit，但 acceptance Todo 仍需由 Lead 更新证据。连续两次忽略 Reviewer 提示后允许带明确未审计
   警告结束，避免门禁本身制造无限 Broker 调用。
5. **Worker Worktree 闭环。** Worker 自动创建并认领 Task、创建 Worktree、执行有界实现并由 Harness
   提交。结果返回 worktree、commit、changed files 和 diff stat；只有 Lead 显式调用
   `integrate_worktree` 后修改才进入主 workspace。集成会预检 Lead/Worker 同路径修改，冲突或模型失败
   时保留 Worktree，不静默覆盖 Lead 变更。
6. **可观测性与失败隔离。** Trace 给 nested LLM request/response 标记 `purpose=delegate_agent` 和
   `agent_role`，并记录 delegation start/finish、角色 verdict、Explorer/Reviewer 门禁和 mutation
   revision。角色模型调用失败会返回结构化 error，不直接使 Leader 进程崩溃。

所有角色仍共享 case-wide Broker 调用和 token 预算；这套机制的目标是用少量有边界的独立上下文替代
Lead 的重复全仓探索，而不是绕过资源限制或无条件增加 agent 数量。

### 验收标准与测试

- 复杂任务在 Explorer 完成前不能执行首次实现修改，Reviewer 必须审核最新 mutation revision；
- Explorer/Reviewer 的真实 tool schema 不包含写入或 bash；
- Worker 修改在集成前不影响主 workspace，集成后 grader 可见，并自动清理成功的 Worktree；
- Lead/Worker 修改同一文件时拒绝集成，失败 Worktree 保留；
- 简单任务保持单 Agent 路径，旧 `task`、teammate、Task 和 Worktree API 保持兼容；
- `tests/test_multiagent_roles.py` 覆盖复杂度分类、只读 Reviewer、Worker 提交/集成和完整
  Explorer→Lead→Reviewer 门禁流程；
- 定向回归：`44 passed`；
- 完整回归：`210 passed, 3 skipped, 1 deselected`。

### 首次真实模型复测与重新打开（2026-07-20）

重新构建镜像后的原始压力运行
`evals/results/runs/20260720-205811/stress_inventory_reservation_consistency` 在第 41 个 Broker request
被 40-call 限额拒绝，Agent 分数 0，保存 workspace 的 diagnostic grader 为 67。该运行证明角色确实
被 Leader 主动调用，但首次 orchestration policy 过度依赖角色输出格式，反而阻止了已经正确的实现：

- Trace 有 28 个 Lead `llm_request`、10 个 Explorer `llm_request`，以及 3 次未计入普通 LLM Trace 的
  compact summary。27 个 Lead response、10 个 Explorer response 和 3 个 summary 正好消耗 40 次成功
  Provider 调用；下一次 Lead request 被拒绝；
- Leader 两次调用 Explorer。每个 Explorer 都连续执行满 5 个回合且每个 response 都是 `tool_use`，
  没有机会在工具阶段结束后进行最终综合；两次分别执行大量源码/测试/README 读取，共记录 37 个
  `delegated_tool_use`；
- `run_role_agent()` 在达到 `max_rounds` 后直接退出，只能拿到最后一段过渡文本，例如
  “Now let me also read the README...” 或 “Let me trace the execution paths carefully”。JSON parser 因而将
  两次结果都标记为 `inconclusive/invalid_json`；
- Leader 随后自行重新读取仓库。Trace 的主工具还有 42 次 `read_file`；模型已经准确识别原子扣减、
  不同请求的幂等冲突、重复取消二次释放和 fingerprint 遗漏 quantity 四个真实根因；
- 但 Explorer gate 只接受精确的 `status=completed + verdict=complete`。两个正确方向的 `edit_file`、
  以及后续 `delegate_agent(role=worker)` 都被 `explorer_required` 拒绝，最终 change manifest 没有任何
  submitted change；
- 第二次 Explorer 失败后，Leader 又混用低层 `create_task/create_worktree` 和高层 Worker 委派，创建了
  一个不会被 `delegate_agent(worker)` 复用的手工 Worktree。说明同时暴露两套生命周期 API 会让模型
  误解所有权边界；
- nested Trace 目前只记录 delegated tool 名称，不记录 input/path 和结果摘要，无法直接统计 Explorer
  内部是否重读同一路径，是新增的可观测性缺口。

这次失败的主因不是“Multiagent 增加了 10 次调用”这么简单，而是三个机制形成了放大环：角色循环没有
预留 final synthesis 回合 → 格式失败被当成语义失败 → 强门禁迫使 Leader 重做探索并重试角色 → 三次
full compact 再消耗调用，最终在任何修改落盘前耗尽预算。

下一轮应保持角色/worktree 骨架，缩小门禁耦合：

1. Explorer/Reviewer 达到工具回合上限后，必须追加一次 `tools=[]` 的强制 synthesis 调用，并明确只返回
   JSON；工具回合和综合回合分别计数，不能让最后一次 `tool_use` 直接成为角色结果；
2. Explorer gate 应要求“一次有界独立探索已经执行”，不应要求模型严格输出某个 JSON token 才允许
   Lead 修改。`complete` 可以增强置信度；`inconclusive/error` 应作为 degraded evidence 返回给 Lead，
   但不能无限阻止已经有代码证据的实现；
3. 一次 Explorer 已执行后默认禁止自动进行第二次全量 Explorer；如需补充，只允许同一角色上下文进行
   synthesis/有限补读，避免 Lead 和新 Explorer 各自重扫仓库；
4. 压缩只读角色预算，例如 Explorer 3 个工具回合 + 1 个 synthesis、Reviewer 2 + 1，并为 Worker 和
   最终 Reviewer 预留 case-wide Broker 调用；
5. `delegate_agent(worker)` 明确独占 Task/Worktree 生命周期，提示和工具层阻止先手工
   `create_task/create_worktree` 再委派 Worker 的重复路径；
6. `delegated_tool_use` Trace 增加安全清理后的 input/path、结果大小和摘要，以便验证角色是否真正减少
   Lead 重读。

HARN-015 因真实压力复测失败从 `Fixed` 重新打开为 `In progress`。已有 worktree 提交/冲突测试仍然
有效；需要新增“角色最后一回合仍为 tool_use 时强制综合”和“Explorer 格式失败不永久锁死 Lead”两个
同构回归，再进行第二次真实模型复测。

### 第二轮轻量化修复（2026-07-20）

保留角色和 Worktree 安全边界，移除把模型输出格式当权限条件的复杂状态机：

- 删除首次修改前的 Explorer 硬门禁。复杂任务只注入一次调用建议；无论 Explorer 返回 `complete`、
  `inconclusive` 还是结构化 error，Lead 都可以依据自己的代码证据继续编辑或委派 Worker；
- 删除 Reviewer pass 对 final 的硬门禁。每个发生实际修改的 revision 在首次 final 时最多收到一次
  advisory；Lead 可以调用 Reviewer，也可以在预算紧张或已有充分证据时直接 final；
- 一个任务只真正执行一次 Explorer；后续 Explorer 调用直接返回首个缓存结果。同一 mutation revision
  的重复 Reviewer 同样复用结果，不再消耗 Broker 调用；
- 角色回合拆成工具阶段和 synthesis 阶段。Explorer 最多 3 个工具回合、Reviewer 2 个、Worker 6 个；
  如果最后一个工具回合仍是 `tool_use`，Harness 自动追加一次 `tools=[]`、`max_tokens=3000` 的 JSON-only
  synthesis 调用，保证读取过程不会被误当成最终角色结果；
- `delegate_agent(worker)` 的 system/tool 提示明确它自动拥有 Task 和 Worktree 生命周期，Lead 不应先
  调用低层 `create_task/create_worktree`；低层 API 保留用于兼容已有手工工作流；
- `delegated_tool_use` Trace 现在记录清理后的 input/path；新增 `delegated_tool_result`，记录最多 2,000
  字符的结果预览、原始大小和截断状态，避免为了诊断角色重读而复制完整大输出；
- Worktree 只能由 Worker 修改、Harness 自动提交、Lead 显式集成、同路径冲突拒绝等硬安全规则保持
  不变。Todo/acceptance 和权限边界也不受此次软编排调整影响。

新增回归覆盖：Reviewer 工具预算耗尽后必须进入无工具 synthesis；Explorer 连续输出非 JSON 时，第二次
调用必须复用缓存且不能阻止 Lead 编辑和 final。Multiagent/Agent loop/Docker policy/Trace 聚焦回归为
`66 passed`；完整回归为 `212 passed, 3 skipped, 1 deselected`。

代码和 scripted 回归已通过，HARN-015 恢复为 `Fixed`；仍需重新构建镜像进行第二次真实模型压力复测，
确认 Explorer 总调用、Lead 重读、Worker 集成、Reviewer 建议和 40-call 总预算后才能标记 `Validated`。

### 第二次真实模型复测（2026-07-20）

Run `evals/results/runs/20260720-221357/stress_inventory_reservation_consistency` 使用了重新构建的镜像。
软编排已经解决“角色格式失败阻止修改”：Agent 成功修改
`inventory_repository.py` 和 `service.py`，diagnostic score 从上轮 67 升到 79。但这次也证明
Multiagent 仍未形成稳定的质量闭环：

- 复杂度策略正确记录 `level=complex, score=7`，但 Leader 没有调用 Explorer 或 Worker，
  所有探索与编辑仍在 Lead context 中完成；
- Leader 在首次 final 后才调用一次 Reviewer。Reviewer 用 2 个工具回合读取 10 个文件，
  又用 1 个无工具 synthesis 回合；因此“必有 synthesis”的代码修复确实生效；
- Reviewer 已经读到 `state.py` 错误允许 `CANCELED -> CONFIRMED`，也读到
  `serialization.py` 的 fingerprint 未包含 quantity；但 synthesis 输出了长篇 Markdown 推理，
  在 3,000-token 上限处被截断，不是约定 JSON，最终 verdict 被解析为 `inconclusive`；
- Lead 只收到被截断的评审摘要，随后错误地将 Reviewer 的状态机警告判定为“边界正确”。
  Reviewer 虽然触及了真实缺陷，但没有产生可执行的 finding；
- 最终可信 grader 通过 atomic reservation、idempotent retry 和 API compatibility，但
  idempotency conflict、cancellation/state 和 regression 失败。

这说明 Reviewer 不能只交付一个 `pass/inconclusive` verdict 和自由文本。首选方向是将结果
改为“结构化 findings 优先”：每条 finding 包含 severity、contract clause、file/symbol 和简短证据，
严格限制数量和文本长度；即使外层 JSON 解析失败，也应保留可提取的 finding 而不是整体降级为
`inconclusive`。Reviewer 的系统任务还必须要求独立对照 root task/README 与完整 diff，不能被
Leader 的“只审我认为的三个修复”缩窄成确认性评审。HARN-015 因此重新打开为
`In progress`；Worktree 本轮未被调用，尚不能用该运行验证 Worker 集成闭环。

### 第三轮编排修复（2026-07-20）

- 复杂任务的第一次 final 尝试不再先注入 Reviewer advisory，而是由 Harness 在额度允许时
  直接运行一次有界 Reviewer；该结果与 contract audit 合并进同一条 pre-final 消息，
  节省一次 Lead 决定是否委派的模型回合；
- Reviewer 的 root task 永远高于 Lead 传入的局部审计描述，要求检查完整 changed-file set、
  direct producer/dependency 和每个命名的字段/状态/错误分支，降低确认性评审；
- Reviewer synthesis 降为 1,600 token，明确禁止 Markdown 和长推理；JSON 解析会标准化字段、
  长度和 verdict。无效/截断 JSON 会从 `critical issue`、`bug`、`missing`、`incorrect` 等显式
  concern 中保留一条 warning finding，不再只返回一个空的 `inconclusive`；
- Explorer/Reviewer/Worker 在启动前会根据各自最大工具回合 + synthesis 预估调用成本；
  不能在保留 finalization reserve 后完成时直接返回 `budget_reserved`。

聚焦回归覆盖自动 pre-final Reviewer、非 JSON finding 保留、revision acceptance 关联和预算跳过；
完整回归为 `220 passed, 3 skipped, 1 deselected`。代码状态恢复为 `Fixed`，Worker/Worktree 的
真实模型路径仍需下一次压力运行验证。

---

## HARN-016：关键收尾阶段没有预留 Broker 调用额度

**严重性：** P1
**状态：** Validated
**模块：** `codepilot_s20/agent_loop.py`、`codepilot_s20/subagent.py`、
`codepilot_s20/compact.py`、`codepilot_s20/model_broker.py`

### 现象与根因

`20260720-221357` 最终仍因 call limit 失败：Broker 成功接受 40 次，第 41 次拒绝。
本轮的成功调用可还原为：

- 34 次 Lead response；
- 3 次 Reviewer response（2 个工具回合 + 1 个 synthesis）；
- 3 次未写入普通 LLM Trace 的 automatic compact summary。

进入 final contract audit 时只剩 6 次可用调用。Audit 用 5 个 Lead response 重读契约和局部代码，
并成功发现 quantity fingerprint 缺陷；紧接着上下文达到 50,076 字符，第三次
automatic compact 消耗最后一次额度。Agent 已将新缺陷记为 `in_progress` plan，但下一次
Lead request 在任何编辑前被拒绝。

额度消耗并不只是“任务太难”：Leader 之前已有 3 次 `end_turn` 输出，一次触发 Reviewer advisory，
一次遇到尚未收到的后台 pytest 通知，第三次才触发 contract audit。Harness 把 Reviewer、
后台结果确认、final audit 和 compact 串行放在最后，但没有为“发现问题后的编辑 + 测试 + final”
保留最低额度。

### 首选方案

1. 将 Reviewer 和 contract audit 合并成一次明确的 pre-final 阶段，优先在 Leader 首次宣布完成时执行，
   避免先后发生“Leader 自证→Reviewer→Leader 再自证→contract audit”；
2. Broker 为关键收尾保留一小段调用预算。进入预留区后，禁止新 Explorer/可选 Reviewer 和
   非必要模型 compact，但允许已发现缺陷的修复、验证和最终回答；
3. 把 compact summary 纳入同一个可见的 purpose 计数和剩余额度决策；低额度时优先做确定性裁剪或使用
   已有 summary，不应让新 summary 抢走最后的修复回合；
4. Agent 在后台测试尚未完成时不应输出 final；Harness 应在 final 转换前合并已在飞的
   `task_notification`，减少为了重复确认同一次 pytest 而新增的模型回合。

### 验收标准

- Trace 和 summary 能按 Lead、角色、compact 和 retry 显示已用/剩余调用；
- 任一 pre-final 审计发现新问题时，仍至少可完成一次修复、一次验证和一次 final；
- 额度紧张时不启动新的可选角色或模型摘要；
- 后台测试完成与 final 只需一次有效收尾，不重复调用模型确认同一证据。

### 实施与验证（2026-07-20）

- Container 通过 `BrokerModelClient.budget_snapshot()` 只读取 live `broker_stats.json`中的非敏感计数，
  并验证 protocol version 和 nonce；普通非 Broker client 保持无限制兼容路径；
- 动态 finalization reserve 为总 call budget 的 20%，下限 4、上限 8；40-call case 保留 8 次。
  可选角色只有在自身有界回合和一次 retry margin 之后仍保留该额度时才能启动；
- 进入预留区后向 Lead 注入一次 `finalization_budget` 约束，禁止新 role、全仓重扫和
  model summary，但不禁止直接编辑、定向测试和 final；
- automatic/reactive/显式 compact 在预留区改用确定性 checkpoint：保留 root task、live
  acceptance 和最近消息，不再消耗最后的 Broker 调用；
- Trace 记录 `model_budget_guard`的 reserve entered、delegation skipped、automatic reviewer skipped 和
  deterministic compact 决策，可直接还原为什么某个角色/摘要没有执行。

新增预算归一化、Broker snapshot、Reviewer 跳过和确定性 compact 回归。聚焦回归 `44 passed`，
完整回归 `222 passed, 3 skipped, 1 deselected`，状态为 `Fixed`。需要重建 Docker 镜像重跑原始
stress case，确认真实 40-call Broker 下能留出修复/测试/final 空间后再标记 `Validated`。

### 第三次真实模型复测与 Docker 接线修复（2026-07-20）

Run `evals/results/runs/20260720-231707/stress_inventory_reservation_consistency` 再次在第 41 个
request 被 40-call Broker 拒绝，但 diagnostic score 已升至 90；atomic、幂等重试、幂等冲突、
regression 和 API compatibility 均通过，只剩 canceled reservation 仍可 confirm。Trace 暴露出两处
通用 Harness 实现缺口：

- Docker Agent 只挂载了 `/broker/requests` 和 `/broker/responses`，没有挂载 Host Broker 写入的
  `broker_stats.json`。因此真实容器中的 `budget_snapshot()` 始终不可用，Trace 中没有任何
  `model_budget_guard`，此前通过单元测试的 reserve 实际从未在 Docker Eval 生效；
- Reviewer 确实读取了 `state.py`，其中注释声明 canceled 为 terminal，但 `_ALLOWED_TARGETS` 又允许
  `CANCELED -> CONFIRMED`。不过它的 synthesis 继承了上一轮“接下来运行测试”的对话惯性，返回了
  文本形式的伪 tool call 而不是 JSON，最终只生成无业务信息的 parse-failure Todo；
- Lead 后续发现并修复 quantity fingerprint，17 个公开测试通过，但一次超限 Todo、后台 pytest
  收取和最终答复仍需要额外回合，最终再次发出第 41 个请求。

修复后 Broker stats 改放在独立目录并只读挂载到 `/broker/stats`；同时把 call budget 作为容器内的
保守 fallback，即使 stats 文件短暂不可读也不会关闭 reserve。Reviewer synthesis 改为基于已读取文件
构造全新的 tool-free evidence request，并取消无明确 concern 时的虚假 finding。最后一次剩余额度会
禁用工具并强制输出 final；若额度已经为零，Harness 直接安全结束而不会向 Broker 发出超限请求。

聚焦回归为 `80 passed, 1 deselected`，完整回归为
`222 passed, 3 skipped, 1 deselected`。重建镜像后的离线 Docker smoke
`evals/results/smoke-budget-mount/runs/20260720-233811/read_file_basic` 得分 100，Trace 记录
`budget_snapshot_available, source=broker_stats, max_calls=32, reserve_calls=7`，证明 live stats
目录挂载已在真实 Docker Runtime 生效。原 stress case 仍需再次使用真实模型验证 Reviewer finding
命中率和 40-call 收尾路径，因此状态保持 `Fixed`，暂不提升为 `Validated`。

### 第四次真实模型复测通过（2026-07-20）

Run `evals/results/runs/20260720-234600/stress_inventory_reservation_consistency` 最终得到 100 分，六个
outcome group、约束、流程、代码质量和效率全部满分。预算路径按设计执行：

- 首轮 Trace 记录 `budget_snapshot_available, source=broker_stats, remaining_calls=40`；
- 第 32 次调用后记录 `finalization_reserve_entered, remaining_calls=8`；
- 第 39 次调用后记录 `last_call_forced_final, remaining_calls=1`，该轮工具被禁用；
- 最后一次 context compact 使用 `deterministic_compact`，没有抢占最后调用；
- Broker 最终为 40 calls / 40 requests / 0 rejected / 0 retries，没有再发出第 41 次请求。

Agent 在额度内修复了 atomic reserve、idempotency conflict/fingerprint、重复取消和 canceled terminal
state，submitted change 涵盖五个允许修改的源码文件。HARN-016 的真实 40-call 验收条件已经满足，
状态提升为 `Validated`。

---

## HARN-017：Legacy `task` subagent 绕过角色编排、预算与 Trace

**严重性：** P1
**状态：** Fixed
**模块：** `codepilot_s20/subagent.py`、`codepilot_s20/tool_defs.py`、`codepilot_s20/agent_loop.py`

### 现象与影响

100 分运行没有调用 `delegate_agent(explorer|reviewer|worker)`，而是调用旧 `task` 工具，要求“读取全部
source 和 tests”。`task` 最终由 `spawn_subagent()` 执行了 3 次 Broker 调用并返回有效代码地图。这对
本次结果有帮助，但暴露出新旧 Multiagent API 并存造成的旁路：

- Trace 只有 35 次 Lead 和 2 次 compact-summary `llm_request`，合计 37；Broker 实际调用 40 次，
  缺少的 3 次正是 legacy task subagent，无法按 role/purpose 统计耗时和额度；
- `spawn_subagent()` 最多允许 30 回合，不使用 `can_spend_optional_calls()`，可以绕过 finalization
  reserve；
- legacy task 同时持有 read/write/edit/bash，并直接工作在主 workspace，绕过 Worker 的 Task、Worktree、
  commit、冲突检测和显式集成边界；
- 因为没有标准 Explorer/Reviewer/Worker 事件，这次 100 分只能验证“确实使用了一个 subagent”，不能
  验证 HARN-015 所定义的通用角色编排与 Worktree 闭环。

### 建议方案

保留 `task` 名称作为兼容入口，但不再保留独立执行引擎：只读探索请求路由到有界 Explorer；实现请求
路由到 Worker Worktree，并要求 Lead 显式集成。所有路径统一复用 role request tracing、live budget
guard、结构化结果和 delegation cache。新增回归需要证明 legacy `task` 不再产生未记账模型调用、不能
直接修改主 workspace，也不能在 reserve 内启动长 subagent。

### 实施与验证（2026-07-21）

- 删除 `spawn_subagent()` 独立的 30 回合全工具循环；保留 `task` 工具名作为兼容入口，并统一进入
  `delegate_agent()` 的有界执行内核；
- 新增与 case 名称、目录和业务字段无关的委托意图路由：阅读/定位/调用链分析进入 Explorer，代码修改进入
  Worker Worktree，最终正确性审计进入 Reviewer，无法归入专门角色的小问题进入只读 General；
- General、Explorer、Reviewer、Worker 统一使用 role Trace、deadline、finalization reserve、结构化结果和
  异常封装。`task` 路由出的 Explorer/Reviewer 也进入 Leader 的 revision cache，重复请求直接复用；
- 每个角色增加独立的唯一读取路径上限，并在同一委托内抑制完全相同路径、offset、limit 的重复读取，防止
  “读取全部源码和测试”再次把一个辅助角色扩张成无界仓库扫描；
- Worker 仍只能修改自动创建的隔离 Worktree，必须由 Lead 显式调用 `integrate_worktree`，旧 `task` 不再有
  直接修改主 workspace 的能力；
- 新增通用意图分类、legacy task Trace/权限、reserve 跳过、读取路径上限和跨入口 Explorer cache 回归。
  聚焦回归为 `15 passed`，完整回归为 `226 passed, 3 skipped, 1 deselected`；同时修复标准角色 Prompt
  重复暴露内部 Host workspace 路径的问题。真实模型压力复测前状态保持 `Fixed`，不提前标记 `Validated`。

### 真实模型复测（2026-07-21）

Run `evals/results/runs/20260721-230949/stress_inventory_reservation_consistency` 得分 100，Broker 的 40 次调用
全部出现在 Trace：35 次 Lead、3 次自动 Reviewer、2 次 compact summary，没有未记账调用。但本次没有任何
`task`、Explorer 或 Worker 调用，唯一角色路径是 Harness 自动启动的 Reviewer。因此它证明统一 Trace 和预算
没有造成回归，但没有经过 HARN-017 的原始 legacy `task` 复现路径，状态继续保持 `Fixed`。后续需要增加一个
会明确调用 `task` 的通用真实模型 case，才能将该问题标记为 `Validated`。

---

## HARN-018：Reviewer finding 缺少稳定身份，Lead 无法可靠提交解决证据

**严重性：** P1
**状态：** Fixed
**模块：** `codepilot_s20/agent_loop.py`、`codepilot_s20/basic_tools.py`

### 现象与证据

Run `evals/results/runs/20260721-230949/stress_inventory_reservation_consistency` 的代码和六个隐藏 outcome group
全部通过，最终得分 100，但 Trace 暴露出不一致的收尾状态：

- 自动 Reviewer 在 `used_calls=27, remaining_calls=13` 时启动，消耗 3 次调用后返回一个 `gaps` finding，
  认为 `ReservationService.reserve()` 的失败分支会留下 idempotency binding；
- Lead 随后读取实际 repository 实现并重跑 17 个公开测试，给出具体路径证据证明该 finding 不成立；
- Lead 两次尝试用 `todo_write` 把 finding 标成 completed，但提交的是对 finding 的概括，而锁定 Todo 使用完整生成
  文案作为身份。Harness 将概括视为新的 acceptance item，再叠加必须保留的旧 item 后触发 acceptance 数量上限，
  两次都返回 `todo update exceeds limits after preserving locked acceptance items`；
- 原 finding 因此仍是 pending，Harness 又触发 `review_followup` 和 7-path final audit，直到
  `last_call_forced_final, used_calls=39, remaining_calls=1`；
- 最终文本一方面说明 finding 已被证据驳回，另一方面又自动附加 `[Acceptance review incomplete]`。评分因代码正确
  仍为 100，但 final 状态、Trace 状态和 Agent 的结论互相矛盾，并额外消耗了大部分收尾预算。

### 根因

Reviewer finding 被转换成普通 acceptance Todo 后，没有独立的稳定 ID。locked acceptance 的协调主要依赖完整
`content`，而 Reviewer 生成的长文案不适合作为模型必须逐字复现的主键。Lead 可以理解并验证 finding，却没有一个
明确的协议来表达“解决或驳回现有 finding #1”；任何改写都可能被当成新增 requirement，并在 Todo 接近容量上限时
永久无法写回。当前错误结果也没有返回可供重试的 finding/Todo ID。

### 首选方案

1. 为 acceptance Todo 增加 Harness 生成且 compact 后仍保留的 `id`；普通契约项使用稳定序号，Reviewer finding
   使用例如 `review:r6:f1` 的 revision-scoped ID；
2. locked item 的 `content` 保持不可静默删除，但允许 Lead 用相同 `id` 更新 `status` 和 `evidence`，不要求逐字复制
   文案，也不占用新的 acceptance 名额；
3. Reviewer 提示、Todo 摘要、写入错误和 final warning 都显示短 ID。写入失败时返回当前可更新 ID，而不是只提示
   “add fewer items”；
4. 只有 ID 对应的 finding 仍为 pending 时才继续阻塞 final audit。已用证据 completed/rejected 的 finding 不再触发
   第二轮全局审计；Reviewer 仍不拥有最终裁决权，Lead 的驳回应保留明确代码或测试证据。

### 验收标准

- Lead 可以在不复制 Reviewer 原文的情况下，用 ID 解决或有证据地驳回 finding；
- 更新 existing finding 不增加 Todo 数量，在 acceptance 已达到上限时仍可完成；
- compact 前后 finding ID、状态和证据一致；
- 未解决 finding 继续阻止无证据 final，已解决 finding 不触发重复仓库审计；
- final answer、Trace acceptance state 和 Todo state 不再出现 resolved/incomplete 矛盾。

### 实施与验证（2026-07-22）

所有 acceptance Todo 现在获得 compact-safe 稳定 ID；普通项为 `accept:N`，Reviewer 每条 finding 独立使用
`review:r<revision>:f<index>`。`todo_write` 保持旧的 content/status 调用兼容，同时允许只提交已知
`id + status + evidence`，由 Harness 保留原 content/kind。按 ID 更新不增加条目，即使已有 12 个
acceptance 也可关闭 finding；重复/非法 ID 会明确报错，容量错误会返回可更新 ID。system prompt、final
audit、warning 与 deterministic compact 均显示 ID。聚焦回归和全量 `250 passed` 已覆盖满容量更新、
compact 保留和旧调用兼容。两次 ledger 真实复测没有产生 Reviewer finding，尚未重现原始 finding 驳回链，
因此状态为 `Fixed` 而非 `Validated`。

---

## HARN-019：静态复杂度不足以决定是否启动 Explorer

**严重性：** P1

**状态：** In progress

**模块：** `codepilot_s20/agent_loop.py`、`codepilot_s20/agent_profiles.py`、`codepilot_s20/subagent.py`

### 对照实验

以同一个 `stress_inventory_reservation_consistency`、同一个 DeepSeek 模型和 40 次 Broker 调用上限进行对照。
`requested max tokens` 是每次请求 `max_tokens` 的累计值，不是 Provider 返回的真实 token 用量；当前 Trace
没有保存 input/output token usage，因此只能把它作为预算上界代理指标。

| Run | 调度方式 | 得分 | 耗时 | Broker calls | requested max tokens | 调用构成 | 阅读量 |
|---|---|---:|---:|---:|---:|---|---|
| `20260721-230949` | 复杂度提示，Lead 自主；收尾 Reviewer | 100 | 356.6s | 40 | 301,600 | Lead 35 / Reviewer 3 / compact 2 | Lead 37 + Reviewer 9 |
| `20260721-234012` | 开局强制 Explorer，原始宽松工具预算 | 79 | 275.1s | 40 | 297,000 | Explorer 4 / Lead 35 / compact 1 | Lead 30 + Explorer 21 |
| `20260721-235241` | 开局强制 Explorer，缩短回合与 synthesis | 90 | 336.3s | 40 | 293,600 | Explorer 3 / Lead 35 / compact 2 | Lead 42 + Explorer 9 |
| `20260722-000703` | 开局强制 Explorer，清单注入且只允许读取 | 89 | 229.8s | 40 | 303,600 | Explorer 2 / Lead 37 / compact 1 | Lead 34 + Explorer 4 |

三次强制 Explorer 都没有复现基线的 100 分。第一次额外探索 21 次仍漏掉 fingerprint、冲突和 canceled
terminal；第二次 Explorer 在错误目录候选上浪费读取预算并漏掉 terminal state；第三次虽把 Explorer
压缩为 2 次模型调用和 4 次有效读取，但仍漏掉 idempotency fingerprint。三次运行都没有执行 Reviewer，
说明在固定 40-call 总预算下，开局角色调用会与最终独立审计直接竞争。

耗时分别降低约 23%、6% 和 36%，但真实模型运行时波动较大，而且正确性下降，不能将其视为有效的效率
提升。requested max tokens 相对基线分别约为 -1.5%、-2.7% 和 +0.7%，也没有稳定 token 节省。尤其是
Lead 调用数没有下降（35、35、37），证明 Explorer 输出没有稳定替代 Lead 的推理和阅读。

### 本轮处理

- 撤回“所有 complex implementation 在首个 Lead turn 前无条件启动 Explorer”；复杂度分类恢复为建议信号，
  不把角色调用本身当作复杂任务的完成指标。
- 保留通用 Explorer 硬化：Harness 提供有界 repository manifest；Explorer 只拥有 `read_file`；最多一轮、
  8 个不同路径和 8 次工具调用；强制 fresh-context JSON synthesis。
- `files_checked` 不再信任模型自报，而是由 Harness 以实际成功读取路径覆盖；未成功读取任何源文件时，
  Explorer 结果降级为 `blocked`，防止虚构证据污染 Lead context。

### 后续方案与验收标准

下一步不应继续增加静态关键词或把 `complex => Explorer` 写死，而应实现 benefit-aware runtime trigger：
只有在 Lead 已出现广泛读取、重复读取、跨模块不确定性，且剩余调用预算足够保留 Reviewer 和 final 时，才启动
一个带具体问题与已知路径的 Explorer。验收必须同时满足：角色调用后 Lead 阅读或模型轮数实际下降、Reviewer
收尾预算仍可用、多个不同复杂 case 的通过率不低于无角色基线，并记录真实 input/output token usage。

### Ledger 运行时信号实验与回滚（2026-07-22）

新 ledger case 首轮在没有运行时信号的情况下得到 63.446：48 次 Lead、2 次 compact、73 次读取、
97 次工具、359.1 秒和 558,164 actual tokens，通过 atomic、ordering、regression/API，失败在 exactly-once
与 recovery。最初的 benefit detector 因绝对路径共同带 `/workspace` 前缀而把 scope 错算为 1；修正后第二轮
在 used_calls=6、27 个不同路径、2 次复读、3 个 scope 时发出一次软提示。

第二轮模型仍未调用 Explorer，结果却退化为 41.289：47 次 Lead、3 次 compact、90 次读取、114 次工具、
436.3 秒和 584,117 actual tokens，只通过 ordering/API。单次随机运行不能证明提示直接导致退化，但也完全没有
满足“角色替代 Lead 阅读/模型轮数”的验收条件。因此会进入模型上下文的提示和 moderate 自动升级均已撤回；
当前只记录 `runtime_benefit_observed` telemetry，不启动、不要求、也不暗示角色调用。自动激活继续关闭，状态为
`In progress`。后续必须跨多个不同复杂 case 做配对重复实验，不能再依据单次启发式改变 Agent 行为。

## HARN-020：PASS、质量、运行效率和 Token 成本未形成独立评分信号

**严重性：** P1
**状态：** Validated
**模块：** `evals/scoring.py`、`evals/run_eval.py`、`evals/grader_common.py`、Model Broker、stress grader

旧版 stress grader 只有 `score == 100` 才判 PASS，同时把“至少两次测试、至少四次探索”和
“1-80 次工具调用”直接计为满分。一次耗时超过七分钟、39 次 LLM 请求、59 次工具调用的运行因此仍得
100；Broker 的 `requested_token_count` 只是每次 `max_tokens` 的累计额度，既不包含 input context，
也不等于 Provider 实际输出，无法表达成本。

评分 v2 将输出拆为独立 `passed` 和连续 `score`：功能正确性 50、确定性代码质量 20、运行效率 15、
Token 成本 15。可信 case grader 只决定功能、约束和静态质量；宿主 Harness 使用 Agent wall time、
logical model calls、工具调用、重复/错误事件和 Provider usage 补齐后两项。每个 lower-is-better 指标
都有 case metadata 中显式的 target 与 hard limit。失败运行的原始效率仍保留，但计分按功能正确率门控，
避免“快速空失败”获得高成本分。

Model Broker 现在累计 input、output、cache creation、cache read 和 total token；DeepSeek/OpenAI-compatible
适配器不再丢弃 HTTP `usage`。requested max tokens 继续只作为预算指标。summary 同时报告 usage 覆盖率、
实际 Token 总量与分项平均分。

真实 run `20260722-193424` 验证了新信号：Agent 未修复 canceled-to-confirmed 状态转换，结果为
`FAIL 76.24`，分项 `40/50 + 20/20 + 5.458/15 + 10.782/15`；Agent 350.394 秒、40 次
Provider call、59 次工具调用，40/40 响应均有 usage。实际 Token 为 421,790（input 410,586、output
11,204、cache read 55,296），而 requested max tokens 仅 301,600，直接证明旧代理指标不可靠。
完整回归 `234 passed, 3 skipped, 1 deselected`。

新 `stress_distributed_ledger_recovery` 又验证了连续评分：两个真实 FAIL 分别为 63.446 和 41.289，
功能分为 30/50 与 15/50，代码质量均为 20/20；运行/Token 分根据 359.1s/558,164 tokens 与
436.3s/584,117 tokens 拉开差距，没有因代码可编译或部分测试通过而接近 100。50/50 Provider 响应均有
usage，证明实际成本字段可用于难度 6 case。

---

## HARN-021：删除命令子字符串误判会终止合法验证

**严重性：** P1
**状态：** Validated
**模块：** `codepilot_s20/command_safety.py`、`hooks.py`、`basic_tools.py`

`20260722-213701` 的最后一个 bash 是合法的 `python -c` 状态转换验证，Python 文本包含
`Confirmed->Cancel raises`。旧实现对整段字符串搜索 `"rm "`，恰好命中 `Confirm ` 末尾，返回不可恢复的
delete permission denial，并把已经完成实现的 final 改成拒绝文本。修复后删除检测只检查 shell segment 的
executable 位置，支持链式/管道、PowerShell、`cmd /c` 和 shell `-c` wrapper；参数、引号内 Python 代码和
文档文本不再参与动词匹配。hook 与 direct `run_bash` 共用同一解析器。

参数化回归既证明 `rm/Remove-Item/del` 等真实删除仍被拦截，也覆盖原始 `Confirm raises` 文本。两次新
ledger 真实 run 均为 `permission_blocks=0`，正常执行 pytest 和 Python 命令；状态标记为 `Validated`。

---

## HARN-022：本地测试缓存可阻断可信 case 复制

**严重性：** P2
**状态：** Validated
**模块：** `evals/run_eval.py`、case copy clean-room

首次 ledger 真实运行在 0.1 秒、Agent 启动前失败：手工 public pytest 在 case workspace 生成的
`.pytest_cache` 带有不可复制的 Windows 权限，`copy_trusted_case()` 没有像 snapshot 一样忽略该运行产物。
现在 Agent workspace 与 trusted case 两条 copytree 路径统一忽略 `.pytest_cache`、`__pycache__`、pyc/pyo；
测试同时覆盖 workspace 和 grader_tests 缓存。清理本轮生成缓存后，随后两次真实 ledger case 均正常进入
Agent、grader 和 cleanup，状态标记为 `Validated`。

---

## HARN-023：单一中型 case 无法区分多组件恢复能力

**严重性：** P1
**状态：** Validated
**模块：** `evals/cases/stress_distributed_ledger_recovery`、Eval coverage

原 inventory case 已两次被纯 Lead 在 31～33 次请求内 PASS，无法证明 subagent/multiagent 能力，也不能覆盖
checkpoint 验证、durable tail replay、跨 partition 原子性等更宽的系统边界。新增 difficulty 6 ledger case：

- workspace 有 28 个文件，生产代码跨 API/service、六个 repository、fingerprint、projection 和 recovery；
- 隐藏 grader 将 atomic ingestion、exactly-once、partition ordering、checkpoint recovery、regression/API
  分组计分，单组失败只失去对应功能分；
- 不把是否调用角色作为验收条件，仍使用 50/20/15/15 的 outcome/quality/runtime/token 评分；
- 原始 fixture 稳定为 FAIL 25/70 静态分，受控参考修复通过全部公开/隐藏测试并得到 70/70 静态分，证明
  case 可解且评分确定；
- 两次真实 DeepSeek 运行分别得到 63.446 与 41.289，均完成容器/grader 生命周期但只通过部分 outcome，
  已实际拉开能力与成本差异。

---

## HARN-024：预算跳过 Reviewer 仍被标成已附加审查

**严重性：** P2
**状态：** Fixed
**模块：** `codepilot_s20/agent_loop.py`、Reviewer/final audit telemetry

ledger 首轮在 used_calls=44、remaining=6 时正确跳过 automatic Reviewer，但随后又记录
`reviewer_auto_observed`，final audit 标成 `reviewer_attached=true`，并把 `budget_reserved/blocked` envelope
包装为“Independent reviewer result”。这使 Trace 和模型都难以区分真实 fresh-context 审查与预算跳过。

现在 skipped envelope 只保留在 cache/telemetry 供同 revision 去重，不作为 Reviewer result 注入；audit 记录
`reviewer_attached=false, reviewer_status=skipped_budget`，只给 Lead 一句非 finding 的预算说明。若任务不需要
acceptance audit，跳过 reviewer 不再强行增加一轮模型调用。聚焦与全量回归已通过，等待下一次真实预算跳过
样本后再标记 `Validated`。

---

## 建议实施顺序

1. **HARN-001（In progress）**：针对 full compact 后的局部复读继续做确定性证据保留；ledger 两轮的
   `read_file=73/90` 表明不能用提示词代替 context 根因分析。
2. **HARN-019（In progress）**：自动角色激活保持关闭；至少对 inventory、ledger 和另一个不同架构 case
   做多次配对实验，只有角色实际降低 Lead calls/reads 且不降低通过率才开放。
3. **HARN-018（Fixed）+ HARN-024（Fixed）**：获取一次真实 Reviewer finding 的 ID 关闭样本，以及一次
   `skipped_budget` 样本，验证 final/Trace/Todo 三者一致后标记 Validated。
4. **HARN-010（Fixed）**：获取一次真实 Agent 异常且执行过 bash 的 Docker 样本，验证失败 summary 非零计数。
5. **HARN-017（Fixed）**：增加明确经过 legacy `task` 路由的真实模型验收 case。
6. **HARN-015（Fixed）**：复测标准 Explorer/Reviewer 和 Worker/Worktree 真实模型闭环。
7. **HARN-012（Fixed）**：验证 Reviewer finding 能防止虚假 completed evidence 直接通过 final。
8. **HARN-009（Fixed）**：在真实 case 验证普通 grep/调试命令保持同步。
9. **HARN-003（Fixed）**：保留一次真实 grader `not_started` 三态 lifecycle 样本。
10. 对 HARN-002/004/005/007/008/011/013/014/016/020/021/022/023 保持现有回归，不用重复改动已验证逻辑。

每个问题进入 `Fixed` 后，都应重新运行：

```powershell
python -m pytest -q <相关单元测试>
python -m pytest -q
python evals/run_eval.py --execution docker --docker-build --request-timeout 60 --case stress_inventory_reservation_consistency --docker-timeout 600
python evals/run_eval.py --execution docker --docker-build --request-timeout 60 --case stress_distributed_ledger_recovery --docker-timeout 900
```

真实模型复测会消耗额度，必须显式执行，不作为普通单元测试的一部分。
## Phase 3 Working Memory verification note (2026-07-23)

HARN-001 remains **In progress**. The Runtime now owns deterministic
`RunKnowledge` for file versions/digests, parsed symbols, contracts, modified
files, tests, Acceptance, and Reviewer findings. Context injects a bounded view
on every turn, and Compact no longer has to preserve those facts only through
raw messages. File tools and successful Worktree integration invalidate
evidence by changed path and version.

This implementation is covered by unit/integration tests, but HARN-001 must not
be marked closed until paired real-model
`stress_distributed_ledger_recovery` runs demonstrate all three criteria:

- median `read_file` calls below 45;
- pass rate no lower than the paired baseline;
- metered median token use at least 15% lower.

Use `evals/compare_phase3.py` for the comparison. Missing provider usage is a
failed verification, not a zero-token success.

The first paired measurement after this implementation used three baseline and
three candidate ledger runs. Baseline medians were 82 reads and 558,164 actual
tokens with 0/3 passes. Candidate medians were 67 reads and 550,619 actual
tokens with 1/3 passes. Pass rate improved, but reads remained above 45 and
token reduction was only 1.35%, so HARN-001 remains open.

A follow-up experiment that retained up to 28,000 characters of raw read
evidence across every full compact was rejected after one real run: 57 reads,
690,818 tokens, and a failed `exactly_once` outcome. That strategy was removed;
the retained implementation keeps structured RunKnowledge outside message
history without pinning a second copy of raw file contents into every prompt.

### Principle hardening (2026-07-24)

Tool projection now intersects Registry role access, AgentProfile tools, parent
Runtime policy, and environment policy for synchronous roles and asynchronous
Teammates. Registry schemas are recursively immutable, and every declared
Safety/Background policy has an executable dispatcher.

RunKnowledge now reconciles actual before/after Workspace fingerprints for
file tools, foreground/background Bash, and Worktree integration under a
thread-safe mutation boundary. Evidence uses `verified`, `stale`, and
`unbound`; TestKnowledge records Workspace-at-run state separately from
explicit source coverage.

Status remains **Implemented, not Validated**. Focused and full pytest passed,
but no real-model Eval was run for this change, as required. HARN-001 remains
open until the existing paired ledger exit criteria pass.
