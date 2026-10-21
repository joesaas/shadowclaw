"""上下文组装：每次调用单元前现拼，不依赖 ADK session 历史。

组成 = 单元 instruction + 事实板已提交事实快照 + 本步输入。
隔离语义由这里保证：未提交的提议、兄弟单元的私有上下文，永远进不了拼装。
"""
from __future__ import annotations

from .contracts import UnitContext


def assemble(*, instruction: str, task_input: str,
             committed_facts: list[str]) -> UnitContext:
    parts = [instruction.strip()] if instruction.strip() else []
    if committed_facts:
        facts_block = "\n".join(f"- {f}" for f in committed_facts)
        parts.append(f"## 已确认事实（事实板）\n{facts_block}")
    parts.append(f"## 本步输入\n{task_input}")
    return UnitContext(prompt="\n\n".join(parts), task_input=task_input,
                       facts=list(committed_facts))
