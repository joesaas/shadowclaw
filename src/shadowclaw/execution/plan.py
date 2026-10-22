"""计划 Plan：数据，不是代码。全文 JSON 可序列化，进轨迹可审计。

校验器管四件事：原语合法性、引用单元存在、委派深度、预算上限。
五原语阶段 5 已全部实现（sequence / delegate / route / parallel /
reflect）；各原语特有字段的形状校验与执行语义保持「三拒口径」一致：
结构不对报 illegal、引用不存在报 unknown_unit，不带病跑到执行中途。
not_implemented 种类保留给将来预留的新原语（当前无成员）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PRIMITIVES = ("sequence", "delegate", "route", "parallel", "reflect")
IMPLEMENTED_PRIMITIVES = PRIMITIVES

# route 规则的取值来源：prev_output = 最近一步的产出文本；
# input = 本步输入（缺省时为任务文本）；facts = 事实板已提交事实的拼接。
ROUTE_SOURCES = ("prev_output", "input", "facts")

# reflect 规则检查的条目形态（确定性规则，不调模型）：
#   contains:<子串> / not_contains:<子串> / min_length:<n> / max_length:<n>
#   / fact_proposed（本轮产出至少提议一条事实）
REFLECT_RULE_PREFIXES = ("contains:", "not_contains:", "min_length:",
                         "max_length:", "fact_proposed")

PRIMITIVES_GUIDE = """\
五种编排原语（Plan 步骤的 primitive 字段，五选一）：
- sequence：执行 unit 字段指定的单个单元，前步产出经事实板与上下文传递。
- delegate：把 subplan 子计划交给隔离的子执行（独立事实板，只回落板事实），
  委派深度至多 2 层。适合可独立完成的子任务。
- route：按规则从多个候选单元中选一执行。route 字段：
  {"rules": [{"when": {"source": "prev_output|input|facts",
  "contains": "子串"}, "unit": "候选单元"}], "default": "兜底单元（可缺）"}。
  规则自上而下首中即选；全不中走 default，无 default 则本步失败。
- parallel：branches 字段给一组 sequence 子步，并发执行、各自隔离
  上下文，产出经事实板单写者合并；单个分支失败不拖垮整组（全失败才
  算本步失败），合并冲突按事实板冲突口径拒落板并留痕。
- reflect：执行 unit 后按 checklist 规则检查产出（条目形态：
  contains:<子串> / not_contains:<子串> / min_length:<n> /
  max_length:<n> / fact_proposed），不过则带反馈重跑原单元，最多
  max_rounds 轮（默认 2），超限本步失败。可选 critic 字段指定一个
  评审单元做模型自评：其回复含「通过」判过、含「打回」判不过。
"""


@dataclass
class Step:
    id: str
    primitive: str = "sequence"
    unit: str | None = None                    # sequence / reflect 步：单元 id
    input: str = ""                            # 本步输入（任务文本 / 简报）
    subplan: "Plan | None" = None              # delegate 步：子计划
    requires_approval: bool = False            # 执行前过审批挂钩
    budget: float | None = None                # 本步预算档位（可选）
    # ---- route 步（阶段 5）
    route: dict[str, Any] | None = None        # {"rules": [...], "default": id|None}
    # ---- parallel 步（阶段 5）
    branches: list["Step"] = field(default_factory=list)  # 仅 sequence 子步
    # ---- reflect 步（阶段 5）
    checklist: list[str] = field(default_factory=list)      # 规则检查条目
    max_rounds: int = 2                                     # 打回重跑上限
    critic: str | None = None                  # 可选：模型自评单元 id（占位接口）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "primitive": self.primitive, "unit": self.unit,
            "input": self.input,
            "subplan": self.subplan.to_dict() if self.subplan else None,
            "requires_approval": self.requires_approval,
            "budget": self.budget,
            "route": self.route,
            "branches": [b.to_dict() for b in self.branches],
            "checklist": list(self.checklist),
            "max_rounds": self.max_rounds,
            "critic": self.critic,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(
            id=d["id"], primitive=d.get("primitive", "sequence"),
            unit=d.get("unit"), input=d.get("input", ""),
            subplan=Plan.from_dict(d["subplan"]) if d.get("subplan") else None,
            requires_approval=bool(d.get("requires_approval", False)),
            budget=d.get("budget"),
            route=d.get("route"),
            branches=[Step.from_dict(b) for b in d.get("branches") or []],
            checklist=list(d.get("checklist") or []),
            max_rounds=int(d.get("max_rounds", 2)),
            critic=d.get("critic"),
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
            yield from _step_unit_ids(step)


def _step_unit_ids(step: Step) -> Iterable[str]:
    if step.unit:
        yield step.unit
    if step.critic:
        yield step.critic
    if step.route:
        for rule in step.route.get("rules") or []:
            if rule.get("unit"):
                yield rule["unit"]
        if step.route.get("default"):
            yield step.route["default"]
    for branch in step.branches:
        yield from _step_unit_ids(branch)
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


def _need_unit(step: Step, unit_id: str | None, known_units: set[str],
               problems: list[PlanProblem], label: str, role: str) -> None:
    if unit_id is None:
        problems.append(PlanProblem(
            "illegal", f"{label}：{role}缺少 unit"))
    elif unit_id not in known_units:
        problems.append(PlanProblem(
            "unknown_unit", f"{label}：{role}单元 {unit_id!r} 未注册"))


def _validate_route(step: Step, known_units: set[str],
                    problems: list[PlanProblem], label: str) -> None:
    spec = step.route
    if not isinstance(spec, dict) or not spec.get("rules"):
        problems.append(PlanProblem(
            "illegal", f"{label}：route 步缺少 route.rules（至少一条规则）"))
        return
    for i, rule in enumerate(spec["rules"]):
        rlabel = f"{label}/rules[{i}]"
        if not isinstance(rule, dict):
            problems.append(PlanProblem(
                "illegal", f"{rlabel}：规则必须是对象"))
            continue
        when = rule.get("when") or {}
        source = when.get("source")
        if source not in ROUTE_SOURCES:
            problems.append(PlanProblem(
                "illegal",
                f"{rlabel}：when.source 必须是 {'/'.join(ROUTE_SOURCES)}"
                f" 之一，得到 {source!r}"))
        if not str(when.get("contains") or "").strip():
            problems.append(PlanProblem(
                "illegal", f"{rlabel}：when.contains 不能为空"))
        _need_unit(step, rule.get("unit"), known_units, problems,
                   rlabel, "候选")
    default = spec.get("default")
    if default is not None and default not in known_units:
        problems.append(PlanProblem(
            "unknown_unit", f"{label}：route.default 单元 {default!r} 未注册"))


def _validate_reflect(step: Step, known_units: set[str],
                      problems: list[PlanProblem], label: str) -> None:
    _need_unit(step, step.unit, known_units, problems, label, "reflect ")
    if step.max_rounds < 1:
        problems.append(PlanProblem(
            "illegal", f"{label}：reflect 的 max_rounds 必须 ≥ 1"))
    for item in step.checklist:
        if item == "fact_proposed":
            continue
        if not any(item.startswith(p) for p in REFLECT_RULE_PREFIXES
                   if p != "fact_proposed"):
            problems.append(PlanProblem(
                "illegal",
                f"{label}：未知检查条目 {item!r}（合法形态："
                "contains:/not_contains:/min_length:/max_length:/"
                "fact_proposed）"))
            continue
        if item.startswith(("min_length:", "max_length:")):
            raw = item.split(":", 1)[1]
            if not raw.isdigit():
                problems.append(PlanProblem(
                    "illegal", f"{label}：检查条目 {item!r} 的数值不合法"))
    if step.critic is not None and step.critic not in known_units:
        problems.append(PlanProblem(
            "unknown_unit", f"{label}：critic 单元 {step.critic!r} 未注册"))


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
                f"{label}：原语 {step.primitive} 已预留但尚未实现"))
            continue
        if step.primitive == "sequence":
            _need_unit(step, step.unit, known_units, problems, label, "")
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
        if step.primitive == "route":
            _validate_route(step, known_units, problems, label)
        if step.primitive == "parallel":
            if not step.branches:
                problems.append(PlanProblem(
                    "illegal", f"{label}：parallel 步缺少 branches"))
            for branch in step.branches:
                blabel = f"{label}/{branch.id}"
                if branch.primitive != "sequence":
                    problems.append(PlanProblem(
                        "illegal",
                        f"{blabel}：parallel 分支只许 sequence 步，"
                        f"得到 {branch.primitive!r}"))
                    continue
                _need_unit(branch, branch.unit, known_units, problems,
                           blabel, "分支")
        if step.primitive == "reflect":
            _validate_reflect(step, known_units, problems, label)


def degrade(plan: Plan, fallback_unit: str, task: str) -> Plan:
    """保底降级：计划两度校验不过时，降为单单元串行（原任务直交保底单元）。"""
    return Plan(
        id=f"{plan.id}-degraded",
        steps=[Step(id="degraded-1", primitive="sequence",
                    unit=fallback_unit, input=task)],
        budget_limit=plan.budget_limit)
