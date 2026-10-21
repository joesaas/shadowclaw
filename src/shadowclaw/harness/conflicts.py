"""事实落板冲突检查（最小版）：接阶段 1 facts.py 预留的 commit_guard 挂钩。

阶段 1 的落板只做同陈述去重；本模块补上「冲突」这一关，语义：

- 事实按 `键：值` 形态书写时，冒号/等号前是键（如「CI 状态：失败」）。
  同键、不同值的新事实与已落板事实**冲突**——不许静默覆盖，落板被拒，
  事实标 conflict，冲突记录留在 guard.conflicts 里，交编排器/人工裁决。
- 无键的自由文本事实无法做键值冲突判定（本版已知边界，如实标注）：
  照常落板，只靠去重兜底。结构化事实的键协议随手册细化再扩展。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..execution.facts import Fact, FactBoard

_KEY_VALUE = re.compile(r"^\s*([^：:=]{2,40})[：:=]\s*(.+?)\s*$")


def fact_key(statement: str) -> str | None:
    """事实的键：`键：值` / `键=值` 形态才有键，否则 None。"""
    match = _KEY_VALUE.match(statement.strip())
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1).strip().lower())


def fact_value(statement: str) -> str | None:
    match = _KEY_VALUE.match(statement.strip())
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(2).strip().lower())


@dataclass
class ConflictRecord:
    key: str
    existing_statement: str
    incoming_statement: str
    existing_source: str = ""
    incoming_source: str = ""


@dataclass
class KeyValueConflictGuard:
    """FactBoard 的 commit_guard 实现：同键异值 → 拒落板并记录冲突。"""

    conflicts: list[ConflictRecord] = field(default_factory=list)

    def __call__(self, fact: Fact, committed: list[Fact]) -> bool:
        """返回 True 放行落板；False = 冲突，板子会把事实标 conflict。"""
        key = fact_key(fact.statement)
        if key is None:
            return True
        for old in committed:
            if (fact_key(old.statement) == key
                    and fact_value(old.statement)
                    != fact_value(fact.statement)):
                self.conflicts.append(ConflictRecord(
                    key=key,
                    existing_statement=old.statement,
                    incoming_statement=fact.statement,
                    existing_source=old.source_unit,
                    incoming_source=fact.source_unit))
                return False
        return True


def find_conflicts(board: FactBoard, statement: str) -> list[ConflictRecord]:
    """只读探针：一条候选事实若此刻落板，会与板上哪些事实冲突。"""
    guard = KeyValueConflictGuard()
    probe = Fact(id=-1, statement=statement, source_unit="(probe)",
                 step_id="(probe)")
    guard(probe, board.committed())
    return guard.conflicts
