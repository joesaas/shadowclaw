"""配置：只读环境变量 / 默认值，不做配置框架，不写死本机绝对路径。

可调项（环境变量）：
- SHADOWCLAW_DB_PATH        轨迹与检查点共用的 SQLite 路径（默认 ./shadowclaw.db）
- SHADOWCLAW_HARNESS_DIR    驾驭层内容正本目录（默认 ./harness，仓库根的 harness/）
- SHADOWCLAW_MODEL          ADK 单元默认模型规格（默认 mock，离线）
- SHADOWCLAW_HARNESS_VERSION 轨迹里打的驾驭层版本号。阶段 2 起：不设此变量
  时由 harness.loader 按内容哈希现算（原则+护栏+场景文件）；显式设置则以
  设置为准（测试/回放旧轨迹时用）。字段默认值 "stage1-dev" 仅为直接构造
  Config 时的占位（如阶段 1 的单测夹具），运行时路径不走它。
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
        harness_dir = Path(env.get("SHADOWCLAW_HARNESS_DIR", "harness"))
        version = env.get("SHADOWCLAW_HARNESS_VERSION") or (
            _resolve_harness_version(harness_dir))
        return cls(
            db_path=Path(env.get("SHADOWCLAW_DB_PATH", "shadowclaw.db")),
            harness_dir=harness_dir,
            model=env.get("SHADOWCLAW_MODEL", "mock"),
            harness_version=version,
            budget_hard_cap=float(env.get("SHADOWCLAW_BUDGET_HARD_CAP", "5.0")),
        )

    def estimate_cost(self, model: str, tokens_in: int,
                      tokens_out: int) -> float | None:
        """按价格表估算花费；表里没有该模型时返回 None（不编造数字）。"""
        price = self.price_table.get(model)
        if price is None:
            return None
        return tokens_in / 1000 * price[0] + tokens_out / 1000 * price[1]


def _resolve_harness_version(harness_dir: Path) -> str:
    """真实版本号来源：驾驭层内容哈希（harness.loader 计算）。

    惰性导入避免层间循环（loader 不依赖执行层）；harness 目录尚不存在
    （如纯阶段 1 用法）时降级为 "unversioned"——轨迹里如实留痕，
    不再用看似真实的占位号冒充版本。
    """
    try:
        from shadowclaw.harness.loader import compute_harness_version
        return compute_harness_version(harness_dir)
    except Exception:
        return "unversioned"
