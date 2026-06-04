# 知识库集成

## Purpose

Nexus 已实现知识库基础模块（SessionStore、VectorStore、HybridSearch、KnowledgeGraph），但这些模块目前独立运行，未与 Agent 执行流程集成。本 spec 定义知识库如何嵌入 Agent 的生命周期：任务执行前检索相关知识作为上下文，任务执行后将结果持久化存储，会话消息自动保存以便后续检索和复用。

<!-- DIAGRAM:flowchart -->

```
用户提交任务
     │
     ▼
AgentRunner.run_turn(user_message)
     │
     ├─ 1. 检索阶段
     │      │
     │      ├─ SessionStore.search_messages(query) ──→ 历史相关消息
     │      ├─ HybridSearch.search(query) ───────────→ 知识库相关文档
     │      └─ 合并结果注入 messages 作为 context
     │
     ├─ 2. 执行阶段
     │      │
     │      └─ LLM 对话 + 工具调用（正常流程）
     │
     └─ 3. 持久化阶段
            │
            ├─ SessionStore.save_message(role, content)
            ├─ VectorStore.upsert_chunks(result_chunks)
            └─ KnowledgeGraph 更新（如有 wikilinks）
```

## Requirements

### Requirement: 会话持久化

AgentRunner SHALL 在每轮对话后自动保存用户消息和助手回复到 SessionStore。

#### Scenario: 首次任务保存
Given 用户提交任务 "分析 Transformer 论文"
When AgentRunner 完成该轮对话
Then SHALL 调用 SessionStore.save_message 保存用户消息（role=user）
And SHALL 调用 SessionStore.save_message 保存助手回复（role=assistant）
And 消息 SHALL 包含 session_id、role、content、timestamp

#### Scenario: 多轮对话连续保存
Given 用户在同一 session 中连续提问
When 每轮对话完成
Then SHALL 按顺序保存每条消息
And 消息顺序 SHALL 与对话顺序一致
And SessionStore.load_session 能完整还原对话历史

#### Scenario: 保存失败不影响执行
Given SessionStore 发生写入错误（如磁盘满）
When save_message 抛出异常
Then SHALL 捕获异常并记录警告日志
And AgentRunner SHALL 继续正常运行不中断
And 用户 SHALL 不感知保存失败

---

### Requirement: 知识检索增强

AgentRunner SHALL 在执行任务前检索知识库，将相关历史信息注入上下文。

#### Scenario: 检索历史对话
Given 用户提交任务 "继续上次的 Transformer 分析"
When AgentRunner 执行 run_turn
Then SHALL 调用 SessionStore.search_messages 搜索相关历史消息
And SHALL 将搜索到的 top-3 相关消息注入 system prompt 或 messages 前部
And 注入的消息 SHALL 标记为 "参考历史" 以区别于当前对话

#### Scenario: 检索知识库文档
Given 知识库中已有 Transformer 论文的摘要文档
When 用户提交 "总结 Transformer 的核心贡献"
Then SHALL 调用 HybridSearch.search(query) 检索相关文档
And SHALL 将 top-3 检索结果注入上下文
And 检索结果 SHALL 包含来源信息（标题、相关度分数）

#### Scenario: 检索结果为空
Given 知识库中没有相关内容
When HybridSearch.search 返回空列表
Then SHALL 继续正常执行（不注入额外上下文）
And 不 SHALL 报错或中断

#### Scenario: 检索超时
Given 知识库检索超过 3 秒
When 检索超时
Then SHALL 放弃本次检索，继续执行
And SHALL 记录警告日志

---

### Requirement: 结果自动入库

任务完成后，AgentRunner SHALL 将有价值的结果自动存入知识库。

#### Scenario: 结果存入向量库
Given 任务完成，返回结果文本
When 结果长度超过 50 字符
Then SHALL 调用 VectorStore.upsert_chunks 将结果分块存储
And 每个 chunk SHALL 包含：content、source（任务描述）、timestamp
And chunk 大小 SHALL 不超过 500 字符

#### Scenario: 短结果不入库
Given 任务完成，返回结果 "42"
When 结果长度不超过 50 字符
Then SHALL 不调用 VectorStore.upsert_chunks
And SHALL 仅通过 SessionStore 保存消息

#### Scenario: 入库失败不影响返回
Given VectorStore 写入失败
When upsert_chunks 抛出异常
Then SHALL 捕获异常并记录警告日志
And 任务结果 SHALL 正常返回给用户

---

### Requirement: 知识图谱更新

当任务结果包含 wikilinks（如 [[Transformer]]）时，SHALL 自动更新知识图谱。

#### Scenario: 提取并更新图谱
Given 任务结果包含 "[[Transformer]] 和 [[BERT]] 的对比"
When 结果保存到知识库
Then SHALL 调用 extract_wikilinks 提取链接
And SHALL 调用 build_graph 更新知识图谱节点和边
And 图谱 SHALL 记录 Transformer 和 BERT 之间的关联

#### Scenario: 无 wikilinks 不更新
Given 任务结果不包含 wikilinks 格式
When 结果保存
Then SHALL 不调用 build_graph
And 知识图谱 SHALL 保持不变
