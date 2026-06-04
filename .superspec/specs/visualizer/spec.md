# Visualizer 智能体画布

## Purpose

Visualizer 是 Nexus 的核心可视化模块，以**画布动画**的形式展现多智能体协作的全过程。用户提交任务后，主智能体（Coordinator）出现在画布中央，随后根据任务分解结果派生出多个子智能体（Worker）。每个子智能体以动画形式移动到自己的工位执行任务，完成后携带结果返回主智能体处汇报。整个过程以直观的动画叙事呈现，让用户能够实时观察任务的拆解、分配、执行和聚合全过程。侧边栏提供智能体列表，点击可查看每个智能体的详细工作内容。

<!-- DIAGRAM:flowchart -->

```
用户提交任务
     │
     ▼
画布中央出现主智能体 (Coordinator)
     │
     ├─ LLM 分解任务
     │      │
     │      ▼
     │  生成子任务列表 [A, B, C]
     │      │
     │      ▼
     │  主智能体逐个派发子任务
     │      │
     │      ├─ 子智能体 A 诞生 ──→ 动画移动到工位 A ──→ 执行任务
     │      ├─ 子智能体 B 诞生 ──→ 动画移动到工位 B ──→ 执行任务
     │      └─ 子智能体 C 诞生 ──→ 动画移动到工位 C ──→ 执行任务
     │                                        │
     │                                        ▼
     │                              子智能体完成任务
     │                                        │
     │                                        ▼
     │                              子智能体动画返回主智能体
     │                                        │
     │                                        ▼
     │                              主智能体聚合结果
     │                                        │
     │                                        ▼
     └──────────────────────────→ 画布展示最终结果
```

## Requirements

### Requirement: 画布场景

Visualizer SHALL 提供一个全屏画布作为智能体活动的场景空间。画布 SHALL 使用浅色主题，背景 SHALL 带有轻微网格纹理以增强空间感。画布 SHALL 占据页面主区域（除侧边栏外的全部空间）。

#### Scenario: 页面加载
Given 用户访问 http://localhost:8080
When 页面加载完成
Then SHALL 显示全屏画布（浅色背景 + 网格纹理）
And SHALL 显示底部输入栏（任务输入框 + 执行按钮）
And SHALL 显示右侧侧边栏（智能体列表面板）
And 画布中央 SHALL 显示 Nexus 品牌标识作为默认状态
And 所有文本 SHALL 使用中文

#### Scenario: 画布尺寸自适应
Given 用户在不同分辨率的显示器上访问
When 页面渲染
Then 画布 SHALL 自动填充可用空间
And 画布 SHALL 支持缩放和平移操作
And 智�体位置 SHALL 基于画布坐标系而非屏幕像素

---

### Requirement: 主智能体（Coordinator）

任务提交后，画布中央 SHALL 出现一个主智能体角色，代表任务协调者。主智能体 SHALL 有独特的视觉样式以区别于子智能体。

#### Scenario: 主智能体出现
Given 用户提交了一个任务
When Orchestrator 接收到任务
Then 画布中央 SHALL 出现主智能体角色（带入场动画）
And 主智能体 SHALL 显示为较大的圆形头像 + "协调者" 标签
And 主智能体 SHALL 带有脉冲光环效果表示正在思考

#### Scenario: 主智能体派发任务
Given 主智能体正在分解任务
When 子任务列表生成完成
Then 主智能体 SHALL 逐个生成子智能体
And 每个子智能体生成时 SHALL 从主智能体位置出发
And 主智能体 SHALL 显示 "派发中..." 状态

#### Scenario: 主智能体聚合结果
Given 所有子智能体已完成任务并返回
When 主智能体接收到所有结果
Then 主智能体 SHALL 显示 "聚合中..." 状态
And 聚合完成后 SHALL 显示最终结果气泡
And 主智能体 SHALL 恢复空闲状态

---

### Requirement: 子智能体（Worker）

子智能体由主智能体派生，每个子智能体代表一个子任务的执行者。子智能体 SHALL 有独立的视觉表现和动画行为。

#### Scenario: 子智能体诞生与移动
Given 主智能体派发了子任务 A
When 子智能体 A 被创建
Then SHALL 在主智能体位置生成子智能体（带诞生动画：从小变大 + 透明度渐变）
And 子智能体 SHALL 以动画形式移动到分配的工位
And 移动轨迹 SHALL 为平滑的贝塞尔曲线而非直线
And 移动过程中 SHALL 显示拖尾效果

#### Scenario: 子智能体在工位工作
Given 子智能体已到达工位
When 子任务正在执行
Then 子智能体 SHALL 显示 "执行中" 状态（旋转加载动画）
And 工位 SHALL 显示当前执行的工具调用信息
And 子智能体 SHALL 有轻微的上下浮动动画表示活跃

#### Scenario: 子智能体完成返回
Given 子智能体已完成子任务
When 结果准备就绪
Then 子智能体 SHALL 从工位出发，以动画形式返回主智能体位置
And 返回路径 SHALL 与去程不同（避免视觉重复）
And 到达主智能体后 SHALL 播放 "汇报" 动画（短暂放大 + 绿色光晕）
And 子智能体 SHALL 变为 "已完成" 样式（灰色 + 勾选标记）

#### Scenario: 子智能体失败
Given 子智能体执行子任务时发生错误
When 任务失败
Then 子智能体 SHALL 显示 "失败" 样式（红色边框 + 错误标记）
And SHALL 仍返回主智能体位置汇报失败原因

---

### Requirement: 工位（Workstation）

工位是画布上预设的位置，子智能体移动到工位执行任务。工位 SHALL 有明确的视觉标识。

#### Scenario: 工位布局
Given 主智能体派发了 3 个子任务
When 子智能体被创建
Then 画布 SHALL 在主智能体周围均匀分布 3 个工位
And 工位 SHALL 以虚线圆圈标识
And 每个工位 SHALL 显示子任务编号和目标摘要

#### Scenario: 工位状态变化
Given 子智能体正在工位执行任务
When 工位状态改变
Then 空闲工位 SHALL 显示为虚线圆圈
And 占用工位 SHALL 显示为实线圆圈 + 子智能体颜色
And 工位 SHALL 显示当前执行的工具名称

---

### Requirement: 侧边栏智能体列表

右侧侧边栏 SHALL 显示所有智能体的列表，支持点击查看详情。

#### Scenario: 智能体列表
Given 画布上有 1 个主智能体和 3 个子智能体
When 用户查看侧边栏
Then SHALL 显示主智能体卡片（始终在顶部）
And SHALL 显示所有子智能体卡片
And 每个卡片 SHALL 显示：智能体 ID、角色、状态、当前任务摘要
And 主智能体 SHALL 有特殊标记（皇冠图标或 "主" 标签）

#### Scenario: 点击查看详情
Given 侧边栏显示子智能体列表
When 用户点击某个子智能体卡片
Then 画布 SHALL 自动平移并缩放以聚焦该智能体
And 侧边栏 SHALL 展开详情面板
And 详情面板 SHALL 显示：工具调用历史、执行耗时、输入输出摘要

#### Scenario: 实时状态更新
Given 子智能体状态发生变化（创建/工作中/完成/失败）
When 状态更新事件通过 SSE 推送
Then 侧边栏卡片 SHALL 实时更新状态标签和颜色
And 画布上的智能体动画 SHALL 同步更新

---

### Requirement: 动画系统

画布 SHALL 使用流畅的动画展现智能体的完整工作流程，动画 SHALL 平滑不卡顿。

#### Scenario: 动画帧率
Given 画布上有多个智能体同时运动
When 动画播放
Then 帧率 SHALL 保持在 30fps 以上
And 动画 SHALL 使用 requestAnimationFrame 驱动
And 移动动画 SHALL 使用缓动函数（ease-in-out）

#### Scenario: 动画队列
Given 多个子智能体同时被派发
When 子智能体依次出发
Then SHALL 使用队列控制出发顺序（间隔 300-500ms）
And 避免多个智能体在同一时间从同一位置出发造成视觉混乱

---

### Requirement: SSE 事件驱动

画布动画 SHALL 由后端 SSE 事件驱动，事件类型映射到动画行为。

#### Scenario: 事件到动画的映射
Given 后端通过 SSE 推送事件
When 事件到达前端
Then task_created SHALL 触发主智能体出现动画
And subtask_created SHALL 触发子智能体诞生 + 移动到工位动画
And subtask_started SHALL 触发工位工作状态动画
And subtask_completed SHALL 触发子智能体返回动画
And task_completed SHALL 触发主智能体结果展示动画
And task_failed/subtask_failed SHALL 触发失败状态动画

#### Scenario: 事件缓冲
Given 短时间内收到多个事件
When 事件处理
Then SHALL 将事件加入队列逐个处理
And 避免多个动画同时触发导致视觉混乱
And 每个动画 SHALL 在上一个动画的关键节点后启动

---

### Requirement: 底部输入栏

页面底部 SHALL 提供固定的任务输入区域。

#### Scenario: 输入与提交
Given 用户在底部输入栏输入任务
When 点击执行按钮或按 Enter
Then SHALL 向后端提交任务
And 输入框 SHALL 变为禁用状态
And 显示 "部署中..." 提示
And 任务完成后 SHALL 恢复可输入状态

#### Scenario: 快捷标签
Given 底部输入栏下方显示快捷标签
When 用户点击某个标签
Then 输入框 SHALL 自动填入对应任务描述
