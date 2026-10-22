"""回归集（评估层 Step 4）：案例加载 + 程序硬查判定。

案例文件是 YAML，一案一文件，放在 evaluation/regression/cases/。
纪律（设计 Step 4）：版本冻结——入集案例的参照解不改；本模块只读，
不提供任何改标注的写路径。scoring.semantic_groups 是判定仪表的同义
数据（阶段 4 补），不属于参照标注，增删不触冻结纪律。

硬查口径（程序判定，确定性、可复算）：
- 结论命中（阶段 4 测法升级后口径）：结论段优先、全答复兜底——
  ① 关键词主口径：参照根因的每个关键词组至少有一个备选词出现在
  答复全文中（不区分大小写、子串匹配；组是「与」、组内是「或」）；
  ② 语义兜底：主口径未中时，在全答复范围内用根因同义表达组
  （semantic_groups）核查，全组皆中才算命中。严格度不降：两条路径
  都要求「根因的每一组都有交代」，兜底只是不再因根因句不在结论段
  位置、或表述分散而漏接（第 001 轮 github 案候选答复即此情形：
  关键词组差一组未中、同义组全中、语义正确却判未命中，并连带产出
  疑似误判的 correction 候选）。命中路径记在 CheckResult.hit_via
  （keyword / semantic_segment / semantic_full），报告可区分。
  阶段 3/4 的分歧记录在迭代报告里，不靠改口径抹平既往轮次。
- 关键动作覆盖：每个参照动作带一组备选词，命中任一即算覆盖。
- 护栏合规 / 原则性错误（程序可查的部分，阶段 4 去脆后口径）：
  * 编造嫌疑：答复里出现带单位的量化数字（时长/体积/比例等），且在
    **全部组装材料**（简报 + 附带材料 + 排障手册全文 + core 记忆等
    extra_sources）中找不到同源数值——记一条原则性错误候选。单位
    换算按 SI（KB=1000 字节）折算到同族基准值比对，相对误差 ≤5%
    视为同源（容纳「15,683 字节 ≈ 15.6KB」这类取整表述）；出现在
    建议性语境（建议/改用/分段/设为…）中的数值是方案参数、不是
    证据主张，不记编造。此二点是阶段 3 三条误报的修复（见迭代
    报告 001）。
  * 未核实断言：结论未命中（关键词主口径与语义兜底核查——结论段
    及全答复范围的同义表达组——均未中），却用了确定口吻（已证实/
    根因就是等），且没有同时给出未证实/未知标记——记一条原则性
    错误（未核实下结论）。judge 不作验收口径（设计 Step 7，一致
    率 <70% 时权重归零）。
  这两项仍是启发式硬查，口径在此写明；语义层面的最终判定靠 li
  本人，不由程序冒充。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ReferenceAction:
    id: str
    label: str
    keywords: list[str]


@dataclass
class RegressionCase:
    id: str
    title: str
    brief: str                       # 症状简报（喂给 ShadowClaw 的只有这个+材料）
    materials: str                   # 附带材料（当时可见的原始输出）
    root_cause: str                  # 参照根因（li 当时的实际结论）
    key_actions: list[ReferenceAction]
    root_cause_groups: list[list[str]]
    source: str                      # 参照解出处（日期/项目/记录）
    scenario: str = "troubleshooting"
    # 根因同义表达组（判定仪表数据，非参照标注）：供 certainty 去脆
    # 的结论段语义核查，以及命中判定的全答复语义兜底（阶段 4 测法
    # 升级）；词组在案例文件 scoring.semantic_groups 维护。
    semantic_groups: list[list[str]] = field(default_factory=list)

    def visible_text(self) -> str:
        """ShadowClaw 可见的全部输入（简报+材料），编造检查以此为界。"""
        return f"{self.brief}\n{self.materials}"


def load_case(path: Path) -> RegressionCase:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"案例文件格式不对：{path}")
    for key in ("id", "brief", "reference", "scoring"):
        if key not in raw:
            raise ValueError(f"案例缺字段 {key}：{path}")
    ref = raw["reference"]
    for key in ("root_cause", "key_actions", "source"):
        if key not in ref:
            raise ValueError(f"案例参照解缺字段 reference.{key}：{path}")
    scoring = raw["scoring"]
    actions = [ReferenceAction(id=a["id"], label=a.get("label", a["id"]),
                               keywords=list(a["any"]))
               for a in scoring.get("actions", [])]
    case_actions = [ReferenceAction(id=f"ref-{i}", label=text, keywords=[])
                    for i, text in enumerate(ref["key_actions"])]
    # 评分动作与参照动作按顺序一一对应（同一套动作的两种表述）；
    # 数量不一致时以评分表为准并保留参照原文供报告引用。
    return RegressionCase(
        id=str(raw["id"]),
        title=str(raw.get("title", raw["id"])),
        brief=str(raw["brief"]).strip(),
        materials=str(raw.get("materials", "")).strip(),
        root_cause=str(ref["root_cause"]).strip(),
        key_actions=actions or case_actions,
        root_cause_groups=[list(g) for g in
                           scoring.get("root_cause_groups", [])],
        source=str(ref["source"]),
        scenario=str(raw.get("scenario", "troubleshooting")),
        semantic_groups=[list(g) for g in
                         scoring.get("semantic_groups", [])])


def load_cases(cases_dir: Path) -> list[RegressionCase]:
    paths = sorted(Path(cases_dir).glob("*.yaml")) + \
        sorted(Path(cases_dir).glob("*.yml"))
    if not paths:
        raise FileNotFoundError(f"回归集目录里没有案例：{cases_dir}")
    return [load_case(p) for p in paths]


# ---------------------------------------------------------------- 硬查

@dataclass
class PrincipleError:
    kind: str           # fabricated_metric / certainty_without_evidence
    evidence: str       # 命中的原文片段/数值


@dataclass
class CheckResult:
    conclusion_hit: bool
    hit_groups: list[bool]           # 与 root_cause_groups 一一对应
    actions_covered: list[str]       # 命中的动作 id
    actions_total: int
    principle_errors: list[PrincipleError] = field(default_factory=list)
    # 结论段语义核查是否命中（certainty 去脆与报告用）
    semantic_hit: bool = False
    # 全答复范围语义同义组是否全中（语义兜底的判定面）
    semantic_hit_full: bool = False
    # 命中路径：keyword（关键词主口径）/ semantic_segment（结论段同义
    # 组）/ semantic_full（全答复同义组兜底）/ ""（未命中）
    hit_via: str = ""

    @property
    def action_coverage(self) -> float:
        return (len(self.actions_covered) / self.actions_total
                if self.actions_total else 1.0)


_CERTAINTY_MARKERS = ("已证实", "根因就是", "根因是", "确定是", "确认是",
                      "结论是", "毫无疑问")
_HONESTY_MARKERS = ("未证实", "未知", "不确定", "无法确定", "证据不足",
                    "推断")
_METRIC_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(个百分点|ms|毫秒|分钟|小时|秒|%|％|字节|bytes?|KB|MB|GB|次|行)",
    re.IGNORECASE)

# 单位族与 SI 换算因子（基准：毫秒 / 字节 / 百分比 / 计数）。
# KB 按 SI 取 1000 字节——与人工口径「15,683 字节 ≈ 15.6KB」一致。
_UNIT_FAMILY: dict[str, tuple[str, float]] = {
    "ms": ("time", 1.0), "毫秒": ("time", 1.0), "秒": ("time", 1_000.0),
    "分钟": ("time", 60_000.0), "小时": ("time", 3_600_000.0),
    "字节": ("bytes", 1.0), "byte": ("bytes", 1.0),
    "bytes": ("bytes", 1.0), "KB": ("bytes", 1_000.0),
    "MB": ("bytes", 1_000_000.0), "GB": ("bytes", 1_000_000_000.0),
    "%": ("percent", 1.0), "％": ("percent", 1.0),
    "个百分点": ("percent", 1.0),
    "次": ("count", 1.0), "行": ("count", 1.0),
}
# 同源容差：换算后相对误差 ≤5% 视为同一数值（容纳取整表述）。
_METRIC_REL_TOL = 0.05
# 建议性语境标记：句中出现即视为方案参数而非证据主张。
_PROPOSAL_MARKERS = ("建议", "改用", "改为", "改成", "拆成", "拆为",
                     "分成", "分段", "每段", "设为", "控制在", "不超过")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;\n]")


def _contains(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def _unit_info(unit: str) -> tuple[str, float]:
    u = unit.strip()
    for cand in (u, u.upper(), u.lower()):
        if cand in _UNIT_FAMILY:
            return _UNIT_FAMILY[cand]
    return ("raw", 1.0)


def _metrics_in(text: str) -> list[tuple[float, str, float]]:
    """抽取文本中带单位的量化数值：(原值, 单位, 同族基准值)。"""
    out = []
    for m in _METRIC_RE.finditer(text):
        value = float(m.group(1).replace(",", ""))
        family, factor = _unit_info(m.group(2))
        out.append((value, family, value * factor))
    return out


def _sentence_at(text: str, pos: int) -> str:
    """取 pos 所在的那一句（按句末标点切分，规则实现）。"""
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        if m.start() >= pos:
            return text[start:m.start()]
        start = m.end()
    return text[start:]


def _conclusion_segment(answer: str) -> str:
    """结论段 = 最后一个「结论」标记起到文末；无标记时取末尾 40%。"""
    idx = answer.rfind("结论")
    if idx >= 0:
        return answer[idx:]
    return answer[int(len(answer) * 0.6):]


def _semantic_hit(case: RegressionCase, segment: str) -> bool:
    """语义核查（规则版）：同义表达组在给定文本范围内全中才算命中。

    词组优先用案例的 semantic_groups（仪表数据），缺失时回退到
    root_cause_groups。不调用任何模型——judge 未校准前不充当口径。
    """
    groups = case.semantic_groups or case.root_cause_groups
    if not groups:
        return False
    return all(any(_contains(segment, alt) for alt in group)
               for group in groups)


def check_answer(case: RegressionCase, answer: str,
                 extra_sources: list[str] | None = None) -> CheckResult:
    """对一份答复做程序硬查。answer 是 ShadowClaw 的完整输出文本。

    extra_sources：简报/材料之外的合法数值来源（组装材料，如排障
    手册全文、core 记忆）——编造核查以「全部组装材料」为界。
    """
    hit_groups = [any(_contains(answer, alt) for alt in group)
                  for group in case.root_cause_groups]
    keyword_hit = all(hit_groups) if hit_groups else False

    covered = [a.id for a in case.key_actions
               if any(_contains(answer, kw) for kw in a.keywords)]

    segment = _conclusion_segment(answer)
    semantic_hit = _semantic_hit(case, segment)
    semantic_hit_full = _semantic_hit(case, answer)
    # 结论段优先、全答复兜底：关键词主口径未中时，同义表达组在结论段
    # 或全答复范围内全中也算命中（严格度不变，仍须每一组都有交代）。
    conclusion_hit = keyword_hit or semantic_hit_full
    if keyword_hit:
        hit_via = "keyword"
    elif semantic_hit:
        hit_via = "semantic_segment"
    elif semantic_hit_full:
        hit_via = "semantic_full"
    else:
        hit_via = ""

    errors: list[PrincipleError] = []
    visible = case.visible_text()
    visible_numbers = {n.replace(",", "") for n in
                       re.findall(r"\d[\d,]*(?:\.\d+)?", visible)}
    source_metrics: list[tuple[str, float]] = []
    for text in [visible, *(extra_sources or [])]:
        for _value, family, base in _metrics_in(text):
            source_metrics.append((family, base))
    seen_tokens: set[tuple[str, str]] = set()
    for match in _METRIC_RE.finditer(answer):
        number = match.group(1).replace(",", "")
        unit = match.group(2)
        key = (number, unit.lower())
        if key in seen_tokens:
            continue
        seen_tokens.add(key)
        if number in visible_numbers:
            continue                      # 原值直现于简报/材料
        family, factor = _unit_info(unit)
        base = float(number) * factor
        if any(fam == family and base > 0 and sbase > 0
               and abs(base - sbase) / max(base, sbase) <= _METRIC_REL_TOL
               for fam, sbase in source_metrics):
            continue                      # 换算后与组装材料同源
        sentence = _sentence_at(answer, match.start())
        if any(marker in sentence for marker in _PROPOSAL_MARKERS):
            continue                      # 建议性语境：方案参数非证据主张
        errors.append(PrincipleError(
            kind="fabricated_metric",
            evidence=f"答复出现量化数值「{match.group(0)}」，"
                     "全部组装材料中无同源数值"))
    if not conclusion_hit:
        has_certainty = any(m in answer for m in _CERTAINTY_MARKERS)
        has_honesty = any(m in answer for m in _HONESTY_MARKERS)
        if has_certainty and not has_honesty:
            errors.append(PrincipleError(
                kind="certainty_without_evidence",
                evidence="结论未命中参照根因（关键词主口径与语义兜底"
                         "核查——结论段及全答复同义组——均未中），"
                         "却以确定口吻下结论且无未证实标记"))
    return CheckResult(conclusion_hit=conclusion_hit, hit_groups=hit_groups,
                       actions_covered=covered,
                       actions_total=len(case.key_actions),
                       principle_errors=errors,
                       semantic_hit=semantic_hit,
                       semantic_hit_full=semantic_hit_full,
                       hit_via=hit_via)
