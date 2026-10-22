"""驾驭层加载器：读仓库根 harness/ 正本文件，组装一份 HarnessBundle。

职责三件：解析（原则 / 护栏 / 场景 / 手册 / 工具卡 / 核心记忆）、校验
（引用不存在或格式不对在加载时报错，不带病跑到任务中途；锁定级文件校验
批准标记）、算版本号（harness_version 打进轨迹，评估层归因的前提）。

本模块只依赖标准库 + yaml，不依赖执行层（保持层间单向：执行层经
assembler 消费本模块的产出）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class HarnessError(Exception):
    """驾驭层加载/校验失败：引用缺失、格式错误、锁定级校验不过。"""


# ---------------------------------------------------------------- 数据形状
@dataclass
class ToolCard:
    name: str
    text: str                                   # 工具卡全文
    access: str = ""                            # 接入方式（frontmatter）
    credential_ref: str = ""                    # 凭证引用（只写引用不写值）
    operations: list[tuple[str, str]] = field(default_factory=list)
    # operations: [(操作名, 风险级)]，从能力清单表解析

    def ops_at(self, risk: str) -> list[str]:
        return [op for op, level in self.operations if level == risk]


# 场景运行模式（阶段 5 影子转分身）：shadow = 只对照出方案不动手；
# autonomous = 允许真正执行工具调用（护栏审批一件不减、fail-closed）。
# 缺省 shadow——转正不是默认状态，是挣来的状态。
SCENARIO_MODES = ("shadow", "autonomous")


@dataclass
class Scenario:
    name: str
    display_name: str = ""
    playbook: str = ""                          # 活类名（playbooks/<活类>.md）
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    memory_searchable: list[str] = field(default_factory=list)
    budget_limit_usd: float | None = None
    mode: str = "shadow"                        # shadow / autonomous
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessBundle:
    root: Path
    version: str
    principles_text: str
    identity_text: str                          # 原则的「身份」节（单列给组装用）
    guardrails_meta: dict[str, Any]             # 护栏 frontmatter（含锁定标记）
    guardrails_policy: dict[str, Any]           # 护栏策略矩阵（机器读 YAML 段）
    scenario: Scenario
    playbooks: dict[str, str]                   # 活类 -> 手册全文
    tool_cards: dict[str, ToolCard]             # 工具名 -> 工具卡
    core_memory: str                            # memory/core.md 全文
    proactivity: Any = None                     # ProactivityConfig（阶段 5）


# ------------------------------------------------------------------- 版本号
def compute_harness_version(root: Path | str) -> str:
    """版本号 = 原则 + 护栏 + 全部场景文件内容的哈希（短哈希）。

    只对「定调子」的文件取哈希：这三类一变，跑出来的行为就可能变，
    轨迹必须能区分。文件缺失时跳过；一个都不在则抛 FileNotFoundError，
    由调用方决定降级（config 运行时路径降级为 unversioned 并留痕）。
    """
    root = Path(root)
    targets: list[Path] = [root / "principles" / "principles.md",
                           root / "guardrails" / "guardrails.md"]
    scenarios_dir = root / "scenarios"
    if scenarios_dir.is_dir():
        targets.extend(sorted(scenarios_dir.glob("*.yaml")))
    digest = hashlib.sha256()
    found = False
    for path in targets:
        if path.exists():
            found = True
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    if not found:
        raise FileNotFoundError(f"harness 目录无可哈希文件：{root}")
    return f"h-{digest.hexdigest()[:10]}"


# ------------------------------------------------------------------- 校验
def validate_locked(meta: dict[str, Any], *, name: str) -> None:
    """锁定级双保险的加载侧：requires_approval 的文件必须带批准标记。"""
    if meta.get("locked") and meta.get("requires_approval"):
        if not meta.get("approved_by") or not meta.get("approved_at"):
            raise HarnessError(
                f"锁定级文件 {name} 缺批准标记（approved_by / approved_at），"
                "拒载：未批准的锁定级改动不生效")


# ------------------------------------------------------------------- 解析
def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    return meta, text[end + 4:]


def _extract_yaml_block(body: str) -> dict[str, Any]:
    match = re.search(r"```yaml\n(.*?)```", body, re.DOTALL)
    if not match:
        raise HarnessError("护栏文件缺少 ```yaml 策略段")
    return yaml.safe_load(match.group(1)) or {}


def _extract_section(text: str, header: str) -> str:
    """取某个二级标题下的正文（到下一个二级标题为止），取不到返回空串。"""
    pattern = re.compile(
        rf"^##\s+{re.escape(header)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _parse_tool_card(name: str, text: str) -> ToolCard:
    meta, _ = _split_frontmatter(text)
    operations: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[1] not in (
                "read", "write", "irreversible", "forbidden"):
            continue
        for op in cells[0].split("/"):
            op = op.strip()
            if op:
                operations.append((op, cells[1]))
    return ToolCard(
        name=name, text=text,
        access=str(meta.get("接入方式", "")),
        credential_ref=str(meta.get("凭证引用", "")),
        operations=operations)


def _load_scenario(root: Path, scenario_name: str) -> Scenario:
    path = root / "scenarios" / f"{scenario_name}.yaml"
    if not path.exists():
        raise HarnessError(f"场景文件不存在：{path}")
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    memory = raw.get("memory") or {}
    mode = str(raw.get("mode") or "shadow")
    if mode not in SCENARIO_MODES:
        raise HarnessError(
            f"场景 {scenario_name} 的 mode 取值非法：{mode!r}"
            f"（合法集：{'/'.join(SCENARIO_MODES)}，缺省 shadow）")
    return Scenario(
        name=raw.get("name", scenario_name),
        display_name=raw.get("display_name", ""),
        playbook=raw.get("playbook", ""),
        tools=raw.get("tools") or {},
        memory_searchable=list(memory.get("searchable") or []),
        budget_limit_usd=raw.get("budget_limit_usd"),
        mode=mode,
        raw=raw)


# ------------------------------------------------------------------- 入口
def load_harness(root: Path | str, scenario_name: str, *,
                 promotion_gate=None) -> HarnessBundle:
    """按场景加载驾驭层正本；任何引用缺失/校验不过都在此报错。

    promotion_gate（阶段 5，依赖注入）：场景 mode=autonomous 时的
    转正门槛核验函数，签名 gate(scenario) -> None，过门才返回、
    不过门抛异常（评估层 evaluation.promotion.ensure_promoted）。
    autonomous 场景未注入 gate 时直接拒载（fail-closed）：分身模式
    不许在无核验的情况下被任何路径加载起来。shadow 场景不查 gate。
    """
    root = Path(root)
    if not root.is_dir():
        raise HarnessError(f"harness 目录不存在：{root}")

    principles_path = root / "principles" / "principles.md"
    if not principles_path.exists():
        raise HarnessError(f"原则文件不存在：{principles_path}")
    principles_text = principles_path.read_text("utf-8")

    guardrails_path = root / "guardrails" / "guardrails.md"
    if not guardrails_path.exists():
        raise HarnessError(f"护栏文件不存在：{guardrails_path}")
    guardrails_meta, guardrails_body = _split_frontmatter(
        guardrails_path.read_text("utf-8"))
    validate_locked(guardrails_meta, name="guardrails/guardrails.md")
    guardrails_policy = _extract_yaml_block(guardrails_body)

    scenario = _load_scenario(root, scenario_name)
    if scenario.mode == "autonomous":
        if promotion_gate is None:
            raise HarnessError(
                f"场景 {scenario.name} 声明 autonomous 模式，但加载时"
                "未提供转正门槛核验（promotion_gate），fail-closed "
                "拒载：先过 evaluation.promotion 的转正门槛并注入核验"
                "函数后再加载")
        promotion_gate(scenario)      # 不过门由 gate 抛错，人话原因在门里

    playbooks: dict[str, str] = {}
    playbook_path = root / "playbooks" / f"{scenario.playbook}.md"
    if scenario.playbook:
        if not playbook_path.exists():
            raise HarnessError(
                f"场景 {scenario.name} 引用的手册不存在：{playbook_path}")
        playbooks[scenario.playbook] = playbook_path.read_text("utf-8")

    tool_cards: dict[str, ToolCard] = {}
    for tool_name, spec in scenario.tools.items():
        card_file = (spec or {}).get("card", f"{tool_name}.md")
        card_path = root / "tools" / card_file
        if not card_path.exists():
            raise HarnessError(
                f"场景 {scenario.name} 引用的工具卡不存在：{card_path}")
        tool_cards[tool_name] = _parse_tool_card(
            tool_name, card_path.read_text("utf-8"))

    core_path = root / "memory" / "core.md"
    if not core_path.exists():
        raise HarnessError(f"核心记忆不存在：{core_path}")

    # 主动性配置（阶段 5）：惰性导入避免与 proactivity 的循环依赖；
    # 文件缺失 = 全关默认，未登记条目在 load_proactivity 里拒载。
    from .proactivity import load_proactivity
    proactivity = load_proactivity(root)

    return HarnessBundle(
        root=root,
        version=compute_harness_version(root),
        principles_text=principles_text,
        identity_text=_extract_section(principles_text, "一、身份"),
        guardrails_meta=guardrails_meta,
        guardrails_policy=guardrails_policy,
        scenario=scenario,
        playbooks=playbooks,
        tool_cards=tool_cards,
        core_memory=core_path.read_text("utf-8"),
        proactivity=proactivity)
