"""计划 Plan：数据，不是代码。全文 JSON 可序列化，进轨迹可审计。

校验器管四件事：原语合法性、引用单元存在、委派深度、预算上限。
五原语只实现 sequence 与 delegate；route / parallel / reflect 在计划里
「合法但未实现」——校验报「未实现」而非「非法」，阶段 5 接入时只动实现、
不动数据形状（开闭）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PRIMITIVES = ("sequence", "delegate", "route", "parallel", "reflect")
IMPLEMENTED_PRIMITIVES = ("sequence", "delegate")


@dataclass
class Step:
    id: str
    primitive: str = "sequence"
    unit: str | None = None                    # sequence 步：执行的单元 id
    input: str = ""                            # 本步输入（任务文本 / 简报）
    subplan: "Plan | None" = None              # delegate 步：子计划
    requires_approval: bool = False            # 执行前过审批挂钩
    budget: float | None = None                # 本步预算档位（可选）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "primitive": self.primitive, "unit": self.unit,
            "input": self.input,
            "subplan": self.subplan.to_dict() if self.subplan else None,
            "requires_approval": self.requires_approval,
            "budget": self.budget,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(
            id=d["id"], primitive=d.get("primitive", "sequence"),
            unit=d.get("unit"), input=d.get("input", ""),
            subplan=Plan.from_dict(d["subplan"]) if d.get("subplan") else None,
            requires_approval=bool(d.get("requires_approval", False)),
            budget=d.get("budget"),
        )


@dataclass
class Plan:
    id: str
    steps: list[Step] = field(default_factory=list)
    budget_limit: float | None = None          # 本计划花费上限（不得超硬顶）

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id,
                "steps": [s.to_dict() for s in self.steps],
                "budget_limit": self.budget_limit}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        return cls(id=d["id"],
                   steps=[Step.from_dict(s) for s in d.get("steps", [])],
                   budget_limit=d.get("budget_limit"))

    def unit_ids(self) -> Iterable[str]:
        for step in self.steps:
            if step.unit:
                yield step.unit
            if step.subplan:
                yield from step.subplan.unit_ids()


@dataclass
class PlanProblem:
    """一条校验问题。kind 区分：illegal / not_implemented / unknown_unit /
    depth_exceeded / budget_exceeded。"""
    kind: str
    message: str


def validate_plan(plan: Plan, known_units: set[str], *,
                  max_depth: int = 2,
                  budget_hard_cap: float) -> list[PlanProblem]:
    """返回问题清单；空清单 = 校验通过。"""
    problems: list[PlanProblem] = []
    if plan.budget_limit is not None and plan.budget_limit > budget_hard_cap:
        problems.append(PlanProblem(
            "budget_exceeded",
            f"计划预算上限 {plan.budget_limit} 超过硬顶 {budget_hard_cap}"))
    step_budget = sum(s.budget or 0.0 for s in plan.steps)
    if plan.budget_limit is not None and step_budget > plan.budget_limit:
        problems.append(PlanProblem(
            "budget_exceeded",
            f"步骤预算合计 {step_budget} 超过计划上限 {plan.budget_limit}"))
    _validate_steps(plan.steps, known_units, depth=0,
                    max_depth=max_depth, problems=problems, where=plan.id)
    return problems


def _validate_steps(steps: list[Step], known_units: set[str], *, depth: int,
                    max_depth: int, problems: list[PlanProblem],
                    where: str) -> None:
    for step in steps:
        label = f"{where}/{step.id}"
        if step.primitive not in PRIMITIVES:
            problems.append(PlanProblem(
                "illegal", f"{label}：原语 {step.primitive!r} 非法"
                f"（合法集：{'/'.join(PRIMITIVES)}）"))
            continue
        if step.primitive not in IMPLEMENTED_PRIMITIVES:
            problems.append(PlanProblem(
                "not_implemented",
                f"{label}：原语 {step.primitive} 已预留但阶段 1 未实现"))
            continue
        if step.primitive == "sequence":
            if step.unit is None:
                problems.append(PlanProblem(
                    "illegal", f"{label}：sequence 步缺少 unit"))
            elif step.unit not in known_units:
                problems.append(PlanProblem(
                    "unknown_unit", f"{label}：单元 {step.unit!r} 未注册"))
        if step.primitive == "delegate":
            if depth + 1 > max_depth:
                problems.append(PlanProblem(
                    "depth_exceeded",
                    f"{label}：委派深度超过上限 {max_depth}"))
                continue
            if step.subplan is None:
                problems.append(PlanProblem(
                    "illegal", f"{label}：delegate 步缺少 subplan"))
            else:
                _validate_steps(step.subplan.steps, known_units,
                                depth=depth + 1, max_depth=max_depth,
                                problems=problems, where=label)


def degrade(plan: Plan, fallback_unit: str, task: str) -> Plan:
    """保底降级：计划两度校验不过时，降为单单元串行（原任务直交保底单元）。"""
    return Plan(
        id=f"{plan.id}-degraded",
        steps=[Step(id="degraded-1", primitive="sequence",
                    unit=fallback_unit, input=task)],
        budget_limit=plan.budget_limit)
