"""CLI 入口：唯一的组装点（依赖注入在此收口，其余模块不自建依赖）。

用法：
  shadowclaw run [--plan plan.json]   跑一个计划文件；不给则跑内置 demo
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


async def _cmd_resume(args: argparse.Namespace) -> int:
    config = Config.from_env()
    orchestrator = build_orchestrator(config)
    result = await orchestrator.resume(args.run_id)
    print(f"run_id: {result.run_id}\nstatus: {result.status}"
          f" 花费：${result.spent:.4f}")
    for out in result.outputs:
        print(f"\n[{out.step_id}] {out.unit_id}:\n{out.text}")
    return 0 if result.status == "success" else 1


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
    p_replay = sub.add_parser("replay", help="按轨迹复盘一个 run")
    p_replay.add_argument("run_id")
    p_resume = sub.add_parser("resume", help="从检查点续跑一个 run")
    p_resume.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "resume":
        return asyncio.run(_cmd_resume(args))
    return _cmd_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
