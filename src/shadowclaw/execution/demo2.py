"""阶段 2 验收演示：排障场景组装 + GitHub 工具接入（全程离线）。

任务：joesaas/data-pipeline 的 CI 红了，按排障手册走——定界 → 固定一手
证据 → 分层排除 → 定根因出结论 → 尝试处置（给 issue 打标签，写操作）。

链路要点（都是阶段 2 的验收面）：
- 上下文由驾驭层场景组装器产出（原则在最前、手册全文、记忆核心+检索）；
- 单元是 AdkUnit + 脚本化 LLM，经 FakeGitHub 真调用工具，轨迹有
  tool_call 事件；只读工具被护栏策略默认放行（步骤不设审批门）；
- 最后的写步骤 requires_approval，风险级 write → 护栏 ASK → 无人值守
  fail-closed 落成 DENY：approval 事件进轨迹，标签没打上（预期行为）；
- 轨迹的 harness_version 是加载器按内容算出的真实版本号，非占位常量；
- 事实落板挂了驾驭层冲突检查 guard（本演示事实无冲突，guard 静默）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types
from pydantic import Field

from ..harness.assembler import AssembledContext, assemble
from ..harness.conflicts import KeyValueConflictGuard
from ..harness.loader import HarnessBundle, load_harness
from ..harness.memory import MemoryIndex
from .checkpoint import SqliteCheckpointStore
from .config import Config
from .contracts import UnitContext, UnitResult
from .github_tools import FakeGitHub
from .orchestrator import Orchestrator, RunResult
from .plan import Plan, Step
from .trajectory import SqliteTrajectoryStore
from .units import AdkUnit, MockUnit, UnitRegistry

DEMO2_TASK = "joesaas/data-pipeline 的 GitHub CI 又红了，按排障手册查清原因并给出结论。"

OWNER, REPO = "joesaas", "data-pipeline"

# 写步骤的风险声明：由计划/场景显式给出，护栏策略据此查矩阵（不猜）。
DEMO2_STEP_RISKS = {"s5-label": "write"}


class ScriptedToolLlm(BaseLlm):
    """脚本化 LLM（离线确定性）：script 是逐轮回应清单。

    每轮或是 ("call", 工具名, 参数) —— 回一个 function_call 让 ADK 真的
    去执行工具；或是 ("text", 正文) —— 回最终文本（FACT: 行按 AdkUnit
    约定提议事实）。脚本用完回兜底文本，不让运行挂死。
    """

    script: list = Field(default_factory=list)

    async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        turn = self.script.pop(0) if self.script else ("text", "（脚本结束）")
        if turn[0] == "call":
            _, name, args = turn
            part = genai_types.Part(
                function_call=genai_types.FunctionCall(name=name, args=args))
        else:
            part = genai_types.Part(text=turn[1])
        yield LlmResponse(
            content=genai_types.Content(role="model", parts=[part]),
            turn_complete=True)


def _scope_llm() -> ScriptedToolLlm:
    return ScriptedToolLlm(model="mock", script=[
        ("call", "get_repository", {"owner": OWNER, "repo": REPO}),
        ("text",
         "定界结论：仓库 joesaas/data-pipeline，默认分支 main；红的是 CI "
         "工作流而非部署，范围锁定在 CI/测试层，下一步固定证据。\n"
         "FACT: 仓库：joesaas/data-pipeline（默认分支 main）"),
    ])


def _evidence_llm() -> ScriptedToolLlm:
    return ScriptedToolLlm(model="mock", script=[
        ("call", "list_workflow_runs", {"owner": OWNER, "repo": REPO}),
        ("call", "get_workflow_run",
         {"owner": OWNER, "repo": REPO, "run_id": 901}),
        ("text",
         "一手证据已固定：最近一次 CI（run 901）失败于 test 步骤，日志摘录"
         "显示 node:sqlite 不可用且运行器 Node 版本低于项目要求。\n"
         "FACT: CI 结论：failure（run 901，workflow CI）\n"
         "FACT: 失败步骤：test（运行器 Node 20.11.0，项目要求 >=22）\n"
         "FACT: 根因证据：node:sqlite 在 Node 20 下不可用，ingest 测试因此失败"),
    ])


def _exclude_llm() -> ScriptedToolLlm:
    return ScriptedToolLlm(model="mock", script=[
        ("call", "list_issues", {"owner": OWNER, "repo": REPO}),
        ("text",
         "分层排除：open issue 里没有 CI/Node 版本相关条目，排除「已知问题"
         "重现」；失败只发生在 CI 运行器（Node 20），与业务代码无关。\n"
         "FACT: 排除项：非已知 issue 重现，代码在 Node 22 环境下测试全过"),
    ])


def _fixer_llm() -> ScriptedToolLlm:
    # 若审批放行才会真正执行：给 issue 12 打 ci-failure 标签。
    return ScriptedToolLlm(model="mock", script=[
        ("call", "add_issue_label",
         {"owner": OWNER, "repo": REPO, "issue_number": 12,
          "label": "ci-failure"}),
        ("text", "已给 issue 12 打上 ci-failure 标签并跟踪。"),
    ])


def _conclude(ctx: UnitContext) -> UnitResult:
    facts = "；".join(ctx.facts) if ctx.facts else "（无已确认事实）"
    return UnitResult(
        text="结论（已证实）：根因是 CI 运行器 Node 版本过旧——运行器 "
             "20.11.0 低于项目要求 >=22，node:sqlite 不可用致 ingest 测试"
             f"失败。依据事实：{facts}。处置建议（待 li 确认后执行）：把 CI "
             "运行器镜像升到 Node 22 并在部署文档补版本要求；复验走原路——"
             "重跑 CI 看 test 步骤转绿。",
        tokens_in=380, tokens_out=120)


def build_demo2_registry(assembled: AssembledContext,
                         fake: FakeGitHub) -> UnitRegistry:
    instruction = assembled.render_instruction()
    read_tools = fake.tools(allow_write=False)
    registry = UnitRegistry()
    registry.register(AdkUnit(
        "gh-scope", instruction=instruction, model=_scope_llm(),
        model_label="mock", tools=read_tools))
    registry.register(AdkUnit(
        "gh-evidence", instruction=instruction, model=_evidence_llm(),
        model_label="mock", tools=read_tools))
    registry.register(AdkUnit(
        "gh-exclude", instruction=instruction, model=_exclude_llm(),
        model_label="mock", tools=read_tools))
    registry.register(MockUnit(
        "gh-conclude", _conclude,
        instruction="你是汇总单元：只依据事实板已确认事实下结论并分级。"))
    registry.register(AdkUnit(
        "gh-fixer", instruction=instruction, model=_fixer_llm(),
        model_label="mock", tools=fake.tools(allow_write=True)))
    return registry


def build_demo2_plan(assembled: AssembledContext) -> Plan:
    # 步骤顺序与排障手册一致：定界 → 固定证据 → 分层排除 → 定根因 →
    # （修复动作是写操作，过护栏审批，本演示会被 ASK 拦下）。
    return Plan(
        id="demo2-github-ci",
        steps=[
            Step(id="s1-scope", primitive="sequence", unit="gh-scope",
                 input=DEMO2_TASK),
            Step(id="s2-evidence", primitive="sequence", unit="gh-evidence",
                 input="固定一手证据：拉最近 CI 运行与失败日志摘录。"),
            Step(id="s3-exclude", primitive="sequence", unit="gh-exclude",
                 input="分层排除：查 issue 列表，排除已知问题重现。"),
            Step(id="s4-conclude", primitive="sequence", unit="gh-conclude",
                 input="依据事实板定根因并分级，给出结论与处置建议。"),
            Step(id="s5-label", primitive="sequence", unit="gh-fixer",
                 input="给 issue 12 打 ci-failure 标签以便跟踪。",
                 requires_approval=True),
        ],
        budget_limit=assembled.budget_limit or 1.0)


@dataclass
class Demo2Run:
    result: RunResult
    fake: FakeGitHub
    assembled: AssembledContext
    guard: KeyValueConflictGuard
    bundle: HarnessBundle


async def run_demo2(config: Config) -> Demo2Run:
    """组装点：加载驾驭层 → 组装场景 → 建注册表与编排器 → 跑。"""
    bundle = load_harness(config.harness_dir, "troubleshooting")
    index = MemoryIndex(bundle.root / "memory")
    assembled = assemble(bundle, query=DEMO2_TASK, memory_index=index,
                         step_risks=DEMO2_STEP_RISKS)
    fake = FakeGitHub()
    guard = KeyValueConflictGuard()
    run_config = replace(config, harness_version=bundle.version)
    orchestrator = Orchestrator(
        registry=build_demo2_registry(assembled, fake),
        trajectory_store=SqliteTrajectoryStore(config.db_path),
        checkpoint_store=SqliteCheckpointStore(config.db_path),
        config=run_config,
        approval_policy=assembled.approval_policy,
        fact_commit_guard=guard)
    result = await orchestrator.run(
        lambda: build_demo2_plan(assembled), task=DEMO2_TASK,
        fallback_unit="gh-conclude")
    return Demo2Run(result=result, fake=fake, assembled=assembled,
                    guard=guard, bundle=bundle)
