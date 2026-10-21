"""事实板 FactBoard：任务级共享状态，单写者。

语义（已拍板）：单元只能提议（propose），只有编排器能落板（commit）。
本类因此只长在编排器手里——单元拿到的永远是已提交事实的只读快照文本，
碰不到板子本身（迪米特）。落板这一关做去重；冲突检查经 commit_guard
挂钩接入（阶段 2 起由驾驭层 harness/conflicts.py 提供：同键异值拒落板、
标 conflict、留给编排器/人工裁决，绝不静默覆盖）。未注入 guard 时行为
与阶段 1 完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Fact:
    id: int
    statement: str
    source_unit: str
    step_id: str
    status: str = "proposed"           # proposed / committed / conflict

    def to_dict(self) -> dict:
        return {"id": self.id, "statement": self.statement,
                "source_unit": self.source_unit, "step_id": self.step_id,
                "status": self.status}

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(id=d["id"], statement=d["statement"],
                   source_unit=d["source_unit"], step_id=d["step_id"],
                   status=d.get("status", "committed"))


@dataclass
class FactBoard:
    _facts: list[Fact] = field(default_factory=list)
    _next_id: int = 1
    # 冲突检查挂钩（阶段 2 注入点）：guard(候选事实, 已落板事实) -> 是否放行。
    commit_guard: Callable[[Fact, list[Fact]], bool] | None = field(
        default=None, repr=False, compare=False)

    # -- 提议（编排器代单元登记，单元本身不接触本对象） --------------------
    def propose(self, statement: str, *, source_unit: str,
                step_id: str) -> Fact:
        fact = Fact(id=self._next_id, statement=statement,
                    source_unit=source_unit, step_id=step_id)
        self._next_id += 1
        self._facts.append(fact)
        return fact

    # -- 落板（仅编排器调用） ------------------------------------------------
    def commit(self, fact: Fact) -> bool:
        """落板：先去重，再过冲突检查挂钩（若注入）。

        同陈述重复 → 返回 False（保持未落板，轨迹留痕即可）。
        冲突（同键异值）→ 事实标 conflict、返回 False，绝不静默覆盖，
        冲突详情由 guard 自己记录，交编排器/人工裁决。"""
        if any(f.status == "committed" and f.statement == fact.statement
               for f in self._facts):
            fact.status = "proposed"   # 保持未落板，轨迹里留痕即可
            return False
        if (self.commit_guard is not None
                and not self.commit_guard(fact, self.committed())):
            fact.status = "conflict"
            return False
        fact.status = "committed"
        return True

    # -- 读 ------------------------------------------------------------------
    def committed(self) -> list[Fact]:
        return [f for f in self._facts if f.status == "committed"]

    def snapshot(self) -> list[str]:
        """已提交事实的陈述文本——上下文组装与兄弟单元能看到的全部。"""
        return [f.statement for f in self.committed()]

    # -- 检查点恢复 ------------------------------------------------------------
    def restore(self, facts: list[dict]) -> None:
        self._facts = [Fact.from_dict(d) for d in facts]
        self._next_id = max((f.id for f in self._facts), default=0) + 1

    def export_committed(self) -> list[dict]:
        return [f.to_dict() for f in self.committed()]
