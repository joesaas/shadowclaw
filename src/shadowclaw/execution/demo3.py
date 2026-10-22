"""阶段 5 验收演示：三新原语（route / parallel / reflect）离线跑通。

任务还是排障味：定界 → 按故障域路由找专家 → 三路并行核查 → 报告
过反思检查。全程 MockUnit 确定性脚本，无网络、无 key。

链路要点（都是阶段 5 的验收面）：
- route：定界产出含「句柄」→ 规则命中 fd-expert，default 的
  general-expert 落选，route_decided 事件记全（命中规则/选中/落选）；
- parallel：logcheck 与 metriccheck 对「故障域」给出同键异值事实，
  合并时后到者被冲突检查拒落板（guard 记录在案）；flakycheck 分支
  直接抛错——失败被隔离，整组照常完成，parallel_finished 标明；
- reflect：reporter 第一版报告缺「根因」、带「未证实」且不提议事实，
  被检查清单打回；第二版带反馈重做后通过，reflect_round 两轮留痕，
  通过轮提议的事实才落真板。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..harness.conflicts import KeyValueConflictGuard
from .checkpoint import SqliteCheckpointStore
from .config import Config
from .contracts import UnitContext, UnitResult
from .orchestrator import Orchestrator, RunResult
from .plan import Plan, Step
from .trajectory import SqliteTrajectoryStore
from .units import MockUnit, UnitRegistry

DEMO3_TASK = "api 服务大面积 500：定界、按域找专家、并行核查、出报告。"

BAD_REPORT = "服务挂了，原因未知，未证实。"

GOOD_REPORT = ("报告：根因是日志轮转 v2 未关闭旧句柄，导致 fd 耗尽、"
               "api 服务大面积 500。处置：回滚轮转脚本至 v1 并重启 "
               "api 服务，复验句柄占用回落。")


def _reporter(ctx: UnitContext) -> UnitResult:
    """脚本化报告单元：收到反思打回反馈才交合格版（演示重跑语义）。"""
    if "反思打回反馈" in ctx.prompt:
        return UnitResult(
            text=GOOD_REPORT,
            proposed_facts=["根因：日志轮转 v2 句柄泄漏"],
            tokens_in=300, tokens_out=90)
    return UnitResult(text=BAD_REPORT, tokens_in=200, tokens_out=30)


def build_demo3_registry() -> UnitRegistry:
    registry = UnitRegistry()
    registry.register(MockUnit(
        "triage",
        UnitResult(text="定界：500 集中在 api 服务，伴随句柄耗尽告警，"
                        "网关与数据库无异常。",
                   tokens_in=200, tokens_out=60),
        instruction="你是定界单元：只圈故障范围，不下根因。"))
    registry.register(MockUnit(
        "fd-expert",
        UnitResult(text="句柄专家结论：日志轮转 v2 打开新文件后未关闭"
                        "旧句柄，fd 持续泄漏。",
                   proposed_facts=["泄漏点：日志轮转 v2 未关闭旧句柄"],
                   tokens_in=400, tokens_out=100),
        instruction="你是文件句柄专家：只查 fd 泄漏类问题。"))
    registry.register(MockUnit(
        "general-expert",
        UnitResult(text="通用专家：未见明确故障域，按常规清单排查。",
                   tokens_in=300, tokens_out=80),
        instruction="你是通用排障专家：无明确故障域时的兜底人选。"))
    registry.register(MockUnit(
        "logcheck",
        UnitResult(text="日志核查：故障域在日志通道。",
                   proposed_facts=["故障域：日志通道"],
                   tokens_in=300, tokens_out=70),
        instruction="你是日志核查单元：只报日志可核查的事实。"))
    registry.register(MockUnit(
        "metriccheck",
        UnitResult(text="指标核查：网络层指标无异常（口径与日志侧不一致）。",
                   proposed_facts=["故障域：网络层"],
                   tokens_in=300, tokens_out=70),
        instruction="你是指标核查单元：只报指标可核查的事实。"))
    registry.register(MockUnit(
        "flakycheck", RuntimeError("指标通道连接超时（模拟分支失败）"),
        instruction="你是不稳定的核查通道：本演示中必然失败。"))
    registry.register(MockUnit(
        "reporter", _reporter,
        instruction="你是报告单元：按检查清单交报告，打回就按反馈重做。"))
    return registry


def build_demo3_plan() -> Plan:
    return Plan(
        id="demo3-primitives",
        steps=[
            Step(id="s1-triage", primitive="sequence", unit="triage",
                 input=DEMO3_TASK),
            Step(id="s2-route", primitive="route",
                 input="按故障域深入分析根因。",
                 route={"rules": [
                     {"when": {"source": "prev_output",
                               "contains": "句柄"},
                      "unit": "fd-expert"}],
                     "default": "general-expert"}),
            Step(id="s3-parallel", primitive="parallel",
                 branches=[
                     Step(id="b-log", unit="logcheck",
                          input="核查日志通道。"),
                     Step(id="b-metric", unit="metriccheck",
                          input="核查网络指标。"),
                     Step(id="b-flaky", unit="flakycheck",
                          input="核查备用通道。"),
                 ]),
            Step(id="s4-report", primitive="reflect", unit="reporter",
                 input="依据已确认事实写排障报告。",
                 checklist=["contains:根因", "not_contains:未证实",
                            "min_length:30", "fact_proposed"],
                 max_rounds=2),
        ],
        budget_limit=1.0)


@dataclass
class Demo3Run:
    result: RunResult
    guard: KeyValueConflictGuard


async def run_demo3(config: Config) -> Demo3Run:
    """组装点：冲突检查 guard 注入编排器（落板口径与驾驭层一致）。"""
    guard = KeyValueConflictGuard()
    orchestrator = Orchestrator(
        registry=build_demo3_registry(),
        trajectory_store=SqliteTrajectoryStore(config.db_path),
        checkpoint_store=SqliteCheckpointStore(config.db_path),
        config=config,
        fact_commit_guard=guard)
    result = await orchestrator.run(
        build_demo3_plan, task=DEMO3_TASK, fallback_unit="triage")
    return Demo3Run(result=result, guard=guard)
