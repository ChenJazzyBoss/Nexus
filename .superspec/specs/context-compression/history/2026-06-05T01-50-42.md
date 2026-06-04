# 上下文压缩

## Purpose

当 Agent 的对话历史超过模型上下文窗口限制时，LLM API 会返回 context_overflow 错误。上下文压缩模块负责检测 token 用量、裁剪旧的工具输出、调用辅助 LLM 生成结构化摘要，并将压缩后的消息重新组装。这是保证长对话可用性的关键基础设施。

<!-- DIAGRAM:flowchart -->

```
AgentRunner 主循环
     │
     ├─ 每次 API 调用前
     │      │
     │      ├─ estimate_tokens(messages) → prompt_tokens
     │      │
     │      ├─ prompt_tokens >= threshold?
     │      │      │
     │      │      NO → 正常调用 LLM
     │      │      │
     │      │      YES → 启动压缩流程
     │      │              │
     │      │              ├─ Phase 1: 裁剪旧 tool outputs
     │      │              │     └─ 替换为 1 行摘要
     │      │              │
     │      │              ├─ Phase 2: 确定裁剪边界
     │      │              │     ├─ 保护头部（system + 前 N 条）
     │      │              │     └─ 保护尾部（最近 ~20K tokens）
     │      │              │
     │      │              ├─ Phase 3: 生成摘要
     │      │              │     └─ 调辅助 LLM 生成结构化摘要
     │      │              │
     │      │              └─ Phase 4: 重新组装
     │      │                    └─ head + summary + tail
     │      │
     │      └─ 压缩后重新检查（最多 3 轮）
     │
     └─ 工具执行后
            │
            └─ 用 API 返回的实际 token 重新检查
```

## Requirements

### Requirement: Token 估算

ContextCompressor SHALL 在每次 LLM 调用前估算消息的 token 数量。

#### Scenario: 粗估计算
Given messages 列表包含 10 条消息，总文本长度约 4000 字符
When 调用 estimate_tokens(messages)
Then SHALL 返回粗估 token 数（英文 ~chars/4，中文 ~chars/2）
And 估算 SHALL 包含 system prompt 和工具定义的 token
And 估算耗时 SHALL 不超过 1ms

#### Scenario: 使用 API 实际值
Given 上一次 LLM 调用返回了 usage.prompt_tokens
When 后续检查 token 量
Then SHALL 优先使用 API 返回的实际 prompt_tokens 值
And 实际值 SHALL 覆盖粗估值

---

### Requirement: 压缩触发判断

ContextCompressor SHALL 根据 token 阈值决定是否触发压缩。

#### Scenario: 正常触发
Given 模型上下文窗口为 128K tokens，threshold_percent 为 0.50
When prompt_tokens 达到 64K tokens
Then should_compress() SHALL 返回 True

#### Scenario: 未达阈值
Given prompt_tokens 为 30K tokens，阈值为 64K
When 调用 should_compress()
Then SHALL 返回 False

#### Scenario: 防抖动保护
Given 最近两次压缩分别只节省了 5% 和 8% 的 token
When 再次触发压缩检查
Then SHALL 跳过本次压缩（防止无效压缩循环）
And SHALL 记录 WARNING 日志

---

### Requirement: 工具输出裁剪

压缩的第一阶段 SHALL 裁剪旧的工具输出，用简短摘要替代。

#### Scenario: 裁剪旧 tool result
Given 消息列表中有 5 条 tool_result，最早的 3 条超过 500 字符
When 执行 _prune_old_tool_results
Then 最早的 3 条 tool_result 内容 SHALL 被替换为 1 行摘要
And 摘要格式 SHALL 为 "[tool_name] 执行完成，输出已裁剪"
And 最近 2 条 tool_result SHALL 保持不变

#### Scenario: 裁剪后 token 减少
Given 裁剪前 prompt_tokens 为 70K
When 工具输出裁剪完成
Then prompt_tokens SHALL 显著减少（至少减少 20%）

---

### Requirement: 摘要生成

压缩的第三阶段 SHALL 调用辅助 LLM 生成结构化摘要。

#### Scenario: 首次压缩
Given 需要压缩的消息段落包含 15 条消息
When 调用 _generate_summary
Then SHALL 使用辅助 LLM（轻量模型）生成摘要
And 摘要 SHALL 包含以下结构：
  - 已解决的问题（Resolved）
  - 待处理的问题（Pending）
  - 当前任务（Active Task）
  - 剩余工作（Remaining Work）
And 摘要长度 SHALL 不超过 500 字

#### Scenario: 迭代更新摘要
Given 已有一个之前的摘要，需要再次压缩
When 调用 _generate_summary
Then SHALL 把旧摘要和新消息一起发给辅助 LLM
And 生成的摘要 SHALL 迭代更新而非从头生成

#### Scenario: 辅助 LLM 失败
Given 辅助 LLM 调用失败（API 错误）
When 摘要生成失败
Then SHALL 降级为简单的消息截断（丢弃中间消息）
And SHALL 记录 ERROR 日志

---

### Requirement: 消息重组

压缩的第四阶段 SHALL 将保护头部、摘要、保护尾部重新组装。

#### Scenario: 标准重组
Given 保护头部 3 条消息、生成的摘要、保护尾部 5 条消息
When 执行重组
Then SHALL 按顺序组装：head messages + summary message + tail messages
And summary message 的 role SHALL 为 "user"（保持交替）
And 孤立的 tool_call/tool_result 对 SHALL 被清理

#### Scenario: 重组后 token 量
Given 压缩前 70K tokens
When 重组完成
Then 压缩后 SHALL 低于阈值（< 64K tokens）
And 关键上下文（最近对话）SHALL 保留完整

---

### Requirement: 压缩编排

AgentRunner SHALL 在对话循环中正确调用压缩流程。

#### Scenario: 循环前预检
Given AgentRunner 开始新一轮对话
When 进入主循环前
Then SHALL 估算 token 量
And 如果超阈值，SHALL 在进入循环前完成压缩

#### Scenario: 工具执行后检查
Given Agent 执行了一个返回大量数据的工具
When 工具执行完成
Then SHALL 用 API 返回的实际 token 重新检查
And 如果超阈值，SHALL 在下一次 LLM 调用前压缩

#### Scenario: 最大压缩轮数
Given 压缩后仍然超阈值
When 需要再次压缩
Then SHALL 最多执行 3 轮压缩
And 3 轮后仍然超阈值 SHALL 记录 ERROR 并继续（不阻塞）
