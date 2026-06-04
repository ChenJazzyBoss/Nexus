# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Nexus is a multi-agent collaboration platform. It orchestrates multiple AI agents to work together on tasks like research, coding, data analysis, and knowledge management.

## Architecture

```
┌─────────────────────────────────────┐
│          Nexus Platform             │
│                                     │
│  ┌──────────┐    ┌──────────┐      │
│  │ Orchestrator│──→│ Agent Pool │    │
│  └──────────┘    └──────────┘      │
│       ↕               ↕            │
│  ┌──────────┐    ┌──────────┐      │
│  │  MCP Hub  │    │ Knowledge │    │
│  └──────────┘    └──────────┘      │
│       ↕                             │
│  ┌──────────┐                       │
│  │ Visualizer│                      │
│  └──────────┘                       │
└─────────────────────────────────────┘
```

### Core Components

- **Orchestrator** — Receives tasks, decomposes into subtasks, assigns to agents
- **Agent Pool** — Manages agent lifecycle, execution, and communication
- **MCP Hub** — Standard protocol for connecting external tools and data sources
- **Knowledge** — Document storage, vector search, knowledge graph
- **Visualizer** — Real-time dashboard showing agent collaboration

## Key Design Decisions

- Python 3.11+ with async/await for concurrency
- MCP as the standard tool/data protocol (not custom integrations)
- SQLite for metadata, LanceDB for vector search
- Docker for deployment, supports cloud server 24/7 operation
- Each agent is isolated with its own context and tools

## Development

```bash
# Install
pip install -e .

# Run tests
pytest

# Lint
ruff check .
ruff check --fix .
```

<!-- superspec:begin -->
## superSpec

这是一个使用 superSpec 管理 spec 的项目。

### 可用技能

- `/generate-spec` — 生成新的 spec 文件
- `/validate-spec` — 校验 spec 文件

### 校验命令

```bash
node .superspec/scripts/validate.js <spec-path>
node .superspec/scripts/validate.js <spec-path> --strict
```

### spec 目录结构

```
.superspec/
├── config.yaml          # 项目配置
├── scripts/
│   └── validate.js      # 校验脚本
├── specs/
│   └── <spec-name>/
│       └── spec.md      # spec 文件
└── templates/
    └── spec-template.md # spec 模板
```
<!-- superspec:end -->
