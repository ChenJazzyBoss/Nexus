"""LanceDB vector storage for document chunks.

Adapted from llm_wiki/src-tauri/src/commands/vectorstore.rs.
Provides chunk-level vector storage with upsert/delete/search semantics.
Uses the Python lancedb SDK.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.chunker import chunk_markdown
    from nexus.core.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)

# LanceDB Python SDK is optional
try:
    import lancedb
    import pyarrow as pa
    HAS_LANCEDB = True
except ImportError:
    HAS_LANCEDB = False


@dataclass
class ChunkResult:
    """Result from a vector search."""
    chunk_id: str
    page_id: str
    chunk_index: int
    chunk_text: str
    heading_path: str
    score: float


@dataclass
class ChunkInput:
    """Input for upserting a chunk."""
    page_id: str
    chunk_index: int
    chunk_text: str
    heading_path: str
    embedding: list[float]

    @property
    def chunk_id(self) -> str:
        return f"{self.page_id}#{self.chunk_index}"


class VectorStore:
    """LanceDB-backed vector store for document chunks."""

    TABLE_NAME = "nexus_chunks"

    def __init__(self, db_path: str | Path = "data/lancedb"):
        if not HAS_LANCEDB:
            raise ImportError(
                "lancedb is required for VectorStore. Install with: pip install lancedb"
            )
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._table = None
        self._dim: int | None = None

    @property
    def db(self):
        if self._db is None:
            self._db = lancedb.connect(str(self.db_path))
        return self._db

    def _get_table(self, dim: int):
        """Get or create the table with the given dimension."""
        if self._table is not None and self._dim == dim:
            return self._table

        try:
            self._table = self.db.open_table(self.TABLE_NAME)
            self._dim = dim
        except Exception:
            # Table doesn't exist, create it
            schema = pa.schema([
                pa.field("chunk_id", pa.string()),
                pa.field("page_id", pa.string()),
                pa.field("chunk_index", pa.int32()),
                pa.field("chunk_text", pa.string()),
                pa.field("heading_path", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), list_size=dim)),
            ])
            self._table = self.db.create_table(self.TABLE_NAME, schema=schema)
            self._dim = dim

        return self._table

    def upsert_chunks(self, chunks: list[ChunkInput]) -> int:
        """Upsert chunks into the vector store."""
        if not chunks:
            return 0

        dim = len(chunks[0].embedding)
        table = self._get_table(dim)

        records = []
        for chunk in chunks:
            records.append({
                "chunk_id": chunk.chunk_id,
                "page_id": chunk.page_id,
                "chunk_index": chunk.chunk_index,
                "chunk_text": chunk.chunk_text,
                "heading_path": chunk.heading_path,
                "vector": chunk.embedding,
            })

        # Delete existing chunks for these pages first
        page_ids = list(set(c.page_id for c in chunks))
        for page_id in page_ids:
            try:
                table.delete(f"page_id = '{page_id}'")
            except Exception:
                pass

        table.add(records)
        return len(records)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        page_id: str | None = None,
    ) -> list[ChunkResult]:
        """Search for similar chunks by vector."""
        dim = len(query_embedding)
        table = self._get_table(dim)

        query = table.search(query_embedding).limit(top_k)
        if page_id:
            query = query.where(f"page_id = '{page_id}'")

        results = query.to_list()
        chunks = []
        for row in results:
            chunks.append(ChunkResult(
                chunk_id=row["chunk_id"],
                page_id=row["page_id"],
                chunk_index=row["chunk_index"],
                chunk_text=row["chunk_text"],
                heading_path=row["heading_path"],
                score=1.0 / (1.0 + row.get("_distance", 0)),
            ))

        return chunks

    def delete_page(self, page_id: str) -> None:
        """Delete all chunks for a page."""
        try:
            table = self.db.open_table(self.TABLE_NAME)
            table.delete(f"page_id = '{page_id}'")
        except Exception:
            pass

    def count(self) -> int:
        """Return total chunk count."""
        try:
            table = self.db.open_table(self.TABLE_NAME)
            return table.count_rows()
        except Exception:
            return 0

    def drop(self) -> None:
        """Drop the table."""
        try:
            self.db.drop_table(self.TABLE_NAME)
            self._table = None
            self._dim = None
        except Exception:
            pass

    def ingest_document(
        self,
        page_id: str,
        text: str,
        embedding_provider: "EmbeddingProvider",
        max_chunk_size: int = 1000,
        overlap: int = 100,
    ) -> int:
        """将文档分块、生成 embedding 并存入向量库。

        使用 Markdown 递归分块器将文本切分，再通过 EmbeddingProvider
        批量生成向量，最后 upsert 到 LanceDB。

        Args:
            page_id: 文档唯一标识。
            text: 文档的 Markdown 文本。
            embedding_provider: embedding 提供者实例。
            max_chunk_size: 每个 chunk 的最大字符数。
            overlap: 硬切时的重叠字符数。

        Returns:
            成功写入的 chunk 数量。
        """
        from nexus.core.chunker import chunk_markdown

        # 分块
        raw_chunks = chunk_markdown(text, max_chunk_size=max_chunk_size, overlap=overlap)
        if not raw_chunks:
            return 0

        # 批量生成 embedding
        texts = [c["text"] for c in raw_chunks]
        embeddings = embedding_provider.embed_batch(texts)

        # 构建 ChunkInput 列表
        chunk_inputs = []
        for i, (raw, emb) in enumerate(zip(raw_chunks, embeddings)):
            chunk_inputs.append(ChunkInput(
                page_id=page_id,
                chunk_index=i,
                chunk_text=raw["text"],
                heading_path=raw["heading_path"],
                embedding=emb,
            ))

        return self.upsert_chunks(chunk_inputs)
