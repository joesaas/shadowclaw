"""迭代回路（评估层 Step 6 对照与合入门，阶段 4 转满第一轮）。

流程：基线跑回归集（当前 harness）→ 生成提案（proposals.py）→
lesson 级提案应用到工作副本 → 候选版再跑 → 分项对比 → 整轮判定 →
合入或整轮拒绝。本模块不参与实时任务执行（设计 Step 1）。

整轮判定（本轮口径，任务定版）：逐案例比基线与候选——命中由真变
假、动作覆盖下降、原则性错误增多，任一成立即该案例退步；任一案例
退步即整轮拒绝并回滚（工作副本不合入）。全不退步才允许合入。

分级处置：infra 提案是评估层工具链改动，已随代码落地，不走驾驭层
合入（状态直接 merged 并注明）；structural / locked 只生成、列入
报告等 li 批准（pending_li），永不自动应用；lesson 过门自动合入。

judge 纪律（设计 Step 7）：judge 与硬查一致率 67% < 70%，本轮权重
归零——迭代运行 judge_fn=None 不调用、不判定，报告中明示。

答复存档：每轮把基线/候选的逐案答复全文存进轮次目录（阶段 3 的
教训：轨迹只存 200 字预览，答复不落盘就无法离线重判）。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..execution.config import Config
from ..harness.loader import load_harness
from .proposals import (ChangeProposal, ProposalStore, apply_edits,
                        generate_proposals, render_unified_diff)
from .regression import RegressionCase
from .shadow import ShadowRun, UnitFactory, run_shadow
from .signals import SignalStore


@dataclass
class CaseScore:
    case_id: str
    status: str
    hit: bool = False
    covered: int = 0
    total: int = 0
    principle_errors: int = 0
    cost: float = 0.0
    run_id: str = ""

    @property
    def coverage(self) -> float:
        return self.covered / self.total if self.total else 1.0


@dataclass
class IterationResult:
    round_id: str
    harness_version: str
    baseline: list[CaseScore] = field(default_factory=list)
    candidate: list[CaseScore] | None = None
    regressions: list[str] = field(default_factory=list)
    verdict: str = ""                    # passed / rejected / no_candidates
    proposals: list[ChangeProposal] = field(default_factory=list)
    generation: dict[str, Any] = field(default_factory=dict)
    merged_files: list[str] = field(default_factory=list)
    baseline_cost: float = 0.0
    candidate_cost: float = 0.0
    stopped_reason: str = ""
    model_label: str = ""


def score_run(run: ShadowRun) -> list[CaseScore]:
    out: list[CaseScore] = []
    for cr in run.cases:
        score = CaseScore(case_id=cr.case_id, status=cr.status,
                          cost=cr.cost, run_id=cr.run_id)
        if cr.status == "completed" and cr.check is not None:
            score.hit = cr.check.conclusion_hit
            score.covered = len(cr.check.actions_covered)
            score.total = cr.check.actions_total
            score.principle_errors = len(cr.check.principle_errors)
        out.append(score)
    return out


def compare_case_scores(baseline: list[CaseScore],
                        candidate: list[CaseScore]) -> list[str]:
    """逐案退步判定（整轮判定的唯一口径，见模块 docstring）。"""
    regressions: list[str] = []
    cand = {c.case_id: c for c in candidate}
    for base in baseline:
        new = cand.get(base.case_id)
        if new is None or new.status != "completed":
            regressions.append(f"{base.case_id}：候选版未跑完"
                               f"（{new.status if new else '缺失'}）")
            continue
        if base.status != "completed":
            continue          # 基线就没跑成的案例不作退步分母
        if base.hit and not new.hit:
            regressions.append(f"{base.case_id}：结论命中由真变假")
        if new.covered < base.covered:
            regressions.append(
                f"{base.case_id}：动作覆盖下降 "
                f"{base.covered}/{base.total} → {new.covered}/{new.total}")
        if new.principle_errors > base.principle_errors:
            regressions.append(
                f"{base.case_id}：原则性错误增多 "
                f"{base.principle_errors} → {new.principle_errors}")
    return regressions


def _save_answers(path: Path, run: ShadowRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {cr.case_id: cr.answer for cr in run.cases},
        ensure_ascii=False, indent=1), encoding="utf-8")


async def _run_pass(*, cases: list[RegressionCase], harness_root: Path,
                    config: Config, round_dir: Path, tag: str,
                    unit_factory: UnitFactory) -> ShadowRun:
    bundle = load_harness(harness_root, "troubleshooting")
    run_config = replace(config, db_path=round_dir / "trajectory.db")
    store = SignalStore(round_dir / f"signals-{tag}.jsonl")
    run = await run_shadow(cases=cases, bundle=bundle, config=run_config,
                           signal_store=store, unit_factory=unit_factory,
                           judge_fn=None)   # judge 权重归零：不调用
    _save_answers(round_dir / f"answers-{tag}.json", run)
    return run


def _apply_lesson_proposals(work_root: Path,
                              proposals: list[ChangeProposal]) -> None:
    """把 lesson 提案的 edits 应用到工作副本，并渲染 diff 存档。"""
    for proposal in proposals:
        by_file: dict[str, list] = {}
        for edit in proposal.edits:
            by_file.setdefault(edit.file, []).append(edit)
        for rel, edits in by_file.items():
            path = work_root / rel
            original = path.read_text(encoding="utf-8")
            updated = apply_edits(original, edits, rel)
            path.write_text(updated, encoding="utf-8")
            proposal.diff = (proposal.diff + render_unified_diff(
                f"harness/{rel}", original, updated)).strip()


def _merge_back(work_root: Path, harness_dir: Path,
                files: list[str], backup_dir: Path) -> list[str]:
    """合入：先把现行文件备份进轮次目录（回滚凭据），再覆盖。"""
    merged: list[str] = []
    for rel in files:
        src = work_root / rel
        dst = harness_dir / rel
        backup = backup_dir / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.copy2(dst, backup)
        shutil.copy2(src, dst)
        merged.append(rel)
    return merged


def persist_round(result: IterationResult, round_dir: Path) -> None:
    (round_dir / "iteration.json").write_text(json.dumps({
        "round_id": result.round_id,
        "harness_version": result.harness_version,
        "model": result.model_label,
        "verdict": result.verdict,
        "baseline": [asdict(s) for s in result.baseline],
        "candidate": [asdict(s) for s in result.candidate or []],
        "regressions": result.regressions,
        "merged_files": result.merged_files,
        "baseline_cost": result.baseline_cost,
        "candidate_cost": result.candidate_cost,
        "stopped_reason": result.stopped_reason,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_iteration(*, cases: list[RegressionCase],
                        harness_dir: Path, eval_dir: Path,
                        config: Config, unit_factory: UnitFactory,
                        round_id: str = "001",
                        apply_merge: bool = True,
                        model_label: str = "") -> IterationResult:
    """转满一轮：基线 → 提案 → 候选 → 判定 → 合入/拒绝。"""
    harness_dir = Path(harness_dir)
    eval_dir = Path(eval_dir)
    round_dir = eval_dir / "iterations" / round_id
    round_dir.mkdir(parents=True, exist_ok=True)
    result = IterationResult(round_id=round_id,
                             harness_version=config.harness_version,
                             model_label=model_label)

    # ---- 基线（当前 harness，judge 不调用）
    baseline_run = await _run_pass(
        cases=cases, harness_root=harness_dir, config=config,
        round_dir=round_dir, tag="baseline", unit_factory=unit_factory)
    result.baseline = score_run(baseline_run)
    result.baseline_cost = sum(c.cost for c in result.baseline)
    if baseline_run.stopped_reason:
        result.stopped_reason = baseline_run.stopped_reason
        result.verdict = "rejected"
        result.regressions.append(
            f"基线运行中途停止：{baseline_run.stopped_reason}")
        persist_round(result, round_dir)
        return result

    # ---- 提案生成（主信号账本；infra/locked 在此分级处置）
    main_store = SignalStore(eval_dir / "signals" / "signals.jsonl")
    proposal_store = ProposalStore(eval_dir / "proposals")
    generation = generate_proposals(
        main_store, proposal_store,
        harness_version=baseline_run.harness_version)
    result.generation = {
        "created": [p.id for p in generation.created],
        "ignored_judge_signals": generation.ignored_judge_signals,
        "recorded_only": generation.recorded_only,
        "dispositions": generation.dispositions,
    }
    result.proposals = generation.created
    lesson_proposals: list[ChangeProposal] = []
    for proposal in generation.created:
        if proposal.change_class == "infra":
            proposal.transition(
                "merged", note="评估层工具链改动，已随本轮代码落地，"
                               "不走驾驭层合入（单列 infra 口径）")
            proposal_store.save(proposal)
        elif proposal.change_class in ("structural", "locked"):
            proposal.transition(
                "pending_li", note="结构/锁定级：只生成，等 li 批准，"
                                   "本轮不自动应用（设计 Step 6.3）")
            proposal_store.save(proposal)
        else:
            lesson_proposals.append(proposal)

    if not lesson_proposals:
        result.verdict = "no_candidates"
        persist_round(result, round_dir)
        return result

    # ---- 工作副本 + 应用 lesson 提案
    work_root = round_dir / "harness_work"
    if work_root.exists():
        shutil.rmtree(work_root)
    shutil.copytree(harness_dir, work_root)
    _apply_lesson_proposals(work_root, lesson_proposals)
    for proposal in lesson_proposals:
        proposal_store.save(proposal)

    # ---- 候选版再跑
    candidate_run = await _run_pass(
        cases=cases, harness_root=work_root, config=config,
        round_dir=round_dir, tag="candidate", unit_factory=unit_factory)
    result.candidate = score_run(candidate_run)
    result.candidate_cost = sum(c.cost for c in result.candidate)
    if candidate_run.stopped_reason:
        result.stopped_reason = candidate_run.stopped_reason

    # ---- 整轮判定
    result.regressions = compare_case_scores(result.baseline,
                                             result.candidate)
    if result.regressions:
        result.verdict = "rejected"
        for proposal in lesson_proposals:
            proposal.transition(
                "rejected",
                note="整轮判定拒绝（负教训存档）："
                     + "；".join(result.regressions)
                     + "。同类改动再次提炼时须先回应本条被拒原因"
                       "（设计 Step 6.5）。")
            proposal_store.save(proposal)
        persist_round(result, round_dir)
        return result

    result.verdict = "passed"
    changed_files = sorted({edit.file for p in lesson_proposals
                            for edit in p.edits})
    for proposal in lesson_proposals:
        proposal.transition("regression_passed",
                            note="分项对比无退步，整轮判定通过")
    if apply_merge:
        merged = _merge_back(work_root, harness_dir, changed_files,
                             round_dir / "pre_merge_backup")
        result.merged_files = merged
        for proposal in lesson_proposals:
            proposal.merged_files = merged
            proposal.transition("merged",
                                note="已合入 harness 正本；回滚凭据在"
                                     f" iterations/{round_id}/"
                                     "pre_merge_backup/")
            proposal_store.save(proposal)
    else:
        for proposal in lesson_proposals:
            proposal.notes = (proposal.notes +
                              "\n未合入：本轮为离线演练（apply_merge="
                              "False），只验回路 mechanics。").strip()
            proposal_store.save(proposal)
    persist_round(result, round_dir)
    return result


# ---------------------------------------------------------------- 报告

def render_iteration_report(result: IterationResult, *,
                              signal_counts: dict[str, int],
                              generated_at: str) -> str:
    """迭代报告（设计 Step 8 固定结构，一页为限）。"""
    lines: list[str] = []
    lines.append(f"# 迭代报告 {result.round_id}（阶段 4 · 第一轮）")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 驾驭层版本：{result.harness_version}（手册改动不进"
                 "版本号哈希——已知缺口，迭代前后靠轮次目录区分）")
    lines.append(f"- 模型：{result.model_label or 'mock'}；整轮判定："
                 f"**{result.verdict}**")
    lines.append("")
    lines.append("## 信号账")
    lines.append("")
    total = sum(signal_counts.values())
    lines.append(f"- 存量：共 {total} 条（"
                 + "、".join(f"{k} {v}" for k, v in
                             sorted(signal_counts.items())) + "）")
    lines.append(f"- 本轮生成器处置：新提案 "
                 f"{len(result.generation.get('created', []))} 条；"
                 f"judge 信号 {result.generation.get('ignored_judge_signals', 0)}"
                 " 条按权重归零只计数、未作证据；只记录不提改动的 "
                 f"{len(result.generation.get('recorded_only', []))} 项"
                 "（见提案目录 dispositions.json）")
    lines.append("- 本轮基线/候选两跑的信号写在轮次目录内（iterations/"
                 f"{result.round_id}/），不混入主账本")
    lines.append("")
    lines.append("## 判定仪表修复（infra，随本轮代码落地）")
    lines.append("")
    lines.append("- 编造核查：合法来源扩到全部组装材料（简报+材料+手册"
                 "全文+core 记忆），数值按 SI 换算同族比对（相对误差 "
                 "≤5%），建议性语境数值豁免")
    lines.append("- certainty 去脆：关键词未中且结论段语义核查"
                 "（semantic_groups 规则同义组）也未中才成立；命中口径"
                 "本身不放松")
    lines.append("- 离线重判：阶段 3 第二轮答复全文未落盘（轨迹只存 "
                 "200 字预览），无法离线重判——三条误报是否消除以下方"
                 "基线实跑的新仪表结果为准；本轮起答复全文按轮次存档，"
                 "后续轮次可离线重判")
    lines.append("")
    lines.append("## 提案清单")
    lines.append("")
    if not result.proposals:
        lines.append("本轮无新提案。")
    for p in result.proposals:
        lines.append(f"- `{p.id}` [{p.change_class}] {p.title} → "
                     f"**{p.status}**（证据信号 "
                     f"{len(p.evidence_signal_ids)} 条）")
    lines.append("")
    lines.append("## 回归对照（分项，不看总平均）")
    lines.append("")
    lines.append("| 案例 | 基线 命中/覆盖/原则错误 | 候选 命中/覆盖/原则错误 |")
    lines.append("|---|---|---|")
    cand = {c.case_id: c for c in result.candidate or []}
    for b in result.baseline:
        c = cand.get(b.case_id)
        brow = f"{'✅' if b.hit else '❌'}/{b.covered}/{b.principle_errors}"
        crow = (f"{'✅' if c.hit else '❌'}/{c.covered}/{c.principle_errors}"
                if c else "—")
        lines.append(f"| {b.case_id} | {brow} | {crow} |")
    lines.append(f"- 花费：基线 ${result.baseline_cost:.4f} → 候选 "
                 f"${result.candidate_cost:.4f}（估算口径，价格表）")
    if result.regressions:
        lines.append("- 退步项：" + "；".join(result.regressions))
    else:
        lines.append("- 退步项：无")
    lines.append("")
    lines.append("## 合入与待批")
    lines.append("")
    if result.merged_files:
        lines.append(f"- 已合入 harness 正本：{result.merged_files}"
                     f"（回滚凭据 iterations/{result.round_id}/"
                     "pre_merge_backup/）")
    else:
        lines.append("- 本轮无驾驭层文件合入")
    pending = [p for p in result.proposals if p.status == "pending_li"]
    if pending:
        for p in pending:
            lines.append(f"- 待 li 批准：`{p.id}` {p.title}")
    else:
        lines.append("- 待 li 项：无（本轮无 structural / locked 提案——"
                     "账本里没有 li 本人的 correction，锁定级无从提）")
    lines.append("")
    lines.append("## 口径说明")
    lines.append("")
    lines.append("- judge 权重归零：与硬查一致率 67% < 70%（设计 "
                 "Step 7），本轮 judge 未被调用、未参与任何判定，待 "
                 "li 标注校准后再议")
    lines.append("- 开卷口径：回归案例本身就记在排障手册里，回归集测的"
                 "是「手册应用一致性」而非新问题泛化（阶段 3 已定口径）")
    if result.stopped_reason:
        lines.append(f"- ⚠️ 中途停止：{result.stopped_reason}")
    lines.append("")
    return "\n".join(lines)
