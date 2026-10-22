"""编排器 Orchestrator：只管编排，不实现原语之外的任何职责。

管的事：计划校验（含重出一次与降级保底）、按计划串行执行、委派的隔离
子执行、审批挂钩、预算硬顶、步级检查点。状态归 FactBoard、轨迹归
Recorder、恢复归 CheckpointStore——都以接口注入（依赖倒置）。

校验与降级的落点在本模块（plan.py 只提供校验器与降级构造）：
supplier 给的计划校验不过 → 再调一次 supplier 重出 → 还不过 →
degrade() 降为单单元串行保底执行，全程进轨迹（plan_validated 留痕）。

五原语阶段 5 齐套：sequence / delegate（阶段 1）+ route / parallel /
reflect（阶段 5）。步骤间不做隐式产出传递，数据流靠计划里写明的
input 与事实板（显式，胜过魔法）；route 的 prev_output 取最近一步
产出，是唯一的例外，口径写在 _execute_route。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import context as context_mod
from .approval import FailClosedPolicy
from .checkpoint import SqliteCheckpointStore
from .config import Config
from .contracts import (ApprovalPolicy, Checkpoint, CheckpointStore,
                        Decision, TrajectoryStore, UnitResult)
from .facts import FactBoard
from .plan import Plan, Step, degrade, validate_plan
from .trajectory import EventType, Recorder
from .units import UnitRegistry


# 并行上限（执行层设计 Step 6：全局默认 4，Plan 可调低不可调高；
# v1 固定 4，分支数超过时排队，不整单拒绝）。
_PARALLEL_LIMIT = 4


def evaluate_checklist(checklist: list[str],
                       result: UnitResult) -> list[str]:
    """reflect 规则检查：返回未过原因清单（空 = 通过）。确定性口径。

    条目形态由 plan._validate_reflect 在计划校验时把关，这里只执行：
    contains / not_contains 看产出正文，min_length / max_length 看
    正文字数，fact_proposed 看本轮是否提议了事实。
    """
    reasons: list[str] = []
    for item in checklist:
        if item == "fact_proposed":
            if not result.proposed_facts:
                reasons.append("fact_proposed 未过：本轮产出未提议任何事实")
        elif item.startswith("contains:"):
            needle = item[len("contains:"):]
            if needle not in result.text:
                reasons.append(f"contains:{needle} 未过：产出未包含该内容")
        elif item.startswith("not_contains:"):
            needle = item[len("not_contains:"):]
            if needle in result.text:
                reasons.append(f"not_contains:{needle} 未过：产出包含禁用内容")
        elif item.startswith("min_length:"):
            if len(result.text) < int(item.split(":", 1)[1]):
                reasons.append(f"{item} 未过：产出长度 {len(result.text)}")
        elif item.startswith("max_length:"):
            if len(result.text) > int(item.split(":", 1)[1]):
                reasons.append(f"{item} 未过：产出长度 {len(result.text)}")
    return reasons


@dataclass
class StepOutput:
    step_id: str
    unit_id: str | None
    text: str


@dataclass
class RunResult:
    run_id: str
    status: str                       # success / failed / denied / budget_stopped
    outputs: list[StepOutput] = field(default_factory=list)
    spent: float = 0.0
    degraded: bool = False


class _Stopped(Exception):
    """内部控制流：预算触顶 / 审批拒绝时优雅中止，携带结局状态。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass
class _RunState:
    recorder: Recorder
    plan: Plan
    task: str
    spent: float = 0.0
    outputs: list[StepOutput] = field(default_factory=list)

    @property
    def budget_limit(self) -> float:
        return self.plan.budget_limit or 0.0


class Orchestrator:
    def __init__(self, *, registry: UnitRegistry,
                 trajectory_store: TrajectoryStore,
                 checkpoint_store: CheckpointStore,
                 config: Config,
                 approval_policy: ApprovalPolicy | None = None,
                 attended: bool = False,
                 ask_handler: Callable[[str], bool] | None = None,
                 fact_commit_guard: Callable | None = None):
        self._registry = registry
        self._store = trajectory_store
        self._checkpoints = checkpoint_store
        self._config = config
        self._policy = approval_policy or FailClosedPolicy()
        self._attended = attended
        self._ask_handler = ask_handler
        # 阶段 2 注入点：事实落板冲突检查（harness.conflicts 的 guard），
        # 不给则行为同阶段 1（只去重）。
        self._fact_guard = fact_commit_guard

    # ------------------------------------------------------------------ run
    async def run(self, plan_supplier: Callable[[], Plan], *, task: str,
                  fallback_unit: str,
                  run_id: str | None = None) -> RunResult:
        """新开一次运行：校验（可重出一次）→ 执行 → 落轨迹与检查点。"""
        run_id = run_id or uuid.uuid4().hex[:12]
        plan, degraded, attempts = self._prepare_plan(
            plan_supplier, fallback_unit=fallback_unit, task=task)
        self._store.start_run(run_id, self._config.harness_version,
                              plan.to_dict())
        recorder = Recorder(self._store, self._config, run_id)
        state = _RunState(recorder=recorder, plan=plan, task=task)
        recorder.emit(EventType.RUN_STARTED,
                      payload={"task": task, "degraded": degraded})
        recorder.emit(EventType.PLAN_VALIDATED, payload={
            "plan_id": plan.id, "degraded": degraded, "attempts": attempts})
        board = FactBoard(commit_guard=self._fact_guard)
        status = await self._execute(state, board, plan.steps, depth=0,
                                     seed_facts=[], run_id=run_id,
                                     start_index=0, top=True)
        return self._finish(state, run_id, status, degraded)

    async def resume(self, run_id: str) -> RunResult:
        """从检查点续跑：跳过已完成步，断步整步重跑（步内不做细粒度恢复）。"""
        cp = self._checkpoints.load(run_id)
        if cp is None:
            raise KeyError(f"没有可恢复的检查点：{run_id}")
        plan = Plan.from_dict(cp.plan)
        self._store.start_run(run_id, self._config.harness_version,
                              plan.to_dict())
        recorder = Recorder(self._store, self._config, run_id)
        state = _RunState(recorder=recorder, plan=plan, task=cp.task,
                          spent=cp.spent)
        recorder.emit(EventType.RUN_STARTED, payload={
            "task": cp.task, "resumed": True,
            "from_step_index": cp.next_step_index})
        recorder.emit(EventType.PLAN_VALIDATED,
                      payload={"plan_id": plan.id, "resumed": True})
        board = FactBoard(commit_guard=self._fact_guard)
        board.restore(cp.facts)
        status = await self._execute(state, board, plan.steps, depth=0,
                                     seed_facts=[], run_id=run_id,
                                     start_index=cp.next_step_index, top=True)
        return self._finish(state, run_id, status, degraded=False)

    # ------------------------------------------------------------- internal
    def _prepare_plan(self, supplier: Callable[[], Plan], *,
                      fallback_unit: str, task: str
                      ) -> tuple[Plan, bool, list[list[dict]]]:
        attempts: list[list[dict]] = []
        plan = supplier()
        problems = validate_plan(plan, self._registry.ids(),
                                 max_depth=self._config.max_depth,
                                 budget_hard_cap=self._config.budget_hard_cap)
        attempts.append([{"kind": p.kind, "message": p.message}
                         for p in problems])
        if not problems:
            return plan, False, attempts
        plan = supplier()   # 重出一次
        problems = validate_plan(plan, self._registry.ids(),
                                 max_depth=self._config.max_depth,
                                 budget_hard_cap=self._config.budget_hard_cap)
        attempts.append([{"kind": p.kind, "message": p.message}
                         for p in problems])
        if not problems:
            return plan, False, attempts
        return degrade(plan, fallback_unit, task), True, attempts

    def _finish(self, state: _RunState, run_id: str, status: str,
                degraded: bool) -> RunResult:
        state.recorder.emit(EventType.RUN_FINISHED,
                            payload={"status": status, "spent": state.spent})
        self._store.finish_run(run_id, status)
        if status == "success":
            self._checkpoints.clear(run_id)
        return RunResult(run_id=run_id, status=status,
                         outputs=state.outputs, spent=state.spent,
                         degraded=degraded)

    async def _execute(self, state: _RunState, board: FactBoard,
                       steps: list[Step], *, depth: int,
                       seed_facts: list[str], run_id: str,
                       start_index: int, top: bool) -> str:
        status = "success"
        for index in range(start_index, len(steps)):
            step = steps[index]
            try:
                await self._execute_step(state, board, step, depth=depth,
                                         seed_facts=seed_facts)
            except _Stopped as stop:
                status = stop.status
                break
            except Exception as exc:   # 单元失败：留痕、存检查点、收尾
                state.recorder.emit(EventType.ERROR, step_id=step.id,
                                    payload={"kind": "step_failed",
                                             "message": str(exc)})
                status = "failed"
                break
            if top:
                self._save_checkpoint(state, board, run_id,
                                      next_index=index + 1)
        return status

    async def _execute_step(self, state: _RunState, board: FactBoard,
                            step: Step, *, depth: int,
                            seed_facts: list[str]) -> None:
        recorder = state.recorder
        if step.requires_approval:
            self._gate(state, step)
        recorder.emit(EventType.STEP_STARTED, step_id=step.id,
                      unit_id=step.unit, payload={"primitive": step.primitive})
        if step.primitive == "delegate":
            await self._execute_delegate(state, board, step, depth=depth,
                                         seed_facts=seed_facts)
        elif step.primitive == "route":
            await self._execute_route(state, board, step,
                                      seed_facts=seed_facts)
        elif step.primitive == "parallel":
            await self._execute_parallel(state, board, step,
                                         seed_facts=seed_facts)
        elif step.primitive == "reflect":
            await self._execute_reflect(state, board, step,
                                        seed_facts=seed_facts)
        else:
            result = await self._call_unit(state, board, step,
                                           seed_facts=seed_facts)
            state.outputs.append(StepOutput(step_id=step.id,
                                            unit_id=step.unit,
                                            text=result.text))
        recorder.emit(EventType.STEP_FINISHED, step_id=step.id,
                      unit_id=step.unit)

    async def _call_unit(self, state: _RunState, board: FactBoard, step: Step,
                         *, seed_facts: list[str],
                         commit_facts: bool = True) -> UnitResult:
        """调一个单元并处理其产出：工具/模型事件、预算记账、事实提议。

        commit_facts=False 时只记 FACT_PROPOSED、不落板（reflect 草稿
        轮、critic 评审用——未通过评审的提议不许以「已落板」面貌进
        轨迹，通过后由调用方经真板正式提议落板）。"""
        recorder = state.recorder
        unit = self._registry.get(step.unit)
        facts_for_ctx = list(dict.fromkeys(seed_facts + board.snapshot()))
        ctx = context_mod.assemble(
            instruction=getattr(unit, "instruction", "") or "",
            task_input=step.input or state.task,
            committed_facts=facts_for_ctx)
        result = await unit.run(ctx)
        for name in result.tool_calls:
            recorder.emit(EventType.TOOL_CALL, step_id=step.id,
                          unit_id=step.unit, payload={"tool": name})
        event = recorder.emit(
            EventType.MODEL_CALL, step_id=step.id, unit_id=step.unit,
            model=unit.model, tokens_in=result.tokens_in,
            tokens_out=result.tokens_out, cost=result.cost,
            payload={"text_preview": result.text[:200]})
        cost = event["cost"] or 0.0
        state.spent += cost
        limit = state.budget_limit or self._config.budget_hard_cap
        over = state.spent > limit
        recorder.emit(EventType.BUDGET_CHECKED, step_id=step.id,
                      payload={"spent": state.spent, "limit": limit,
                               "over": over})
        if over:
            raise _Stopped("budget_stopped")
        for statement in result.proposed_facts:
            fact = board.propose(statement, source_unit=step.unit or "",
                                 step_id=step.id)
            recorder.emit(EventType.FACT_PROPOSED, step_id=step.id,
                          unit_id=step.unit,
                          payload={"fact_id": fact.id, "statement": statement})
            if commit_facts and board.commit(fact):
                recorder.emit(EventType.FACT_COMMITTED, step_id=step.id,
                              unit_id=step.unit,
                              payload={"fact_id": fact.id,
                                       "statement": statement})
        return result

    async def _execute_delegate(self, state: _RunState, board: FactBoard,
                                step: Step, *, depth: int,
                                seed_facts: list[str]) -> None:
        recorder = state.recorder
        assert step.subplan is not None
        recorder.emit(EventType.DELEGATE_STARTED, step_id=step.id,
                      payload={"subplan_id": step.subplan.id,
                               "brief": step.input, "depth": depth + 1})
        # 隔离：子级用全新事实板，只带走父级已提交事实的只读快照作种子；
        # 子级的提议在子板内落板，返回时才由父级编排器登记到父板。
        sub_board = FactBoard(commit_guard=self._fact_guard)
        sub_seed = list(dict.fromkeys(seed_facts + board.snapshot()))
        sub_steps = [
            Step(id=f"{step.id}/{s.id}", primitive=s.primitive, unit=s.unit,
                 input=s.input or step.input, subplan=s.subplan,
                 requires_approval=s.requires_approval, budget=s.budget)
            for s in step.subplan.steps
        ]
        status = await self._execute(state, sub_board, sub_steps,
                                     depth=depth + 1, seed_facts=sub_seed,
                                     run_id=state.recorder.run_id,
                                     start_index=0, top=False)
        returned = sub_board.snapshot()
        for statement in returned:
            fact = board.propose(statement, source_unit="(delegate)",
                                 step_id=step.id)
            recorder.emit(EventType.FACT_PROPOSED, step_id=step.id,
                          payload={"fact_id": fact.id, "statement": statement,
                                   "via_delegate": step.id})
            if board.commit(fact):
                recorder.emit(EventType.FACT_COMMITTED, step_id=step.id,
                              payload={"fact_id": fact.id,
                                       "statement": statement,
                                       "via_delegate": step.id})
        recorder.emit(EventType.DELEGATE_FINISHED, step_id=step.id,
                      payload={"status": status, "facts_returned": returned})
        if status != "success":
            raise _Stopped(status)

    # ------------------------------------------------------- route 原语
    async def _execute_route(self, state: _RunState, board: FactBoard,
                             step: Step, *, seed_facts: list[str]) -> None:
        """路由：规则自上而下首中即选，全不中走 default，再无则本步失败。

        规则取值来源（plan.ROUTE_SOURCES）：prev_output = 全局最近一步
        的产出文本（唯一的隐式数据流，口径在此写明）；input = 本步
        输入（缺省任务文本）；facts = 事实板已提交事实拼接。选择过程
        （命中规则、选中者、落选者）全量记 route_decided 事件。
        """
        recorder = state.recorder
        spec = step.route or {}
        rules = spec.get("rules") or []
        sources = {
            "prev_output": (state.outputs[-1].text
                            if state.outputs else ""),
            "input": step.input or state.task,
            "facts": "\n".join(board.snapshot()),
        }
        chosen: str | None = None
        matched: Any = None
        for i, rule in enumerate(rules):
            when = rule.get("when") or {}
            needle = str(when.get("contains") or "")
            if needle and needle in sources.get(str(when.get("source")),
                                                ""):
                chosen = rule.get("unit")
                matched = i
                break
        if chosen is None and spec.get("default"):
            chosen = spec["default"]
            matched = "default"
        candidates = [r.get("unit") for r in rules]
        if spec.get("default"):
            candidates.append(spec["default"])
        recorder.emit(EventType.ROUTE_DECIDED, step_id=step.id,
                      unit_id=chosen,
                      payload={"matched_rule": matched, "chosen": chosen,
                               "candidates": candidates,
                               "rejected": [c for c in candidates
                                            if c != chosen]})
        if chosen is None:
            raise RuntimeError(
                f"route 步 {step.id} 无规则命中且未设 default，本步失败")
        result = await self._call_unit(
            state, board,
            Step(id=step.id, primitive="sequence", unit=chosen,
                 input=step.input),
            seed_facts=seed_facts)
        state.outputs.append(StepOutput(step_id=step.id, unit_id=chosen,
                                        text=result.text))

    # ---------------------------------------------------- parallel 原语
    async def _execute_parallel(self, state: _RunState, board: FactBoard,
                                step: Step, *,
                                seed_facts: list[str]) -> None:
        """并行：分支并发跑（协程池，上限 _PARALLEL_LIMIT；单元契约是
        async，线程池需跨线程桥接事件循环，故用 asyncio 而非线程池，
        隔离/失败隔离/合并语义不变）。

        每分支独立事实板（隔离副本，只带父板快照作种子）；分支产出先
        在分支板内落板，整组结束后由编排器按计划顺序把分支已提交
        事实逐条提议到父板——单写者与冲突检查口径不变（冲突拒落板、
        留痕）。单分支失败不拖垮整组（记 error 事件并在结果里标明）；
        全部分支失败才算本步失败。预算触顶（_Stopped）不隔离，直接
        中止整单。
        """
        recorder = state.recorder
        recorder.emit(EventType.PARALLEL_STARTED, step_id=step.id,
                      payload={"branches": [b.id for b in step.branches]})
        parent_seed = list(dict.fromkeys(seed_facts + board.snapshot()))
        semaphore = asyncio.Semaphore(_PARALLEL_LIMIT)

        async def run_branch(branch: Step):
            branch_board = FactBoard(commit_guard=self._fact_guard)
            bstep = Step(id=f"{step.id}/{branch.id}", primitive="sequence",
                         unit=branch.unit,
                         input=branch.input or step.input,
                         requires_approval=branch.requires_approval)
            async with semaphore:
                if bstep.requires_approval:
                    self._gate(state, bstep)
                result = await self._call_unit(
                    state, branch_board, bstep, seed_facts=parent_seed)
            return result, branch_board

        outcomes = await asyncio.gather(
            *(run_branch(b) for b in step.branches),
            return_exceptions=True)
        succeeded: list[str] = []
        failed: list[dict[str, str]] = []
        for branch, outcome in zip(step.branches, outcomes):
            if isinstance(outcome, _Stopped):
                raise outcome
            if isinstance(outcome, BaseException):
                failed.append({"branch": branch.id,
                               "error": str(outcome)})
                recorder.emit(EventType.ERROR, step_id=step.id,
                              payload={"kind": "parallel_branch_failed",
                                       "branch": branch.id,
                                       "message": str(outcome)})
                continue
            result, branch_board = outcome
            succeeded.append(branch.id)
            state.outputs.append(StepOutput(
                step_id=f"{step.id}/{branch.id}", unit_id=branch.unit,
                text=result.text))
            for statement in branch_board.snapshot():
                fact = board.propose(statement,
                                     source_unit=branch.unit or "",
                                     step_id=step.id)
                recorder.emit(EventType.FACT_PROPOSED, step_id=step.id,
                              unit_id=branch.unit,
                              payload={"fact_id": fact.id,
                                       "statement": statement,
                                       "via_parallel": step.id,
                                       "branch": branch.id})
                if board.commit(fact):
                    recorder.emit(EventType.FACT_COMMITTED, step_id=step.id,
                                  unit_id=branch.unit,
                                  payload={"fact_id": fact.id,
                                           "statement": statement,
                                           "via_parallel": step.id,
                                           "branch": branch.id})
        recorder.emit(EventType.PARALLEL_FINISHED, step_id=step.id,
                      payload={"succeeded": succeeded, "failed": failed})
        if not succeeded:
            raise RuntimeError(
                f"parallel 步 {step.id} 全部 {len(failed)} 个分支失败")

    # ----------------------------------------------------- reflect 原语
    async def _execute_reflect(self, state: _RunState, board: FactBoard,
                               step: Step, *,
                               seed_facts: list[str]) -> None:
        """反思：产出过检查（规则清单 + 可选 critic 自评），不过带反馈
        重跑原单元，最多 max_rounds 轮；超限按失败处理（任务口径，
        优先于设计稿的「交最好一版」）。

        每轮在草稿事实板上跑（打回轮的事实提议不落真板）；通过轮的
        提议才由编排器落真板。critic 是占位接口：以普通单元身份被
        调用，回复含「打回」判不过、含「通过」判过、都无则按
        fail-closed 判不过（判定无法解析）。
        """
        recorder = state.recorder
        feedback = ""
        last_reasons: list[str] = []
        # 上下文种子在进循环前固定：真板已提交事实 + 入参种子；
        # 草稿板只承接本轮提议，不作上下文来源。
        reflect_seeds = list(dict.fromkeys(seed_facts + board.snapshot()))
        for round_no in range(1, step.max_rounds + 1):
            scratch = FactBoard(commit_guard=self._fact_guard)
            trial = Step(id=step.id, primitive="sequence", unit=step.unit,
                         input=(step.input or state.task) + feedback)
            result = await self._call_unit(state, scratch, trial,
                                           seed_facts=reflect_seeds,
                                           commit_facts=False)
            reasons = evaluate_checklist(step.checklist, result)
            via = "rules"
            if step.critic is not None:
                via = "rules+critic"
                verdict = await self._call_unit(
                    state, FactBoard(),
                    Step(id=f"{step.id}/critic", primitive="sequence",
                         unit=step.critic,
                         input=("请评审下面的产出是否合格。合格只回复"
                                "「通过」，不合格回复「打回：<理由>」"
                                "。\n\n## 待评审产出\n" + result.text)),
                    seed_facts=reflect_seeds, commit_facts=False)
                if "打回" in verdict.text:
                    reasons.append(f"critic 打回：{verdict.text[:200]}")
                elif "通过" not in verdict.text:
                    reasons.append(
                        "critic 判定无法解析（无「通过/打回」标记，"
                        "fail-closed 按不过处理）")
            recorder.emit(EventType.REFLECT_ROUND, step_id=step.id,
                          unit_id=step.unit,
                          payload={"round": round_no,
                                   "verdict": "pass" if not reasons
                                   else "rejected",
                                   "reasons": reasons, "via": via})
            if not reasons:
                for statement in result.proposed_facts:
                    fact = board.propose(statement,
                                         source_unit=step.unit or "",
                                         step_id=step.id)
                    recorder.emit(EventType.FACT_PROPOSED, step_id=step.id,
                                  unit_id=step.unit,
                                  payload={"fact_id": fact.id,
                                           "statement": statement,
                                           "via_reflect": step.id,
                                           "round": round_no})
                    if board.commit(fact):
                        recorder.emit(EventType.FACT_COMMITTED,
                                      step_id=step.id, unit_id=step.unit,
                                      payload={"fact_id": fact.id,
                                               "statement": statement,
                                               "via_reflect": step.id})
                state.outputs.append(StepOutput(
                    step_id=step.id, unit_id=step.unit, text=result.text))
                return
            last_reasons = reasons
            feedback = (f"\n\n## 反思打回反馈（第 {round_no} 轮）\n"
                        + "\n".join(f"- {r}" for r in reasons)
                        + "\n请针对以上问题重做本步产出。")
        raise RuntimeError(
            f"reflect 步 {step.id} 经过 {step.max_rounds} 轮仍未通过"
            f"检查：{'；'.join(last_reasons)}")

    def _gate(self, state: _RunState, step: Step) -> None:
        recorder = state.recorder
        recorder.emit(EventType.APPROVAL_REQUESTED, step_id=step.id,
                      unit_id=step.unit)
        decision = self._policy.decide(step_id=step.id, unit_id=step.unit,
                                       primitive=step.primitive)
        effective = decision
        if decision is Decision.ASK:
            # fail-closed：无人值守时 ASK = DENY；有人值守且给了 handler
            # 才现场问，答复即最终决定。
            if self._attended and self._ask_handler is not None:
                effective = (Decision.ALLOW if self._ask_handler(step.id)
                             else Decision.DENY)
            else:
                effective = Decision.DENY
        recorder.emit(EventType.APPROVAL_DECIDED, step_id=step.id,
                      unit_id=step.unit,
                      payload={"decision": decision.value,
                               "effective": effective.value})
        if effective is not Decision.ALLOW:
            recorder.emit(EventType.ERROR, step_id=step.id,
                          payload={"kind": "approval_denied",
                                   "decision": decision.value})
            raise _Stopped("denied")

    def _save_checkpoint(self, state: _RunState, board: FactBoard,
                         run_id: str, *, next_index: int) -> None:
        self._checkpoints.save(Checkpoint(
            run_id=run_id, plan=state.plan.to_dict(), task=state.task,
            next_step_index=next_index, facts=board.export_committed(),
            spent=state.spent))
