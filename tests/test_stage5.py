"""阶段 5：三新原语（route/parallel/reflect）+ 转正门 + 主动性开关。

原语部分：校验三拒口径（illegal / unknown_unit）与执行语义（路由
留痕、并行失败隔离与冲突合并、反思打回重跑与超限失败）。
转正部分：三项门槛在临时 eval 目录里逐项造数核验——Mock/回放样本
不计数、无批准时 autonomous 加载即拒、全过时 ensure_promoted 放行。
主动性部分：默认全关、未登记条目拒载、显式开启可读。
"""
import json
from pathlib import Path

import pytest

from shadowclaw.evaluation.promotion import (PromotionError,
                                             check_promotion,
                                             ensure_promoted,
                                             list_promotions,
                                             record_approval)
from shadowclaw.evaluation.signals import Signal, SignalStore
from shadowclaw.execution.contracts import UnitResult
from shadowclaw.execution.plan import Plan, Step, validate_plan
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.execution.units import MockUnit, UnitRegistry
from shadowclaw.harness.loader import HarnessError, load_harness
from shadowclaw.harness.proactivity import (KNOWN_ITEMS, load_proactivity)

from conftest import run_coro

UNITS = {"a", "b", "c"}
CAP = 5.0
REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = REPO_ROOT / "harness"


def kinds(problems):
    return {p.kind for p in problems}


def _events(make_orchestrator, orch, run_id):
    return SqliteTrajectoryStore(orch._config.db_path).events(run_id)


def _registry(*units):
    registry = UnitRegistry()
    for unit in units:
        registry.register(unit)
    return registry


# ------------------------------------------------------------ route 校验
def test_route_validation_rejections():
    no_rules = Plan(id="p", steps=[Step(id="s", primitive="route")])
    assert kinds(validate_plan(no_rules, UNITS,
                               budget_hard_cap=CAP)) == {"illegal"}
    bad_source = Plan(id="p", steps=[Step(id="s", primitive="route", route={
        "rules": [{"when": {"source": "moon", "contains": "x"},
                   "unit": "a"}]})])
    assert "illegal" in kinds(validate_plan(bad_source, UNITS,
                                            budget_hard_cap=CAP))
    ghost = Plan(id="p", steps=[Step(id="s", primitive="route", route={
        "rules": [{"when": {"source": "input", "contains": "x"},
                   "unit": "ghost"}]})])
    assert kinds(validate_plan(ghost, UNITS,
                               budget_hard_cap=CAP)) == {"unknown_unit"}
    ghost_default = Plan(id="p", steps=[Step(
        id="s", primitive="route",
        route={"rules": [{"when": {"source": "input", "contains": "x"},
                          "unit": "a"}], "default": "ghost"})])
    assert kinds(validate_plan(ghost_default, UNITS,
                               budget_hard_cap=CAP)) == {"unknown_unit"}


# --------------------------------------------------------- parallel 校验
def test_parallel_validation_rejections():
    empty = Plan(id="p", steps=[Step(id="s", primitive="parallel")])
    assert kinds(validate_plan(empty, UNITS,
                               budget_hard_cap=CAP)) == {"illegal"}
    nested = Plan(id="p", steps=[Step(id="s", primitive="parallel",
                                     branches=[Step(id="b",
                                                    primitive="parallel")])])
    assert "illegal" in kinds(validate_plan(nested, UNITS,
                                            budget_hard_cap=CAP))
    ghost = Plan(id="p", steps=[Step(id="s", primitive="parallel",
                                     branches=[Step(id="b",
                                                    unit="ghost")])])
    assert kinds(validate_plan(ghost, UNITS,
                               budget_hard_cap=CAP)) == {"unknown_unit"}


# ---------------------------------------------------------- reflect 校验
def test_reflect_validation_rejections():
    zero_rounds = Plan(id="p", steps=[Step(id="s", primitive="reflect",
                                           unit="a", max_rounds=0)])
    assert "illegal" in kinds(validate_plan(zero_rounds, UNITS,
                                            budget_hard_cap=CAP))
    bad_item = Plan(id="p", steps=[Step(id="s", primitive="reflect",
                                        unit="a", checklist=["feel:good"])])
    assert "illegal" in kinds(validate_plan(bad_item, UNITS,
                                            budget_hard_cap=CAP))
    bad_num = Plan(id="p", steps=[Step(id="s", primitive="reflect",
                                       unit="a",
                                       checklist=["min_length:abc"])])
    assert "illegal" in kinds(validate_plan(bad_num, UNITS,
                                            budget_hard_cap=CAP))
    ghost_critic = Plan(id="p", steps=[Step(id="s", primitive="reflect",
                                            unit="a", critic="ghost")])
    assert kinds(validate_plan(ghost_critic, UNITS,
                               budget_hard_cap=CAP)) == {"unknown_unit"}


def test_plan_roundtrip_keeps_new_fields():
    plan = Plan(id="p", steps=[
        Step(id="r", primitive="route",
             route={"rules": [{"when": {"source": "facts",
                                        "contains": "k"}, "unit": "a"}],
                    "default": "b"}),
        Step(id="par", primitive="parallel",
             branches=[Step(id="b1", unit="a")]),
        Step(id="ref", primitive="reflect", unit="c",
             checklist=["contains:x"], max_rounds=3, critic="b"),
    ])
    back = Plan.from_dict(plan.to_dict())
    assert back.steps[0].route["default"] == "b"
    assert back.steps[1].branches[0].unit == "a"
    assert back.steps[2].checklist == ["contains:x"]
    assert back.steps[2].max_rounds == 3 and back.steps[2].critic == "b"
    assert set(back.unit_ids()) == {"a", "b", "c"}


# ------------------------------------------------------------ route 执行
def _route_plan():
    return Plan(id="rp", steps=[
        Step(id="s1", unit="a", input="开始"),
        Step(id="s2", primitive="route", input="深入",
             route={"rules": [{"when": {"source": "prev_output",
                                        "contains": "句柄"},
                               "unit": "b"}], "default": "c"}),
    ])


def test_route_hits_rule_and_records_decision(make_orchestrator):
    registry = _registry(
        MockUnit("a", UnitResult(text="定界：伴随句柄耗尽告警")),
        MockUnit("b", UnitResult(text="句柄专家出马")),
        MockUnit("c", UnitResult(text="通用专家出马")))
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(_route_plan, task="t", fallback_unit="a"))
    assert result.status == "success"
    assert result.outputs[-1].unit_id == "b"
    decided = [e for e in _events(make_orchestrator, orch, result.run_id)
               if e["type"] == "route_decided"]
    assert len(decided) == 1
    payload = decided[0]["payload"]
    assert payload["matched_rule"] == 0 and payload["chosen"] == "b"
    assert payload["rejected"] == ["c"]


def test_route_falls_back_to_default(make_orchestrator):
    registry = _registry(
        MockUnit("a", UnitResult(text="定界：普通故障")),
        MockUnit("b", UnitResult(text="句柄专家出马")),
        MockUnit("c", UnitResult(text="通用专家出马")))
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(_route_plan, task="t", fallback_unit="a"))
    assert result.status == "success"
    assert result.outputs[-1].unit_id == "c"
    decided = [e for e in _events(make_orchestrator, orch, result.run_id)
               if e["type"] == "route_decided"][0]
    assert decided["payload"]["matched_rule"] == "default"


def test_route_no_match_no_default_fails(make_orchestrator):
    registry = _registry(
        MockUnit("a", UnitResult(text="定界：普通故障")),
        MockUnit("b", UnitResult(text="句柄专家出马")))
    plan = Plan(id="rp2", steps=[
        Step(id="s1", unit="a"),
        Step(id="s2", primitive="route",
             route={"rules": [{"when": {"source": "prev_output",
                                        "contains": "句柄"},
                               "unit": "b"}]}),
    ])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="a"))
    assert result.status == "failed"


# --------------------------------------------------------- parallel 执行
def test_parallel_merges_and_isolates_failure(make_orchestrator):
    registry = _registry(
        MockUnit("a", UnitResult(text="甲完成",
                                 proposed_facts=["甲事实：已确认"])),
        MockUnit("b", RuntimeError("乙通道炸了")),
        MockUnit("c", UnitResult(text="丙完成")),
        MockUnit("d", lambda ctx: UnitResult(
            text=f"汇总看到的事实：{ctx.facts}")))
    plan = Plan(id="pp", steps=[
        Step(id="s1", primitive="parallel", branches=[
            Step(id="b1", unit="a"), Step(id="b2", unit="b"),
            Step(id="b3", unit="c")]),
        Step(id="s2", unit="d"),
    ])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="a"))
    assert result.status == "success"          # 乙失败被隔离，整单照常
    finished = [e for e in _events(make_orchestrator, orch, result.run_id)
                if e["type"] == "parallel_finished"][0]
    assert finished["payload"]["succeeded"] == ["b1", "b3"]
    assert finished["payload"]["failed"][0]["branch"] == "b2"
    # 甲分支的事实经父板合并，后续步能看到
    assert "甲事实：已确认" in result.outputs[-1].text


def test_parallel_all_fail_fails_step(make_orchestrator):
    registry = _registry(
        MockUnit("a", RuntimeError("甲炸")),
        MockUnit("b", RuntimeError("乙炸")))
    plan = Plan(id="pp2", steps=[
        Step(id="s1", primitive="parallel", branches=[
            Step(id="b1", unit="a"), Step(id="b2", unit="b")])])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="a"))
    assert result.status == "failed"


def test_parallel_merge_conflict_rejected(tmp_path):
    """合并冲突：同键异值，后到分支的事实被父板冲突检查拒落板。"""
    from shadowclaw.execution.checkpoint import SqliteCheckpointStore
    from shadowclaw.execution.config import Config
    from shadowclaw.execution.orchestrator import Orchestrator
    from shadowclaw.harness.conflicts import KeyValueConflictGuard

    guard = KeyValueConflictGuard()
    db = tmp_path / "t.db"
    registry = _registry(
        MockUnit("a", UnitResult(text="甲",
                                 proposed_facts=["故障域：日志通道"])),
        MockUnit("b", UnitResult(text="乙",
                                 proposed_facts=["故障域：网络层"])),
        MockUnit("c", lambda ctx: UnitResult(text=f"事实：{ctx.facts}")))
    plan = Plan(id="pp3", steps=[
        Step(id="s1", primitive="parallel", branches=[
            Step(id="b1", unit="a"), Step(id="b2", unit="b")]),
        Step(id="s2", unit="c"),
    ])
    orch = Orchestrator(
        registry=registry,
        trajectory_store=SqliteTrajectoryStore(db),
        checkpoint_store=SqliteCheckpointStore(db),
        config=Config(db_path=db, harness_dir=tmp_path / "harness"),
        fact_commit_guard=guard)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="a"))
    assert result.status == "success"
    assert len(guard.conflicts) == 1
    # 先到的落板、冲突的被拒：后续步只看到日志通道
    assert "故障域：日志通道" in result.outputs[-1].text
    assert "网络层" not in result.outputs[-1].text


# ---------------------------------------------------------- reflect 执行
def _flaky_reporter(ctx):
    if "反思打回反馈" in ctx.prompt:
        return UnitResult(text="定稿：根因已定位，处置明确。",
                          proposed_facts=["定稿事实：应落板"])
    return UnitResult(text="草稿，未证实。",
                      proposed_facts=["草稿事实：不应落板"])


def test_reflect_rejects_then_passes(make_orchestrator):
    registry = _registry(MockUnit("r", _flaky_reporter))
    plan = Plan(id="fp", steps=[
        Step(id="s1", primitive="reflect", unit="r", input="写报告",
             checklist=["contains:根因", "not_contains:未证实",
                        "fact_proposed"], max_rounds=2)])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="r"))
    assert result.status == "success"
    events = _events(make_orchestrator, orch, result.run_id)
    rounds = [e["payload"] for e in events if e["type"] == "reflect_round"]
    assert [r["verdict"] for r in rounds] == ["rejected", "pass"]
    committed = [e["payload"]["statement"] for e in events
                 if e["type"] == "fact_committed"]
    assert committed == ["定稿事实：应落板"]    # 草稿轮提议从未落板


def test_reflect_exhaustion_fails(make_orchestrator):
    registry = _registry(MockUnit(
        "r", UnitResult(text="永远不合格的产出")))
    plan = Plan(id="fp2", steps=[
        Step(id="s1", primitive="reflect", unit="r",
             checklist=["contains:根因"], max_rounds=2)])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="r"))
    assert result.status == "failed"
    rounds = [e for e in _events(make_orchestrator, orch, result.run_id)
              if e["type"] == "reflect_round"]
    assert len(rounds) == 2                    # 跑满 max_rounds 才判失败


def test_reflect_critic_can_reject(make_orchestrator):
    registry = _registry(
        MockUnit("r", UnitResult(text="根因写清楚了的好产出")),
        MockUnit("critic", UnitResult(text="打回：证据链不完整")))
    plan = Plan(id="fp3", steps=[
        Step(id="s1", primitive="reflect", unit="r",
             checklist=["contains:根因"], max_rounds=1, critic="critic")])
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(lambda: plan, task="t", fallback_unit="r"))
    assert result.status == "failed"           # 规则过、critic 打回 → 失败
    rounds = [e["payload"] for e in
              _events(make_orchestrator, orch, result.run_id)
              if e["type"] == "reflect_round"]
    assert rounds[0]["via"] == "rules+critic"
    assert any("critic 打回" in r for r in rounds[0]["reasons"])


# ------------------------------------------------------- demo3 全链路
def test_demo3_full_chain(make_orchestrator, store_of):
    from shadowclaw.execution.demo3 import build_demo3_plan, \
        build_demo3_registry
    from shadowclaw.harness.conflicts import KeyValueConflictGuard

    from shadowclaw.execution.checkpoint import SqliteCheckpointStore
    from shadowclaw.execution.config import Config
    from shadowclaw.execution.orchestrator import Orchestrator

    guard = KeyValueConflictGuard()
    db = make_orchestrator.db_path
    orch = Orchestrator(
        registry=build_demo3_registry(),
        trajectory_store=SqliteTrajectoryStore(db),
        checkpoint_store=SqliteCheckpointStore(db),
        config=Config(db_path=db, harness_dir=HARNESS_DIR),
        fact_commit_guard=guard)
    result = run_coro(orch.run(build_demo3_plan, task="排障",
                               fallback_unit="triage"))
    assert result.status == "success"
    types = [e["type"] for e in store_of(db).events(result.run_id)]
    for expected in ("route_decided", "parallel_started",
                     "parallel_finished", "reflect_round"):
        assert expected in types
    assert len(guard.conflicts) == 1


# ------------------------------------------------------------ 转正门
def _mk_eval_dir(tmp_path: Path) -> Path:
    eval_dir = tmp_path / "evaluation"
    (eval_dir / "signals").mkdir(parents=True)
    (eval_dir / "iterations").mkdir()
    return eval_dir


def _add_sample(eval_dir: Path, extra: dict) -> None:
    store = SignalStore(eval_dir / "signals" / "signals.jsonl")
    store.append(Signal(kind="outcome", source="shadow-replay",
                        content="样本", reference="出处",
                        extra=extra))


def _add_round(eval_dir: Path, round_id: str, regressions: list) -> None:
    rdir = eval_dir / "iterations" / round_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "iteration.json").write_text(json.dumps({
        "round_id": round_id,
        "verdict": "rejected" if regressions else "passed",
        "regressions": regressions}, ensure_ascii=False),
        encoding="utf-8")


def test_promotion_empty_dir_reports_all_gaps(tmp_path):
    eval_dir = _mk_eval_dir(tmp_path)
    check = check_promotion("troubleshooting", eval_dir=eval_dir)
    assert not check.ok
    by_name = {i.name: i for i in check.items}
    assert by_name["regression"].ok              # 无档案 = 无退步记录
    assert not by_name["samples"].ok             # 0/5
    assert not by_name["approval"].ok            # 无批准
    with pytest.raises(PromotionError) as excinfo:
        ensure_promoted("troubleshooting", eval_dir=eval_dir)
    assert "samples" in str(excinfo.value)
    assert "approval" in str(excinfo.value)


def test_promotion_regression_record_blocks(tmp_path):
    eval_dir = _mk_eval_dir(tmp_path)
    _add_round(eval_dir, "001", ["telegram 案：动作覆盖下降"])
    check = check_promotion("troubleshooting", eval_dir=eval_dir)
    item = {i.name: i for i in check.items}["regression"]
    assert not item.ok and "001" in item.detail


def test_promotion_counts_only_real_live_samples(tmp_path):
    eval_dir = _mk_eval_dir(tmp_path)
    for _ in range(4):
        _add_sample(eval_dir, {"origin": "replay_contrast",
                               "sample_kind": "real",
                               "scenario": "troubleshooting"})
    for _ in range(3):
        _add_sample(eval_dir, {"origin": "replay_contrast",
                               "sample_kind": "mock",
                               "scenario": "troubleshooting"})
    _add_sample(eval_dir, {"case_id": "x"})      # 未标记 → unknown
    check = check_promotion("troubleshooting", eval_dir=eval_dir)
    assert check.sample_counts == {"real_live": 0, "replay": 4,
                                   "mock": 3, "unknown": 1}
    assert not {i.name: i for i in check.items}["samples"].ok
    for _ in range(5):
        _add_sample(eval_dir, {"origin": "live_task",
                               "sample_kind": "real",
                               "scenario": "troubleshooting"})
    check = check_promotion("troubleshooting", eval_dir=eval_dir)
    assert check.sample_counts["real_live"] == 5
    assert {i.name: i for i in check.items}["samples"].ok


def test_promotion_full_pass_and_approval_ledger(tmp_path):
    eval_dir = _mk_eval_dir(tmp_path)
    _add_round(eval_dir, "001", [])              # 有档案、无退步
    for _ in range(5):
        _add_sample(eval_dir, {"origin": "live_task",
                               "sample_kind": "real",
                               "scenario": "troubleshooting"})
    before = check_promotion("troubleshooting", eval_dir=eval_dir)
    assert not before.ok                         # 就差批准
    record = record_approval(eval_dir, "troubleshooting", before)
    assert record["approved_by"] == "li"
    assert record["gate_snapshot"]["scenario"] == "troubleshooting"
    assert len(list_promotions(eval_dir)) == 1   # 只追加台账有一条
    after = check_promotion("troubleshooting", eval_dir=eval_dir)
    assert after.ok
    ensure_promoted("troubleshooting", eval_dir=eval_dir)   # 不抛 = 放行


# ------------------------------------------------------- 场景 mode 门
def _write_min_harness(root: Path, scenario_yaml: str) -> None:
    (root / "principles").mkdir(parents=True)
    (root / "principles" / "principles.md").write_text("# 原则\n正文")
    (root / "guardrails").mkdir()
    (root / "guardrails" / "guardrails.md").write_text(
        "---\nlocked: false\n---\n\n```yaml\napproval_matrix:\n"
        "  read: allow\n```\n")
    (root / "scenarios").mkdir()
    (root / "scenarios" / "mini.yaml").write_text(scenario_yaml)
    (root / "memory").mkdir()
    (root / "memory" / "core.md").write_text("# core")


def test_scenario_mode_default_shadow_and_invalid_rejected(tmp_path):
    _write_min_harness(tmp_path, "name: mini\nplaybook: ''\n")
    bundle = load_harness(tmp_path, "mini")
    assert bundle.scenario.mode == "shadow"      # 缺省 shadow
    _write_min_harness(tmp_path / "h2", "name: mini\nplaybook: ''\n"
                                        "mode: wild\n")
    with pytest.raises(HarnessError, match="mode"):
        load_harness(tmp_path / "h2", "mini")


def test_autonomous_without_gate_refused(tmp_path):
    _write_min_harness(tmp_path, "name: mini\nplaybook: ''\n"
                                 "mode: autonomous\n")
    with pytest.raises(HarnessError, match="fail-closed"):
        load_harness(tmp_path, "mini")           # 无 gate 注入 → 拒载


def test_autonomous_gate_blocks_then_passes(tmp_path):
    _write_min_harness(tmp_path, "name: mini\nplaybook: ''\n"
                                 "mode: autonomous\n")
    eval_dir = _mk_eval_dir(tmp_path)
    from shadowclaw.evaluation.promotion import gate_for
    gate = gate_for(eval_dir)
    with pytest.raises(PromotionError):          # 门槛未过 → 拒载
        load_harness(tmp_path, "mini", promotion_gate=gate)
    for _ in range(5):
        _add_sample(eval_dir, {"origin": "live_task",
                               "sample_kind": "real",
                               "scenario": "mini"})
    record_approval(eval_dir, "mini",
                    check_promotion("mini", eval_dir=eval_dir))
    bundle = load_harness(tmp_path, "mini", promotion_gate=gate)
    assert bundle.scenario.mode == "autonomous"  # 三项全过 → 放行


def test_real_scenario_stays_shadow_and_carries_mode(tmp_path):
    bundle = load_harness(HARNESS_DIR, "troubleshooting")
    assert bundle.scenario.mode == "shadow"
    from shadowclaw.harness.assembler import assemble
    ctx = assemble(bundle)
    assert ctx.mode == "shadow"
    assert "route" in ctx.planner_view()         # 规划者视图含新原语说明
    assert "reflect" in ctx.planner_view()


# ------------------------------------------------------------ 主动性
def test_proactivity_defaults_all_off_on_real_harness():
    config = load_proactivity(HARNESS_DIR)
    assert config.enabled_items() == []
    assert all(not config.is_enabled(name) for name in KNOWN_ITEMS)
    bundle = load_harness(HARNESS_DIR, "troubleshooting")
    assert bundle.proactivity is not None
    assert bundle.proactivity.enabled_items() == []


def test_proactivity_unknown_item_rejected(tmp_path):
    (tmp_path / "proactivity.yaml").write_text(
        "items:\n  teleport_reports:\n    enabled: true\n")
    with pytest.raises(HarnessError, match="未登记"):
        load_proactivity(tmp_path)


def test_proactivity_explicit_on_loads(tmp_path):
    (tmp_path / "proactivity.yaml").write_text(
        "items:\n  remind_pending:\n    enabled: true\n")
    config = load_proactivity(tmp_path)
    assert config.is_enabled("remind_pending")
    assert not config.is_enabled("sync_progress")
    with pytest.raises(KeyError):
        config.is_enabled("teleport_reports")    # 未登记条目问询即报错


def test_proactivity_missing_file_is_all_off(tmp_path):
    config = load_proactivity(tmp_path)          # 无文件不报错
    assert config.enabled_items() == []
