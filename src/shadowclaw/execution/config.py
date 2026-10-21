"""配置：只读环境变量 / 默认值，不做配置框架，不写死本机绝对路径。

可调项（环境变量）：
- SHADOWCLAW_DB_PATH        轨迹与检查点共用的 SQLite 路径（默认 ./shadowclaw.db）
- SHADOWCLAW_HARNESS_DIR    驾驭层内容正本目录（默认 ./harness，仓库根的 harness/）
- SHADOWCLAW_MODEL          ADK 单元默认模型规格（默认 mock，离线）
- SHADOWCLAW_HARNESS_VERSION 轨迹里打的驾驭层版本号（阶段 1 为占位常量）
- SHADOWCLAW_BUDGET_HARD_CAP 任务花费硬顶（美元），计划预算不得超过它
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 价格表：模型名 -> (输入价, 输出价)，单位为美元 / 1K tokens。
# 仅用于模型未回报花费时的估算（轨迹里标 estimated）。
DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "mock": (0.001, 0.002),
    "openai/gpt-5.6-luna": (0.003, 0.012),
    "google/gemini-3.8-flash": (0.0015, 0.006),
    "x-ai/grok-4.7": (0.003, 0.012),
}


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("shadowclaw.db")
    harness_dir: Path = Path("harness")
    model: str = "mock"
    harness_version: str = "stage1-dev"
    budget_hard_cap: float = 5.0
    max_depth: int = 2
    price_table: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_PRICE_TABLE))

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            db_path=Path(env.get("SHADOWCLAW_DB_PATH", "shadowclaw.db")),
            harness_dir=Path(env.get("SHADOWCLAW_HARNESS_DIR", "harness")),
            model=env.get("SHADOWCLAW_MODEL", "mock"),
            harness_version=env.get(
                "SHADOWCLAW_HARNESS_VERSION", "stage1-dev"),
            budget_hard_cap=float(env.get("SHADOWCLAW_BUDGET_HARD_CAP", "5.0")),
        )

    def estimate_cost(self, model: str, tokens_in: int,
                      tokens_out: int) -> float | None:
        """按价格表估算花费；表里没有该模型时返回 None（不编造数字）。"""
        price = self.price_table.get(model)
        if price is None:
            return None
        return tokens_in / 1000 * price[0] + tokens_out / 1000 * price[1]
