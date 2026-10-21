"""阶段 3 测试：信号 schema 校验、回归集加载与硬查、影子运行器
mechanics（Mock 单元离线跑通，不依赖真实模型）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from shadowclaw.evaluation.regression import (check_answer, load_case,
                                              load_cases)
from shadowclaw.evaluation.shadow import run_shadow
from shadowclaw.evaluation.signals import Signal, SignalStore
from shadowclaw.execution.config import Config
from shadowclaw.execution.contracts import UnitResult
from shadowclaw.execution.units import MockUnit
from shadowclaw.harness.loader import load_harness

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = REPO_ROOT / "evaluation" / "regression" / "cases"
HARNESS_DIR = REPO_ROOT / "harness"

CASE_YAML = """
id: synthetic-case
title: 合成案例
brief: |
  症状：服务重启后配置不生效，日志显示走了默认端口。
materials: |
  附带材料：启动日志一行（据记录还原）。
reference:
  root_cause: 进程没读到配置文件，走了默认值。
  key_actions:
    - 查进程实际配置
    - 重启后复验
  source: 合成测试案例
scoring:
  root_cause_groups:
    - ["没读到", "未加载"]
    - ["配置"]
  actions:
    - id: check-actual
      label: 查进程实际配置
      any: ["实际配置", "实际读到"]
    - id: reverify
      label: 重启后复验
      any: ["复验"]
"""


@pytest.fixture
def case_file(tmp_path):
    p = tmp_path / "synthetic.yaml"
    p.write_text(CASE_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 信号

def test_correction_without_reference_rejected(tmp_path):
    store = SignalStore(tmp_path / "signals.jsonl")
    with pytest.raises(ValueError, match="拒存"):
        store.append(Signal(kind="correction", source="li",
                            content="这条不对"))
    assert store.list() == []


def test_outcome_without_reference_rejected(tmp_path):
    store = SignalStore(tmp_path / "signals.jsonl")
    with pytest.raises(ValueError, match="拒存"):
        store.append(Signal(kind="outcome", source="task-close",
                            content="修好了"))
    ok = store.append(Signal(kind="feedback", source="li",
                             content="顺手反馈可以不带出处"))
    assert ok.id.startswith("sig-")
    assert store.count_by_kind() == {"feedback": 1}


def test_signal_roundtrip_and_filters(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(Signal(kind="outcome", source="shadow-replay",
                        content="命中", reference="案例出处",
                        trace_id="run-1"))
    store.append(Signal(kind="judge", source="judge", content="参考",
                        trace_id="run-2"))
    assert len(store.list()) == 2
    assert len(store.list(kind="outcome")) == 1
    assert store.list(trace_id="run-2")[0].kind == "judge"


# ---------------------------------------------------------------- 回归集

def test_real_cases_load_and_schema():
    cases = load_cases(CASES_DIR)
    assert len(cases) == 6
    for case in cases:
        assert case.brief and case.root_cause and case.source
        assert case.root_cause_groups, case.id
        assert len(case.key_actions) >= 3, case.id
        # 简报不许泄题：参照根因原文不出现在可见输入里
        assert case.root_cause[:12] not in case.visible_text(), case.id


def test_check_answer_hit_and_coverage(case_file):
    case = load_case(case_file)
    good = ("结论：根因是进程没读到配置，未加载配置文件走了默认。"
            "先查进程实际配置，再重启后复验。")
    result = check_answer(case, good)
    assert result.conclusion_hit is True
    assert result.actions_covered == ["check-actual", "reverify"]
    assert result.principle_errors == []


def test_check_answer_miss_and_certainty_flagged(case_file):
    case = load_case(case_file)
    bad = "结论：已证实，根因就是代码 bug，重启 3 次就好，耗时 45 秒。"
    result = check_answer(case, bad)
    assert result.conclusion_hit is False
    kinds = {e.kind for e in result.principle_errors}
    assert "certainty_without_evidence" in kinds
    assert "fabricated_metric" in kinds  # 45 秒不在材料里


def test_check_answer_honest_miss_not_flagged(case_file):
    case = load_case(case_file)
    honest = "结论：未证实。现有材料不足以判断根因，缺进程日志。"
    result = check_answer(case, honest)
    assert result.conclusion_hit is False
    assert result.principle_errors == []


# ---------------------------------------------------------------- 运行器

def test_shadow_runner_mechanics_with_mock(tmp_path, case_file):
    async def _run():
        bundle = load_harness(HARNESS_DIR, "troubleshooting")
        case = load_case(case_file)

        def factory(c, assembled):  # noqa: ANN001
            return MockUnit(
                "shadow-solver",
                UnitResult(
                    text=("结论（已证实）：根因是进程没读到配置，未加载。"
                          "建议先查进程实际配置，重启后复验。"),
                    tokens_in=20, tokens_out=10),
                model="mock",
                instruction=assembled.render_instruction())

        store = SignalStore(tmp_path / "signals.jsonl")
        config = Config(db_path=tmp_path / "shadow.db",
                        harness_dir=HARNESS_DIR, model="mock")
        return await run_shadow(cases=[case], bundle=bundle, config=config,
                                signal_store=store, unit_factory=factory), \
            store

    import asyncio
    run, store = asyncio.run(_run())
    cr = run.cases[0]
    assert cr.status == "completed"
    assert cr.check is not None and cr.check.conclusion_hit is True
    assert cr.deviations == []
    assert cr.run_id
    # 命中案例只入 outcome 信号，不产 correction 候选
    kinds = [s.kind for s in store.list()]
    assert kinds == ["outcome"]
    assert store.list()[0].trace_id == cr.run_id
    assert run.signals_written == {"outcome": 1}
