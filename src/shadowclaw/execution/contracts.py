"""最小接口契约（Protocol）：层内各模块只依赖这些抽象，互不直连实现。

依赖倒置的落点——编排器只认识 Unit / TrajectoryStore / ApprovalPolicy /
CheckpointStore 四个小接口（接口隔离：每个接口只含本职责需要的方法）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


@dataclass
class UnitContext:
    """组装好的一次单元调用上下文（由 context.py 拼装，不含 session 历史）。"""
    prompt: str                 # 完整提示文本（instruction + 事实快照 + 本步输入）
    task_input: str             # 本步的原始输入
    facts: list[str] = field(default_factory=list)  # 已提交事实的陈述文本


@dataclass
class UnitResult:
    """单元一次调用的产出包。单元只能「提议」事实，不能直接落板。"""
    text: str
    proposed_facts: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = None          # None = 模型未回报，轨迹层按价格表估算
    tool_calls: list[str] = field(default_factory=list)


@runtime_checkable
class Unit(Protocol):
    """执行单元契约：MockUnit 与 AdkUnit 同契约可互换（里氏替换）。"""
    unit_id: str
    model: str                          # 模型标签，用于花费估算与轨迹

    async def run(self, ctx: UnitContext) -> UnitResult:
        ...


@runtime_checkable
class TrajectoryStore(Protocol):
    """轨迹存储契约：写事件、按 run 读回。本阶段只有 SQLite 一个实现。"""

    def start_run(self, run_id: str, harness_version: str,
                  plan: dict[str, Any]) -> None:
        ...

    def append(self, event: dict[str, Any]) -> None:
        ...

    def finish_run(self, run_id: str, status: str) -> None:
        ...

    def events(self, run_id: str) -> list[dict[str, Any]]:
        ...

    def list_runs(self) -> list[str]:
        ...


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@runtime_checkable
class ApprovalPolicy(Protocol):
    """审批策略契约：给定步骤上下文，返回放行 / 先问 / 拒绝。"""

    def decide(self, *, step_id: str, unit_id: str | None,
               primitive: str) -> Decision:
        ...


@dataclass
class Checkpoint:
    """计划步级检查点：恢复 = 读它、跳过已完成步、从断步重跑。"""
    run_id: str
    plan: dict[str, Any]
    task: str
    next_step_index: int
    facts: list[dict[str, Any]] = field(default_factory=list)  # 已提交事实
    spent: float = 0.0


@runtime_checkable
class CheckpointStore(Protocol):
    """检查点存储契约。"""

    def save(self, checkpoint: Checkpoint) -> None:
        ...

    def load(self, run_id: str) -> Checkpoint | None:
        ...

    def clear(self, run_id: str) -> None:
        ...
