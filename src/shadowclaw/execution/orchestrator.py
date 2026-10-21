"""编排器 Orchestrator：只管编排，不实现原语之外的任何职责。

管的事：计划校验（含重出一次与降级保底）、按计划串行执行、委派的隔离
子执行、审批挂钩、预算硬顶、步级检查点。状态归 FactBoard、轨迹归
Recorder、恢复归 CheckpointStore——都以接口注入（依赖倒置）。

校验与降级的落点在本模块（plan.py 只提供校验器与降级构造）：
supplier 给的计划校验不过 → 再调一次 supplier 重出 → 还不过 →
degrade() 降为单单元串行保底执行，全程进轨迹（plan_validated 留痕）。

阶段 1 只实现 sequence 与 delegate 两原语；步骤间不做隐式产出传递，
数据流靠计划里写明的 input 与事实板（显式，胜过魔法）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

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
        else:
            result = await self._call_unit(state, board, step,
                                           seed_facts=seed_facts)
            state.outputs.append(StepOutput(step_id=step.id,
                                            unit_id=step.unit,
                                            text=result.text))
        recorder.emit(EventType.STEP_FINISHED, step_id=step.id,
                      unit_id=step.unit)

    async def _call_unit(self, state: _RunState, board: FactBoard, step: Step,
                         *, seed_facts: list[str]) -> UnitResult:
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
            if board.commit(fact):
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
