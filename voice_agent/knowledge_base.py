"""A tiny, dependency-free knowledge base over a folder of Markdown files.

Each file is split into sections at its headings, and sections are ranked with
BM25 keyword scoring. That is plenty for a few dozen pages; for thousands of
documents, swap `search()` for a vector store and keep the same interface.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its me my "
    "of on or our so that the their this to us was we what when where which who why "
    "will with you your".split()
)


def _stem(word: str) -> str:
    """Very light stemming so "refund", "refunds" and "refunded" match."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3 and not word.endswith("ss"):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


@dataclass(frozen=True)
class Section:
    source: str  # file name
    title: str  # heading the text sits under
    text: str


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float


class KnowledgeBase:
    def __init__(self, sections: list[Section]) -> None:
        self.sections = sections
        self._docs = [Counter(_tokens(f"{s.title} {s.title} {s.text}")) for s in sections]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avg_len = sum(self._lengths) / max(len(self._lengths), 1)
        df: Counter[str] = Counter()
        for doc in self._docs:
            df.update(doc.keys())
        n = len(sections)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def load(cls, folder: Path) -> KnowledgeBase:
        sections: list[Section] = []
        for path in sorted(folder.glob("*.md")):
            sections.extend(_split_sections(path))
        return cls(sections)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        terms = _tokens(query)
        k1, b = 1.5, 0.75
        hits = []
        for section, doc, length in zip(self.sections, self._docs, self._lengths):
            score = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    norm = tf + k1 * (1 - b + b * length / self._avg_len)
                    score += self._idf[t] * tf * (k1 + 1) / norm
            if score > 0:
                hits.append(Hit(section, score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


def _split_sections(path: Path) -> list[Section]:
    sections: list[Section] = []
    doc_title, title, lines = path.stem, path.stem, []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            sections.append(Section(path.name, title, body))

    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading:
            flush()
            lines = []
            if heading.group(1) == "#":
                doc_title = title = heading.group(2).strip()
            else:
                title = f"{doc_title} / {heading.group(2).strip()}"
        else:
            lines.append(line)
    flush()
    return sections
