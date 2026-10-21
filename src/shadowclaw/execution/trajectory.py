"""轨迹：固定事件枚举 + SQLite 存储 + 复盘导出。

事件类型是执行层→评估层的契约面，阶段 1 固定为下方 15 种，不随手新增。
每条事件带 run_id、时间戳、驾驭层版本号。花费口径：模型回报了就用回报值；
没回报则按配置价格表以 token 估算并标 estimated=True，估不出来记 None。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .config import Config


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    PLAN_VALIDATED = "plan_validated"
    STEP_STARTED = "step_started"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    DELEGATE_STARTED = "delegate_started"
    DELEGATE_FINISHED = "delegate_finished"
    FACT_PROPOSED = "fact_proposed"
    FACT_COMMITTED = "fact_committed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    BUDGET_CHECKED = "budget_checked"
    STEP_FINISHED = "step_finished"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SqliteTrajectoryStore:
    """TrajectoryStore 的 SQLite 实现（本阶段唯一存储实现）。"""

    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                harness_version TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                step_id TEXT,
                unit_id TEXT,
                payload_json TEXT NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost REAL,
                estimated INTEGER NOT NULL DEFAULT 0,
                harness_version TEXT NOT NULL
            );
            """)
        self._conn.commit()

    def start_run(self, run_id: str, harness_version: str,
                  plan: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,NULL,NULL)",
                (run_id, harness_version, json.dumps(plan, ensure_ascii=False),
                 _now()))
            self._conn.commit()

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (run_id, ts, type, step_id, unit_id,"
                " payload_json, tokens_in, tokens_out, cost, estimated,"
                " harness_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event["run_id"], event["ts"], event["type"],
                 event.get("step_id"), event.get("unit_id"),
                 json.dumps(event.get("payload", {}), ensure_ascii=False),
                 event.get("tokens_in", 0), event.get("tokens_out", 0),
                 event.get("cost"), 1 if event.get("estimated") else 0,
                 event["harness_version"]))
            self._conn.commit()

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
                (_now(), status, run_id))
            self._conn.commit()

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, run_id, ts, type, step_id, unit_id, payload_json,"
            " tokens_in, tokens_out, cost, estimated, harness_version"
            " FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [{
            "seq": r[0], "run_id": r[1], "ts": r[2], "type": r[3],
            "step_id": r[4], "unit_id": r[5],
            "payload": json.loads(r[6]), "tokens_in": r[7],
            "tokens_out": r[8], "cost": r[9], "estimated": bool(r[10]),
            "harness_version": r[11],
        } for r in rows]

    def list_runs(self) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at").fetchall()]

    def export(self, run_id: str) -> dict[str, Any]:
        """复盘导出：一份 run 的全量结构（头信息 + 事件流 + 用量汇总）。"""
        row = self._conn.execute(
            "SELECT harness_version, plan_json, started_at, finished_at,"
            " status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run 不存在：{run_id}")
        events = self.events(run_id)
        return {
            "run_id": run_id, "harness_version": row[0],
            "plan": json.loads(row[1]), "started_at": row[2],
            "finished_at": row[3], "status": row[4], "events": events,
            "totals": {
                "tokens_in": sum(e["tokens_in"] for e in events),
                "tokens_out": sum(e["tokens_out"] for e in events),
                "cost": sum(e["cost"] or 0.0 for e in events),
            },
        }


class Recorder:
    """事件记录器：编排器与单元层只通过它发事件（事件形状在一处收口）。"""

    def __init__(self, store: SqliteTrajectoryStore, config: Config,
                 run_id: str):
        self._store = store
        self._config = config
        self.run_id = run_id

    def emit(self, type: EventType, *, step_id: str | None = None,
             unit_id: str | None = None, payload: dict[str, Any] | None = None,
             model: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
             cost: float | None = None) -> dict[str, Any]:
        estimated = False
        if cost is None and (tokens_in or tokens_out) and model:
            cost = self._config.estimate_cost(model, tokens_in, tokens_out)
            estimated = cost is not None
        event = {
            "run_id": self.run_id, "ts": _now(), "type": type.value,
            "step_id": step_id, "unit_id": unit_id, "payload": payload or {},
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost": cost,
            "estimated": estimated,
            "harness_version": self._config.harness_version,
        }
        self._store.append(event)
        return event
