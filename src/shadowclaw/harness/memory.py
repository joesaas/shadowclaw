"""记忆检索 v1：关键词 + 条目索引（设计待确认已定：只做关键词，向量后置）。

长期记忆按类分文件，每条是 {陈述，来源，日期，状态}（推断另带置信度与
依据）。本模块把五个可检索文件解析成条目索引，查询时按关键词命中数排序。
core.md 是常驻层，永远全量注入，**不进检索**（assembler 单独处理）。

索引在加载时现建（文件就是正本，索引是派生物），不另落索引文件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 可检索的记忆文件（顺序即同分时的稳定排序依据）；core.md 不在此列。
SEARCHABLE_FILES = ("facts", "people", "projects", "decisions", "inferences")

_FIELD_KEYS = ("陈述", "来源", "日期", "状态", "置信度", "依据")


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    file: str                    # 来源文件 stem（facts / projects / ...）
    statement: str
    source: str = ""
    date: str = ""
    status: str = "active"
    confidence: str | None = None      # 仅推断条目
    basis: str | None = None           # 仅推断条目（依据）

    def render(self) -> str:
        """注入上下文时的一行形态：推断条目永远带置信度标注，不冒充事实。"""
        if self.confidence:
            return f"[{self.id}·推断·置信度{self.confidence}] {self.statement}"
        return f"[{self.id}] {self.statement}"


_ENTRY_HEAD = re.compile(r"^###\s+(\S+)")
_FIELD_LINE = re.compile(r"^-\s*([^：:]+)[：:]\s*(.*)$")


def parse_entries(text: str, *, file: str) -> list[MemoryEntry]:
    """解析一个记忆文件：### <id> 起一条，- 字段：值 逐行填。"""
    entries: list[MemoryEntry] = []
    current: dict | None = None
    for line in text.splitlines():
        head = _ENTRY_HEAD.match(line)
        if head:
            if current is not None:
                entries.append(_to_entry(current, file))
            current = {"id": head.group(1)}
            continue
        if current is None:
            continue
        field = _FIELD_LINE.match(line.strip())
        if field and field.group(1).strip() in _FIELD_KEYS:
            current[field.group(1).strip()] = field.group(2).strip()
    if current is not None:
        entries.append(_to_entry(current, file))
    return entries


def _to_entry(raw: dict, file: str) -> MemoryEntry:
    return MemoryEntry(
        id=raw["id"], file=file,
        statement=raw.get("陈述", ""), source=raw.get("来源", ""),
        date=raw.get("日期", ""), status=raw.get("状态", "active"),
        confidence=raw.get("置信度"), basis=raw.get("依据"))


_LATIN = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_CJK_RUN = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> set[str]:
    """粗粒度分词：英文/数字词元 + 中文 bigram（单字查询保留单字）。"""
    lowered = text.lower()
    tokens = {t for t in _LATIN.findall(lowered) if len(t) >= 2}
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class MemoryIndex:
    """条目索引：加载时从记忆文件现建，查询按命中词元数排序。"""

    def __init__(self, memory_dir: Path | str):
        self._dir = Path(memory_dir)
        self._entries: list[MemoryEntry] = []
        self._tokens: list[set[str]] = []
        for name in SEARCHABLE_FILES:
            path = self._dir / f"{name}.md"
            if not path.exists():
                continue
            for entry in parse_entries(path.read_text("utf-8"), file=name):
                self._entries.append(entry)
                self._tokens.append(tokenize(entry.statement))

    def entries(self) -> list[MemoryEntry]:
        return list(self._entries)

    def search(self, query: str, *, limit: int = 10) -> list[MemoryEntry]:
        """关键词检索：命中词元越多越靠前；零命中不返回（不硬凑）。"""
        wanted = tokenize(query)
        if not wanted:
            return []
        scored: list[tuple[int, int, MemoryEntry]] = []
        for idx, (entry, tokens) in enumerate(
                zip(self._entries, self._tokens)):
            score = len(wanted & tokens)
            if score > 0:
                scored.append((-score, idx, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in scored[:limit]]
