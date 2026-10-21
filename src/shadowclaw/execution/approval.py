"""审批挂钩：策略接口在本模块实现默认策略。

默认 fail-closed（muse-agent 评审教训）：无规则命中一律 ASK；
ASK 在无人值守时等同 DENY（由编排器执行，轨迹留痕）。规则是显式字典，
阶段 2 由驾驭层护栏文件生成，不在这里长出配置框架。
"""
from __future__ import annotations

from .contracts import Decision


class FailClosedPolicy:
    """ApprovalPolicy 实现：rules 命中才放行/拒绝，其余 ASK。"""

    def __init__(self, rules: dict[str, Decision] | None = None):
        # rules 的键：单元 id 或 "*"（通配）。值只能是 ALLOW / DENY。
        self._rules = rules or {}

    def decide(self, *, step_id: str, unit_id: str | None,
               primitive: str) -> Decision:
        if unit_id is not None and unit_id in self._rules:
            return self._rules[unit_id]
        if "*" in self._rules:
            return self._rules["*"]
        return Decision.ASK
