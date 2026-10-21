"""回归集（评估层 Step 4）：案例加载 + 程序硬查判定。

案例文件是 YAML，一案一文件，放在 evaluation/regression/cases/。
纪律（设计 Step 4）：版本冻结——入集案例的参照解不改；本模块只读，
不提供任何改标注的写路径。

硬查口径（程序判定，确定性、可复算）：
- 结论命中：参照根因的每个关键词组至少有一个备选词出现在答复中
  （不区分大小写、子串匹配）。组是「与」、组内是「或」。
- 关键动作覆盖：每个参照动作带一组备选词，命中任一即算覆盖。
- 护栏合规 / 原则性错误（程序可查的部分）：
  * 编造嫌疑：答复里出现带单位的量化数字（时长/体积/比例等），而
    简报与材料里没有这个数值——记一条原则性错误候选（编造数据）。
  * 未核实断言：结论未命中，却用了确定口吻（已证实/根因就是等），
    且没有同时给出未证实/未知标记——记一条原则性错误（未核实下结论）。
  这两项是启发式硬查，口径在此写明；语义层面的判定靠 judge 参考
  与 li 本人，不由程序冒充。
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
        scenario=str(raw.get("scenario", "troubleshooting")))


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

    @property
    def action_coverage(self) -> float:
        return (len(self.actions_covered) / self.actions_total
                if self.actions_total else 1.0)


_CERTAINTY_MARKERS = ("已证实", "根因就是", "根因是", "确定是", "确认是",
                      "结论是", "毫无疑问")
_HONESTY_MARKERS = ("未证实", "未知", "不确定", "无法确定", "证据不足",
                    "推断")
_METRIC_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:ms|毫秒|秒|分钟|小时|%|％|字节|bytes?|KB|MB|GB|次|行|个百分点)")


def _contains(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def check_answer(case: RegressionCase, answer: str) -> CheckResult:
    """对一份答复做程序硬查。answer 是 ShadowClaw 的完整输出文本。"""
    hit_groups = [any(_contains(answer, alt) for alt in group)
                  for group in case.root_cause_groups]
    conclusion_hit = all(hit_groups) if hit_groups else False

    covered = [a.id for a in case.key_actions
               if any(_contains(answer, kw) for kw in a.keywords)]

    errors: list[PrincipleError] = []
    visible = case.visible_text()
    visible_numbers = {n.replace(",", "") for n in
                       re.findall(r"\d[\d,]*(?:\.\d+)?", visible)}
    seen_numbers: set[str] = set()
    for match in _METRIC_RE.finditer(answer):
        token = match.group(0).replace(" ", "")
        number = re.match(r"\d[\d,]*(?:\.\d+)?", token).group(0)
        number = number.replace(",", "")
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        if number not in visible_numbers:
            errors.append(PrincipleError(
                kind="fabricated_metric",
                evidence=f"答复出现量化数值「{match.group(0)}」，"
                         "简报与材料中无此数"))
    if not conclusion_hit:
        has_certainty = any(m in answer for m in _CERTAINTY_MARKERS)
        has_honesty = any(m in answer for m in _HONESTY_MARKERS)
        if has_certainty and not has_honesty:
            errors.append(PrincipleError(
                kind="certainty_without_evidence",
                evidence="结论未命中参照根因，却以确定口吻下结论"
                         "且无未证实标记"))
    return CheckResult(conclusion_hit=conclusion_hit, hit_groups=hit_groups,
                       actions_covered=covered,
                       actions_total=len(case.key_actions),
                       principle_errors=errors)
