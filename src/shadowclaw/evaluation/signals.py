"""信号存储（评估层 Step 2 的字段级落地，阶段 3 先行版）。

存储形态：JSONL 追加写——信号只增不改（设计纪律），JSONL 天然契合：
一行一条、写错了追加更正信号，不回头改历史行。文件落在评估层数据
目录 evaluation/signals/signals.jsonl（仓库根 evaluation/，与回归集
同级），路径由调用方传入，不写死。

字段按 docs/design-evaluation.md Step 2：
- kind 四类，可信度即权重序：correction > outcome > feedback > judge
- correction 必须带原话/出处引用（reference），缺失拒存；
- outcome 必须带可核查的结果引用，缺失拒存；
- feedback / judge 的 reference 可选（judge 只作参考）。
影子回放产生的 correction 是「候选」：source 必须注明来自回放对照、
不是 li 亲口纠正（见 shadow.py），可信度在 extra 里显式标级，
不与 li 本人的 correction 混账。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SIGNAL_KINDS = ("correction", "outcome", "feedback", "judge")

# 可信度权重序（设计 Step 2）：correction 最高、judge 最低。
KIND_WEIGHT = {"correction": 4, "outcome": 3, "feedback": 2, "judge": 1}

_REFERENCE_REQUIRED = ("correction", "outcome")


@dataclass
class Signal:
    kind: str                       # correction / outcome / feedback / judge
    source: str                     # 来源（如 li 本人、shadow-replay、judge 模型）
    content: str                    # 信号内容
    reference: str = ""             # 出处引用：correction=原话/出处，outcome=可核查引用
    trace_id: str = ""              # 关联轨迹 run_id
    task: str = ""                  # 关联任务
    harness_version: str = ""       # 产生该信号时的驾驭层版本（归因按版本切分）
    id: str = field(default_factory=lambda: f"sig-{uuid.uuid4().hex[:10]}")
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"未知信号类型：{self.kind}")
        if self.kind in _REFERENCE_REQUIRED and not self.reference.strip():
            raise ValueError(
                f"{self.kind} 信号必须带出处引用（reference），缺失拒存")
        if not self.content.strip():
            raise ValueError("信号内容为空，拒存")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class SignalStore:
    """JSONL 信号库：append-only。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, signal: Signal) -> Signal:
        signal.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(signal.to_dict(), ensure_ascii=False) + "\n")
        return signal

    def list(self, *, kind: str | None = None,
             trace_id: str | None = None) -> list[Signal]:
        if not self.path.exists():
            return []
        out: list[Signal] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            sig = Signal.from_dict(json.loads(line))
            if kind and sig.kind != kind:
                continue
            if trace_id and sig.trace_id != trace_id:
                continue
            out.append(sig)
        return out

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sig in self.list():
            counts[sig.kind] = counts.get(sig.kind, 0) + 1
        return counts
