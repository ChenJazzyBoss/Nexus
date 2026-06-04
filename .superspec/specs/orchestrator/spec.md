# Orchestrator 编排引擎

## Purpose

Orchestrator 是 Nexus 的核心调度模块，负责接收用户任务、分解为子任务、派发给 Agent 并行执行、聚合结果返回。它是多 Agent 协作的大脑，决定了任务如何被拆解、分配和执行。Orchestrator 通过 AgentPool 管理并发，通过 EventBus 发布生命周期事件供 Visualizer 监听。

<!-- DIAGRAM:flowchart -->

```
用户提交任务
    │
    ▼
Orchestrator.execute_task(task)
    │
    ├─ 任务简单？ ──YES──→ 直接派发单个 Agent
    │                          │
    │                          ▼
    │                    Agent.run_turn(task)
    │                          │
    │                          ▼
    │                    返回结果给用户
    │
    └─ 任务复杂？ ──YES──→ decompose_task(task)
                               │
                               ▼
                         子任务列表 [A, B, C]
                               │
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
              spawn_worker spawn_worker spawn_worker
              (Agent A)    (Agent B)    (Agent C)
                    │          │          │
                    ▼          ▼          ▼
              并行执行等待完成
                               │
                               ▼
                    aggregate_results(results)
                               │
                               ▼
                         返回聚合结果给用户
```

## Requirements

### Requirement: 任务接收与执行

Orchestrator SHALL 提供统一的任务接收接口，支持简单任务直接执行和复杂任务分解执行。

#### Scenario: 简单任务直接执行
Given 用户提交一个任务 "查询 Transformer 论文的发表年份"
When Orchestrator 判断任务复杂度为简单（无需分解）
Then SHALL 直接创建单个 AgentRunner 并执行
And SHALL 返回该 Agent 的执行结果
And SHALL 通过 EventBus 发布 task_started 和 task_completed 事件

#### Scenario: 复杂任务分解执行
Given 用户提交一个任务 "分析 Transformer 论文的核心贡献并对比 BERT"
When Orchestrator 判断任务复杂度为复杂
Then SHALL 调用 decompose_task 将任务分解为子任务列表
And SHALL 为每个子任务创建独立的 AgentRunner
And SHALL 并行派发子任务到 AgentPool
And SHALL 等待所有子任务完成
And SHALL 调用 aggregate_results 合并结果
And SHALL 返回聚合后的最终结果

#### Scenario: 任务提交返回任务 ID
Given 用户提交一个任务
When Orchestrator 接收到任务
Then SHALL 立即返回一个唯一的 task_id
And 任务 SHALL 在后台异步执行
And 用户 SHALL 可以通过 task_id 查询任务状态

---

### Requirement: 任务分解

Orchestrator SHALL 使用 LLM 将复杂任务分解为可独立执行的子任务。

#### Scenario: LLM 分解任务
Given 一个复杂任务 "分析 Transformer 论文并生成报告"
When Orchestrator 调用 decompose_task
Then SHALL 使用 LLM（配置的默认模型）分析任务
And SHALL 返回结构化的子任务列表
And 每个子任务 SHALL 包含：goal（目标）、tools（所需工具集）、role（角色描述）

#### Scenario: 子任务独立性
Given LLM 返回了子任务列表 [A, B, C]
When 子任务被派发执行
Then 每个子任务 SHALL 拥有独立的上下文
And 子任务之间 SHALL 不能直接访问彼此的消息历史
And 子任务失败 SHALL 不影响其他子任务执行

#### Scenario: 分解失败降级
Given LLM 调用 decompose_task 时发生错误
When 分解失败
Then SHALL 将原始任务作为单个子任务直接执行
And SHALL 记录警告日志

---

### Requirement: Agent 派发与并发控制

Orchestrator SHALL 通过 AgentPool 管理 Agent 并发数量，防止资源耗尽。

#### Scenario: 并发数限制
Given AgentPool 的 max_concurrent 设置为 5
When 同时有 8 个子任务需要执行
Then SHALL 最多同时运行 5 个 Agent
And 剩余 3 个 SHALL 在队列中等待
And 等待中的任务 SHALL 在有 Agent 空闲时自动启动

#### Scenario: Agent 隔离
Given Orchestrator 派发了子任务给 Agent A 和 Agent B
When Agent A 和 Agent B 并行执行
Then 每个 Agent SHALL 拥有独立的 IterationBudget
And 每个 Agent SHALL 拥有独立的消息历史
And 每个 Agent SHALL 拥有配置的工具子集（不能访问全部工具）

#### Scenario: 工具集限制
Given 子任务需要 "文档解析" 工具集
When 创建 AgentRunner 时
Then Agent 的 tool_names SHALL 仅包含 "文档解析" 工具集中的工具
And Agent SHALL 不能调用其他工具集的工具

---

### Requirement: 结果聚合

Orchestrator SHALL 将多个子任务的结果聚合为最终响应。

#### Scenario: 所有子任务成功
Given 子任务 A、B、C 全部执行成功
When Orchestrator 调用 aggregate_results
Then SHALL 使用 LLM 将子任务结果合并为连贯的最终响应
And SHALL 标记任务状态为 completed

#### Scenario: 部分子任务失败
Given 子任务 A 成功、B 失败、C 成功
When Orchestrator 调用 aggregate_results
Then SHALL 合并成功子任务的结果
And SHALL 在最终响应中标明哪些子任务失败及原因
And SHALL 标记任务状态为 completed（非 failed）

#### Scenario: 全部子任务失败
Given 子任务 A、B、C 全部执行失败
When Orchestrator 调用 aggregate_results
Then SHALL 返回错误信息，列出所有失败原因
And SHALL 标记任务状态为 failed

---

### Requirement: 事件发布

Orchestrator SHALL 通过 EventBus 发布任务生命周期事件，供 Visualizer 等组件监听。

#### Scenario: 任务生命周期事件
Given 一个任务从提交到完成的完整流程
When 任务经历各个阶段
Then SHALL 按顺序发布以下事件：
  - task_created（任务创建，包含 task_id）
  - task_started（任务开始执行）
  - subtask_created（子任务创建，包含子任务信息）
  - subtask_started（子任务开始执行）
  - subtask_completed（子任务完成，包含结果摘要）
  - subtask_failed（子任务失败，包含错误信息）
  - task_completed（任务完成）或 task_failed（任务失败）

#### Scenario: 事件包含上下文
Given 一个 subtask_completed 事件
When 事件被发布
Then 事件数据 SHALL 包含：task_id、subtask_id、agent_id、result_summary、duration_seconds

---

### Requirement: 超时与错误处理

Orchestrator SHALL 处理子任务超时和各种错误场景。

#### Scenario: 子任务超时
Given 子任务执行超过配置的超时时间（默认 300 秒）
When Orchestrator 检测到超时
Then SHALL 终止该子任务的 Agent
And SHALL 标记该子任务为 failed
And 其他子任务 SHALL 继续执行不受影响

#### Scenario: LLM 调用失败
Given Orchestrator 调用 LLM 进行任务分解或结果聚合时失败
When 发生 API 错误
Then SHALL 使用 error_classifier 进行错误分类
And 如果可重试，SHALL 使用 jittered_backoff 重试（最多 3 次）
And 如果不可重试，SHALL 返回错误信息给用户

#### Scenario: AgentPool 耗尽
Given 所有 Agent 槽位都被占用且等待队列已满
When 新的子任务需要派发
Then SHALL 返回错误 "Agent pool exhausted"
And SHALL 记录错误日志

---

### Requirement: 任务状态查询

Orchestrator SHALL 支持通过 task_id 查询任务状态。

#### Scenario: 查询进行中的任务
Given 一个正在执行的任务
When 用户查询该任务的 task_id
Then SHALL 返回任务状态（pending/running/completed/failed）
And SHALL 返回已完成子任务的结果
And SHALL 返回进行中子任务的当前状态

#### Scenario: 查询已完成任务
Given 一个已完成的任务
When 用户查询该任务的 task_id
Then SHALL 返回完整的任务结果
And SHALL 返回所有子任务的执行详情（耗时、状态）

---

### Requirement: 可视化事件流

Orchestrator SHALL 支持 SSE（Server-Sent Events）实时事件流。

#### Scenario: SSE 连接
Given 用户打开可视化面板
When 建立 SSE 连接
Then SHALL 立即发送当前所有进行中任务的状态
And SHALL 实时推送后续的所有生命周期事件

#### Scenario: 多客户端监听
Given 多个用户同时打开可视化面板
When 任务执行过程中产生事件
Then 所有连接的 SSE 客户端 SHALL 收到相同的事件
And 一个客户端断开 SHALL 不影响其他客户端
