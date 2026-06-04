"""Knowledge graph builder from wikilinks.

Adapted from llm_wiki/src-tauri/src/api_server.rs.
Extracts [[wikilinks]] from markdown files and builds a knowledge graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class GraphNode:
    """A node in the knowledge graph."""
    id: str
    label: str
    node_type: str = "other"
    path: str = ""
    link_count: int = 0


@dataclass
class GraphEdge:
    """An edge in the knowledge graph."""
    source: str
    target: str
    weight: float = 1.0


@dataclass
class GraphData:
    """Complete graph data."""
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


def extract_type(content: str) -> str:
    """Extract 'type' from YAML frontmatter."""
    lines = content.split('\n')
    in_frontmatter = False

    for line in lines:
        stripped = line.strip()
        if stripped == '---':
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith('type:'):
            return stripped[5:].strip().strip('"\'').lower()

    return "other"


def extract_wikilinks(content: str) -> list[str]:
    """Extract [[wikilinks]] from markdown content.

    Supports [[target|alias]] format.
    """
    links = []
    rest = content
    while True:
        start = rest.find('[[')
        if start == -1:
            break
        rest = rest[start + 2:]
        end = rest.find(']]')
        if end == -1:
            break
        inner = rest[:end]
        target = inner.split('|')[0].strip()
        if target:
            links.append(target)
        rest = rest[end + 2:]
    return links


def resolve_link(raw: str, ids: set[str]) -> str | None:
    """Resolve a wikilink to a known document ID.

    Case-insensitive matching with space-to-hyphen normalization.
    """
    if raw in ids:
        return raw

    normalized = raw.lower().replace(' ', '-')
    for doc_id in ids:
        if doc_id.lower() == normalized or doc_id.lower() == raw.lower():
            return doc_id

    return None


def build_graph(documents: dict[str, str]) -> GraphData:
    """Build a knowledge graph from documents with wikilinks.

    Args:
        documents: {doc_id: content} mapping

    Returns:
        GraphData with nodes and edges
    """
    # Extract metadata for each document
    doc_meta: dict[str, dict[str, Any]] = {}

    for doc_id, content in documents.items():
        # Extract title
        title = doc_id
        lines = content.split('\n')
        in_frontmatter = False
        for line in lines:
            stripped = line.strip()
            if stripped == '---':
                if in_frontmatter:
                    break
                in_frontmatter = True
                continue
            if in_frontmatter and stripped.startswith('title:'):
                title = stripped[6:].strip().strip('"\'')
                break
        else:
            # Check for first heading
            for line in lines:
                if line.startswith('# '):
                    title = line[2:].strip()
                    break

        node_type = extract_type(content)
        links = extract_wikilinks(content)

        doc_meta[doc_id] = {
            "title": title,
            "type": node_type,
            "links": links,
        }

    ids = set(doc_meta.keys())

    # Build edges
    link_count: dict[str, int] = {doc_id: 0 for doc_id in ids}
    seen_edges: set[str] = set()
    edges: list[GraphEdge] = []

    for source, meta in doc_meta.items():
        for link in meta["links"]:
            target = resolve_link(link, ids)
            if not target or target == source:
                continue

            # Deduplicate edges (undirected)
            edge_key = f"{min(source, target)}::{max(source, target)}"
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            link_count[source] = link_count.get(source, 0) + 1
            link_count[target] = link_count.get(target, 0) + 1

            edges.append(GraphEdge(
                source=source,
                target=target,
                weight=1.0,
            ))

    # Build nodes (exclude query type)
    nodes = []
    for doc_id, meta in doc_meta.items():
        if meta["type"] == "query":
            continue
        nodes.append(GraphNode(
            id=doc_id,
            label=meta["title"],
            node_type=meta["type"],
            path=f"wiki/{doc_id}.md",
            link_count=link_count.get(doc_id, 0),
        ))

    return GraphData(nodes=nodes, edges=edges)
