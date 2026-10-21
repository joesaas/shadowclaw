"""阶段 2：驾驭层加载/组装/检索/护栏/冲突 + GitHub 接入 + demo2 验收。

驾驭层内容用仓库根的真实 harness/ 正本（审定过的种子与定版文件），
锁定级拒载类用例用 tmp 里搭的最小 harness 树。
"""
from pathlib import Path

import pytest

from shadowclaw.execution.config import Config
from shadowclaw.execution.demo2 import run_demo2
from shadowclaw.execution.facts import FactBoard
from shadowclaw.execution.github_tools import (FakeGitHub,
                                               build_github_mcp_toolset,
                                               tool_risk)
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.harness.assembler import assemble
from shadowclaw.harness.conflicts import (KeyValueConflictGuard,
                                          fact_key, find_conflicts)
from shadowclaw.harness.guardrails import GuardrailPolicy
from shadowclaw.harness.loader import (HarnessError, compute_harness_version,
                                       load_harness)
from shadowclaw.harness.memory import MemoryIndex

from conftest import run_coro

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS_DIR = REPO_ROOT / "harness"


@pytest.fixture(scope="module")
def bundle():
    return load_harness(HARNESS_DIR, "troubleshooting")


# ---------------------------------------------------------------- 加载与版本
def test_loader_loads_real_harness(bundle):
    assert bundle.scenario.name == "troubleshooting"
    assert "ShadowClaw 原则" in bundle.principles_text
    assert "个人分身" in bundle.identity_text      # 身份节已单列抽出
    assert "troubleshooting" in bundle.playbooks
    github = bundle.tool_cards["github"]
    assert ("get_repository", "read") in github.operations
    assert ("delete_repository", "forbidden") in github.operations
    assert "核心常驻" in bundle.core_memory


def test_harness_version_is_content_hash(bundle):
    version = compute_harness_version(HARNESS_DIR)
    assert version == bundle.version
    assert version.startswith("h-") and len(version) == 12
    assert version != "stage1-dev"                 # 不再是阶段 1 占位


def _write_min_harness(root: Path, guardrails_md: str) -> None:
    (root / "principles").mkdir(parents=True)
    (root / "principles" / "principles.md").write_text("# 原则\n正文")
    (root / "guardrails").mkdir()
    (root / "guardrails" / "guardrails.md").write_text(guardrails_md)
    (root / "scenarios").mkdir()
    (root / "scenarios" / "mini.yaml").write_text(
        "name: mini\nplaybook: ''\n")
    (root / "memory").mkdir()
    (root / "memory" / "core.md").write_text("# core")


_GUARDRAILS_OK = """---
locked: true
requires_approval: true
approved_by: li
approved_at: 2026-10-21
---

```yaml
approval_matrix:
  read: allow
```
"""


def test_locked_guardrails_without_approval_marks_rejected(tmp_path):
    bad = _GUARDRAILS_OK.replace("approved_by: li\n", "")
    _write_min_harness(tmp_path, bad)
    with pytest.raises(HarnessError, match="批准标记"):
        load_harness(tmp_path, "mini")


def test_locked_guardrails_with_approval_marks_loads(tmp_path):
    _write_min_harness(tmp_path, _GUARDRAILS_OK)
    loaded = load_harness(tmp_path, "mini")
    assert loaded.guardrails_policy["approval_matrix"]["read"] == "allow"


# ---------------------------------------------------------------- 场景组装
def test_assemble_order_principles_first(bundle):
    index = MemoryIndex(HARNESS_DIR / "memory")
    ctx = assemble(bundle, query="CI 失败排障", memory_index=index,
                   step_risks={"sx": "write"})
    text = ctx.render_instruction()
    pos_principles = text.index("ShadowClaw 原则")
    pos_core = text.index("核心常驻")
    pos_playbook = text.index("方法手册")
    pos_tools = text.index("工具卡摘要")
    assert pos_principles < pos_core < pos_playbook < pos_tools
    assert ctx.identity.startswith("- ShadowClaw 是 li 的个人分身")
    # 规划者只看摘要：手册深处（常见坑/输出规范）不进规划者视图
    planner = ctx.planner_view()
    assert "方法手册" in planner and "步骤：" in planner
    assert "常见坑" not in planner
    assert "常见坑" in text                        # 执行单元拿全文
    # 预算合并从严：场景 5.0 与护栏顶 5.0 → 5.0
    assert ctx.budget_limit == 5.0
    assert ctx.approval_policy is not None


# ---------------------------------------------------------------- 记忆检索
def test_memory_index_loads_all_seed_entries():
    index = MemoryIndex(HARNESS_DIR / "memory")
    entries = index.entries()
    by_file = {}
    for entry in entries:
        by_file[entry.file] = by_file.get(entry.file, 0) + 1
    # 事实 19 + 项目 18 + 决策 4 = 41 条事实类；推断 13；人物按审定口径为 0
    assert by_file == {"facts": 19, "projects": 18, "decisions": 4,
                       "inferences": 13}


def test_memory_search_hit_and_miss():
    index = MemoryIndex(HARNESS_DIR / "memory")
    # 精确单关键词：只有 F-02 含「生日」，必须唯一命中排第一
    assert [e.id for e in index.search("生日")] == ["F-02"]
    # 自然问句：v1 只按命中词元数排序，同分并列可接受，召回命中即可
    hits = index.search("li 的生日是哪天")
    assert any(e.id == "F-02" for e in hits)
    hits = index.search("data-pipeline 上线地址")
    assert any(e.id == "P-09" for e in hits)
    hits = index.search("ShadowClaw 只建议不代投")
    assert hits                                     # 至少命中事实/推断各一
    assert index.search("火星殖民计划进度") == []     # 未命中不硬凑
    # 推断条目带置信度标注，不冒充事实
    inference_hits = [e for e in index.search("攒批一次审批哲学")
                      if e.file == "inferences"]
    assert inference_hits and inference_hits[0].confidence in ("高", "中")


# ---------------------------------------------------------------- 护栏三级
def test_guardrail_three_levels(bundle):
    policy = GuardrailPolicy(bundle.guardrails_policy,
                             step_risks={"w": "write", "r": "read",
                                         "i": "irreversible",
                                         "f": "forbidden"})
    from shadowclaw.execution.contracts import Decision
    assert policy.decide_for_risk("read") is Decision.ALLOW
    assert policy.decide_for_risk("write") is Decision.ASK
    assert policy.action_for("write") == "ask_batch"     # 攒批先以 ASK 落地
    assert policy.decide_for_risk("irreversible") is Decision.ASK
    assert policy.requires_separate_confirmation("irreversible") is True
    assert policy.requires_separate_confirmation("write") is False
    assert policy.decide_for_risk("forbidden") is Decision.DENY
    assert policy.decide_for_risk("nonsense") is Decision.ASK  # 矩阵外 fail-closed
    # ApprovalPolicy 协议面：按步查声明的风险；未声明 → ASK
    assert policy.decide(step_id="r", unit_id="u",
                         primitive="sequence") is Decision.ALLOW
    assert policy.decide(step_id="w", unit_id="u",
                         primitive="sequence") is Decision.ASK
    assert policy.decide(step_id="undeclared", unit_id="u",
                         primitive="sequence") is Decision.ASK
    assert policy.budget_cap == 5.0
    assert policy.red_lines                                # 红线已载入


# ---------------------------------------------------------------- 冲突检查
def test_conflict_guard_blocks_silent_overwrite():
    guard = KeyValueConflictGuard()
    board = FactBoard(commit_guard=guard)
    first = board.propose("CI 状态：通过", source_unit="u1", step_id="s1")
    assert board.commit(first) is True
    clash = board.propose("CI 状态：失败", source_unit="u2", step_id="s2")
    assert board.commit(clash) is False            # 同键异值：拒落板
    assert clash.status == "conflict"
    assert board.snapshot() == ["CI 状态：通过"]      # 原事实不被覆盖
    assert len(guard.conflicts) == 1
    assert guard.conflicts[0].key == "ci 状态"
    # 同键同值是重复不是冲突；无键事实照常
    probe = find_conflicts(board, "CI 状态：未知")
    assert len(probe) == 1
    free = board.propose("一条自由文本事实", source_unit="u1", step_id="s3")
    assert board.commit(free) is True
    assert fact_key("一条自由文本事实") is None


def test_fact_board_without_guard_unchanged():
    board = FactBoard()                            # 阶段 1 行为不变
    assert board.commit(board.propose("甲：1", source_unit="u",
                                      step_id="s")) is True
    assert board.commit(board.propose("甲：2", source_unit="u",
                                      step_id="s")) is True


# ---------------------------------------------------------------- GitHub 接入
def test_tool_risk_classification():
    assert tool_risk("get_repository") == "read"
    assert tool_risk("add_issue_label") == "write"
    assert tool_risk("merge_pull_request") == "irreversible"
    assert tool_risk("delete_repository") == "forbidden"
    assert tool_risk("something_new") == "unknown"


def test_github_factory_honest_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert build_github_mcp_toolset() is None      # 不假装连上


def test_github_factory_builds_toolset_with_token():
    toolset = build_github_mcp_toolset(token="dummy-not-real")
    assert toolset is not None                     # 构造不发起连接（惰性）


def test_fake_github_tools_filter_by_risk():
    fake = FakeGitHub()
    read_names = {t.name for t in fake.tools(allow_write=False)}
    assert read_names == {"get_repository", "list_issues",
                          "list_workflow_runs", "get_workflow_run"}
    write_names = {t.name for t in fake.tools(allow_write=True)}
    assert "add_issue_label" in write_names
    assert "delete_repository" not in write_names  # forbidden 永不挂载


# ---------------------------------------------------------------- demo2 验收
def test_demo2_end_to_end(tmp_path, bundle):
    config = Config(db_path=tmp_path / "demo2.db", harness_dir=HARNESS_DIR)
    run = run_coro(run_demo2(config))
    result = run.result
    # 结论先行：s4 已出结论，最终停在 s5 的护栏门前（denied 属预期）
    assert result.status == "denied"
    assert [o.step_id for o in result.outputs] == [
        "s1-scope", "s2-evidence", "s3-exclude", "s4-conclude"]
    assert "Node" in result.outputs[-1].text
    # 工具真调用了只读集，写工具从未被调用
    called = [name for name, _ in run.fake.calls]
    assert called == ["get_repository", "list_workflow_runs",
                      "get_workflow_run", "list_issues"]
    events = SqliteTrajectoryStore(tmp_path / "demo2.db").events(
        result.run_id)
    types = [e["type"] for e in events]
    assert types.count("tool_call") == 4
    tools_in_traj = {e["payload"]["tool"] for e in events
                     if e["type"] == "tool_call"}
    assert tools_in_traj == set(called)
    # 护栏事件：s5 被 ASK 并在无人值守下落成 deny
    decided = [e for e in events if e["type"] == "approval_decided"]
    assert len(decided) == 1
    assert decided[0]["step_id"] == "s5-label"
    assert decided[0]["payload"]["decision"] == "ask"
    assert decided[0]["payload"]["effective"] == "deny"
    # 事实落板齐全且无冲突
    committed = [e["payload"]["statement"] for e in events
                 if e["type"] == "fact_committed"]
    assert any(s.startswith("CI 结论") for s in committed)
    assert len(committed) == 5
    assert run.guard.conflicts == []
    # 轨迹版本号是真实内容哈希
    assert all(e["harness_version"] == bundle.version for e in events)
    assert bundle.version.startswith("h-")
