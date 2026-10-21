"""CLI 入口：唯一的组装点（依赖注入在此收口，其余模块不自建依赖）。

用法：
  shadowclaw run [--plan plan.json]   跑一个计划文件；不给则跑内置 demo
  shadowclaw demo2                    阶段 2 验收演示：排障场景组装 +
                                      GitHub 假工具排障（离线）
  shadowclaw shadow [--mock]          阶段 3 影子回放：回归案例只给症状
                                      简报，真实模型独立排障并与参照解
                                      对照，产出 reports/shadow-report-v1.md
                                      （--mock 用脚本单元离线验流程）
  shadowclaw replay <run_id>          按轨迹复盘打印时间线
  shadowclaw resume <run_id>          从检查点续跑（单元注册表用 demo 集，
                                      阶段 1 范围；真实场景注册表随阶段 2）
数据库路径等经环境变量配置（见 config.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .checkpoint import SqliteCheckpointStore
from .config import Config
from .demo import DEMO_TASK, build_demo_plan, build_demo_registry
from .demo2 import run_demo2
from .orchestrator import Orchestrator
from .plan import Plan
from .trajectory import SqliteTrajectoryStore


def build_orchestrator(config: Config) -> Orchestrator:
    return Orchestrator(
        registry=build_demo_registry(),
        trajectory_store=SqliteTrajectoryStore(config.db_path),
        checkpoint_store=SqliteCheckpointStore(config.db_path),
        config=config)


async def _cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    orchestrator = build_orchestrator(config)
    if args.plan:
        plan = Plan.from_dict(json.loads(Path(args.plan).read_text("utf-8")))
        task = plan.steps[0].input if plan.steps else ""
        supplier = lambda: plan  # noqa: E731
    else:
        task, supplier = DEMO_TASK, build_demo_plan
    fallback = sorted(build_demo_registry().ids())[0]
    result = await orchestrator.run(supplier, task=task,
                                    fallback_unit=fallback)
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}"
          f"（降级保底：{result.degraded}）花费：${result.spent:.4f}")
    for out in result.outputs:
        print(f"\n[{out.step_id}] {out.unit_id}:\n{out.text}")
    return 0 if result.status == "success" else 1


async def _cmd_demo2(args: argparse.Namespace) -> int:
    config = Config.from_env()
    run = await run_demo2(config)
    result = run.result
    print(f"驾驭层版本: {run.bundle.version}（内容哈希）")
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}（s5 写步骤被护栏 ASK 拦下时为 denied，"
          "属预期）")
    for out in result.outputs:
        print(f"\n[{out.step_id}] {out.unit_id}:\n{out.text}")
    print(f"\nGitHub 工具实际调用：{[name for name, _ in run.fake.calls]}")
    print(f"事实冲突：{len(run.guard.conflicts)} 条")
    print(f"复盘：shadowclaw replay {result.run_id}")
    return 0


async def _cmd_resume(args: argparse.Namespace) -> int:
    config = Config.from_env()
    orchestrator = build_orchestrator(config)
    result = await orchestrator.resume(args.run_id)
    print(f"run_id: {result.run_id}\nstatus: {result.status}"
          f" 花费：${result.spent:.4f}")
    for out in result.outputs:
        print(f"\n[{out.step_id}] {out.unit_id}:\n{out.text}")
    return 0 if result.status == "success" else 1


async def _cmd_shadow(args: argparse.Namespace) -> int:
    """阶段 3 影子回放：组装点在此（加载驾驭层、取凭证、选单元工厂）。

    真实模式：OpenRouter key 经 authd 替身注入环境（只留内存）；拿不到
    key 就诚实报错退出，绝不假装跑过。--mock 用脚本化 MockUnit 离线
    验流程（答复固定未命中，只验 mechanics，不产出效果结论）。
    """
    from dataclasses import replace
    from datetime import datetime, timezone

    from ..evaluation.regression import load_cases
    from ..evaluation.shadow import (build_judge_fn, build_real_unit_factory,
                                     ensure_openrouter_api_key,
                                     render_report, run_shadow)
    from ..evaluation.signals import SignalStore
    from ..harness.loader import load_harness

    repo_root = Path(__file__).resolve().parents[3]
    eval_dir = Path(args.eval_dir) if args.eval_dir else repo_root / "evaluation"
    cases = load_cases(eval_dir / "regression" / "cases")
    config = Config.from_env()
    bundle = load_harness(config.harness_dir, "troubleshooting")

    if args.mock:
        from .contracts import UnitResult
        from .units import MockUnit

        def factory(case, assembled):  # noqa: ANN001
            return MockUnit(
                "shadow-solver",
                UnitResult(text="结论：未证实，材料不足，无法判断根因。",
                           tokens_in=10, tokens_out=10),
                model="mock",
                instruction=assembled.render_instruction())
        judge_fn = None
        config = replace(config, model="mock")
    else:
        if not ensure_openrouter_api_key():
            print("拿不到 OpenRouter 凭证（环境变量未设、authd 替身也取"
                  "不到），影子回放未运行。", file=sys.stderr)
            return 2
        model_slug = args.model
        factory = build_real_unit_factory(model_slug)
        judge_fn = build_judge_fn(model_slug, config)
        config = replace(config, model=model_slug)

    store = SignalStore(eval_dir / "signals" / "signals.jsonl")
    run = await run_shadow(cases=cases, bundle=bundle, config=config,
                           signal_store=store, unit_factory=factory,
                           judge_fn=judge_fn)
    report = render_report(
        run, generated_at=datetime.now(timezone.utc).isoformat())
    out = Path(args.out) if args.out else repo_root / "reports" / \
        "shadow-report-v1.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"影子报告：{out}")
    for cr in run.cases:
        if cr.status == "completed" and cr.check is not None:
            print(f"[{cr.case_id}] 命中={cr.check.conclusion_hit} "
                  f"覆盖={len(cr.check.actions_covered)}/"
                  f"{cr.check.actions_total} 偏差={len(cr.deviations)} "
                  f"花费=${cr.cost:.4f} run={cr.run_id}")
        else:
            print(f"[{cr.case_id}] 状态={cr.status} {cr.error}")
    if run.stopped_reason:
        print(f"⚠️ {run.stopped_reason}")
    print(f"信号入账：{run.signals_written}")
    print(f"原则性错误：{len(run.principle_errors)} 条")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    config = Config.from_env()
    store = SqliteTrajectoryStore(config.db_path)
    try:
        export = store.export(args.run_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"run {export['run_id']} · 状态 {export['status']} · "
          f"驾驭层版本 {export['harness_version']} · "
          f"{export['started_at']} → {export['finished_at']}")
    print(f"计划：{export['plan']['id']}"
          f"（{len(export['plan']['steps'])} 步）")
    for e in export["events"]:
        step = f" [{e['step_id']}]" if e["step_id"] else ""
        unit = f" <{e['unit_id']}>" if e["unit_id"] else ""
        cost = ""
        if e["type"] == "model_call":
            cost = (f" tokens {e['tokens_in']}→{e['tokens_out']}, "
                    f"${e['cost']:.4f}" + ("（估算）" if e["estimated"]
                                           else ""))
        detail = ""
        if e["payload"].get("statement"):
            detail = f" — {e['payload']['statement']}"
        elif e["payload"].get("status"):
            detail = f" — {e['payload']['status']}"
        print(f"  {e['seq']:>3} {e['ts'][11:23]} {e['type']:<18}"
              f"{step}{unit}{cost}{detail}")
    t = export["totals"]
    print(f"合计：tokens {t['tokens_in']}→{t['tokens_out']}，"
          f"花费 ${t['cost']:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shadowclaw",
                                     description="ShadowClaw 执行层（阶段 1）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="跑一个计划（默认内置 demo）")
    p_run.add_argument("--plan", help="计划 JSON 文件路径")
    sub.add_parser("demo2", help="阶段 2 验收演示（排障场景 + GitHub 假工具）")
    p_shadow = sub.add_parser("shadow", help="阶段 3 影子回放（回归集对照）")
    p_shadow.add_argument("--mock", action="store_true",
                          help="用 MockUnit 离线验流程（不调真实模型）")
    p_shadow.add_argument("--model", default="google/gemini-3.8-flash",
                          help="OpenRouter 模型 slug（默认 google/gemini-3.8-flash）")
    p_shadow.add_argument("--eval-dir", help="评估层数据目录（默认仓库根 evaluation/）")
    p_shadow.add_argument("--out", help="影子报告输出路径")
    p_replay = sub.add_parser("replay", help="按轨迹复盘一个 run")
    p_replay.add_argument("run_id")
    p_resume = sub.add_parser("resume", help="从检查点续跑一个 run")
    p_resume.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "demo2":
        return asyncio.run(_cmd_demo2(args))
    if args.command == "shadow":
        return asyncio.run(_cmd_shadow(args))
    if args.command == "resume":
        return asyncio.run(_cmd_resume(args))
    return _cmd_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
