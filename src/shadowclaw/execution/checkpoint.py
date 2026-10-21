"""检查点：计划步级，存 SQLite。恢复不依赖 ADK session（它只当单次
调用的工作内存）——读检查点、跳过已完成步、从断步重跑整步。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import Checkpoint


class SqliteCheckpointStore:
    """CheckpointStore 的 SQLite 实现（与轨迹共用同一个 db 文件）。"""

    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            " run_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
        self._conn.commit()

    def save(self, checkpoint: Checkpoint) -> None:
        payload = {
            "run_id": checkpoint.run_id, "plan": checkpoint.plan,
            "task": checkpoint.task,
            "next_step_index": checkpoint.next_step_index,
            "facts": checkpoint.facts, "spent": checkpoint.spent,
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?)",
            (checkpoint.run_id, json.dumps(payload, ensure_ascii=False)))
        self._conn.commit()

    def load(self, run_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT payload_json FROM checkpoints WHERE run_id=?",
            (run_id,)).fetchone()
        if row is None:
            return None
        d = json.loads(row[0])
        return Checkpoint(run_id=d["run_id"], plan=d["plan"], task=d["task"],
                          next_step_index=d["next_step_index"],
                          facts=d.get("facts", []), spent=d.get("spent", 0.0))

    def clear(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM checkpoints WHERE run_id=?",
                           (run_id,))
        self._conn.commit()
