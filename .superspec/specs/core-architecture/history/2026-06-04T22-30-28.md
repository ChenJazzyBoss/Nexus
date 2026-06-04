# Nexus 核心架构

## Purpose

Nexus 是一个通用的多智能体协作平台，支持多个 AI Agent 协同完成任务（科研、编码、数据分析等）。系统采用模块化架构，通过 MCP 协议连接外部工具和数据源，支持云端 7×24 部署。核心设计原则：不重复造轮子，优先从现有开源项目（hermes-agent、claude-code、openclaw、llm_wiki）迁移可复用代码。

<!-- DIAGRAM:flowchart -->

```
                         ┌─────────────────────────────────────────────────────┐
                         │                   REST / WebSocket API              │
                         │              (FastAPI, port 8600)                   │
                         └──────────┬──────────────────────────┬──────────────┘
                                    │                          │
                    ┌───────────────┴───────────┐    ┌────────┴────────────┐
                    │       Orchestrator         │    │   Visualizer Hub    │
                    │  (task decomposition,      │    │  (SSE event stream, │
                    │   agent lifecycle,         │    │   status dashboard) │
                    │   result aggregation)      │    └────────────────────┘
                    └──────┬──────────┬──────────┘
                           │          │
              ┌────────────┴──┐  ┌────┴────────────┐
              │  Agent Pool   │  │   Session Store  │
              │  (AgentRunner │  │  (SQLite, JSONL  │
              │   instances)  │  │   transcripts)   │
              └───┬───────┬───┘  └─────────────────┘
                  │       │
       ┌──────────┴──┐  ┌─┴──────────────┐
       │ Tool Engine │  │ Context Engine  │
       │ (registry,  │  │ (compression,   │
       │  MCP tools, │  │  token budget)  │
       │  execution) │  └────────────────┘
       └──────┬──────┘
              │
    ┌─────────┴──────────┐
    │     MCP Hub         │
    │ (client to external │
    │  MCP servers,       │
    │  server exposing    │
    │  Nexus tools)       │
    └─────────┬──────────┘
              │
   ┌──────────┴──────────────────┐
   │       Knowledge Store        │
   │  ┌───────────┐ ┌──────────┐ │
   │  │  SQLite    │ │ LanceDB  │ │
   │  │ (metadata, │ │ (vector  │ │
   │  │  sessions, │ │  search) │ │
   │  │  config)   │ │          │ │
   │  └───────────┘ └──────────┘ │
   │  ┌───────────────────────┐  │
   │  │ Knowledge Graph       │  │
   │  │ (entity/relation      │  │
   │  │  extraction)          │  │
   │  └───────────────────────┘  │
   └──────────────────────────────┘
```

## Requirements

### Requirement: 模块化分层架构

系统 SHALL 采用分层模块化架构，每个模块独立可替换，模块间通过定义良好的接口通信。

#### Scenario: 正常流程 - 模块独立启动
Given 系统的所有模块已定义
When 启动任意单个模块
Then 该模块 SHALL 能够独立初始化而不依赖其他模块的运行时实例
And 模块间通过接口（Protocol/ABC）而非具体实现耦合

#### Scenario: 正常流程 - 替换存储后端
Given 系统使用 LanceDB 作为向量存储
When 将 LanceDB 替换为其他向量数据库（如 ChromaDB）
Then 仅需修改 `nexus.knowledge.vector_store` 模块
And 其他模块 SHALL 无需任何代码变更

#### Scenario: 异常场景 - 模块初始化失败
Given 某个非核心模块（如 Visualizer）初始化失败
When 系统启动
Then 核心功能（Orchestrator、Agent、Tool）SHALL 仍然可用
And 失败模块 SHALL 记录错误日志并降级运行

---

### Requirement: Agent 运行时

系统 SHALL 提供 Agent 运行时，支持单个 Agent 执行对话循环（模型调用、工具分发、迭代预算、重试）。

#### Scenario: 正常流程 - Agent 执行一轮对话
Given 一个已初始化的 AgentRunner 实例
When 收到用户消息
Then Agent SHALL 调用 LLM 获取响应
And 如果响应包含工具调用，SHALL 执行工具并将结果反馈给 LLM
And 循环直到 LLM 返回最终文本响应或达到迭代预算上限

#### Scenario: 正常流程 - 迭代预算控制
Given Agent 的迭代预算设置为 10 次
When Agent 执行第 10 次工具调用
Then Agent SHALL 停止执行并返回当前累积结果
And 预算消耗 SHALL 线程安全

#### Scenario: 异常场景 - LLM 调用失败
Given LLM API 返回可重试错误（如 429、503）
When Agent 执行对话循环
Then Agent SHALL 使用指数退避重试（最多 3 次）
And 如果重试耗尽，SHALL 返回错误信息而非崩溃

#### Scenario: 异常场景 - 工具执行超时
Given 工具执行超过配置的超时时间
When Agent 等待工具结果
Then Agent SHALL 终止该工具调用
And 将超时错误作为工具结果反馈给 LLM

---

### Requirement: 工具注册系统

系统 SHALL 提供自注册的工具系统，支持工具动态注册、分组、分发。

#### Scenario: 正常流程 - 注册工具
Given 一个 Python 模块定义了使用 `@registry.register` 装饰器的工具
When 该模块被导入
Then 工具 SHALL 自动注册到全局 ToolRegistry
And 工具的 schema、handler、元数据 SHALL 被正确记录

#### Scenario: 正常流程 - 按工具集过滤
Given Agent 配置了允许的工具集 `{"web", "file"}`
When Agent 请求可用工具列表
Then 仅返回属于 "web" 和 "file" 工具集的工具
And 其他工具集的工具 SHALL 不可见

#### Scenario: 正常流程 - 通过 MCP 扩展工具
Given 用户配置了外部 MCP 服务器
When MCP Hub 连接到该服务器
Then MCP 服务器提供的工具 SHALL 被自动发现并注册到 ToolRegistry
And 工具名称 SHALL 带有 MCP 服务器前缀（如 `mcp_server_name__tool_name`）

#### Scenario: 异常场景 - 工具执行失败
Given 工具 handler 抛出异常
When Agent 调用该工具
Then ToolRegistry SHALL 捕获异常并返回错误信息
And 异常 SHALL 不导致 Agent 进程崩溃

---

### Requirement: MCP 协议集成

系统 SHALL 支持 MCP（Model Context Protocol）协议，既能作为 MCP 客户端连接外部服务器，也能作为 MCP 服务器暴露自身能力。

#### Scenario: 正常流程 - 连接外部 MCP 服务器
Given 配置了 stdio 类型的 MCP 服务器
When Nexus 启动
Then MCP Hub SHALL 启动子进程并建立 stdio 连接
And 自动发现该服务器提供的所有工具
And 工具 SHALL 被注册到 ToolRegistry 供 Agent 使用

#### Scenario: 正常流程 - 作为 MCP 服务器
Given Nexus 已启动且配置了 MCP 服务器模式
When 外部 MCP 客户端（如 Claude Code）连接
Then Nexus SHALL 暴露已注册的工具供外部调用
And 支持 stdio 传输方式

#### Scenario: 异常场景 - MCP 服务器断开
Given 已连接的 MCP 服务器进程异常退出
When MCP Hub 检测到连接断开
Then SHALL 尝试重新连接（指数退避，最多 5 次）
And 如果重连失败，SHALL 从 ToolRegistry 中移除该服务器的工具
And 通知相关 Agent 工具已不可用

---

### Requirement: 多 Agent 编排

系统 SHALL 支持多 Agent 协作，包括任务分解、Agent 派发、结果聚合。

#### Scenario: 正常流程 - 协调者分解任务
Given 用户提交一个复杂任务
When Orchestrator 接收到任务
Then SHALL 将任务分解为多个子任务
And 为每个子任务分配合适的 Agent（指定工具集、系统提示）
And 并行派发子任务到 Agent Pool

#### Scenario: 正常流程 - Agent 隔离
Given Orchestrator 派发了子任务给 Agent A 和 Agent B
When Agent A 和 Agent B 并行执行
Then 每个 Agent SHALL 拥有独立的上下文、工具集和迭代预算
And Agent 之间 SHALL 不能直接访问彼此的消息历史

#### Scenario: 正常流程 - 结果聚合
Given 所有子任务 Agent 已完成执行
When Orchestrator 收集结果
Then SHALL 将子任务结果聚合为最终响应
And 返回给用户时 SHALL 包含任务完成状态和关键结果摘要

#### Scenario: 异常场景 - 子任务失败
Given 某个子任务 Agent 执行失败
When Orchestrator 检测到失败
Then SHALL 记录失败原因
And 其他子任务 SHALL 继续执行不受影响
And 最终结果 SHALL 标明哪些子任务失败

---

### Requirement: 知识存储

系统 SHALL 提供统一的知识存储，支持文档导入、混合检索（关键词+向量）、知识图谱。

#### Scenario: 正常流程 - 文档导入
Given 一个 Markdown 文档被放入监控目录
When 文件同步服务检测到变更
Then 文档 SHALL 被解析并存储到 SQLite（元数据）和 LanceDB（向量嵌入）
And 知识图谱 SHALL 更新文档中的 wikilink 关系

#### Scenario: 正常流程 - 混合检索
Given 用户查询 "Transformer 的注意力机制"
When 执行搜索
Then 系统 SHALL 同时执行关键词搜索和向量搜索
And 使用 RRF（Reciprocal Rank Fusion）融合两种搜索结果
And 返回 top-k 结果，包含相关度分数和上下文片段

#### Scenario: 正常流程 - 定期更新
Given 配置了定期检索任务（如每周扫描 arXiv）
When 定时任务触发
Then MCP Hub SHALL 通过连接的 MCP 服务器检索新论文
And 新论文 SHALL 被自动导入知识库
And 已存在的论文 SHALL 被跳过（基于 DOI 或标题去重）

#### Scenario: 异常场景 - 嵌入服务不可用
Given 外部嵌入 API 返回错误
When 执行向量搜索
Then 系统 SHALL 降级为纯关键词搜索
And SHALL 记录警告日志

---

### Requirement: 可视化面板

系统 SHALL 提供实时可视化面板，展示 Agent 协作过程。

#### Scenario: 正常流程 - 实时事件流
Given 多个 Agent 正在执行任务
When 用户打开可视化面板
Then SHALL 通过 SSE 实时推送 Agent 状态变更事件
And 事件 SHALL 包含：Agent ID、当前状态、正在执行的工具、进度

#### Scenario: 正常流程 - 任务树展示
Given 一个包含多个子任务的复杂任务
When 用户查看任务详情
Then SHALL 展示任务树（父任务 → 子任务 → Agent 状态）
And 每个节点 SHALL 显示执行时间和结果状态

---

### Requirement: 云端部署

系统 SHALL 支持 Docker 容器化部署，能够在云服务器上 7×24 运行。

#### Scenario: 正常流程 - Docker 启动
Given Docker 镜像已构建
When 运行 `docker-compose up`
Then 系统 SHALL 启动所有核心服务
And API 服务器 SHALL 在配置的端口（默认 8600）监听
And MCP 服务器 SHALL 通过 stdio 可访问

#### Scenario: 正常流程 - 环境变量配置
Given 通过环境变量设置了 LLM API 密钥
When 系统启动
Then ProviderProfile SHALL 自动读取环境变量配置 Agent
And 敏感信息 SHALL 不被记录到日志

#### Scenario: 异常场景 - 容器重启
Given 容器因异常崩溃
When Docker 自动重启容器
Then 系统 SHALL 从 SQLite 恢复未完成的会话
And 已注册的 MCP 服务器 SHALL 自动重新连接
