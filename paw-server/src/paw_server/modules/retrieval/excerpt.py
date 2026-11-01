"""Query-aware excerpts: show the part of a passage that matched, not just its beginning."""

import re

_STOP = {"the", "and", "for", "with", "of", "to", "in", "is", "on", "an", "a", "at", "by", "or"}


def query_terms(q: str) -> list[str]:
    words = re.findall(r"[^\W_]+", q.lower())
    seen: dict[str, None] = {}
    for w in words:
        if len(w) > 1 and w not in _STOP:
            seen.setdefault(w, None)
    return list(seen)


def _stem(t: str) -> str:
    """Crude prefix stem so 'specification' also finds 'specifications' / 'specific'."""
    return t[: max(4, len(t) - 3)] if len(t) > 6 else t


def make_excerpt(text: str, terms: list[str], size: int) -> tuple[str, int]:
    """Return (excerpt, start_offset) of ~`size` chars centred on the first matching term."""
    if len(text) <= size:
        return text, 0
    idx = -1
    for t in terms:
        m = re.search(re.escape(_stem(t)), text, re.IGNORECASE)
        if m and (idx == -1 or m.start() < idx):
            idx = m.start()
    if idx == -1:
        return text[:size], 0
    start = max(0, idx - size // 4)
    if start > 0:  # begin on a word boundary
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < idx else start
    return text[start : start + size], start
