"""离线重判（阶段 4 测法升级 · li 拍板 (a)+(c)）：先解决测不准，
拒绝门槛不动。

用途：判定仪表修订后，用**当前**仪表对某轮存档的答复全文
（answers-baseline.json / answers-candidate.json）重新判定——不调
模型、零花费。两侧用同一把尺子、同样的组装材料来源重构
（基线侧用 harness 正本、候选侧用轮次目录里的 harness_work 工作
副本，与实跑时 run_shadow 的 extra_sources 构造方式一致），保证
对照公平。samples>1 的轮次答复存为列表，按 iterate 的聚合口径
（严格多数/中位数/任一轮原则错误即记）先聚合再对照。

纪律：重判只出结论，不推翻原轮判定、不合入任何改动。原判定与
重判的关系写进 rejudge-<round>.md：重判后无退步 → 原拒绝的证据
基础不成立，建议恢复提案待 li 确认；仍有退步 → 拒绝成立。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..harness.assembler import assemble
from ..harness.loader import load_harness
from ..harness.memory import MemoryIndex
from .iterate import (CaseScore, aggregate_case_scores,
                      compare_case_scores)
from .regression import RegressionCase, check_answer, load_cases


@dataclass
class SideRejudge:
    case_id: str
    original: CaseScore | None = None
    rejudged: CaseScore | None = None
    hit_vias: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        o, r = self.original, self.rejudged
        if o is None or r is None:
            return o is not r
        return (o.hit, o.covered, o.principle_errors) != \
               (r.hit, r.covered, r.principle_errors)


@dataclass
class RejudgeResult:
    round_id: str
    round_dir: Path
    baseline: list[SideRejudge] = field(default_factory=list)
    candidate: list[SideRejudge] = field(default_factory=list)
    original_verdict: str = ""
    original_regressions: list[str] = field(default_factory=list)
    rejudged_regressions: list[str] = field(default_factory=list)
    item_conclusions: list[str] = field(default_factory=list)
    verdict_conclusion: str = ""
    signal_notes: list[str] = field(default_factory=list)


def _extra_sources(harness_root: Path, case: RegressionCase) -> list[str]:
    """按实跑同法重构组装材料（编造核查的合法来源面）。"""
    bundle = load_harness(harness_root, case.scenario)
    memory_index = MemoryIndex(bundle.root / "memory")
    assembled = assemble(bundle, query=f"{case.title}\n{case.brief}",
                         memory_index=memory_index)
    return [assembled.playbook_full, assembled.core_memory,
            *(e.render() for e in assembled.memory_hits)]


def _load_answers(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for case_id, value in raw.items():
        out[case_id] = [value] if isinstance(value, str) \
            else [str(v) for v in value]
    return out


def _score_from_dict(d: dict[str, Any]) -> CaseScore:
    return CaseScore(case_id=d["case_id"], status=d.get("status", ""),
                     hit=bool(d.get("hit")), covered=int(d.get("covered", 0)),
                     total=int(d.get("total", 0)),
                     principle_errors=int(d.get("principle_errors", 0)),
                     cost=float(d.get("cost", 0.0)),
                     run_id=str(d.get("run_id", "")))


def _rejudge_side(*, case: RegressionCase, answers: list[str],
                  harness_root: Path,
                  original: CaseScore | None) -> SideRejudge:
    side = SideRejudge(case_id=case.id, original=original)
    if original is not None and original.status != "completed":
        side.rejudged = CaseScore(case_id=case.id,
                                  status=original.status,
                                  total=original.total)
        return side
    if not answers:
        side.rejudged = CaseScore(
            case_id=case.id, status="error",
            total=original.total if original else len(case.key_actions))
        return side
    sources = _extra_sources(harness_root, case)
    per_sample: list[CaseScore] = []
    for answer in answers:
        check = check_answer(case, answer, extra_sources=sources)
        per_sample.append(CaseScore(
            case_id=case.id, status="completed",
            hit=check.conclusion_hit,
            covered=len(check.actions_covered),
            total=check.actions_total,
            principle_errors=len(check.principle_errors)))
        side.hit_vias.append(check.hit_via)
    side.rejudged = aggregate_case_scores(per_sample)
    if original is not None:
        side.rejudged.cost = original.cost
        side.rejudged.run_id = original.run_id
    return side


def rejudge_round(round_dir: Path, *, cases_dir: Path,
                  harness_dir: Path) -> RejudgeResult:
    """对一轮存档答复做对称离线重判（零模型调用）。"""
    round_dir = Path(round_dir)
    iteration = json.loads(
        (round_dir / "iteration.json").read_text(encoding="utf-8"))
    result = RejudgeResult(
        round_id=str(iteration.get("round_id", round_dir.name)),
        round_dir=round_dir,
        original_verdict=str(iteration.get("verdict", "")),
        original_regressions=list(iteration.get("regressions", [])))
    cases = {c.id: c for c in load_cases(Path(cases_dir))}
    work_root = round_dir / "harness_work"

    sides: dict[str, tuple[Path, str]] = {
        "baseline": (Path(harness_dir), "answers-baseline.json"),
        "candidate": (work_root, "answers-candidate.json"),
    }
    for tag, (harness_root, answers_name) in sides.items():
        answers_path = round_dir / answers_name
        if not answers_path.exists():
            continue
        if tag == "candidate" and not harness_root.exists():
            raise FileNotFoundError(
                f"候选侧工作副本不存在（{harness_root}），无法按实跑"
                "口径重构组装材料，拒绝重判")
        answers = _load_answers(answers_path)
        originals = {s["case_id"]: _score_from_dict(s)
                     for s in iteration.get(tag, [])}
        order = [s["case_id"] for s in iteration.get(tag, [])] or \
            sorted(answers)
        rejudged_sides: list[SideRejudge] = []
        for case_id in order:
            case = cases.get(case_id)
            if case is None:
                continue
            rejudged_sides.append(_rejudge_side(
                case=case, answers=answers.get(case_id, []),
                harness_root=harness_root,
                original=originals.get(case_id)))
        setattr(result, tag, rejudged_sides)

    base_scores = [s.rejudged for s in result.baseline if s.rejudged]
    cand_scores = [s.rejudged for s in result.candidate if s.rejudged]
    if base_scores and cand_scores:
        result.rejudged_regressions = compare_case_scores(
            base_scores, cand_scores)

    _conclude(result)
    _dispose_signals(result)
    return result


# ---------------------------------------------------------------- 结论

def _fmt(score: CaseScore | None) -> str:
    if score is None:
        return "—"
    if score.status != "completed":
        return score.status
    return f"{'✅' if score.hit else '❌'}/{score.covered}/" \
           f"{score.principle_errors}"


def _item_verdict(result: RejudgeResult, case_id: str,
                  kind: str) -> str:
    """逐项回答原退步项在重判下是否还成立（kind: hit / coverage）。"""
    orig_hit = any(case_id in r for r in result.original_regressions)
    still = any(case_id in r for r in result.rejudged_regressions)
    if not orig_hit:
        return f"{case_id}：原判定无此退步项。"
    if still:
        return (f"{case_id}：退步**仍成立**——重判后该项退步未消除"
                f"（详见对照表）。")
    return (f"{case_id}：退步**不成立**——重判后该项退步消除，"
            f"原判系判定仪表漏接（详见对照表）。")


def _conclude(result: RejudgeResult) -> None:
    for case_id, kind in (("github-app-no-create-permission", "hit"),
                          ("telegram-hook-log-array", "coverage")):
        result.item_conclusions.append(_item_verdict(result, case_id,
                                                     kind))
    if not result.rejudged_regressions:
        result.verdict_conclusion = (
            "原拒绝的证据基础不成立：重判后无任何退步项，"
            "prop-lesson-001 的整轮拒绝失去依据——**建议恢复提案，"
            "待 li 确认后合入**。本重判不自行推翻原判定、不合入"
            "任何改动。")
    else:
        result.verdict_conclusion = (
            "拒绝成立：重判后仍有退步项（"
            + "；".join(result.rejudged_regressions)
            + "），prop-lesson-001 维持 rejected。原判定中已被重判"
              "消除的退步项如上逐项注明，不改变整轮结论。")


def _dispose_signals(result: RejudgeResult) -> None:
    """疑似误判信号的处置随重判结论走（只追加注记，不动原记录）。"""
    github = next((s for s in result.candidate
                   if s.case_id == "github-app-no-create-permission"),
                  None)
    if github is None or github.rejudged is None \
            or not github.rejudged.hit:
        return
    note = ("sig-78a40fd166（signals-candidate.jsonl 中源于 github 案"
            "候选答复「未命中」的 correction 候选）：重判后该答复为"
            "命中（语义兜底），其生成前提不成立——**经重判否定**，"
            "不得再作提案证据。")
    result.signal_notes.append(note)
    notes_path = result.round_dir / "DIAGNOSIS_NOTES.md"
    existing = notes_path.read_text(encoding="utf-8") \
        if notes_path.exists() else ""
    if "经重判否定" not in existing:
        with notes_path.open("a", encoding="utf-8") as fh:
            fh.write("\n## 重判处置（离线重判 rejudge 追加）\n\n"
                     f"- {note}\n")


# ---------------------------------------------------------------- 报告

def render_rejudge_report(result: RejudgeResult, *,
                          generated_at: str) -> str:
    lines: list[str] = []
    lines.append(f"# 离线重判报告（轮次 {result.round_id}）")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append("- 方式：当前判定仪表对存档答复全文对称重判，"
                 "不调模型、零花费；两侧组装材料按实跑同法重构")
    lines.append("- 仪表变更：根因命中判定加全答复语义兜底"
                 "（结论段优先、兜底同义组全中才算命中，严格度"
                 "不变）；编造/未核实断言两类原则错误口径不变")
    lines.append(f"- 原整轮判定：**{result.original_verdict}**；"
                 f"原退步项 {len(result.original_regressions)} 条 → "
                 f"重判后 {len(result.rejudged_regressions)} 条")
    lines.append("")
    lines.append("## 对照表（原判 → 重判，格式：命中/覆盖数/原则错误）")
    lines.append("")
    lines.append("| 案例 | 基线 原判 → 重判 | 候选 原判 → 重判 |")
    lines.append("|---|---|---|")
    cand = {s.case_id: s for s in result.candidate}
    for b in result.baseline:
        c = cand.get(b.case_id)
        brow = f"{_fmt(b.original)} → {_fmt(b.rejudged)}"
        crow = (f"{_fmt(c.original)} → {_fmt(c.rejudged)}"
                if c else "—")
        if b.changed or (c and c.changed):
            lines.append(f"| **{b.case_id}** | {brow} | {crow} |")
        else:
            lines.append(f"| {b.case_id} | {brow} | {crow} |")
    lines.append("")
    lines.append("## 两处退步逐项结论")
    lines.append("")
    for item in result.item_conclusions:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 与原整轮判定的关系")
    lines.append("")
    lines.append(f"- {result.verdict_conclusion}")
    vias = [(s.case_id, v) for s in (*result.baseline, *result.candidate)
            for v in s.hit_vias if v.startswith("semantic")]
    if vias:
        lines.append("- 本轮有答复经语义兜底命中（路径："
                     + "；".join(f"{cid} {v}" for cid, v in vias)
                     + "），均为关键词主口径差一组未中、同义组全中。")
    lines.append("")
    if result.signal_notes:
        lines.append("## 信号处置")
        lines.append("")
        for note in result.signal_notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("## 纪律")
    lines.append("")
    lines.append("- 本重判只出结论：不推翻原轮 iteration.json 的判定"
                 "记录、不合入任何驾驭层改动；恢复提案须 li 确认。")
    lines.append("")
    return "\n".join(lines)
