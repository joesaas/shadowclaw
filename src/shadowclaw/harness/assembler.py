"""场景组装器：运行时组装驾驭视图的唯一入口（驾驭层设计 Step 3）。

给一个任务先定场景，组装顺序**固定**（不许绕过场景零散拼凑）：

    原则全文（最前，永远全量）→ 身份（原则首节，单列字段）
    → core 常驻记忆 → 场景手册（执行单元给全文，规划者只看摘要）
    → 工具卡摘要 → 检索命中的记忆条目

产出 AssembledContext 给执行层：render_instruction() 是单元 instruction
的拼装文本；approval_policy 是可直接注入编排器的护栏策略对象；
budget_limit 是场景预算与护栏预算顶合并从严的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .guardrails import GuardrailPolicy
from .loader import HarnessBundle, ToolCard
from .memory import MemoryEntry, MemoryIndex


@dataclass
class AssembledContext:
    scenario_name: str
    harness_version: str
    principles: str                     # 原则全文（注入最前）
    identity: str                       # 原则「身份」节（单列，规划者/审计用）
    core_memory: str                    # core.md 全文（常驻）
    playbook_full: str                  # 命中的手册全文（执行单元用）
    playbook_summary: str               # 手册摘要（规划者选路用）
    tool_summaries: list[str] = field(default_factory=list)
    memory_hits: list[MemoryEntry] = field(default_factory=list)
    approval_policy: GuardrailPolicy | None = None
    budget_limit: float | None = None   # 合并从严后的单任务预算顶

    def render_instruction(self) -> str:
        """单元 instruction 的固定拼装顺序：原则 → 核心记忆 → 手册 →
        工具卡摘要 → 检索记忆。前段顺序是硬约束，测试看守。"""
        parts = [self.principles.strip()]
        if self.core_memory.strip():
            parts.append(self.core_memory.strip())
        if self.playbook_full.strip():
            parts.append(self.playbook_full.strip())
        if self.tool_summaries:
            parts.append("## 工具卡摘要\n" + "\n".join(self.tool_summaries))
        if self.memory_hits:
            hits = "\n".join(f"- {e.render()}" for e in self.memory_hits)
            parts.append(f"## 本任务检索到的记忆\n{hits}")
        return "\n\n".join(parts)

    def planner_view(self) -> str:
        """规划者视图：原则 + 手册摘要 + 工具卡摘要（不给手册全文）。"""
        parts = [self.principles.strip()]
        if self.playbook_summary.strip():
            parts.append(self.playbook_summary.strip())
        if self.tool_summaries:
            parts.append("## 工具卡摘要\n" + "\n".join(self.tool_summaries))
        return "\n\n".join(parts)

    def memory_texts(self) -> list[str]:
        """注入事实/上下文用的命中条目文本（含推断置信度标注）。"""
        return [entry.render() for entry in self.memory_hits]


def _playbook_summary(playbook_text: str) -> str:
    """手册摘要：frontmatter + 适用场景 + 步骤标题清单（选路够用）。"""
    lines = playbook_text.splitlines()
    chunks: list[str] = []
    in_steps = False
    step_titles: list[str] = []
    for line in lines:
        if line.startswith("## 步骤"):
            in_steps = True
            continue
        if in_steps and line.startswith("## "):
            in_steps = False
        if in_steps and line.startswith("### "):
            step_titles.append(line[4:].strip())
    head = playbook_text.split("## 步骤")[0].strip()
    chunks.append(head)
    if step_titles:
        chunks.append("步骤：" + " → ".join(step_titles))
    return "\n\n".join(chunks)


def _tool_summary(card: ToolCard, *, max_risk: str | None) -> str:
    counts = {level: len(card.ops_at(level)) for level in
              ("read", "write", "irreversible", "forbidden")}
    bits = [f"只读 {counts['read']} 项默认启用"]
    if counts["write"]:
        bits.append(f"写 {counts['write']} 项须护栏放行")
    if counts["irreversible"]:
        bits.append(f"不可逆 {counts['irreversible']} 项须单独确认")
    if counts["forbidden"]:
        bits.append(f"红线 {counts['forbidden']} 项永不挂载")
    ceiling = f"；本场景风险上限 {max_risk}" if max_risk else ""
    return (f"- {card.name}（接入：{card.access or '见工具卡'}；"
            f"凭证：{card.credential_ref or '见工具卡'}；"
            f"{'，'.join(bits)}{ceiling}）")


def assemble(bundle: HarnessBundle, *, query: str = "",
             memory_index: MemoryIndex | None = None,
             step_risks: dict[str, str] | None = None) -> AssembledContext:
    """按 bundle 的场景组装一份执行上下文。"""
    scenario = bundle.scenario
    playbook_full = bundle.playbooks.get(scenario.playbook, "")

    tool_summaries = []
    for tool_name, spec in scenario.tools.items():
        card = bundle.tool_cards.get(tool_name)
        if card is None:
            continue
        tool_summaries.append(_tool_summary(
            card, max_risk=(spec or {}).get("max_risk")))

    hits: list[MemoryEntry] = []
    if query and memory_index is not None:
        hits = memory_index.search(query)

    policy = GuardrailPolicy(bundle.guardrails_policy,
                             step_risks=step_risks)
    caps = [c for c in (scenario.budget_limit_usd, policy.budget_cap)
            if c is not None]
    budget_limit = min(caps) if caps else None    # 合并从严

    return AssembledContext(
        scenario_name=scenario.name,
        harness_version=bundle.version,
        principles=bundle.principles_text,
        identity=bundle.identity_text,
        core_memory=bundle.core_memory,
        playbook_full=playbook_full,
        playbook_summary=_playbook_summary(playbook_full),
        tool_summaries=tool_summaries,
        memory_hits=hits,
        approval_policy=policy,
        budget_limit=budget_limit)
