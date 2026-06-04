# 多轮对话

## Purpose

当前 Nexus 的 Orchestrator 只支持单次任务提交——用户提交任务，Agent 执行一次，返回结果，结束。实际使用中，用户需要基于上一轮结果进行追问、修改方向、深入探索。本 spec 定义多轮对话能力：维护 session 上下文，支持追问和任务延续，让 Orchestrator 成为一个真正的对话式研究助手。

<!-- DIAGRAM:flowchart -->

```
用户: "分析 Transformer 论文的核心贡献"
     │
     ▼
Orchestrator.execute_task(task, session_id=null)
     │
     ├─ 创建新 session → session_abc123
     ├─ 分解任务 → 派发 Agent → 收集结果
     └─ 返回结果 + session_id
            │
            ▼
用户: "对比一下 BERT 的异同"  ← 追问
     │
     ▼
Orchestrator.execute_task(task, session_id="session_abc123")
     │
     ├─ 加载 session 历史（含上轮结果）
     ├─ 将历史注入 Agent 上下文
     ├─ 分解任务 → 派发 Agent → 收集结果
     └─ 返回结果（继承上下文）
```

## Requirements

### Requirement: Session 管理

Orchestrator SHALL 为每个任务会话创建和维护 session，支持跨多轮对话保持上下文。

#### Scenario: 首次任务创建 session
Given 用户提交任务 "分析 Transformer 论文" 且未指定 session_id
When Orchestrator.execute_task 执行
Then SHALL 创建新 session（通过 SessionStore.create_session）
And SHALL 返回 session_id 给客户端
And session SHALL 记录创建时间和初始任务描述

#### Scenario: 追问复用 session
Given 用户已有 session_id="session_abc123"
When 用户提交追问 "对比 BERT 的异同" 并指定 session_id
Then SHALL 通过 SessionStore.load_session 加载历史消息
And SHALL 将历史消息注入 Agent 上下文
And 新的对话消息 SHALL 追加到同一 session

#### Scenario: session 不存在
Given 用户指定的 session_id 不存在
When Orchestrator.execute_task 执行
Then SHALL 创建新 session（降级为首次任务）
And SHALL 记录 WARNING 日志

---

### Requirement: 上下文注入

多轮对话时，前几轮的对话历史 SHALL 注入 Agent 的上下文，使 Agent 能理解追问的语境。

#### Scenario: 注入历史消息
Given session 中已有 3 轮对话（6 条消息）
When 用户提交第 4 轮追问
Then AgentRunner 的 messages 初始内容 SHALL 包含历史消息
And 历史消息 SHALL 在 system prompt 之后、当前用户消息之前
And 历史消息 SHALL 保留原始 role（user/assistant）

#### Scenario: 历史消息截断
Given session 中已有 20 轮对话（40 条消息）
When 用户提交第 21 轮追问
Then SHALL 仅保留最近 10 轮（20 条）历史消息
And 更早的消息 SHALL 被截断（不注入上下文）
And 截断处 SHALL 添加标记 "[历史消息已截断]"

#### Scenario: 上下文超限
Given 注入历史消息后总 token 数超过模型上下文窗口
When AgentRunner 检测到超限
Then SHALL 逐步截断历史消息（从最旧开始）
And SHALL 记录 WARNING 日志
And SHALL 确保当前轮消息不被截断

---

### Requirement: API 接口变更

POST /api/task 接口 SHALL 支持 session_id 参数，返回结果 SHALL 包含 session_id。

#### Scenario: 无 session_id 请求
Given 客户端发送 {"task": "分析 Transformer 论文"}
When 服务器处理请求
Then SHALL 创建新 session
And 返回结果 SHALL 包含 session_id 字段

#### Scenario: 带 session_id 请求
Given 客户端发送 {"task": "对比 BERT", "session_id": "session_abc123"}
When 服务器处理请求
Then SHALL 复用指定 session
And 返回结果 SHALL 包含相同的 session_id

#### Scenario: 前端自动传递 session_id
Given 用户首次提交任务，返回了 session_id
When 用户再次提交任务（追问）
Then 前端 SHALL 自动在请求中携带上次返回的 session_id
And 用户 SHALL 不需要手动管理 session_id

---

### Requirement: 会话历史查看

用户 SHALL 能查看当前 session 的对话历史。

#### Scenario: API 查询历史
Given session "session_abc123" 已有 3 轮对话
When 客户端请求 GET /api/session/session_abc123
Then SHALL 返回该 session 的所有消息列表
And 每条消息 SHALL 包含 role、content、timestamp

#### Scenario: 前端展示历史
Given 用户在多轮对话中
When 新一轮结果返回
Then 前端 SHALL 保留并展示之前的对话内容
And 新结果 SHALL 追加在之前内容下方
And 用户 SHALL 能看到完整的对话脉络
