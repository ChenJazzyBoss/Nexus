"""Hybrid search engine — keyword + vector + RRF fusion.

Adapted from llm_wiki/src-tauri/src/commands/search.rs.
Provides hybrid search combining keyword matching and vector similarity
using Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# RRF constant (from llm_wiki)
RRF_K = 60.0

# Scoring weights (from llm_wiki)
FILENAME_EXACT_BONUS = 200.0
PHRASE_IN_TITLE_BONUS = 50.0
PHRASE_IN_CONTENT_PER_OCC = 20.0
TITLE_TOKEN_WEIGHT = 5.0
CONTENT_TOKEN_WEIGHT = 1.0
SNIPPET_CONTEXT = 80


@dataclass
class SearchResult:
    """A single search result."""
    path: str
    title: str
    snippet: str
    score: float
    title_match: bool = False
    vector_score: float | None = None
    content: str | None = None


@dataclass
class SearchResponse:
    """Search response with metadata."""
    mode: str  # keyword | vector | hybrid
    results: list[SearchResult]
    token_hits: int = 0
    vector_hits: int = 0


# ── Tokenizer (from llm_wiki) ──────────────────────────────────


def tokenize_query(query: str) -> list[str]:
    """Tokenize query with CJK bigram support.

    Splits on whitespace and punctuation, keeps CJK characters as bigrams.
    """
    query = query.lower().strip()
    tokens = []

    # Split on non-word, non-CJK characters
    parts = re.split(r'[^\w一-鿿぀-ゟ゠-ヿ]+', query)

    for part in parts:
        if not part:
            continue
        # Check if CJK
        if re.match(r'[一-鿿]', part):
            # Generate bigrams for CJK
            for i in range(len(part) - 1):
                tokens.append(part[i:i+2])
            if len(part) == 1:
                tokens.append(part)
        else:
            tokens.append(part)

    return [t for t in tokens if len(t) > 1]


def extract_title(content: str, filename: str = "") -> str:
    """Extract title from YAML frontmatter or first heading."""
    lines = content.split('\n')
    in_frontmatter = False

    for line in lines:
        stripped = line.strip()
        if stripped == '---':
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped.startswith('title:'):
                return stripped[6:].strip().strip('"\'')
        elif stripped.startswith('# '):
            return stripped[2:].strip()

    # Fallback to filename
    if filename:
        return filename.replace('.md', '').replace('-', ' ').replace('_', ' ')
    return "Untitled"


def extract_wikilinks(content: str) -> list[str]:
    """Extract [[wikilinks]] from markdown content."""
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


def build_snippet(content: str, query_tokens: list[str], context_chars: int = SNIPPET_CONTEXT) -> str:
    """Build a context-aware snippet around the first match."""
    content_lower = content.lower()

    # Find first token match
    best_pos = -1
    for token in query_tokens:
        pos = content_lower.find(token)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos == -1:
        # No match found, return beginning
        return content[:context_chars * 2].strip() + "..."

    # Extract context around match
    start = max(0, best_pos - context_chars)
    end = min(len(content), best_pos + context_chars)

    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."

    return snippet


# ── Keyword Search ──────────────────────────────────────────────


def keyword_search(
    documents: dict[str, str],
    query: str,
    top_k: int = 20,
) -> list[SearchResult]:
    """Search documents by keyword matching.

    Args:
        documents: {path: content} mapping
        query: search query
        top_k: max results

    Returns:
        Sorted list of SearchResult
    """
    if not query or not query.strip():
        return []

    tokens = tokenize_query(query)
    if not tokens:
        tokens = [query.lower().strip()]

    query_lower = query.lower().strip()
    results = []

    for path, content in documents.items():
        title = extract_title(content, Path(path).stem)
        content_lower = content.lower()
        title_lower = title.lower()
        filename_lower = Path(path).stem.lower().replace('-', ' ').replace('_', ' ')

        score = 0.0
        title_match = False

        # Filename exact match bonus
        if query_lower == filename_lower:
            score += FILENAME_EXACT_BONUS

        # Phrase in title bonus
        if query_lower in title_lower:
            score += PHRASE_IN_TITLE_BONUS
            title_match = True

        # Token scoring
        for token in tokens:
            # Title matches (weighted higher)
            count = title_lower.count(token)
            if count > 0:
                score += count * TITLE_TOKEN_WEIGHT
                title_match = True

            # Content matches
            count = content_lower.count(token)
            if count > 0:
                score += min(count, 10) * CONTENT_TOKEN_WEIGHT

        # Phrase in content bonus
        phrase_count = content_lower.count(query_lower)
        if phrase_count > 0:
            score += min(phrase_count, 10) * PHRASE_IN_CONTENT_PER_OCC

        if score > 0:
            snippet = build_snippet(content, tokens)
            results.append(SearchResult(
                path=path,
                title=title,
                snippet=snippet,
                score=score,
                title_match=title_match,
            ))

    # Sort by score descending, then path ascending
    results.sort(key=lambda r: (-r.score, r.path))
    return results[:top_k]


# ── RRF Fusion ──────────────────────────────────────────────────


def apply_rrf_scores(
    keyword_results: list[SearchResult],
    vector_results: list[dict[str, Any]],
) -> list[SearchResult]:
    """Apply Reciprocal Rank Fusion to combine keyword and vector results.

    RRF score = sum(1 / (k + rank)) for each ranking list.
    """
    # Build keyword rank map
    keyword_rank: dict[str, int] = {}
    for i, r in enumerate(keyword_results):
        keyword_rank[r.path] = i + 1

    # Build vector rank map
    vector_rank: dict[str, int] = {}
    vector_score_map: dict[str, float] = {}
    for i, r in enumerate(vector_results):
        path = r.get("path", r.get("page_id", ""))
        vector_rank[path] = i + 1
        vector_score_map[path] = r.get("score", 0.0)

    # Merge all paths
    all_paths = set(keyword_rank.keys()) | set(vector_rank.keys())

    # Build result map
    result_map: dict[str, SearchResult] = {}
    for r in keyword_results:
        result_map[r.path] = r

    # Apply RRF
    results = []
    for path in all_paths:
        existing = result_map.get(path)
        if existing:
            result = SearchResult(
                path=existing.path,
                title=existing.title,
                snippet=existing.snippet,
                score=0.0,
                title_match=existing.title_match,
                vector_score=vector_score_map.get(path),
                content=existing.content,
            )
        else:
            result = SearchResult(
                path=path,
                title=Path(path).stem,
                snippet="",
                score=0.0,
                vector_score=vector_score_map.get(path),
            )

        # RRF calculation
        rrf = 0.0
        if path in keyword_rank:
            rrf += 1.0 / (RRF_K + keyword_rank[path])
        if path in vector_rank:
            rrf += 1.0 / (RRF_K + vector_rank[path])

        result.score = rrf
        results.append(result)

    results.sort(key=lambda r: (-r.score, r.path))
    return results


# ── Hybrid Search ───────────────────────────────────────────────


class HybridSearch:
    """Hybrid search engine combining keyword and vector search."""

    def __init__(self, vector_store=None):
        self.vector_store = vector_store

    async def search(
        self,
        documents: dict[str, str],
        query: str,
        top_k: int = 20,
        query_embedding: list[float] | None = None,
    ) -> SearchResponse:
        """Execute hybrid search.

        Args:
            documents: {path: content} mapping
            query: search query
            top_k: max results
            query_embedding: optional vector embedding for the query

        Returns:
            SearchResponse with merged results
        """
        # Keyword search
        keyword_results = keyword_search(documents, query, top_k=top_k)

        # Vector search (if available)
        vector_results: list[dict[str, Any]] = []
        if query_embedding and self.vector_store:
            try:
                chunks = self.vector_store.search(query_embedding, top_k=top_k)
                for chunk in chunks:
                    vector_results.append({
                        "path": chunk.page_id,
                        "score": chunk.score,
                        "chunk_text": chunk.chunk_text,
                    })
            except Exception as e:
                logger.warning("Vector search failed: %s", e)

        # Determine mode
        if vector_results:
            mode = "hybrid" if keyword_results else "vector"
        else:
            mode = "keyword"

        # Apply RRF if we have both
        if keyword_results and vector_results:
            merged = apply_rrf_scores(keyword_results, vector_results)
            return SearchResponse(
                mode="hybrid",
                results=merged[:top_k],
                token_hits=len(keyword_results),
                vector_hits=len(vector_results),
            )

        # Keyword only
        if keyword_results:
            return SearchResponse(
                mode="keyword",
                results=keyword_results[:top_k],
                token_hits=len(keyword_results),
            )

        # Vector only
        if vector_results:
            results = []
            for vr in vector_results:
                results.append(SearchResult(
                    path=vr["path"],
                    title=Path(vr["path"]).stem,
                    snippet=vr.get("chunk_text", "")[:200],
                    score=vr["score"],
                    vector_score=vr["score"],
                ))
            return SearchResponse(
                mode="vector",
                results=results[:top_k],
                vector_hits=len(vector_results),
            )

        return SearchResponse(mode="keyword", results=[])
