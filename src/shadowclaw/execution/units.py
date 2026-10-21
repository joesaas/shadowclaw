"""执行单元：注册表 + 两种实现（Mock / ADK），同一 Unit 契约可互换。

- 注册表是开闭的落点：新单元靠 register 接入，编排器不改。
- MockUnit：脚本化确定性输出，供离线 demo 与测试。脚本可为固定
  UnitResult、按输入返回结果的函数，或异常（模拟失败）。
- AdkUnit：LlmAgent 作「身体」——模型与工具循环归 ADK，每次调用开一个
  一次性 session（用完即弃），事实提议走输出约定（行首 `FACT:`）。
  模型经参数注入：传 BaseLlm 实例（如离线 MockLlm）或模型名字符串
  （走 LiteLlm 接 OpenRouter 等兼容端点）。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Union

from .contracts import Unit, UnitContext, UnitResult

Script = Union[UnitResult, Callable[[UnitContext], UnitResult], BaseException]


class UnitRegistry:
    def __init__(self):
        self._units: dict[str, Unit] = {}

    def register(self, unit: Unit) -> Unit:
        self._units[unit.unit_id] = unit
        return unit

    def get(self, unit_id: str) -> Unit:
        try:
            return self._units[unit_id]
        except KeyError:
            raise KeyError(f"单元未注册：{unit_id}") from None

    def ids(self) -> set[str]:
        return set(self._units)


class MockUnit:
    """脚本化单元：输出完全由脚本决定，确定性、无外部依赖。"""

    def __init__(self, unit_id: str, script: Script, *, model: str = "mock",
                 instruction: str = ""):
        self.unit_id = unit_id
        self.model = model
        self.instruction = instruction
        self._script = script
        self.seen_contexts: list[UnitContext] = []   # 测试可读：收到过什么上下文

    async def run(self, ctx: UnitContext) -> UnitResult:
        self.seen_contexts.append(ctx)
        if isinstance(self._script, BaseException):
            raise self._script
        if callable(self._script):
            return self._script(ctx)
        return self._script


class AdkUnit:
    """ADK 单元：LlmAgent 运行 + Runner 驱动；隔离靠「每次新 session」。"""

    def __init__(self, unit_id: str, *, instruction: str,
                 model: Any, model_label: str | None = None,
                 tools: list | None = None):
        self.unit_id = unit_id
        self.model = model_label or (model if isinstance(model, str)
                                      else type(model).__name__)
        self._instruction = instruction
        self._model = model
        self._tools = tools or []

    @property
    def instruction(self) -> str:
        return self._instruction

    def _build_runner(self):
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        model = self._model
        if isinstance(model, str) and "/" in model:
            from google.adk.models.lite_llm import LiteLlm
            model = LiteLlm(model=model)
        agent = LlmAgent(name=self.unit_id.replace("-", "_"),
                         model=model, instruction=self._instruction,
                         tools=self._tools)
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="shadowclaw",
                        session_service=session_service)
        return runner, session_service

    async def run(self, ctx: UnitContext) -> UnitResult:
        from google.genai import types

        runner, session_service = self._build_runner()
        session_id = uuid.uuid4().hex
        await session_service.create_session(
            app_name="shadowclaw", user_id="shadowclaw",
            session_id=session_id)
        message = types.Content(role="user",
                                parts=[types.Part(text=ctx.prompt)])
        text = ""
        tokens_in = tokens_out = 0
        tool_calls: list[str] = []
        async for event in runner.run_async(
                user_id="shadowclaw", session_id=session_id,
                new_message=message):
            if event.usage_metadata is not None:
                tokens_in += event.usage_metadata.prompt_token_count or 0
                tokens_out += (event.usage_metadata.candidates_token_count
                               or 0)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.function_call:
                        tool_calls.append(part.function_call.name)
            if event.is_final_response() and event.content:
                text = "".join(p.text or "" for p in event.content.parts)
        # 事实提议约定：输出中行首 FACT: 的行是提议，其余是正文。
        facts = [line[len("FACT:"):].strip() for line in text.splitlines()
                 if line.startswith("FACT:")]
        body = "\n".join(line for line in text.splitlines()
                         if not line.startswith("FACT:")).strip()
        return UnitResult(text=body, proposed_facts=facts,
                          tokens_in=tokens_in, tokens_out=tokens_out,
                          tool_calls=tool_calls)
