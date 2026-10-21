"""事实板 FactBoard：任务级共享状态，单写者。

语义（已拍板）：单元只能提议（propose），只有编排器能落板（commit）。
本类因此只长在编排器手里——单元拿到的永远是已提交事实的只读快照文本，
碰不到板子本身（迪米特）。落板这一关做去重；冲突检查留待阶段 2
（驾驭层到位后按场景规则），此处先只记来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fact:
    id: int
    statement: str
    source_unit: str
    step_id: str
    status: str = "proposed"           # proposed / committed

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
        """落板并去重：同陈述已提交过则不再重复入板，返回 False。"""
        if any(f.status == "committed" and f.statement == fact.statement
               for f in self._facts):
            fact.status = "proposed"   # 保持未落板，轨迹里留痕即可
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
