"""审批 fail-closed：无规则命中 = ASK；无人值守时 ASK 等同 DENY。"""
from shadowclaw.execution.approval import FailClosedPolicy
from shadowclaw.execution.contracts import Decision, UnitResult
from shadowclaw.execution.plan import Plan, Step
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.execution.units import MockUnit, UnitRegistry

from conftest import run_coro


def test_policy_defaults_to_ask():
    policy = FailClosedPolicy()
    assert policy.decide(step_id="s", unit_id="any", primitive="sequence") \
        is Decision.ASK


def test_policy_rule_hit():
    policy = FailClosedPolicy({"safe": Decision.ALLOW,
                               "bad": Decision.DENY})
    assert policy.decide(step_id="s", unit_id="safe",
                         primitive="sequence") is Decision.ALLOW
    assert policy.decide(step_id="s", unit_id="bad",
                         primitive="sequence") is Decision.DENY


def _plan_with_approval(unit="worker"):
    return Plan(id="p", steps=[
        Step(id="s1", unit=unit, input="干活", requires_approval=True)])


def test_ask_denies_when_unattended(make_orchestrator):
    unit = MockUnit("worker", UnitResult(text="不该出现"))
    registry = UnitRegistry()
    registry.register(unit)
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: _plan_with_approval(), task="t",
                               fallback_unit="worker"))
    assert result.status == "denied"
    assert unit.seen_contexts == []               # 单元根本没被调用
    events = SqliteTrajectoryStore(orch._config.db_path) \
        .events(result.run_id)
    types = [e["type"] for e in events]
    assert "approval_requested" in types
    decided = [e for e in events if e["type"] == "approval_decided"][0]
    assert decided["payload"] == {"decision": "ask", "effective": "deny"}


def test_allow_rule_lets_step_run(make_orchestrator):
    unit = MockUnit("worker", UnitResult(text="干完了"))
    registry = UnitRegistry()
    registry.register(unit)
    orch = make_orchestrator(
        registry, approval_policy=FailClosedPolicy({"worker":
                                                    Decision.ALLOW}))
    result = run_coro(orch.run(lambda: _plan_with_approval(), task="t",
                               fallback_unit="worker"))
    assert result.status == "success"
    assert result.outputs[0].text == "干完了"


def test_attended_handler_can_approve(make_orchestrator):
    unit = MockUnit("worker", UnitResult(text="现场批准后干完"))
    registry = UnitRegistry()
    registry.register(unit)
    orch = make_orchestrator(registry, attended=True,
                             ask_handler=lambda step_id: True)
    result = run_coro(orch.run(lambda: _plan_with_approval(), task="t",
                               fallback_unit="worker"))
    assert result.status == "success"
