"""阶段 4 测法升级测试（li 拍板 (a)+(c)）：

- 命中判定全答复语义兜底（结论段优先、兜底仍须同义组全中）；
- 离线重判对第 001 轮冻结档案的对称性与逐项结论；
- 多轮采样聚合口径（严格多数 / 中位数 / 任一轮原则错误即记）与
  iterate samples=2 的 Mock mechanics。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from shadowclaw.evaluation.iterate import (CaseScore, aggregate_case_scores,
                                           run_iteration)
from shadowclaw.evaluation.regression import check_answer, load_case, load_cases
from shadowclaw.evaluation.rejudge import (render_rejudge_report,
                                           rejudge_round)
from shadowclaw.execution.contracts import UnitResult
from shadowclaw.execution.units import MockUnit

from test_stage4 import _crafted_answer, _setup_iter_env

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = REPO_ROOT / "evaluation" / "regression" / "cases"
HARNESS_DIR = REPO_ROOT / "harness"
ROUND_001 = REPO_ROOT / "evaluation" / "iterations" / "001"

FALLBACK_YAML = """
id: fallback-case
title: 兜底合成案例
brief: |
  症状：服务启动后环境变量不生效。
materials: |
  附带材料：无。
reference:
  root_cause: 进程没读到配置。
  key_actions:
    - 查进程实际配置
  source: 合成测试案例
scoring:
  root_cause_groups:
    - ["没读到", "未加载"]
    - ["配置"]
  semantic_groups:
    - ["进程", "服务"]
    - ["未生效", "读不到"]
  actions:
    - id: check-actual
      label: 查进程实际配置
      any: ["实际配置"]
"""


# ---------------------------------------------------------- 语义兜底

def test_fallback_full_answer_semantic_hit(tmp_path):
    """根因句只在正文（结论段抽不到）时，全答复同义组兜底命中。"""
    p = tmp_path / "fallback.yaml"
    p.write_text(FALLBACK_YAML, encoding="utf-8")
    case = load_case(p)
    answer = ("先说分析：服务进程启动时变量未生效，读不到任何覆盖值。"
              "以下省略排查记录若干。结论：已证实，就是这个问题，"
              "后续按建议处理。")
    result = check_answer(case, answer)
    assert result.semantic_hit is False       # 结论段内没有同义组
    assert result.semantic_hit_full is True   # 全答复范围同义组全中
    assert result.conclusion_hit is True
    assert result.hit_via == "semantic_full"
    assert "certainty_without_evidence" not in {
        e.kind for e in result.principle_errors}


def test_fallback_remains_strict_on_partial_groups(tmp_path):
    """兜底严格度不降：同义组只中一部分仍判未命中，certainty 照记。"""
    p = tmp_path / "fallback.yaml"
    p.write_text(FALLBACK_YAML, encoding="utf-8")
    case = load_case(p)
    answer = "分析：服务进程启动正常。结论：已证实，根因就是代码写错了。"
    result = check_answer(case, answer)
    assert result.conclusion_hit is False
    assert result.hit_via == ""
    assert "certainty_without_evidence" in {
        e.kind for e in result.principle_errors}


def test_keyword_hit_via_label(tmp_path):
    p = tmp_path / "fallback.yaml"
    p.write_text(FALLBACK_YAML, encoding="utf-8")
    case = load_case(p)
    result = check_answer(case, "结论：进程没读到配置，未加载。")
    assert result.conclusion_hit is True
    assert result.hit_via == "keyword"


# ---------------------------------------------------------- 采样聚合

def _score(**kw) -> CaseScore:
    base = dict(case_id="c", status="completed", hit=False, covered=0,
                total=4, principle_errors=0, cost=0.0)
    base.update(kw)
    return CaseScore(**base)


def test_aggregate_majority_rule():
    assert aggregate_case_scores(
        [_score(hit=True), _score(hit=True), _score()]).hit is True
    # 严格多数：2 个样本 1 票不算命中
    assert aggregate_case_scores(
        [_score(hit=True), _score()]).hit is False
    assert aggregate_case_scores(
        [_score(hit=True), _score(hit=True)]).hit is True
    assert aggregate_case_scores([_score()]).hit is False


def test_aggregate_coverage_median_floor():
    assert aggregate_case_scores(
        [_score(covered=3), _score(covered=2),
         _score(covered=4)]).covered == 3
    # 中位数 2.5 向下取整（从严）
    assert aggregate_case_scores(
        [_score(covered=3), _score(covered=2)]).covered == 2


def test_aggregate_principle_errors_any_sample():
    assert aggregate_case_scores(
        [_score(), _score(principle_errors=2),
         _score()]).principle_errors == 2
    assert aggregate_case_scores(
        [_score(), _score()]).principle_errors == 0


def test_aggregate_status_and_cost():
    agg = aggregate_case_scores(
        [_score(cost=0.1), _score(status="error", cost=0.2)])
    assert agg.status == "error"
    assert abs(agg.cost - 0.3) < 1e-9


def test_iterate_samples_two_mechanics(tmp_path):
    """samples=2：两侧各跑两轮、聚合成行、答复存为列表、判定不变。"""
    harness_copy, eval_dir, config = _setup_iter_env(tmp_path)
    cases = load_cases(CASES_DIR)

    def factory(case, assembled):  # noqa: ANN001
        return MockUnit("shadow-solver",
                        UnitResult(text=_crafted_answer(case),
                                   tokens_in=10, tokens_out=10),
                        model="mock",
                        instruction=assembled.render_instruction())

    result = asyncio.run(run_iteration(
        cases=cases, harness_dir=harness_copy, eval_dir=eval_dir,
        config=config, unit_factory=factory, round_id="t3",
        apply_merge=True, model_label="mock", samples=2))
    assert result.samples == 2
    assert result.verdict == "passed"
    round_dir = eval_dir / "iterations" / "t3"
    saved = json.loads((round_dir / "iteration.json").read_text(
        encoding="utf-8"))
    assert saved["samples"] == 2
    answers = json.loads((round_dir / "answers-candidate.json").read_text(
        encoding="utf-8"))
    assert all(isinstance(v, list) and len(v) == 2
               for v in answers.values())
    assert (round_dir / "signals-baseline-1.jsonl").exists()
    assert (round_dir / "signals-baseline-2.jsonl").exists()


# ---------------------------------------------------------- 离线重判

def test_rejudge_round_001_archive():
    """第 001 轮冻结档案重判：基线侧不变（对称、无普遍放水），
    github 候选翻转、telegram 覆盖仍降、整轮拒绝仍成立。"""
    result = rejudge_round(ROUND_001, cases_dir=CASES_DIR,
                           harness_dir=HARNESS_DIR)
    assert result.original_verdict == "rejected"
    # 对称性：同一把尺子量两侧，基线侧 6 格与原判全一致
    assert result.baseline and all(not s.changed
                                   for s in result.baseline)
    cand = {s.case_id: s for s in result.candidate}
    github = cand["github-app-no-create-permission"]
    assert github.original is not None and github.original.hit is False
    assert github.rejudged is not None and github.rejudged.hit is True
    assert any(v.startswith("semantic") for v in github.hit_vias)
    telegram = cand["telegram-hook-log-array"]
    assert telegram.rejudged is not None
    assert telegram.rejudged.covered == 2
    assert result.rejudged_regressions == [
        "telegram-hook-log-array：动作覆盖下降 3/4 → 2/4"]
    assert result.verdict_conclusion.startswith("拒绝成立")
    # 疑似误判信号经重判否定（只注记，不动原信号记录）
    assert any("sig-78a40fd166" in n and "经重判否定" in n
               for n in result.signal_notes)
    report = render_rejudge_report(result, generated_at="test")
    assert "对照表" in report and "github-app-no-create-permission" in report
