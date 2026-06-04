# MCP 工具集成

## Purpose

Nexus 已实现 McpHub 客户端模块，能够连接 MCP 服务器并调用其工具，但目前未将 MCP 工具注册到 Agent 的 ToolRegistry 中。本 spec 定义如何将用户在 `.claude/settings.json` 中配置的 MCP 服务器自动发现、连接、并将工具注册为 Agent 可用的 CoreTool，使 Agent 能够使用 arXiv 检索、GitHub 搜索、文件系统操作等外部能力。

<!-- DIAGRAM:flowchart -->

```
Nexus 启动
     │
     ├─ 1. 发现阶段
     │      │
     │      ├─ 读取 .claude/settings.json
     │      ├─ 解析 mcpServers 配置
     │      └─ 获取每个服务器的 command + args
     │
     ├─ 2. 连接阶段
     │      │
     │      ├─ McpHub.connect_server(name, command, args)
     │      ├─ 获取服务器工具列表
     │      └─ 超时/失败 → 跳过并记录警告
     │
     ├─ 3. 注册阶段
     │      │
     │      ├─ 将每个 MCP 工具转换为 CoreTool
     │      ├─ ToolRegistry.register(name, schema, handler)
     │      └─ handler → McpHub.call_tool(server, name, args)
     │
     └─ 4. 运行阶段
            │
            ├─ Agent 调用工具 → ToolRegistry.dispatch
            ├─ MCP 工具 → McpHub.call_tool → MCP 服务器
            └─ 返回结果给 Agent
```

## Requirements

### Requirement: MCP 配置自动发现

Nexus SHALL 从 `.claude/settings.json` 自动读取 MCP 服务器配置。

#### Scenario: 读取标准配置
Given `.claude/settings.json` 包含 mcpServers 配置：
  ```json
  {
    "mcpServers": {
      "arxiv": {
        "command": "npx",
        "args": ["-y", "arxiv-mcp-server"]
      }
    }
  }
  ```
When Nexus 启动
Then SHALL 解析出服务器名称 "arxiv"
And SHALL 提取 command="npx" 和 args=["-y", "arxiv-mcp-server"]

#### Scenario: 配置文件不存在
Given `.claude/settings.json` 文件不存在
When Nexus 启动
Then SHALL 使用空配置继续运行
And SHALL 记录 INFO 日志 "MCP config not found, skipping"
And Agent SHALL 仍可使用内置工具正常工作

#### Scenario: 配置中无 mcpServers 字段
Given `.claude/settings.json` 存在但不包含 mcpServers 字段
When Nexus 启动
Then SHALL 跳过 MCP 初始化
And 不 SHALL 报错

#### Scenario: 多服务器配置
Given 配置包含 3 个 MCP 服务器（arxiv、github、filesystem）
When Nexus 启动
Then SHALL 依次尝试连接每个服务器
And 每个服务器的连接 SHALL 独立（一个失败不影响其他）

---

### Requirement: MCP 服务器连接

Nexus SHALL 通过 McpHub 连接 MCP 服务器并获取工具列表。

#### Scenario: 成功连接
Given MCP 服务器 "arxiv" 可正常启动
When 调用 McpHub.connect_server("arxiv", "npx", ["-y", "arxiv-mcp-server"])
Then SHALL 建立 stdio 连接
And SHALL 获取服务器提供的工具列表
And 每个工具 SHALL 包含：name、description、inputSchema

#### Scenario: 连接超时
Given MCP 服务器启动超过 15 秒
When 连接超时
Then SHALL 放弃该服务器
And SHALL 记录 WARNING 日志 "MCP server arxiv connection timeout"
And 其他服务器 SHALL 继续正常连接

#### Scenario: 连接失败
Given MCP 服务器命令不存在（如 npx 未安装）
When 连接失败
Then SHALL 捕获异常
And SHALL 记录 ERROR 日志包含错误详情
And SHALL 跳过该服务器继续运行

#### Scenario: 需要可选依赖
Given mcp Python 包未安装
When 尝试连接任何 MCP 服务器
Then SHALL 记录 WARNING "MCP support requires: pip install mcp"
And SHALL 跳过所有 MCP 服务器连接
And 内置工具 SHALL 不受影响

---

### Requirement: MCP 工具注册

成功连接的 MCP 工具 SHALL 被注册到 ToolRegistry 中，使 Agent 能够像调用内置工具一样调用 MCP 工具。

#### Scenario: 自动注册
Given MCP 服务器 "arxiv" 提供了工具 "search_papers"（description="Search arXiv papers"）
When 连接成功
Then SHALL 在 ToolRegistry 中注册工具 "mcp__arxiv__search_papers"
And toolset SHALL 为 "mcp_arxiv"
And schema SHALL 从 MCP 工具的 inputSchema 转换而来
And handler SHALL 调用 McpHub.call_tool("arxiv", "search_papers", args)

#### Scenario: 工具名冲突
Given 内置工具已注册 "search_papers"
And MCP 服务器也提供 "search_papers"
When 注册 MCP 工具
Then SHALL 使用前缀 "mcp__{server}__{tool}" 避免冲突
And 内置工具 SHALL 不被覆盖

#### Scenario: 工具列表刷新
Given MCP 服务器连接后工具列表发生变化
When Agent 执行任务时
Then SHALL 以连接时获取的工具列表为准
And 不 SHALL 动态刷新（简化实现）

---

### Requirement: MCP 工具调用

Agent SHALL 通过 ToolRegistry 调用 MCP 工具，调用过程对 Agent 透明。

#### Scenario: 正常调用
Given Agent 决定调用 "mcp__arxiv__search_papers" 工具
When ToolRegistry.dispatch("mcp__arxiv__search_papers", {"query": "transformer"})
Then SHALL 调用 McpHub.call_tool("arxiv", "search_papers", {"query": "transformer"})
And SHALL 将 MCP 服务器返回的结果转为 JSON 字符串
And SHALL 返回给 Agent 作为工具调用结果

#### Scenario: 调用超时
Given MCP 工具执行超过 30 秒
When 调用超时
Then SHALL 返回错误结果 "Tool execution timeout"
And SHALL 不阻塞 Agent 后续执行

#### Scenario: MCP 服务器断开
Given MCP 服务器在任务执行过程中断开连接
When Agent 调用该服务器的工具
Then SHALL 返回错误结果 "MCP server disconnected"
And SHALL 记录 ERROR 日志
And Agent SHALL 可以尝试其他工具继续执行
