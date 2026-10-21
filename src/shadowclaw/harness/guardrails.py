"""护栏策略：把 guardrails.md 的策略矩阵变成执行层 ApprovalPolicy。

三级映射（矩阵原话在 harness/guardrails/guardrails.md）：
- read         → allow（只读自动放行）
- write        → ask（攒批确认 ask_batch；阶段 2 先以 ASK 落地——写步骤
                 执行前过审批挂钩，无人值守时编排器按 fail-closed 把
                 ASK 落成 DENY 并留轨迹，批处理引擎后置）
- irreversible → ask（单独确认 ask_separate：requires_separate_confirmation
                 为真，将来有人值守时不可与其它写操作合并放行）
- forbidden    → deny（红线，任何场景不可放宽）

矩阵之外（风险未知 / 步骤未声明风险）一律 ASK，fail-closed 不变。
"""
from __future__ import annotations

from typing import Any

from ..execution.contracts import Decision

# 矩阵动作 -> 执行层决定。ask_batch / ask_separate 在执行层都先落 ASK，
# 区别只在「确认形态」标记上（见 requires_separate_confirmation）。
_ACTION_TO_DECISION = {
    "allow": Decision.ALLOW,
    "ask_batch": Decision.ASK,
    "ask_separate": Decision.ASK,
    "deny": Decision.DENY,
}

KNOWN_RISKS = ("read", "write", "irreversible", "forbidden")


class GuardrailPolicy:
    """ApprovalPolicy 实现：按步骤声明的风险级查护栏矩阵。

    风险级不在策略里猜：由计划/场景在 step_risks 里显式声明
    （step_id -> 风险级）。未声明的步骤走矩阵之外口径：ASK。
    """

    def __init__(self, policy: dict[str, Any], *,
                 step_risks: dict[str, str] | None = None):
        matrix = policy.get("approval_matrix") or {}
        unknown = set(matrix.values()) - set(_ACTION_TO_DECISION)
        if unknown:
            raise ValueError(f"护栏矩阵出现未知动作：{sorted(unknown)}")
        self._matrix: dict[str, str] = dict(matrix)
        self._step_risks = dict(step_risks or {})
        caps = policy.get("budget_caps") or {}
        self.budget_cap: float | None = caps.get("default_task_usd")
        self.red_lines: list[str] = list(policy.get("red_lines") or [])

    # ------------------------------------------------------- 风险级 API
    def action_for(self, risk: str) -> str | None:
        """风险级在矩阵里的动作原话（allow / ask_batch / ...）；无则 None。"""
        return self._matrix.get(risk)

    def decide_for_risk(self, risk: str) -> Decision:
        action = self._matrix.get(risk)
        if action is None:
            return Decision.ASK            # 矩阵之外：fail-closed 先问
        return _ACTION_TO_DECISION[action]

    def requires_separate_confirmation(self, risk: str) -> bool:
        """不可逆级：确认时必须单独进行，不许搭车批量放行。"""
        return self._matrix.get(risk) == "ask_separate"

    # -------------------------------------------- contracts.ApprovalPolicy
    def decide(self, *, step_id: str, unit_id: str | None,
               primitive: str) -> Decision:
        risk = self._step_risks.get(step_id)
        if risk is None:
            return Decision.ASK            # 未声明风险：fail-closed 先问
        return self.decide_for_risk(risk)
