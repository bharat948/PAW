"""Deterministic chunking: same text + same config always yields the same spans."""

from typing import Any

from paw_server.modules.ingestion.models import Chunk, Segment

_BREAKS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


def split_spans(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Split into [start, end) windows of <= size chars, preferring paragraph/sentence breaks."""
    n = len(text)
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < n:
        end = min(cursor + size, n)
        if end < n:
            floor = cursor + size // 2
            for brk in _BREAKS:
                i = text.rfind(brk, floor, end)
                if i != -1:
                    end = i + len(brk)
                    break
        # trim whitespace but keep offsets exact
        s, e = cursor, end
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            spans.append((s, e))
        if end >= n:
            break
        nxt = max(end - overlap, cursor + 1)
        # start the overlap at a word boundary so chunks never begin mid-word
        while nxt < end and not text[nxt - 1].isspace():
            nxt += 1
        cursor = nxt if nxt > cursor else end
    return spans


def chunk_segments(segments: list[Segment], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []

    def add(
        seg: Segment, text: str, start: int, end: int, extra: dict[str, Any] | None = None
    ) -> None:
        md = dict(seg.metadata)
        if extra:
            md.update(extra)
        chunks.append(Chunk(len(chunks), text, seg.page_number, seg.section, start, end, md))

    for seg in segments:
        if seg.atomic:
            md = seg.metadata
            # tabular: offsets are record numbers, not characters
            add(seg, seg.text, md.get("row_start", 0), md.get("row_end", 0))
            continue
        for s, e in split_spans(seg.text, size, overlap):
            add(seg, seg.text[s:e], seg.offset + s, seg.offset + e, {"source_unit": "chars"})
    return chunks
