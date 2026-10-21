"""计划校验：深度 / 未知单元 / 预算三拒，未实现与非法的区分，降级形状。"""
from shadowclaw.execution.plan import (Plan, Step, degrade, validate_plan)

UNITS = {"a", "b"}
CAP = 5.0


def kinds(problems):
    return {p.kind for p in problems}


def test_valid_plan_passes():
    plan = Plan(id="p", steps=[Step(id="s1", unit="a")], budget_limit=1.0)
    assert validate_plan(plan, UNITS, budget_hard_cap=CAP) == []


def test_depth_exceeded_rejected():
    inner = Plan(id="l3", steps=[Step(id="x", unit="a")])
    mid = Plan(id="l2", steps=[Step(id="d2", primitive="delegate",
                                    subplan=inner)])
    outer = Plan(id="l1", steps=[Step(id="d1", primitive="delegate",
                                      subplan=mid)])
    top = Plan(id="l0", steps=[Step(id="d0", primitive="delegate",
                                    subplan=outer)])
    problems = validate_plan(top, UNITS, budget_hard_cap=CAP)
    assert "depth_exceeded" in kinds(problems)


def test_depth_two_is_allowed():
    inner = Plan(id="l2", steps=[Step(id="x", unit="a")])
    mid = Plan(id="l1", steps=[Step(id="d1", primitive="delegate",
                                    subplan=inner)])
    top = Plan(id="l0", steps=[Step(id="d0", primitive="delegate",
                                    subplan=mid)])
    assert validate_plan(top, UNITS, budget_hard_cap=CAP) == []


def test_unknown_unit_rejected():
    plan = Plan(id="p", steps=[Step(id="s1", unit="ghost")])
    problems = validate_plan(plan, UNITS, budget_hard_cap=CAP)
    assert "unknown_unit" in kinds(problems)


def test_budget_over_hard_cap_rejected():
    plan = Plan(id="p", steps=[Step(id="s1", unit="a")], budget_limit=99.0)
    problems = validate_plan(plan, UNITS, budget_hard_cap=CAP)
    assert "budget_exceeded" in kinds(problems)


def test_step_budgets_over_plan_limit_rejected():
    plan = Plan(id="p", budget_limit=1.0, steps=[
        Step(id="s1", unit="a", budget=0.7),
        Step(id="s2", unit="b", budget=0.7)])
    problems = validate_plan(plan, UNITS, budget_hard_cap=CAP)
    assert "budget_exceeded" in kinds(problems)


def test_reserved_primitive_is_not_implemented_not_illegal():
    plan = Plan(id="p", steps=[Step(id="s1", primitive="route", unit="a")])
    problems = validate_plan(plan, UNITS, budget_hard_cap=CAP)
    assert kinds(problems) == {"not_implemented"}


def test_bogus_primitive_is_illegal():
    plan = Plan(id="p", steps=[Step(id="s1", primitive="teleport",
                                    unit="a")])
    problems = validate_plan(plan, UNITS, budget_hard_cap=CAP)
    assert kinds(problems) == {"illegal"}


def test_degrade_shape():
    plan = Plan(id="p", steps=[Step(id="s1", unit="ghost")],
                budget_limit=2.0)
    degraded = degrade(plan, "a", "原始任务")
    assert len(degraded.steps) == 1
    assert degraded.steps[0].primitive == "sequence"
    assert degraded.steps[0].unit == "a"
    assert degraded.steps[0].input == "原始任务"
    assert validate_plan(degraded, UNITS, budget_hard_cap=CAP) == []
