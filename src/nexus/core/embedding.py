"""Embedding provider — OpenAI 兼容的 embedding API 调用。

支持 /embeddings 端点，适用于任何 OpenAI 兼容 API。
"""

from __future__ import annotations

import logging
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """OpenAI 兼容的 embedding 提供者。

    使用 /embeddings API 生成文本向量。
    支持与 config.yaml 中相同 base_url 的服务。

    用法：
        provider = EmbeddingProvider(
            base_url="https://api.openai.com/v1",
            api_key="sk-...",
            model="text-embedding-3-small",
        )
        vector = provider.embed_text("你好世界")
        vectors = provider.embed_batch(["文本1", "文本2"])
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "text-embedding-3-small",
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        """懒初始化 OpenAI 客户端。"""
        if self._client is None:
            self._client = OpenAI(
                api_key=self.api_key or "dummy",
                base_url=self.base_url or None,
            )
        return self._client

    def embed_text(self, text: str) -> list[float]:
        """对单条文本生成 embedding 向量。

        Args:
            text: 要嵌入的文本。

        Returns:
            浮点数列表，表示文本的向量表示。
        """
        response = self.client.embeddings.create(
            model=self.model,
            input=[text],
        )
        return response.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成 embedding 向量。

        自动分批处理（每批最多 100 条），避免 API 限制。

        Args:
            texts: 要嵌入的文本列表。

        Returns:
            与输入顺序对应的向量列表。
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        batch_size = 100

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = self.client.embeddings.create(
                model=self.model,
                input=batch,
            )
            # 按 index 排序确保顺序正确
            sorted_data = sorted(response.data, key=lambda x: x.index)
            for item in sorted_data:
                all_embeddings.append(item.embedding)

        return all_embeddings
