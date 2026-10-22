"""影子运行器（阶段 3 · 历史回放版）。

形态：回放双跑——每个回归案例只给 ShadowClaw「当时的症状简报 +
附带材料」（不给答案），它经排障场景组装（原则 + 手册 + 记忆）后用
真实模型独立排障；产出与参照解对照，偏差定位到层。

判定口径（不许混）：
- 程序硬查（regression.check_answer）：结论命中 / 动作覆盖 / 程序可查
  的原则性错误——确定性口径，验收线看这个。
- judge（模型自评）：只作参考，报告中单独标注口径；与硬查不一致时
  以硬查为准，差异本身记 judge 信号留待校准（设计 Step 7）。

额度纪律：每案例模型调用 ≤ max_calls_per_case（默认 8；本实现求解 1 次
+ judge 1 次）；遇 402/额度类错误立刻停，不重试、不继续烧，未跑案例
如实标 not_run。

凭证：OpenRouter key 只从环境变量 OPENROUTER_API_KEY 读；没有时经
dynamic_credentials 帮手向 authd 取 custom.openrouter 的替身注入环境
（替身由 Sentinel 在出口换成真 key）。key/替身绝不打印、不落盘、不入仓。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..execution.checkpoint import SqliteCheckpointStore
from ..execution.config import Config
from ..execution.contracts import Unit
from ..execution.orchestrator import Orchestrator
from ..execution.plan import Plan, Step
from ..execution.trajectory import SqliteTrajectoryStore
from ..execution.units import UnitRegistry
from ..harness.assembler import AssembledContext, assemble
from ..harness.loader import HarnessBundle
from ..harness.memory import MemoryIndex
from .regression import CheckResult, RegressionCase, check_answer
from .signals import Signal, SignalStore

SHADOW_SUFFIX = """

## 影子模式附加指令（本轮回放专用）

你现在处于影子回放：只做排障分析，不执行任何修复动作，也不要声称已经
执行了任何操作。你没有任何外部工具，只能依据下面的症状简报与附带材料
推理。严格按排障手册的步骤与输出规范作答：先定界，再列你依据的一手
证据，然后分层排除，最后给结论并分级（已证实/推断/未证实）。
证据不足时如实写「未证实」与缺什么证据，绝不许编造日志、数字或状态。
结论部分要明确写出你判断的根因与建议的关键动作（供事后对照）。"""

UnitFactory = Callable[[RegressionCase, AssembledContext], Unit]
JudgeFn = Callable[[RegressionCase, str], Awaitable[dict[str, Any]]]

_QUOTA_MARKERS = ("402", "insufficient credits", "in_flight_budget",
                  "insufficient_quota", "quota exceeded")


def _is_quota_error(text: str) -> bool:
    low = text.lower()
    return any(m in low or m in text for m in _QUOTA_MARKERS)


# ------------------------------------------------------------------ 凭证

def ensure_openrouter_api_key() -> bool:
    """确保 OPENROUTER_API_KEY 在环境里（值是替身或真 key 都只留内存）。

    已有环境变量直接用；否则经 authd 取 custom.openrouter 的替身。
    成功返回 True；拿不到返回 False（调用方据此诚实报错，不假装能跑）。
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    helper_dir = "/opt/muse/skills/custom-connector-setup/bin"
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    try:
        import dynamic_credentials as dc
    except Exception:
        return False
    for entry_name in ("api_key", "access_token", "bearer", "key"):
        try:
            entry = dc.dynamic_credential_entry("custom.openrouter",
                                                entry_name)
        except Exception:
            continue
        surrogate = str(entry.get("surrogate", "")).strip()
        if surrogate:
            os.environ["OPENROUTER_API_KEY"] = surrogate
            return True
    return False


def build_litellm_model(model_slug: str, *, max_tokens: int = 4000):
    """OpenRouter slug（如 google/gemini-3.8-flash）→ ADK LiteLlm 实例。

    max_tokens 必须显式设：ADK/LiteLlm 默认预留 65536，OpenRouter 按
    预留额计价，key 额度紧时直接 402（harness2-demo 同型教训）；但压到
    1000 会把答复截断在结论之前（2026-10-21 首轮回放实测），4000 是
    当前 key 限额下的平衡点。kwargs 经 LiteLlm 透传到 completion 参数。
    """
    from google.adk.models.lite_llm import LiteLlm
    name = model_slug if model_slug.startswith("openrouter/") \
        else f"openrouter/{model_slug}"
    return LiteLlm(model=name, max_tokens=max_tokens)


def build_real_unit_factory(model_slug: str) -> UnitFactory:
    """真实模型单元工厂：AdkUnit + LiteLlm(OpenRouter)，无外部工具。"""
    from ..execution.units import AdkUnit

    def factory(case: RegressionCase,
                assembled: AssembledContext) -> Unit:
        instruction = assembled.render_instruction() + SHADOW_SUFFIX
        return AdkUnit("shadow-solver", instruction=instruction,
                       model=build_litellm_model(model_slug),
                       model_label=model_slug)
    return factory


def build_judge_fn(model_slug: str, config: Config) -> JudgeFn:
    """judge：同模型自评（仅参考口径）。给它参照解，让它判命中与给分。"""
    import litellm

    name = model_slug if model_slug.startswith("openrouter/") \
        else f"openrouter/{model_slug}"

    async def judge(case: RegressionCase, answer: str) -> dict[str, Any]:
        prompt = (
            "你是对照裁判。下面是一个历史排障案例的参照解与被测答复，"
            "请判定被测答复的根因判断是否命中参照根因，并给 0-10 分。\n"
            f"参照根因：{case.root_cause}\n"
            f"参照关键动作：{'；'.join(a.label for a in case.key_actions)}\n"
            f"被测答复：\n{answer[:4000]}\n"
            "只输出 JSON：{\"hit\": true/false, \"score\": 0-10, "
            "\"comment\": \"一句话理由\"}")
        resp = await litellm.acompletion(
            model=name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400)
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        cost = config.estimate_cost(
            model_slug,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0)
        parsed: dict[str, Any] = {"raw": text[:500], "cost": cost}
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                parsed.update(hit=bool(data.get("hit")),
                              score=data.get("score"),
                              comment=str(data.get("comment", ""))[:200])
            except json.JSONDecodeError:
                pass
        return parsed
    return judge


# ------------------------------------------------------------------ 归因

@dataclass
class Deviation:
    case_id: str
    phenomenon: str
    layer: str              # 原则 / 手册 / 记忆 / 工具 / 执行 / 模型
    evidence: str
    trace_id: str = ""


def attribute_deviations(case: RegressionCase, check: CheckResult,
                         ) -> list[Deviation]:
    """偏差定位到层（v1 规则口径，写死在此，不许临场猜）：
    - 程序查到的原则性错误 → 原则层；
    - 结论未命中参照根因 → 模型层（判断错误）；
    - 关键动作覆盖 < 60% → 执行层（步骤/动作没走全）。
    定位不到的规则之外情形不臆造，本函数只产出规则内偏差。
    """
    devs: list[Deviation] = []
    for err in check.principle_errors:
        devs.append(Deviation(
            case_id=case.id,
            phenomenon=f"原则性错误（{err.kind}）",
            layer="原则", evidence=err.evidence))
    if not check.conclusion_hit:
        missed = ["/".join(g) for g, hit in
                  zip(case.root_cause_groups, check.hit_groups) if not hit]
        devs.append(Deviation(
            case_id=case.id,
            phenomenon="根因判断未命中参照解",
            layer="模型",
            evidence="未命中的根因关键词组：" + "；".join(missed)))
    if check.actions_total and check.action_coverage < 0.6:
        missing = [a.label for a in case.key_actions
                   if a.id not in check.actions_covered]
        devs.append(Deviation(
            case_id=case.id,
            phenomenon=f"关键动作覆盖不足（{len(check.actions_covered)}/"
                       f"{check.actions_total}）",
            layer="执行",
            evidence="未覆盖动作：" + "；".join(missing)))
    return devs


# ------------------------------------------------------------------ 运行

@dataclass
class CaseRun:
    case_id: str
    title: str
    status: str                       # completed / error / not_run
    run_id: str = ""
    answer: str = ""
    check: CheckResult | None = None
    deviations: list[Deviation] = field(default_factory=list)
    judge: dict[str, Any] | None = None
    cost: float = 0.0                 # 轨迹花费 + judge 估算花费
    model_calls: int = 0
    error: str = ""


@dataclass
class ShadowRun:
    harness_version: str
    model_label: str
    cases: list[CaseRun] = field(default_factory=list)
    stopped_reason: str = ""
    signals_written: dict[str, int] = field(default_factory=dict)

    @property
    def principle_errors(self) -> list[Deviation]:
        return [d for c in self.cases for d in c.deviations
                if d.layer == "原则"]


async def run_shadow(*, cases: list[RegressionCase], bundle: HarnessBundle,
                     config: Config, signal_store: SignalStore,
                     unit_factory: UnitFactory,
                     judge_fn: JudgeFn | None = None,
                     max_calls_per_case: int = 8) -> ShadowRun:
    """逐案例回放双跑。unit_factory 决定真实模型还是 Mock（测试用）。"""
    from dataclasses import replace
    run = ShadowRun(harness_version=bundle.version,
                    model_label=config.model)
    memory_index = MemoryIndex(bundle.root / "memory")
    trajectory_store = SqliteTrajectoryStore(config.db_path)
    checkpoint_store = SqliteCheckpointStore(config.db_path)
    quota_stop = False

    for case in cases:
        if quota_stop:
            run.cases.append(CaseRun(case_id=case.id, title=case.title,
                                     status="not_run"))
            continue
        cr = CaseRun(case_id=case.id, title=case.title, status="completed")
        assembled = assemble(
            bundle, query=f"{case.title}\n{case.brief}",
            memory_index=memory_index)
        unit = unit_factory(case, assembled)
        registry = UnitRegistry()
        registry.register(unit)
        run_config = replace(config, harness_version=bundle.version)
        orchestrator = Orchestrator(
            registry=registry, trajectory_store=trajectory_store,
            checkpoint_store=checkpoint_store, config=run_config,
            approval_policy=assembled.approval_policy)
        plan = Plan(
            id=f"shadow-{case.id}",
            steps=[Step(id="s1-diagnose", primitive="sequence",
                        unit=unit.unit_id,
                        input=f"## 症状简报\n{case.brief}\n\n"
                              f"## 附带材料\n{case.materials}")],
            budget_limit=assembled.budget_limit or 1.0)
        result = await orchestrator.run(lambda: plan, task=case.title,
                                        fallback_unit=unit.unit_id)
        cr.run_id = result.run_id
        cr.model_calls += 1
        cr.answer = "\n\n".join(o.text for o in result.outputs)
        try:
            export = trajectory_store.export(result.run_id)
            cr.cost += float(export["totals"]["cost"])
            error_text = " ".join(
                str(e.get("payload", {})) for e in export["events"]
                if e["type"] == "error")
        except Exception:
            error_text = ""
        if result.status != "success":
            cr.status = "error"
            cr.error = (error_text or result.status)[:300]
            if _is_quota_error(error_text) or _is_quota_error(cr.error):
                quota_stop = True
                run.stopped_reason = (
                    "模型通道额度/上限错误，已按纪律停止，"
                    "未跑案例标 not_run")
                m = re.search(r"Prompt tokens limit exceeded: "
                              r"(\d+) > (\d+)", error_text)
                if m:
                    run.stopped_reason = (
                        f"提示词 {m.group(1)} tokens 超过 OpenRouter key "
                        f"允许的提示上限 {m.group(2)} tokens"
                        "（组装提示含原则+手册全文，属设计口径，不缩减），"
                        "已按纪律停止，未跑案例标 not_run；"
                        "需 li 调高 key 限额或充值后再跑")
            run.cases.append(cr)
            continue

        cr.check = check_answer(
            case, cr.answer,
            extra_sources=[assembled.playbook_full, assembled.core_memory,
                           *(e.render() for e in assembled.memory_hits)])
        cr.deviations = attribute_deviations(case, cr.check)
        for dev in cr.deviations:
            dev.trace_id = cr.run_id

        if judge_fn is not None and cr.model_calls < max_calls_per_case:
            try:
                cr.judge = await judge_fn(case, cr.answer)
                cr.model_calls += 1
                if cr.judge.get("cost"):
                    cr.cost += float(cr.judge["cost"])
            except Exception as exc:  # judge 挂了不拖垮主判定
                cr.judge = {"error": str(exc)[:200]}
                if _is_quota_error(str(exc)):
                    quota_stop = True
                    run.stopped_reason = ("judge 调用遇额度错误（402 类），"
                                          "已按纪律停止后续案例")
        _write_signals(signal_store, case, cr, bundle.version, run)
        run.cases.append(cr)
    return run


def _bump(run: ShadowRun, kind: str) -> None:
    run.signals_written[kind] = run.signals_written.get(kind, 0) + 1


def _write_signals(store: SignalStore, case: RegressionCase, cr: CaseRun,
                   harness_version: str, run: ShadowRun) -> None:
    """信号入账（范围 D）：每案例一条 outcome；结论与参照不符或有原则性
    错误时再产一条 correction 候选——出处写明回放对照，非 li 亲口纠正。"""
    assert cr.check is not None
    check = cr.check
    store.append(Signal(
        kind="outcome", source="shadow-replay", trace_id=cr.run_id,
        task=case.title, harness_version=harness_version,
        content=(f"回放对照：结论命中={check.conclusion_hit}，"
                 f"关键动作覆盖 {len(check.actions_covered)}/"
                 f"{check.actions_total}，原则性错误 "
                 f"{len(check.principle_errors)} 条"),
        reference=f"参照解出处：{case.source}；案例 {case.id}",
        extra={"case_id": case.id, "origin": "replay_contrast"}))
    _bump(run, "outcome")
    if (not check.conclusion_hit) or check.principle_errors:
        store.append(Signal(
            kind="correction", source="shadow-replay",
            trace_id=cr.run_id, task=case.title,
            harness_version=harness_version,
            content=("回放对照产生的 correction 候选（非 li 亲口纠正）："
                     + "；".join(d.phenomenon for d in cr.deviations)),
            reference=(f"参照解出处：{case.source}；参照根因："
                       f"{case.root_cause}"),
            extra={"case_id": case.id, "origin": "replay_contrast",
                   "credibility": "回放对照生成，可信度低于 li 本人 "
                                  "correction，阶段 4 提炼时须降权"}))
        _bump(run, "correction")
    if cr.judge and "hit" in cr.judge:
        store.append(Signal(
            kind="judge", source="shadow-judge（模型自评，仅参考）",
            trace_id=cr.run_id, task=case.title,
            harness_version=harness_version,
            content=(f"judge 判定 hit={cr.judge.get('hit')}，"
                     f"score={cr.judge.get('score')}，"
                     f"{cr.judge.get('comment', '')}"),
            reference=f"案例 {case.id}，run {cr.run_id}",
            extra={"case_id": case.id,
                   "hard_check_hit": check.conclusion_hit,
                   "agrees_with_hard_check":
                       cr.judge.get("hit") == check.conclusion_hit}))
        _bump(run, "judge")


# ------------------------------------------------------------------ 报告

def render_report(run: ShadowRun, *, generated_at: str) -> str:
    lines: list[str] = []
    lines.append("# 影子报告 v1（阶段 3 · 历史回放版）")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 驾驭层版本：{run.harness_version}（内容哈希）")
    lines.append(f"- 模型：{run.model_label}（经 OpenRouter）")
    lines.append("- 形态：历史回放双跑——只给症状简报，ShadowClaw 独立"
                 "排障后与参照解对照；非真实任务同步双跑。")
    lines.append("- 判定口径：程序硬查为验收口径（结论命中=根因关键词"
                 "组全中，或同义表达组在全答复范围全中——语义兜底；"
                 "动作覆盖=参照动作关键词命中数）；judge 为模型"
                 "自评，仅参考，与硬查不一致时以硬查为准。")
    if run.stopped_reason:
        lines.append(f"- ⚠️ 中途停止：{run.stopped_reason}")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 案例 | 结论命中 | 动作覆盖 | 花费 | 偏差数 | run_id |")
    lines.append("|---|---|---|---|---|---|")
    for cr in run.cases:
        if cr.status == "not_run":
            lines.append(f"| {cr.title} | 未跑 | 未跑 | - | - | - |")
            continue
        if cr.status == "error":
            lines.append(f"| {cr.title} | 运行失败 | - | ${cr.cost:.4f} | "
                         f"- | {cr.run_id} |")
            continue
        assert cr.check is not None
        hit = "✅" if cr.check.conclusion_hit else "❌"
        lines.append(
            f"| {cr.title} | {hit} | {len(cr.check.actions_covered)}/"
            f"{cr.check.actions_total} | ${cr.cost:.4f} | "
            f"{len(cr.deviations)} | {cr.run_id} |")
    lines.append("")
    lines.append("## 原则性错误（验收线：应为 0）")
    lines.append("")
    completed = [c for c in run.cases if c.status == "completed"]
    perr = run.principle_errors
    if not completed:
        lines.append("未能判定：本轮没有案例跑完（见上方中途停止原因），"
                     "不作「无原则性错误」的验收结论。")
    elif not perr:
        lines.append("无。程序硬查未发现编造数据或未核实断言。")
    else:
        for d in perr:
            lines.append(f"- [{d.case_id}] {d.phenomenon}——{d.evidence}"
                         f"（轨迹 {d.trace_id}）")
    lines.append("")
    lines.append("## 偏差清单（定位到层）")
    lines.append("")
    all_devs = [d for c in run.cases for d in c.deviations]
    if not completed:
        lines.append("无已跑完案例，未产生偏差清单。")
    elif not all_devs:
        lines.append("无偏差。")
    else:
        by_layer: dict[str, int] = {}
        for d in all_devs:
            by_layer[d.layer] = by_layer.get(d.layer, 0) + 1
            lines.append(f"- [{d.layer}] {d.case_id}：{d.phenomenon}。"
                         f"证据：{d.evidence}（轨迹 {d.trace_id}）")
        lines.append("")
        lines.append("按层分布：" + "，".join(
            f"{k} {v} 条" for k, v in sorted(by_layer.items())))
    lines.append("")
    lines.append("## judge 参考（模型自评口径，不作验收依据）")
    lines.append("")
    judged = [c for c in run.cases if c.judge and "hit" in c.judge]
    if not judged:
        lines.append("本轮无可用 judge 结果。")
    else:
        agree = sum(1 for c in judged
                    if c.check and c.judge["hit"] == c.check.conclusion_hit)
        for c in judged:
            lines.append(f"- {c.case_id}：hit={c.judge.get('hit')}，"
                         f"score={c.judge.get('score')}，"
                         f"{c.judge.get('comment', '')}")
        lines.append(f"- 与硬查一致率：{agree}/{len(judged)}"
                     "（校准口径见设计 Step 7，低于 70% 时 judge 权重归零）")
    lines.append("")
    lines.append("## 信号入账汇总")
    lines.append("")
    if run.signals_written:
        for kind, n in sorted(run.signals_written.items()):
            lines.append(f"- {kind}：{n} 条")
        lines.append("- correction 均为回放对照产生的候选（非 li 亲口"
                     "纠正），已在信号 extra 里标 origin 与降权说明。")
    else:
        lines.append("本轮未写入信号。")
    lines.append("")
    errors = [c for c in run.cases if c.status == "error"]
    if errors:
        lines.append("## 运行失败")
        lines.append("")
        for c in errors:
            lines.append(f"- {c.case_id}：{c.error}")
        lines.append("")
    return "\n".join(lines)
