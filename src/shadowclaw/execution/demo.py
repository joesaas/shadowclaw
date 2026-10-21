"""内置演示场景（验收用）：一次离线、确定性的排障任务。

链路：triage 定界（串行）→ delegate 委派 logcheck 查日志（子单元在隔离
上下文里跑，产出两条事实提议）→ 编排器把子级事实落回父板 →
summarize 读事实板出结论。全程 MockUnit，无网络、无 key。
"""
from __future__ import annotations

from .contracts import UnitContext, UnitResult
from .plan import Plan, Step
from .units import MockUnit, UnitRegistry

DEMO_TASK = "线上 API 服务大面积 500，帮我定界并给出初步结论。"

TRIAGE_TEXT = (
    "定界结论：故障不在数据库与网关层——错误集中在 api 服务自身，"
    "500 伴随文件句柄耗尽告警，范围锁定 api 服务的日志通道。")

LOG_FACTS = [
    "api 服务在 09:12 出现 fd 耗尽告警（Too many open files）",
    "日志轮转脚本昨夜由 v1 升级到 v2，v2 未关闭旧日志句柄",
]

LOG_TEXT = "日志核查完成：确认句柄泄漏点在日志轮转，证据已提议入事实板。"


def _summarize(ctx: UnitContext) -> UnitResult:
    facts = "；".join(ctx.facts) if ctx.facts else "（无已确认事实）"
    return UnitResult(
        text=f"初步结论：根因指向日志轮转 v2 的句柄泄漏。依据事实：{facts}。"
             "建议：回滚轮转脚本至 v1 并重启 api 服务，复验 fd 占用。",
        tokens_in=420, tokens_out=110)


def build_demo_registry() -> UnitRegistry:
    registry = UnitRegistry()
    registry.register(MockUnit(
        "triage",
        UnitResult(text=TRIAGE_TEXT, tokens_in=300, tokens_out=80),
        instruction="你是排障定界单元：只做故障范围定界，不下根因结论。"))
    registry.register(MockUnit(
        "logcheck",
        UnitResult(text=LOG_TEXT, proposed_facts=LOG_FACTS,
                   tokens_in=900, tokens_out=150),
        instruction="你是日志核查单元：只报日志里可核查的事实。"))
    registry.register(MockUnit(
        "summarize", _summarize,
        instruction="你是汇总单元：只依据事实板已确认事实下结论，"
                    "事实之外的推断要标注为建议。"))
    return registry


def build_demo_plan() -> Plan:
    return Plan(
        id="demo-troubleshoot",
        steps=[
            Step(id="s1-triage", primitive="sequence", unit="triage",
                 input=DEMO_TASK),
            Step(id="s2-delegate-logs", primitive="delegate",
                 input="核查 api 服务近一小时日志，找出与句柄耗尽相关的证据。",
                 subplan=Plan(id="demo-logcheck", steps=[
                     Step(id="check", primitive="sequence", unit="logcheck"),
                 ])),
            Step(id="s3-summarize", primitive="sequence", unit="summarize",
                 input="依据事实板给出初步结论与处置建议。"),
        ],
        budget_limit=1.0)
