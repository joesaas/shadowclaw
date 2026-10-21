"""候选改动提炼（评估层 Step 5 改动契约 + Step 3 归因，阶段 4 落地）。

ChangeProposal 字段按 docs/design-evaluation.md Step 5：目标文件、
diff、class 三级（lesson / structural / locked）+ 本项目单列的 infra
（评估层自身工具链改动，不走驾驭层合入，阶段 4 任务口径）、证据
信号 id、预期效果与副作用（必填）、状态机。

状态机：proposed → regression_passed / rejected → merged / pending_li。
约束：lesson 只能经 regression_passed 到 merged；structural / locked
不自动应用，过门也只到 pending_li 等 li 批准（设计 Step 6.3）；
infra 是工具链改动，由 proposed 直接 merged 并在 notes 里写明理由。

归因纪律（设计 Step 3）：先按 harness_version 切分（跨版本不混算）、
定位到层才提改动、定位不到只记录。生成器规则写死在本模块（v1）：

- R1 编造核查误报：correction 候选内容含 fabricated_metric 的 →
  infra 提案（核查合法来源扩展到全部组装材料 + 单位换算）。
- R2 certainty 误报：correction 候选内容含 certainty_without_evidence
  的 → infra 提案（结论段语义核查去脆 + semantic_groups 数据）。
- R3 持续动作覆盖缺口：同一案例有 ≥2 条 outcome、且最近一轮动作
  覆盖 <60%（执行层偏差口径）的 → lesson 提案（目标文件与替换文本
  由 LESSON_ANCHORS 规则表给；表里没有的案例只记录不提）。
- 其余 correction / outcome：若该案例最近一轮已命中，记为「被后续
  轮次覆盖」；未命中又不落入上列规则的，记为「只记录」（评分口径
  分歧类，命中判定本身不放松）。judge 信号权重归零：只计数，
  不作任何提案的证据（设计 Step 7）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .signals import Signal, SignalStore

PROPOSAL_CLASSES = ("lesson", "structural", "locked", "infra")
PROPOSAL_STATUSES = ("proposed", "regression_passed", "rejected",
                     "merged", "pending_li")

_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"regression_passed", "rejected", "pending_li", "merged"},
    "regression_passed": {"merged", "pending_li"},
    "rejected": set(),
    "merged": set(),
    "pending_li": set(),
}


@dataclass
class Edit:
    """一处可机器应用的精确替换（diff 由它渲染，二者同源不漂移）。"""
    file: str                 # 相对 harness 根目录的路径
    old: str
    new: str


@dataclass
class ChangeProposal:
    id: str
    title: str
    target_files: list[str]
    diff: str                              # 统一 diff 文本（合入存档）
    change_class: str                      # lesson/structural/locked/infra
    evidence_signal_ids: list[str]
    attribution: dict[str, str]            # {phenomenon, layer, file}
    expected_effect: str                   # 预期改善哪个分项（必填）
    expected_side_effect: str              # 可能伤到哪个（必填）
    edits: list[Edit] = field(default_factory=list)
    status: str = "proposed"
    harness_version_base: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    merged_files: list[str] = field(default_factory=list)
    notes: str = ""

    def validate(self) -> None:
        if self.change_class not in PROPOSAL_CLASSES:
            raise ValueError(f"未知改动等级：{self.change_class}")
        if self.status not in PROPOSAL_STATUSES:
            raise ValueError(f"未知提案状态：{self.status}")
        if not self.evidence_signal_ids:
            raise ValueError("无证据不许提：evidence_signal_ids 为空")
        if not self.expected_effect.strip():
            raise ValueError("预期效果必填（设计 Step 5）")
        if not self.expected_side_effect.strip():
            raise ValueError("副作用必填：不许只写好处（设计 Step 5）")
        if not self.target_files:
            raise ValueError("目标文件为空")

    def transition(self, to: str, *, note: str = "") -> None:
        if to not in _TRANSITIONS.get(self.status, set()):
            raise ValueError(
                f"非法状态迁移：{self.status} → {to}（提案 {self.id}）")
        if to == "merged" and self.status == "proposed" \
                and self.change_class != "infra":
            raise ValueError(
                "lesson/structural/locked 不许绕过回归门直接合入")
        self.status = to
        if note:
            self.notes = (self.notes + "\n" + note).strip() if self.notes \
                else note

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChangeProposal":
        d = dict(d)
        d["edits"] = [Edit(**e) for e in d.get("edits", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class ProposalStore:
    """提案落盘：evaluation/proposals/<id>.json + index.json 索引 +
    dispositions.json（信号处置账，保证生成器幂等）。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, proposal_id: str) -> Path:
        return self.root / f"{proposal_id}.json"

    def save(self, proposal: ChangeProposal) -> ChangeProposal:
        proposal.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(proposal.id).write_text(
            json.dumps(proposal.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        self._write_index()
        return proposal

    def load(self, proposal_id: str) -> ChangeProposal:
        return ChangeProposal.from_dict(json.loads(
            self._path(proposal_id).read_text(encoding="utf-8")))

    def list(self) -> list[ChangeProposal]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("prop-*.json")):
            out.append(ChangeProposal.from_dict(
                json.loads(path.read_text(encoding="utf-8"))))
        return out

    def _write_index(self) -> None:
        index = {p.id: {"status": p.status, "class": p.change_class,
                        "title": p.title}
                 for p in self.list()}
        (self.root / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=1),
            encoding="utf-8")

    # -------------------------------------------------- 信号处置账
    def dispositions(self) -> dict[str, str]:
        path = self.root / "dispositions.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def set_dispositions(self, mapping: dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        merged = self.dispositions()
        merged.update(mapping)
        (self.root / "dispositions.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=1),
            encoding="utf-8")


# ---------------------------------------------------------------- 归因

# R3 的规则表：案例 → 手册里可落点的精确替换。表里没有的案例即使
# 命中 R3 条件也只记录不提（不许猜着改）。
_TELEGRAM_OLD = ("教训：链路型系统按段定位，不要盯着表象层；"
                 "破坏性读取的环节，消费前先想好保全与回放。")
LESSON_ANCHORS: dict[str, Edit] = {
    "telegram-hook-log-array": Edit(
        file="playbooks/troubleshooting.md",
        old=_TELEGRAM_OLD,
        new=_TELEGRAM_OLD + "答复与修复建议要写到可执行粒度：点名出错"
            "的那一步与参数，并给出具体改法（如把 log() 的 fields 由"
            "数组改为对象包裹 {count, messages}），不只说「参数类型"
            "不对」。"),
}

_COVERAGE_RE = re.compile(r"关键动作覆盖\s*(\d+)/(\d+)")


@dataclass
class GenerationResult:
    created: list[ChangeProposal] = field(default_factory=list)
    dispositions: dict[str, str] = field(default_factory=dict)
    ignored_judge_signals: int = 0
    recorded_only: list[str] = field(default_factory=list)


def _next_id(store: ProposalStore, prefix: str) -> str:
    existing = {p.id for p in store.list()}
    n = 1
    while f"{prefix}-{n:03d}" in existing:
        n += 1
    return f"{prefix}-{n:03d}"


def _coverage_of(signal: Signal) -> tuple[int, int] | None:
    m = _COVERAGE_RE.search(signal.content)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def generate_proposals(signal_store: SignalStore,
                       proposal_store: ProposalStore, *,
                       harness_version: str) -> GenerationResult:
    """从信号账本提炼本轮提案（规则见模块 docstring，幂等）。"""
    result = GenerationResult()
    done = proposal_store.dispositions()
    signals = [s for s in signal_store.list()
               if s.harness_version == harness_version]
    result.ignored_judge_signals = sum(
        1 for s in signals if s.kind == "judge")
    usable = [s for s in signals
              if s.kind in ("correction", "outcome")
              and s.id not in done]
    corrections = [s for s in usable if s.kind == "correction"]
    outcomes = [s for s in usable if s.kind == "outcome"]

    def evidence_ids(sigs: list[Signal]) -> list[str]:
        return [s.id for s in sigs]

    # ---- R1：fabricated_metric 误报 → infra
    fab = [s for s in corrections if "fabricated_metric" in s.content]
    if fab:
        p = ChangeProposal(
            id=_next_id(proposal_store, "prop-infra"),
            title="判定仪表修复：编造核查的合法来源扩展到全部组装材料"
                  "（含单位换算与建议性语境豁免）",
            target_files=["src/shadowclaw/evaluation/regression.py",
                          "src/shadowclaw/evaluation/shadow.py"],
            diff="（评估层工具链改动，diff 见本轮代码提交："
                 "regression.py 的 check_answer 增加 extra_sources 入参，"
                 "合法数值来源 = 简报+材料+手册全文+core 记忆；带单位"
                 "数值按 SI 换算同族比对、相对误差 ≤5% 视为同源；"
                 "建议性语境中的数值豁免。shadow.py 传入组装材料。）",
            change_class="infra",
            evidence_signal_ids=evidence_ids(fab),
            attribution={"phenomenon": "编造核查把手册来源数值误报为"
                                       "编造数据（write-false-success 案"
                                       " 15.6KB ↔ 材料 15,683 字节）",
                         "layer": "评估层工具链（判定仪表）",
                         "file": "src/shadowclaw/evaluation/regression.py"},
            expected_effect="write-false-success 类案例的 fabricated_metric"
                            "误报消除；真编造（材料全无来源的数值）仍被查出",
            expected_side_effect="合法来源变宽后，真编造若恰与手册中某数值"
                                 "换算后接近（≤5%）会被放过——靠数值必须带"
                                 "单位同族比对收窄此风险",
            harness_version_base=harness_version)
        proposal_store.save(p)
        result.created.append(p)
        for s in fab:
            result.dispositions[s.id] = p.id

    # ---- R2：certainty_without_evidence 误报 → infra
    cert = [s for s in corrections
            if "certainty_without_evidence" in s.content]
    if cert:
        p = ChangeProposal(
            id=_next_id(proposal_store, "prop-infra"),
            title="判定仪表修复：certainty 判定去脆（结论段语义核查门）"
                  "与根因同义表达组数据补齐",
            target_files=["src/shadowclaw/evaluation/regression.py",
                          "evaluation/regression/cases/*.yaml"],
            diff="（评估层工具链改动，diff 见本轮代码提交：certainty "
                 "仅当关键词未中且结论段语义核查（结论段抽取 + 案例 "
                 "scoring.semantic_groups 同义组规则匹配）也未中时成立；"
                 "6 个案例文件新增 semantic_groups 仪表数据，参照标注"
                 "（root_cause / key_actions / root_cause_groups）未动。）",
            change_class="infra",
            evidence_signal_ids=evidence_ids(cert),
            attribution={"phenomenon": "语义已命中根因的答复因关键词组"
                                       "未全中被记 certainty_without_"
                                       "evidence（ec2 / github 案）",
                         "layer": "评估层工具链（判定仪表）",
                         "file": "src/shadowclaw/evaluation/regression.py"},
            expected_effect="ec2 / github 类案例的 certainty 误报消除；"
                            "真未核实断言（语义也未中且确定口吻）仍被记",
            expected_side_effect="语义核查是规则同义组，覆盖不了组外新说法"
                                 "——这类答复仍可能被记 certainty，靠后续"
                                 "轮次扩充 semantic_groups 收敛，不放松命中"
                                 "口径本身",
            harness_version_base=harness_version)
        proposal_store.save(p)
        result.created.append(p)
        for s in cert:
            result.dispositions[s.id] = p.id

    # ---- 按案例看最近一轮 outcome（R3 与处置用）
    by_case: dict[str, list[Signal]] = {}
    for s in outcomes:
        by_case.setdefault(s.extra.get("case_id", ""), []).append(s)
    latest_hit: dict[str, bool] = {}
    for case_id, sigs in by_case.items():
        latest = max(sigs, key=lambda s: s.created_at)
        cov = _coverage_of(latest)
        latest_hit[case_id] = "结论命中=True" in latest.content
        # ---- R3：持续覆盖缺口 → lesson
        if len(sigs) >= 2 and cov and cov[1] and cov[0] / cov[1] < 0.6:
            anchor = LESSON_ANCHORS.get(case_id)
            if anchor is not None:
                p = ChangeProposal(
                    id=_next_id(proposal_store, "prop-lesson"),
                    title="手册补条目：排障答复与修复建议须写到可执行"
                          "粒度（telegram-hook-log-array 两轮覆盖缺口）",
                    target_files=[f"harness/{anchor.file}"],
                    diff="",       # 应用到工作副本时由 edits 渲染
                    change_class="lesson",
                    evidence_signal_ids=evidence_ids(sigs),
                    attribution={
                        "phenomenon": f"关键动作覆盖持续不足（最近一轮 "
                                      f"{cov[0]}/{cov[1]}），答复讲清根因"
                                      "但修复动作没写到可执行粒度",
                        "layer": "手册",
                        "file": f"harness/{anchor.file}"},
                    expected_effect="telegram-hook-log-array 案例动作覆盖"
                                    "期望由 2/4 提升（命中「对象包裹」或"
                                    "「按段定位」类动作）",
                    expected_side_effect="手册变长（组装提示约 +90 字，"
                                         "成本可忽略）；答复风格可能被带向"
                                         "其他案例——靠整轮回归门看其余"
                                         "案例分项是否退步",
                    edits=[anchor],
                    harness_version_base=harness_version)
                proposal_store.save(p)
                result.created.append(p)
                for s in sigs:
                    result.dispositions[s.id] = p.id
            else:
                result.recorded_only.append(
                    f"{case_id}：覆盖持续不足但规则表无落点，只记录")

    # ---- 其余信号处置（幂等账）
    for s in usable:
        if s.id in result.dispositions:
            continue
        case_id = s.extra.get("case_id", "")
        if latest_hit.get(case_id):
            result.dispositions[s.id] = "superseded_by_later_run"
        elif s.kind == "outcome":
            result.dispositions[s.id] = "evidence_base"
        else:
            result.dispositions[s.id] = "recorded_only"
            result.recorded_only.append(
                f"{s.id}（{case_id}）：{s.content[:80]}")
    proposal_store.set_dispositions(result.dispositions)
    return result


def render_unified_diff(file_label: str, old_text: str,
                        new_text: str) -> str:
    """由 edits 应用前后的全文渲染统一 diff（合入存档用）。"""
    import difflib
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{file_label}", tofile=f"b/{file_label}"))


def apply_edits(text: str, edits: list[Edit], file_label: str) -> str:
    """把 edits 依次精确应用到 text；锚点缺失即抛错（不许静默跳过）。"""
    out = text
    for edit in edits:
        if edit.file != file_label:
            continue
        if edit.old not in out:
            raise ValueError(f"替换锚点在 {file_label} 中不存在，拒应用")
        out = out.replace(edit.old, edit.new, 1)
    return out
