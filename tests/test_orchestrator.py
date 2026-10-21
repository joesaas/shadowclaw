"""编排器行为：降级保底、检查点恢复、委派隔离、预算硬顶、ADK 单元互换。"""
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types

from shadowclaw.execution.checkpoint import SqliteCheckpointStore
from shadowclaw.execution.contracts import UnitResult
from shadowclaw.execution.plan import Plan, Step
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.execution.units import AdkUnit, MockUnit, UnitRegistry

from conftest import run_coro


def _events(orch, run_id):
    return SqliteTrajectoryStore(orch._config.db_path).events(run_id)


# ---------------------------------------------------------------- 降级保底
def test_invalid_plan_retried_once_then_degraded(make_orchestrator):
    fallback = MockUnit("backup", UnitResult(text="保底完成"))
    registry = UnitRegistry()
    registry.register(fallback)
    calls = {"n": 0}

    def bad_supplier():
        calls["n"] += 1
        return Plan(id="bad", steps=[Step(id="s1", unit="ghost")])

    orch = make_orchestrator(registry)
    result = run_coro(orch.run(bad_supplier, task="原始任务",
                               fallback_unit="backup"))
    assert calls["n"] == 2                       # 原稿 + 重出一次
    assert result.degraded is True
    assert result.status == "success"
    assert result.outputs[0].text == "保底完成"
    validated = [e for e in _events(orch, result.run_id)
                 if e["type"] == "plan_validated"][0]
    assert validated["payload"]["degraded"] is True
    assert len(validated["payload"]["attempts"]) == 2


# ------------------------------------------------------------ 检查点恢复
def test_checkpoint_resume_skips_finished_steps(make_orchestrator):
    a = MockUnit("a", UnitResult(text="A 完成"))
    b_broken = MockUnit("b", RuntimeError("b 炸了"))
    c = MockUnit("c", UnitResult(text="C 完成"))
    registry = UnitRegistry()
    for u in (a, b_broken, c):
        registry.register(u)
    plan = Plan(id="p3", steps=[Step(id="s1", unit="a"),
                                Step(id="s2", unit="b"),
                                Step(id="s3", unit="c")])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="a",
                               run_id="run-resume"))
    assert result.status == "failed"
    cp = SqliteCheckpointStore(orch._config.db_path).load("run-resume")
    assert cp is not None and cp.next_step_index == 1   # s1 已过，断在 s2

    registry.register(MockUnit("b", UnitResult(text="B 修复后完成")))
    resumed = run_coro(orch.resume("run-resume"))
    assert resumed.status == "success"
    assert [o.step_id for o in resumed.outputs] == ["s2", "s3"]
    assert len(a.seen_contexts) == 1                   # s1 没有重跑
    assert SqliteCheckpointStore(orch._config.db_path) \
        .load("run-resume") is None                    # 成功后检查点清除
    starts = [e for e in _events(orch, "run-resume")
              if e["type"] == "run_started"]
    assert starts[-1]["payload"].get("resumed") is True


# ------------------------------------------------------------ 委派隔离
def test_delegate_isolation_between_siblings(make_orchestrator):
    private_marker = "甲的私有分析-不应外泄"
    unit_a = MockUnit("ua", UnitResult(
        text=f"分析完成。{private_marker}",
        proposed_facts=["共享事实甲"]))
    unit_b = MockUnit("ub", UnitResult(text="乙完成"))
    registry = UnitRegistry()
    registry.register(unit_a)
    registry.register(unit_b)
    plan = Plan(id="iso", steps=[
        Step(id="da", primitive="delegate", input="甲去查",
             subplan=Plan(id="pa", steps=[Step(id="x", unit="ua")])),
        Step(id="db", primitive="delegate", input="乙去查",
             subplan=Plan(id="pb", steps=[Step(id="y", unit="ub")])),
    ])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="ua"))
    assert result.status == "success"
    b_ctx = unit_b.seen_contexts[0]
    # 兄弟的原始产出文本不进乙的上下文；只有经事实板落板的事实可见。
    assert private_marker not in b_ctx.prompt
    assert "共享事实甲" in b_ctx.facts


def test_sub_unit_sees_brief_and_committed_facts_only(make_orchestrator):
    triage = MockUnit("t", UnitResult(text="定界原文-子级不应看到全文"))
    checker = MockUnit("k", UnitResult(text="查完"))
    registry = UnitRegistry()
    registry.register(triage)
    registry.register(checker)
    plan = Plan(id="iso2", steps=[
        Step(id="s1", unit="t"),
        Step(id="d", primitive="delegate", input="这是给子级的简报",
             subplan=Plan(id="sub", steps=[Step(id="x", unit="k")])),
    ])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="t"))
    assert result.status == "success"
    k_ctx = checker.seen_contexts[0]
    assert k_ctx.task_input == "这是给子级的简报"
    assert "定界原文-子级不应看到全文" not in k_ctx.prompt


# ------------------------------------------------------------ 预算硬顶
def test_budget_hard_stop(make_orchestrator):
    pricey = MockUnit("p", UnitResult(text="贵", cost=10.0))
    nxt = MockUnit("n", UnitResult(text="不该跑"))
    registry = UnitRegistry()
    registry.register(pricey)
    registry.register(nxt)
    plan = Plan(id="b", budget_limit=1.0,
                steps=[Step(id="s1", unit="p"), Step(id="s2", unit="n")])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="p"))
    assert result.status == "budget_stopped"
    assert nxt.seen_contexts == []
    checks = [e for e in _events(orch, result.run_id)
              if e["type"] == "budget_checked"]
    assert checks[-1]["payload"]["over"] is True


# ------------------------------------------------- ADK 单元（离线 MockLlm）
class _EchoLlm(BaseLlm):
    async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=genai_types.Content(role="model", parts=[
                genai_types.Part(text="ADK 正文回复\nFACT: ADK 提议的事实")]),
            turn_complete=True)


def test_adk_unit_swappable_and_fact_convention(make_orchestrator):
    adk_unit = AdkUnit("adk", instruction="测试指令",
                       model=_EchoLlm(model="echo-llm"),
                       model_label="mock")
    registry = UnitRegistry()
    registry.register(adk_unit)
    checker = MockUnit("after", lambda ctx: UnitResult(
        text=f"后续单元看到的事实：{ctx.facts}"))
    registry.register(checker)
    plan = Plan(id="adk", steps=[Step(id="s1", unit="adk"),
                                 Step(id="s2", unit="after")])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="adk"))
    assert result.status == "success"
    assert result.outputs[0].text == "ADK 正文回复"     # FACT 行已剥离正文
    assert "ADK 提议的事实" in result.outputs[1].text    # 经事实板传给后续单元
