# 记忆注入

## Purpose

Agent 需要在对话中访问持久化的记忆信息（用户偏好、项目知识、历史决策），而不需要用户每次重复说明。记忆注入模块负责从内置记忆文件（MEMORY.md）和外部记忆 provider 中读取信息，注入到 system prompt 和 user message 中，使 Agent 具备跨会话的长期记忆能力。

<!-- DIAGRAM:flowchart -->

```
AgentRunner.run_turn(user_message)
     │
     ├─ 1. 构建 system prompt
     │      │
     │      ├─ 基础 prompt（角色定义 + 工具说明）
     │      ├─ + 内置记忆块（MEMORY.md 内容）
     │      └─ + 外部记忆块（provider.system_prompt_block()）
     │
     ├─ 2. 主循环中（每次 API 调用前）
     │      │
     │      ├─ MemoryManager.prefetch_all(query)
     │      │      └─ 各 provider 返回相关记忆片段
     │      │
     │      └─ 注入到 user message
     │             └─ "<memory-context>...记忆内容...</memory-context>"
     │
     └─ 3. 对话结束后
            │
            └─ MemoryManager.sync_all(user_msg, assistant_reply)
                   └─ 各 provider 记录本轮对话
```

## Requirements

### Requirement: 内置记忆 Provider

Nexus SHALL 提供一个内置的 MemoryProvider，从本地文件读取记忆。

#### Scenario: 读取 MEMORY.md
Given ~/.nexus/memory/MEMORY.md 文件存在，包含用户偏好和项目知识
When MemoryStore 初始化
Then SHALL 读取文件内容
And SHALL 解析为记忆条目列表
And format_for_system_prompt() SHALL 返回格式化的记忆块

#### Scenario: 文件不存在
Given ~/.nexus/memory/MEMORY.md 不存在
When MemoryStore 初始化
Then SHALL 创建空文件
And format_for_system_prompt() SHALL 返回空字符串
And 不 SHALL 报错

#### Scenario: 记忆文件过大
Given MEMORY.md 超过 10000 字符
When format_for_system_prompt()
Then SHALL 截断到 memory_char_limit（默认 8000 字符）
And SHALL 在末尾添加 "[记忆已截断]" 标记

---

### Requirement: System Prompt 注入

MemoryManager SHALL 将记忆信息注入 Agent 的 system prompt。

#### Scenario: 注入内置记忆
Given MemoryStore 包含用户偏好 "用户偏好中文回复"
When AgentRunner 构建 system prompt
Then system prompt 的 volatile tier SHALL 包含记忆块
And 记忆块 SHALL 用 `<memory-context>` 标签包裹
And 记忆块 SHALL 在基础 prompt 之后、工具说明之前

#### Scenario: 注入外部记忆
Given MemoryManager 有一个外部 provider 且 system_prompt_block() 返回内容
When AgentRunner 构建 system prompt
Then 外部记忆块 SHALL 追加在内置记忆之后
And 两个记忆块 SHALL 用分隔线隔开

#### Scenario: 无记忆
Given MemoryStore 为空且无外部 provider
When AgentRunner 构建 system prompt
Then system prompt SHALL 不包含记忆块
And 不 SHALL 出现空的 `<memory-context>` 标签

---

### Requirement: Prefetch 注入

每轮对话开始时，SHALL 从外部记忆 provider 预取相关信息并注入上下文。

#### Scenario: 正常 prefetch
Given 用户提交 "继续上次的 Transformer 分析"
When MemoryManager.prefetch_all("继续上次的 Transformer 分析")
Then SHALL 调用每个 provider 的 prefetch() 方法
And 返回的记忆片段 SHALL 包裹在 `<memory-context>` 中
And SHALL 注入到当前轮的 user message 中

#### Scenario: prefetch 超时
Given 外部 provider 的 prefetch 超过 5 秒
When prefetch 超时
Then SHALL 放弃本次 prefetch
And SHALL 记录 WARNING 日志
And Agent SHALL 继续正常执行

#### Scenario: prefetch 失败
Given 外部 provider 抛出异常
When prefetch 失败
Then SHALL 捕获异常并记录 WARNING
And 不 SHALL 影响 Agent 执行

---

### Requirement: Turn 同步

对话结束后，SHALL 将本轮对话同步给外部记忆 provider。

#### Scenario: 正常同步
Given 本轮对话 user_message="分析 Transformer"，assistant_reply="Transformer 的核心..."
When MemoryManager.sync_all(user_message, assistant_reply)
Then SHALL 调用每个 provider 的 sync_turn() 方法
And 同步 SHALL 在 Agent 返回结果后异步执行
And 同步失败 SHALL 不影响用户收到结果

#### Scenario: 无外部 provider
Given MemoryManager 没有注册任何外部 provider
When 调用 sync_all()
Then SHALL 直接返回（无操作）
And 不 SHALL 报错
