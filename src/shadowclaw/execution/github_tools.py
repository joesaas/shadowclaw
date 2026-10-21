"""GitHub 工具接入：MCP 优先（驾驭层工具卡 harness/tools/github.md 为准）。

两条路径、同一个注册形态（都产出可直接塞给 AdkUnit 的 tools 列表）：

- 真接入 `build_github_mcp_toolset`：ADK McpToolset 连远程 GitHub MCP
  端点，token 只从环境变量 GITHUB_TOKEN 读。**token 缺失时返回 None 并
  明确告警，绝不假装已连上**（无 token 时本模块从未真实联调过——见
  README 阶段 2 出入说明）。工具按工具卡分级挂载：只读集默认启用；
  写/不可逆须 allow_write=True 才进过滤清单；forbidden 永不挂载。
- 测试/离线 `FakeGitHub`：一组返回固定数据的假工具（FunctionTool），
  记录每次调用，供测试与 demo2 验证「单元经工具卡拿到工具并真调用、
  轨迹有 tool_call 事件」这条链路。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_MCP_ENDPOINT = "https://api.githubcopilot.com/mcp/"
TOKEN_ENV = "GITHUB_TOKEN"

# 风险分级与 harness/tools/github.md 的能力清单表保持一致（改一处改两处）。
READ_TOOLS = {
    "get_repository", "list_issues", "get_issue", "list_pull_requests",
    "get_pull_request", "list_workflow_runs", "get_workflow_run",
    "get_file_contents", "search_code",
}
WRITE_TOOLS = {
    "create_issue", "update_issue", "add_issue_label", "add_issue_comment",
    "create_pull_request", "create_branch",
}
IRREVERSIBLE_TOOLS = {"merge_pull_request", "push_files", "delete_branch"}
FORBIDDEN_TOOLS = {"delete_repository"}


def tool_risk(name: str) -> str:
    """操作名 -> 风险级；未登记的返回 unknown（调用方按 fail-closed 处理）。"""
    if name in READ_TOOLS:
        return "read"
    if name in WRITE_TOOLS:
        return "write"
    if name in IRREVERSIBLE_TOOLS:
        return "irreversible"
    if name in FORBIDDEN_TOOLS:
        return "forbidden"
    return "unknown"


def build_github_mcp_toolset(*, token: str | None = None,
                             allow_write: bool = False,
                             endpoint: str = DEFAULT_GITHUB_MCP_ENDPOINT):
    """GitHub MCP 接入工厂。无 token → None + 明确告警（诚实降级）。

    allow_write=False（默认）：tool_filter 只放只读集；
    allow_write=True：放只读+写+不可逆集——forbidden 仍永不挂载，
    且写操作真正执行前还要过护栏审批（挂载 ≠ 放行）。
    """
    token = token if token is not None else os.environ.get(TOKEN_ENV)
    if not token:
        message = (f"GitHub MCP 未接入：环境变量 {TOKEN_ENV} 未设置，"
                   "不假装已连上；离线/测试请走 FakeGitHub。")
        logger.warning(message)
        print(message, file=sys.stderr)
        return None
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StreamableHTTPConnectionParams)
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    params = StreamableHTTPConnectionParams(
        url=endpoint, headers={"Authorization": f"Bearer {token}"})
    allowed = READ_TOOLS if not allow_write else (
        READ_TOOLS | WRITE_TOOLS | IRREVERSIBLE_TOOLS)
    return McpToolset(connection_params=params,
                      tool_filter=sorted(allowed))


# ------------------------------------------------------------------ 假工具
class FakeGitHub:
    """离线假 GitHub：固定数据、记录调用，与真接入走相同注册路径。

    数据设定：joesaas/data-pipeline 的 CI 在 test 步骤失败——运行器
    Node 20 低于项目要求 >=22（呼应 li 2026-10-20 的真实部署事故）。
    """

    REPO = {"full_name": "joesaas/data-pipeline", "owner": "joesaas",
            "name": "data-pipeline", "default_branch": "main",
            "language": "TypeScript", "open_issues": 3}
    RUNS = [{"id": 901, "workflow": "CI", "conclusion": "failure",
             "head_branch": "main", "event": "push",
             "created_at": "2026-10-21T08:14:00Z"}]
    RUN_DETAIL = {
        "id": 901, "workflow": "CI", "conclusion": "failure",
        "failed_step": "test",
        "log_excerpt": (
            "FAIL tests/ingest.test.ts\n"
            "Error: No such built-in module: node:sqlite\n"
            "The engine \"node\" is incompatible with this module. "
            "Expected version \">=22\". Got \"20.11.0\"\n"
            "Test Suites: 1 failed, 17 passed, 18 total"),
    }
    ISSUES = [
        {"number": 12, "title": "部署文档补充 Node 版本要求",
         "labels": ["docs"], "state": "open"},
        {"number": 9, "title": "ingest 大批量时偶发超时",
         "labels": ["bug"], "state": "open"},
    ]

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- 只读工具 ------------------------------------------------------
    def get_repository(self, owner: str, repo: str) -> dict:
        """读取仓库基本信息（名称、默认分支、语言、未关闭 issue 数）。"""
        self.calls.append(("get_repository",
                           {"owner": owner, "repo": repo}))
        return dict(self.REPO)

    def list_issues(self, owner: str, repo: str,
                    state: str = "open") -> list[dict]:
        """列出仓库的 issue（编号、标题、标签、状态）。"""
        self.calls.append(("list_issues",
                           {"owner": owner, "repo": repo, "state": state}))
        return [dict(issue) for issue in self.ISSUES]

    def list_workflow_runs(self, owner: str, repo: str) -> list[dict]:
        """列出最近的 CI 工作流运行（id、工作流名、结论、分支）。"""
        self.calls.append(("list_workflow_runs",
                           {"owner": owner, "repo": repo}))
        return [dict(run) for run in self.RUNS]

    def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict:
        """读取一次 CI 运行的详情：失败步骤与日志摘录。"""
        self.calls.append(("get_workflow_run",
                           {"owner": owner, "repo": repo, "run_id": run_id}))
        return dict(self.RUN_DETAIL)

    # -- 写工具 ---------------------------------------------------------
    def add_issue_label(self, owner: str, repo: str, issue_number: int,
                        label: str) -> dict:
        """给 issue 加标签（写操作：真实链路中须先过护栏审批）。"""
        self.calls.append(("add_issue_label",
                           {"owner": owner, "repo": repo,
                            "issue_number": issue_number, "label": label}))
        return {"ok": True, "issue_number": issue_number, "label": label}

    # -- 注册 ------------------------------------------------------------
    _METHODS = ("get_repository", "list_issues", "list_workflow_runs",
                "get_workflow_run", "add_issue_label")

    def tools(self, *, allow_write: bool = False) -> list:
        """按风险级产出 FunctionTool 列表（与真接入相同的过滤口径）：
        只读默认启用，写工具须 allow_write，forbidden 永不出现。"""
        from google.adk.tools import FunctionTool

        selected = []
        for name in self._METHODS:
            risk = tool_risk(name)
            if risk == "read" or (allow_write and risk == "write"):
                selected.append(FunctionTool(getattr(self, name)))
        return selected
