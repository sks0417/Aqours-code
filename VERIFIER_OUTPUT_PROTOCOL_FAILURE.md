# Verifier 输出协议失败总结

## 摘要

在 `stress_atomic_cache_recovery` 的 `20260810-144145` 运行中，Verifier 已正确使用共享全局预算，但没有交付有效审查结果。它最终被记录为：

```text
status: inconclusive
failure_reason: invalid_verifier_json
model_calls: 12
tool_calls: 23
tests_run: 2
findings_found: 0
```

这不是模型调用或累计 Token 预算耗尽。Verifier 启动时仍有 30 次全局调用可用，整次运行最终只使用了 57/64 次调用。

## 直接原因

Verifier 工具阶段结束后的第一次结果生成请求使用 `max_tokens=6000`，响应消耗了完整 6000 output tokens，并以 `stop_reason=max_tokens` 结束，但没有产生可解析的可见文本。Token 很可能消耗在 Provider reasoning 阶段，最终 JSON 尚未生成。

随后代码启动 fresh-context synthesis。该请求丢弃了前一次响应中的 reasoning continuation，只重新提供 assignment 和截断后的 `role_evidence`。虽然 synthesis 请求没有工具权限，system prompt 仍包含 Verifier 的工具使用指令，模型最终把新的读文件意图输出成了纯文本 DSML tool-call 标记，而不是要求的 JSON。

当前解析器只接受 JSON 对象。DSML 文本解析失败后没有进一步的格式修复，因此结果被标记为 `invalid_verifier_json`。

## 是否已经发现隐藏缺陷

没有可审计证据表明 Verifier 已经发现了具体缺陷后仅因协议失败而丢失结果。

Verifier 读取了相关源码并运行了两次完整 public pytest，但没有运行或输出以下关键检查：

```python
# after_publish 已完成 durable publication，即使 Hook 抛错，lock 也必须终结为 committed
assert lock_state == "committed"
```

隐藏测试发现的实际问题仍是 `after_publish` 抛错后通用异常路径将 lock 写成 `aborted`。Verifier 的可见输出没有包含这一判断。截断响应可能含有未记录的内部 reasoning，因此不能绝对排除它曾考虑过该问题，但不能据此认定它已经形成 finding。

## 建议的最小修复方向

1. 当 Verifier 结果响应因 `max_tokens` 截断时，保留 Provider reasoning continuation，不要立即丢弃并从空白上下文重新 synthesis。
2. synthesis 使用独立、精简、无工具语义的 system instruction，避免在 `tools=[]` 时继续诱导模型输出工具调用。
3. 使用结构化结果 schema 提交 Verifier 结果，避免完全依赖自由文本 JSON。
4. 第一次 synthesis 格式无效时，允许一次仍受全局 `max_model_calls` 管理的格式修复调用。
5. Trace 明确记录截断、格式修复尝试和最终失败原因。

需要补充的聚焦测试应覆盖：reasoning-only 截断响应、DSML 非 JSON 响应、格式修复后的合法 finding、全局调用耗尽，以及 finding 正确返回 Lead 的闭环。

## 边界

修复输出协议只能保证 Verifier 的结论能够可靠交付，不能保证它一定发现 `after_publish` lock-state 缺陷。协议可靠性与审查质量是两个独立问题；后者需要单独评估针对错误路径和持久化状态收敛的验证策略。
