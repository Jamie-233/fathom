"""Private knowledge base: runbooks searched with BM25.

Retrieval is lexical on purpose. Operations text is full of exact tokens —
``BPDU``, ``CRC``, ``%LINK-3-UPDOWN``, ``err-disable`` — and those tokens are
precisely what an engineer searches for; embeddings tend to blur them into
"something about network errors". BM25 also needs no model, no GPU and no
external service, so the knowledge base works on an air-gapped network.

Documents are split by heading so a hit points at the relevant section, not
a whole file the agent then has to wade through. Tokenisation handles mixed
English/Chinese/Japanese queries by indexing CJK text as character bigrams,
the standard trick when there is no word segmenter.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

_WORD = re.compile(r"[A-Za-z0-9%_/.-]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "for", "be", "by", "with", "as", "at"}


def tokenize(text: str) -> list[str]:
    tokens = [w.lower().strip(".-/") for w in _WORD.findall(text)]
    tokens = [t for t in tokens if t and t not in _STOP]
    for run in _CJK.findall(text):
        tokens += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return tokens


@dataclass
class Section:
    doc: str
    title: str
    heading: str
    text: str


@dataclass
class Hit:
    section: Section
    score: float


class KnowledgeBase:
    def __init__(self, directory: Path = KNOWLEDGE_DIR, k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.sections: list[Section] = []
        for path in sorted(directory.glob("*.md")):
            self._load(path)
        self.docs = [tokenize(f"{s.title} {s.heading} {s.text}") for s in self.sections]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def _load(self, path: Path) -> None:
        title, heading, buf = path.stem, "", []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                if buf:
                    self.sections.append(Section(path.stem, title, heading, "\n".join(buf).strip()))
                heading, buf = line[3:].strip(), []
            else:
                buf.append(line)
        if buf:
            self.sections.append(Section(path.stem, title, heading, "\n".join(buf).strip()))

    def search(self, query: str, limit: int = 3) -> list[Hit]:
        q = tokenize(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            score = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append(Hit(self.sections[i], round(score, 3)))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]
