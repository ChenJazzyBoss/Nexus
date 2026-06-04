"""Markdown 递归分块器 — 按 heading/段落/句子/空白/硬切 逐级分割。

用于将长文档切分为适合向量化的 chunk。
每个 chunk 保留 heading 路径上下文，便于检索时还原语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    """一个分块结果。"""
    text: str
    heading_path: str
    start_pos: int
    end_pos: int


# 分割优先级：heading → 段落(双换行) → 句子 → 单换行 → 空白 → 硬切
_HEADING_RE = re.compile(r"^(#{1,6})\s+.+$", re.MULTILINE)
_SENTENCE_RE = re.compile(r"[.!?。！？]\s+")


def _find_headings(text: str) -> list[tuple[int, int, int, str]]:
    """在文本中查找所有 heading，返回 (start, end, level, heading_text)。"""
    headings = []
    for m in _HEADING_RE.finditer(text):
        level = len(m.group(1))
        heading_text = m.group(0).strip()
        headings.append((m.start(), m.end(), level, heading_text))
    return headings


def _build_heading_path(headings: list[tuple[int, int, int, str]], pos: int) -> str:
    """根据当前位置，构建 heading 层级路径。"""
    path: list[str] = []
    for start, end, level, heading_text in headings:
        if start <= pos:
            # 移除同级或更低级别的 heading
            while len(path) >= level:
                path.pop()
            path.append(heading_text.lstrip("#").strip())
        else:
            break
    return " > ".join(path)


def _split_recursive(
    text: str,
    max_chunk_size: int,
    overlap: int,
    headings: list[tuple[int, int, int, str]],
    base_pos: int = 0,
) -> list[Chunk]:
    """递归分割文本。

    分割优先级：heading → 段落 → 句子 → 换行 → 空白 → 硬切。
    """
    if not text.strip():
        return []

    # 如果文本足够小，直接返回
    if len(text) <= max_chunk_size:
        heading_path = _build_heading_path(headings, base_pos)
        return [Chunk(
            text=text.strip(),
            heading_path=heading_path,
            start_pos=base_pos,
            end_pos=base_pos + len(text),
        )]

    # 尝试按不同分隔符分割
    splits = _try_split(text, max_chunk_size, headings, base_pos)
    if splits:
        return splits

    # 所有分割策略失败，硬切
    return _hard_split(text, max_chunk_size, overlap, headings, base_pos)


def _try_split(
    text: str,
    max_chunk_size: int,
    headings: list[tuple[int, int, int, str]],
    base_pos: int,
) -> list[Chunk] | None:
    """尝试各种分割策略，返回结果或 None。"""

    # 策略 1：按 heading 分割
    text_headings = _find_headings(text)
    if text_headings:
        chunks = _split_by_boundaries(
            text, [h[0] for h in text_headings], max_chunk_size, headings, base_pos
        )
        if chunks:
            return chunks

    # 策略 2：按双换行（段落）分割
    para_splits = [m.end() for m in re.finditer(r"\n\s*\n", text)]
    if para_splits:
        chunks = _split_by_boundaries(
            text, para_splits, max_chunk_size, headings, base_pos
        )
        if chunks:
            return chunks

    # 策略 3：按句子分割
    sentence_splits = [m.end() for m in _SENTENCE_RE.finditer(text)]
    if sentence_splits:
        chunks = _split_by_boundaries(
            text, sentence_splits, max_chunk_size, headings, base_pos
        )
        if chunks:
            return chunks

    # 策略 4：按单换行分割
    line_splits = [m.end() for m in re.finditer(r"\n", text)]
    if line_splits:
        chunks = _split_by_boundaries(
            text, line_splits, max_chunk_size, headings, base_pos
        )
        if chunks:
            return chunks

    # 策略 5：按空白分割
    space_splits = [m.end() for m in re.finditer(r"\s+", text)]
    if space_splits:
        chunks = _split_by_boundaries(
            text, space_splits, max_chunk_size, headings, base_pos
        )
        if chunks:
            return chunks

    return None


def _split_by_boundaries(
    text: str,
    boundaries: list[int],
    max_chunk_size: int,
    headings: list[tuple[int, int, int, str]],
    base_pos: int,
) -> list[Chunk] | None:
    """根据分割点将文本切分为不超过 max_chunk_size 的段落。

    使用贪心策略：尽量把文本合并到接近 max_chunk_size。
    如果某个片段仍然超过限制则递归处理。
    """
    pieces: list[tuple[str, int]] = []
    prev = 0
    for bp in boundaries:
        piece = text[prev:bp]
        if piece.strip():
            pieces.append((piece, base_pos + prev))
        prev = bp
    # 最后一段
    if prev < len(text):
        piece = text[prev:]
        if piece.strip():
            pieces.append((piece, base_pos + prev))

    if not pieces:
        return None

    # 检查是否所有片段都足够小（可以合并）
    all_small = all(len(p[0]) <= max_chunk_size for p in pieces)
    if not all_small:
        # 有片段太大，需要递归处理
        chunks: list[Chunk] = []
        for piece_text, piece_pos in pieces:
            if len(piece_text) > max_chunk_size:
                sub = _split_recursive(piece_text, max_chunk_size, 0, headings, piece_pos)
                chunks.extend(sub)
            else:
                heading_path = _build_heading_path(headings, piece_pos)
                chunks.append(Chunk(
                    text=piece_text.strip(),
                    heading_path=heading_path,
                    start_pos=piece_pos,
                    end_pos=piece_pos + len(piece_text),
                ))
        return chunks

    # 贪心合并小片段
    return _greedy_merge(pieces, max_chunk_size, headings)


def _greedy_merge(
    pieces: list[tuple[str, int]],
    max_chunk_size: int,
    headings: list[tuple[int, int, int, str]],
) -> list[Chunk]:
    """将小片段贪心合并为不超过 max_chunk_size 的 chunk。"""
    chunks: list[Chunk] = []
    current_text = ""
    current_start = pieces[0][1]

    for piece_text, piece_pos in pieces:
        candidate = current_text + piece_text if current_text else piece_text
        if len(candidate) <= max_chunk_size:
            current_text = candidate
        else:
            # 保存当前 chunk
            if current_text.strip():
                heading_path = _build_heading_path(headings, current_start)
                chunks.append(Chunk(
                    text=current_text.strip(),
                    heading_path=heading_path,
                    start_pos=current_start,
                    end_pos=current_start + len(current_text),
                ))
            current_text = piece_text
            current_start = piece_pos

    # 最后一个 chunk
    if current_text.strip():
        heading_path = _build_heading_path(headings, current_start)
        chunks.append(Chunk(
            text=current_text.strip(),
            heading_path=heading_path,
            start_pos=current_start,
            end_pos=current_start + len(current_text),
        ))

    return chunks


def _hard_split(
    text: str,
    max_chunk_size: int,
    overlap: int,
    headings: list[tuple[int, int, int, str]],
    base_pos: int,
) -> list[Chunk]:
    """硬切 — 固定长度切分，带 overlap。"""
    chunks: list[Chunk] = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chunk_size, len(text))
        chunk_text = text[pos:end]
        if chunk_text.strip():
            heading_path = _build_heading_path(headings, base_pos + pos)
            chunks.append(Chunk(
                text=chunk_text.strip(),
                heading_path=heading_path,
                start_pos=base_pos + pos,
                end_pos=base_pos + end,
            ))
        pos += max_chunk_size - overlap
    return chunks


def chunk_markdown(
    text: str,
    max_chunk_size: int = 1000,
    overlap: int = 100,
) -> list[dict]:
    """将 Markdown 文本递归分块。

    按 heading → 段落 → 句子 → 空白 → 硬切 逐级分割。

    Args:
        text: Markdown 文本。
        max_chunk_size: 每个 chunk 的最大字符数（默认 1000）。
        overlap: 硬切时的重叠字符数（默认 100）。

    Returns:
        字典列表，每个包含 text, heading_path, start_pos, end_pos。
    """
    if not text or not text.strip():
        return []

    headings = _find_headings(text)
    chunks = _split_recursive(text, max_chunk_size, overlap, headings)

    return [
        {
            "text": c.text,
            "heading_path": c.heading_path,
            "start_pos": c.start_pos,
            "end_pos": c.end_pos,
        }
        for c in chunks
    ]
