"""阶段 4 测试：判定仪表两处修复、ChangeProposal 状态机、提案生成器
归因规则、整轮判定、iterate 回路 mechanics（Mock 单元离线跑通）。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from shadowclaw.evaluation.iterate import (CaseScore, compare_case_scores,
                                           run_iteration)
from shadowclaw.evaluation.proposals import (ChangeProposal, Edit,
                                             ProposalStore, apply_edits,
                                             generate_proposals)
from shadowclaw.evaluation.regression import check_answer, load_case, load_cases
from shadowclaw.evaluation.signals import Signal, SignalStore
from shadowclaw.execution.config import Config
from shadowclaw.execution.contracts import UnitResult
from shadowclaw.execution.units import MockUnit
from shadowclaw.harness.loader import compute_harness_version

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = REPO_ROOT / "evaluation" / "regression" / "cases"
HARNESS_DIR = REPO_ROOT / "harness"
PLAYBOOK_TEXT = (HARNESS_DIR / "playbooks" / "troubleshooting.md").read_text(
    encoding="utf-8")


def _case(case_id: str):
    return load_cases(CASES_DIR)[{"datapipeline-info-page-404": 0,
                                  "docreader-env-not-loaded": 1,
                                  "ec2-node-version-too-old": 2,
                                  "github-app-no-create-permission": 3,
                                  "telegram-hook-log-array": 4,
                                  "write-false-success": 5}[case_id]]


# ---------------------------------------------------------- 仪表修复 A1

def test_fabricated_metric_from_playbook_not_flagged():
    case = _case("write-false-success")
    answer = "结论：文件没有真正落盘，是假成功，回执里的 15.6KB 并未写到磁盘。"
    with_sources = check_answer(case, answer, extra_sources=[PLAYBOOK_TEXT])
    kinds = {e.kind for e in with_sources.principle_errors}
    assert "fabricated_metric" not in kinds
    # 来源扩展示例：6.4 秒只在手册（Bankr 案例）里出现，简报/材料没有
    sourced = "结论：假成功，未落盘。另有一次直调耗时 6.4 秒。"
    with_playbook = check_answer(case, sourced,
                                 extra_sources=[PLAYBOOK_TEXT])
    assert "fabricated_metric" not in {
        e.kind for e in with_playbook.principle_errors}
    without = check_answer(case, sourced)
    assert "fabricated_metric" in {e.kind for e in without.principle_errors}


def test_fabricated_metric_conversion_tolerance():
    case = _case("write-false-success")
    ok = check_answer(case, "结论：落盘失败，假成功，体积约 15.7KB。",
                      extra_sources=[PLAYBOOK_TEXT])
    assert "fabricated_metric" not in {e.kind for e in ok.principle_errors}
    bad = check_answer(case, "结论：落盘失败，假成功，体积 20KB。",
                       extra_sources=[PLAYBOOK_TEXT])
    assert "fabricated_metric" in {e.kind for e in bad.principle_errors}


def test_fabricated_metric_proposal_context_exempt():
    case = _case("write-false-success")
    answer = "结论：假成功，未落盘。建议改用分段写入，每段 4KB，逐段回读核验。"
    result = check_answer(case, answer)
    assert "fabricated_metric" not in {e.kind for e in result.principle_errors}


def test_fabricated_metric_truly_invented_still_flagged():
    case = _case("write-false-success")
    answer = "结论：假成功，未落盘。整个过程耗时 99 秒。"
    result = check_answer(case, answer, extra_sources=[PLAYBOOK_TEXT])
    assert "fabricated_metric" in {e.kind for e in result.principle_errors}


# ---------------------------------------------------------- 仪表修复 A2

SEMANTIC_YAML = """
id: semantic-case
title: 语义合成案例
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


def test_semantic_fallback_counts_as_hit(tmp_path):
    """测法升级（li 拍板 (a)）：同义组全中即命中，不再只作 certainty
    去脆门——取代阶段 4 初版「semantic 只去脆不算命中」的旧断言。"""
    p = tmp_path / "semantic.yaml"
    p.write_text(SEMANTIC_YAML, encoding="utf-8")
    case = load_case(p)
    # 关键词组未中（无「没读到/未加载/配置」），但同义组全中
    answer = "结论：已证实，根因是服务进程启动时变量未生效，读不到。"
    result = check_answer(case, answer)
    assert result.conclusion_hit is True
    assert result.hit_via.startswith("semantic")
    assert result.semantic_hit is True
    assert "certainty_without_evidence" not in {
        e.kind for e in result.principle_errors}


def test_certainty_still_flagged_when_semantic_miss(tmp_path):
    p = tmp_path / "semantic.yaml"
    p.write_text(SEMANTIC_YAML, encoding="utf-8")
    case = load_case(p)
    answer = "结论：已证实，根因就是代码写错了，跟环境无关。"
    result = check_answer(case, answer)
    assert result.semantic_hit is False
    assert "certainty_without_evidence" in {
        e.kind for e in result.principle_errors}


# ---------------------------------------------------------- 提案状态机

def _proposal(**overrides) -> ChangeProposal:
    base = dict(id="prop-test-001", title="测试提案", target_files=["x.md"],
                diff="diff", change_class="lesson",
                evidence_signal_ids=["sig-1"], attribution={},
                expected_effect="改善覆盖", expected_side_effect="可能变长")
    base.update(overrides)
    return ChangeProposal(**base)


def test_proposal_state_machine():
    lesson = _proposal()
    with pytest.raises(ValueError, match="绕过回归门"):
        lesson.transition("merged")
    lesson.transition("regression_passed")
    lesson.transition("merged")
    assert lesson.status == "merged"
    with pytest.raises(ValueError, match="非法状态迁移"):
        lesson.transition("proposed")

    rejected = _proposal(id="prop-test-002")
    rejected.transition("rejected")
    with pytest.raises(ValueError, match="非法状态迁移"):
        rejected.transition("merged")

    locked = _proposal(id="prop-test-003", change_class="locked")
    locked.transition("pending_li")
    assert locked.status == "pending_li"

    infra = _proposal(id="prop-test-004", change_class="infra")
    infra.transition("merged")          # infra 允许 proposed → merged
    assert infra.status == "merged"


def test_proposal_requires_evidence_and_side_effect():
    with pytest.raises(ValueError, match="无证据不许提"):
        _proposal(evidence_signal_ids=[]).validate()
    with pytest.raises(ValueError, match="副作用必填"):
        _proposal(expected_side_effect=" ").validate()


def test_apply_edits_anchor_must_exist():
    edit = Edit(file="a.md", old="不存在的锚点", new="x")
    with pytest.raises(ValueError, match="拒应用"):
        apply_edits("正文", [edit], "a.md")
    ok = Edit(file="a.md", old="正文", new="正文加料")
    assert apply_edits("正文", [ok], "a.md") == "正文加料"


# ---------------------------------------------------------- 整轮判定

def _score(case_id, hit=True, covered=3, total=3, errors=0,
           status="completed"):
    return CaseScore(case_id=case_id, status=status, hit=hit,
                     covered=covered, total=total, principle_errors=errors)


def test_compare_case_scores_regressions():
    base = [_score("a"), _score("b", covered=2, total=4)]
    same = [_score("a"), _score("b", covered=2, total=4)]
    assert compare_case_scores(base, same) == []
    worse_hit = [_score("a", hit=False), _score("b", covered=2, total=4)]
    assert any("由真变假" in r for r in compare_case_scores(base, worse_hit))
    worse_cov = [_score("a"), _score("b", covered=1, total=4)]
    assert any("覆盖下降" in r for r in compare_case_scores(base, worse_cov))
    worse_err = [_score("a", errors=1), _score("b", covered=2, total=4)]
    assert any("原则性错误增多" in r
               for r in compare_case_scores(base, worse_err))


# ---------------------------------------------------------- 生成器

def _seed_store(path: Path) -> SignalStore:
    store = SignalStore(path)
    v = "h-test"
    store.append(Signal(
        kind="correction", source="shadow-replay", harness_version=v,
        content="回放对照产生的 correction 候选：原则性错误（fabricated_metric）",
        reference="参照解出处：合成", trace_id="r1",
        extra={"case_id": "write-false-success"}))
    store.append(Signal(
        kind="correction", source="shadow-replay", harness_version=v,
        content="回放对照产生的 correction 候选：原则性错误"
                "（certainty_without_evidence）；根因判断未命中参照解",
        reference="参照解出处：合成", trace_id="r2",
        extra={"case_id": "ec2-node-version-too-old"}))
    store.append(Signal(
        kind="judge", source="shadow-judge", harness_version=v,
        content="judge 判定 hit=False", reference="合成", trace_id="r1",
        extra={"case_id": "write-false-success"}))
    for hit, cov, rid in ((False, "0/4", "r3"), (True, "2/4", "r4")):
        store.append(Signal(
            kind="outcome", source="shadow-replay", harness_version=v,
            content=f"回放对照：结论命中={hit}，关键动作覆盖 {cov}，"
                    "原则性错误 0 条",
            reference="参照解出处：合成", trace_id=rid,
            extra={"case_id": "telegram-hook-log-array"}))
    return store


def test_generator_attribution_rules(tmp_path):
    signals = _seed_store(tmp_path / "signals.jsonl")
    proposals = ProposalStore(tmp_path / "proposals")
    result = generate_proposals(signals, proposals, harness_version="h-test")
    classes = {p.change_class for p in result.created}
    assert classes == {"infra", "lesson"}
    infra = [p for p in result.created if p.change_class == "infra"]
    assert len(infra) == 2
    lesson = [p for p in result.created if p.change_class == "lesson"][0]
    assert lesson.edits and "playbooks/troubleshooting.md" in \
        lesson.target_files[0]
    assert result.ignored_judge_signals == 1
    # judge 信号不作任何提案证据
    for p in result.created:
        assert all(not s.startswith("sig-judge")
                   for s in p.evidence_signal_ids)
    # 幂等：第二遍不再生成
    again = generate_proposals(signals, proposals, harness_version="h-test")
    assert again.created == []


# ---------------------------------------------------------- iterate 回路

def _crafted_answer(case) -> str:  # noqa: ANN001
    parts = ["结论（推断）：根因涉及"]
    parts += [g[0] for g in case.root_cause_groups]
    parts.append("。建议动作：")
    parts += [a.keywords[0] for a in case.key_actions if a.keywords]
    return "".join(parts)


def _setup_iter_env(tmp_path: Path):
    harness_copy = tmp_path / "harness"
    shutil.copytree(HARNESS_DIR, harness_copy)
    eval_dir = tmp_path / "evaluation"
    (eval_dir / "signals").mkdir(parents=True)
    version = compute_harness_version(harness_copy)
    store = SignalStore(eval_dir / "signals" / "signals.jsonl")
    for hit, cov, rid in ((False, "0/4", "seed-r1"), (True, "2/4", "seed-r2")):
        store.append(Signal(
            kind="outcome", source="shadow-replay", harness_version=version,
            content=f"回放对照：结论命中={hit}，关键动作覆盖 {cov}，"
                    "原则性错误 0 条",
            reference="参照解出处：种子信号（mechanics 测试）",
            trace_id=rid, extra={"case_id": "telegram-hook-log-array"}))
    config = Config(db_path=tmp_path / "base.db", harness_dir=harness_copy,
                    model="mock", harness_version=version)
    return harness_copy, eval_dir, config


def test_iterate_mechanics_merge_on_pass(tmp_path):
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
        config=config, unit_factory=factory, round_id="t1",
        apply_merge=True, model_label="mock"))
    assert result.verdict == "passed"
    assert result.merged_files == ["playbooks/troubleshooting.md"]
    merged_text = (harness_copy / "playbooks" / "troubleshooting.md"
                   ).read_text(encoding="utf-8")
    assert "可执行粒度" in merged_text
    backup = (eval_dir / "iterations" / "t1" / "pre_merge_backup"
              / "playbooks" / "troubleshooting.md")
    assert backup.exists() and "可执行粒度" not in backup.read_text(
        encoding="utf-8")
    statuses = {p.change_class: p.status for p in result.proposals}
    assert statuses["lesson"] == "merged"
    # 基线答复已存档（后续轮次可离线重判）
    assert (eval_dir / "iterations" / "t1" / "answers-baseline.json").exists()
    assert (eval_dir / "iterations" / "t1" / "iteration.json").exists()


def test_iterate_mechanics_reject_on_regression(tmp_path):
    harness_copy, eval_dir, config = _setup_iter_env(tmp_path)
    cases = load_cases(CASES_DIR)

    def factory(case, assembled):  # noqa: ANN001
        if "可执行粒度" in assembled.playbook_full:
            text = "结论：未证实，材料不足，无法判断根因。"
        else:
            text = _crafted_answer(case)
        return MockUnit("shadow-solver",
                        UnitResult(text=text, tokens_in=10, tokens_out=10),
                        model="mock",
                        instruction=assembled.render_instruction())

    result = asyncio.run(run_iteration(
        cases=cases, harness_dir=harness_copy, eval_dir=eval_dir,
        config=config, unit_factory=factory, round_id="t2",
        apply_merge=True, model_label="mock"))
    assert result.verdict == "rejected"
    assert result.regressions
    assert result.merged_files == []
    playbook = (harness_copy / "playbooks" / "troubleshooting.md").read_text(
        encoding="utf-8")
    assert "可执行粒度" not in playbook
    lesson = [p for p in result.proposals if p.change_class == "lesson"][0]
    assert lesson.status == "rejected"
    # 拒绝路径也要落盘轮次记录（曾因提前 return 漏写，回归锁）
    assert (eval_dir / "iterations" / "t2" / "iteration.json").exists()
