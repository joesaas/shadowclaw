"""demo 验收：全程跑通、轨迹事件完整可复盘、花费估算口径正确。"""
from shadowclaw.execution.demo import (LOG_FACTS, build_demo_plan,
                                       build_demo_registry)
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.execution.units import UnitRegistry

from conftest import run_coro


def test_demo_end_to_end_trajectory(make_orchestrator):
    registry = build_demo_registry()
    orch = make_orchestrator(registry)
    result = run_coro(orch.run(build_demo_plan, task="排障",
                               fallback_unit="triage"))
    assert result.status == "success"
    assert result.degraded is False

    store = SqliteTrajectoryStore(orch._config.db_path)
    export = store.export(result.run_id)
    types = [e["type"] for e in export["events"]]

    # 事件完整性：验收链路上的关键事件一个不少，且首尾正确。
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    for expected in ("plan_validated", "delegate_started",
                     "delegate_finished", "fact_proposed", "fact_committed",
                     "budget_checked"):
        assert expected in types, expected
    assert types.count("step_started") == 4   # triage+delegate+子步+summarize
    assert types.count("model_call") == 3
    assert types.count("fact_committed") == 4      # 子板 2 条 + 父板落回 2 条

    # 汇总单元的结论建立在事实板的两条事实之上。
    final_text = result.outputs[-1].text
    for fact in LOG_FACTS:
        assert fact in final_text

    # 花费口径：Mock 单元只报 token 不报花费 → 按价格表估算并标 estimated。
    model_calls = [e for e in export["events"] if e["type"] == "model_call"]
    assert all(e["estimated"] for e in model_calls)
    assert all(e["cost"] is not None and e["cost"] > 0 for e in model_calls)
    assert export["totals"]["tokens_in"] == 300 + 900 + 420
    assert export["totals"]["cost"] == result.spent

    # 每条事件都带 run_id 与驾驭层版本号（轨迹契约）。
    assert all(e["run_id"] == result.run_id for e in export["events"])
    assert all(e["harness_version"] == "stage1-dev"
               for e in export["events"])

    # 复盘可重复读：list_runs 能找到这次运行。
    assert result.run_id in store.list_runs()


def test_demo_registry_units_are_real_registry():
    registry = build_demo_registry()
    assert isinstance(registry, UnitRegistry)
    assert registry.ids() == {"triage", "logcheck", "summarize"}
