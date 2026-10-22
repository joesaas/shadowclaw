"""场景转正门（阶段 5 · 影子转分身）。

一个场景从 shadow 升 autonomous，须**同时**满足三项（代码判定，
不许口头放行）：

1. **无回归退步记录**：读迭代档案 evaluation/iterations/*/iteration.json，
   该场景（档案无 scenario 字段时按 iterate 的唯一场景 troubleshooting
   归属）名下没有任何一轮留下 regressions 记录。
2. **真实任务影子双跑样本 ≥ 阈值**（默认 5，可配）：样本来自信号账本
   evaluation/signals/signals.jsonl 中 shadow 运行写入的 outcome
   信号，按 extra 分类——**只数真实任务双跑**（sample_kind=real 且
   origin=live_task）；历史回放样本（origin=replay_contrast）与
   Mock 样本（sample_kind=mock）都**不计数**，只在核验明细里如实
   报数。未带分类标记的存量信号按 unknown 处理，同样不计数
   （fail-closed：说不清来历的样本不算数）。
3. **li 的批准记录**：台账 evaluation/promotions.jsonl（只追加）里
   有该场景的批准记录。批准记录由 `shadowclaw promotion approve`
   写入——该命令本身即批准动作，须 li 本人在场执行（见 cli.py）。

核验不过时 ensure_promoted 抛 PromotionError，消息是人话清单：
差哪几项、每项差多少。台账只追加：批准是事实记录，不回改；门槛
现状以每次 check 的实时核验为准（批准在案但另两项后来退步，
autonomous 照样拒载——批准不是免死金牌）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MIN_SAMPLES = 5
PROMOTIONS_FILE = "promotions.jsonl"


class PromotionError(Exception):
    """转正门槛未过：消息即人话原因清单，可直接给人看。"""


@dataclass
class GateItem:
    name: str                       # regression / samples / approval
    ok: bool
    detail: str                     # 人话明细（数字带出处）


@dataclass
class PromotionCheck:
    scenario: str
    items: list[GateItem] = field(default_factory=list)
    sample_counts: dict[str, int] = field(default_factory=dict)
    min_samples: int = DEFAULT_MIN_SAMPLES

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)

    def unmet(self) -> list[str]:
        return [f"{item.name}：{item.detail}"
                for item in self.items if not item.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "ok": self.ok,
            "min_samples": self.min_samples,
            "sample_counts": dict(self.sample_counts),
            "items": [{"name": i.name, "ok": i.ok, "detail": i.detail}
                      for i in self.items],
        }


# ------------------------------------------------------------ 样本分类

def classify_sample(extra: dict[str, Any]) -> str:
    """影子样本分类（门槛只数 real_live）：

    - mock：sample_kind=mock（MockUnit 演练样本）；
    - real_live：sample_kind=real 且 origin=live_task（真实任务同步
      双跑——转正要的就是这种样本）；
    - replay：origin=replay_contrast（历史回放对照，非真实任务双跑）；
    - unknown：标记缺失/对不上（存量信号），fail-closed 不计数。
    """
    if extra.get("sample_kind") == "mock":
        return "mock"
    if extra.get("origin") == "replay_contrast":
        return "replay"
    if extra.get("sample_kind") == "real" \
            and extra.get("origin") == "live_task":
        return "real_live"
    return "unknown"


def _count_samples(eval_dir: Path, scenario: str) -> dict[str, int]:
    """数信号账本里该场景的影子 outcome 样本（按 classify_sample 分桶）。

    场景归属取信号 extra.scenario，缺失时按 troubleshooting（账本
    存量信号全部产生于排障场景的影子运行，口径在此写明）。
    """
    from .signals import SignalStore
    counts = {"real_live": 0, "replay": 0, "mock": 0, "unknown": 0}
    store = SignalStore(Path(eval_dir) / "signals" / "signals.jsonl")
    for sig in store.list(kind="outcome"):
        if not str(sig.source).startswith("shadow"):
            continue                # 只数影子运行产生的样本
        sig_scenario = str(sig.extra.get("scenario") or "troubleshooting")
        if sig_scenario != scenario:
            continue
        counts[classify_sample(sig.extra)] += 1
    return counts


def _regression_rounds(eval_dir: Path, scenario: str) -> list[str]:
    """读迭代档案，返回该场景留下退步记录的轮次清单（轮次号 + 条数）。"""
    bad: list[str] = []
    iterations = Path(eval_dir) / "iterations"
    if not iterations.is_dir():
        return bad
    for round_dir in sorted(iterations.iterdir()):
        archive = round_dir / "iteration.json"
        if not archive.exists():
            continue
        try:
            data = json.loads(archive.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue                # 档案损坏不冒充无退步，下方计数口径注明
        if str(data.get("scenario") or "troubleshooting") != scenario:
            continue
        regressions = data.get("regressions") or []
        if regressions:
            bad.append(f"轮次 {data.get('round_id', round_dir.name)}"
                       f"（{len(regressions)} 条退步记录，"
                       f"判定 {data.get('verdict', '?')}）")
    return bad


# ---------------------------------------------------------------- 台账

def _promotions_path(eval_dir: Path) -> Path:
    return Path(eval_dir) / PROMOTIONS_FILE


def list_promotions(eval_dir: Path) -> list[dict[str, Any]]:
    """读批准台账（只追加文件，一行一条 JSON）。文件不存在 = 空台账。"""
    path = _promotions_path(eval_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def record_approval(eval_dir: Path, scenario: str, check: PromotionCheck,
                    *, source: str = "cli:promotion approve"
                    ) -> dict[str, Any]:
    """把 li 的批准写入台账（只追加）。记录自带写入时刻的门槛核验
    快照——日后复核时能看到「批准时各项是什么状态」，不靠回忆。

    注意：写入批准 ≠ 场景已转正。转正生效与否永远以 check 的三项
    实时核验为准；另两项不满足时，加载 autonomous 仍被拒。
    """
    record = {
        "scenario": scenario,
        "approved_by": "li",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "gate_snapshot": check.to_dict(),
    }
    path = _promotions_path(eval_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---------------------------------------------------------------- 核验

def check_promotion(scenario: str, *, eval_dir: Path,
                    min_samples: int = DEFAULT_MIN_SAMPLES
                    ) -> PromotionCheck:
    """三项门槛实时核验。只读，不写任何文件。"""
    check = PromotionCheck(scenario=scenario, min_samples=min_samples)

    bad_rounds = _regression_rounds(eval_dir, scenario)
    check.items.append(GateItem(
        name="regression",
        ok=not bad_rounds,
        detail=("迭代档案中无退步记录" if not bad_rounds else
                "存在退步记录：" + "；".join(bad_rounds)
                + "（整轮拒绝的轮次在案，转正须先清账或经 li 认定）")))

    counts = _count_samples(eval_dir, scenario)
    check.sample_counts = counts
    real = counts["real_live"]
    check.items.append(GateItem(
        name="samples",
        ok=real >= min_samples,
        detail=(f"真实任务双跑样本 {real}/{min_samples}"
                f"（回放样本 {counts['replay']} 条、Mock 样本 "
                f"{counts['mock']} 条、来历未标 {counts['unknown']} "
                "条均不计数——门槛只认真实任务同步双跑）")))

    approvals = [r for r in list_promotions(eval_dir)
                 if r.get("scenario") == scenario]
    latest = approvals[-1] if approvals else None
    check.items.append(GateItem(
        name="approval",
        ok=latest is not None,
        detail=(f"li 批准记录在案（{latest.get('approved_at', '?')}，"
                f"来源 {latest.get('source', '?')}）" if latest else
                "台账中无 li 的批准记录（走 shadowclaw promotion "
                "approve 写入，须 li 本人在场执行）")))
    return check


def ensure_promoted(scenario, *, eval_dir: Path,
                    min_samples: int = DEFAULT_MIN_SAMPLES) -> None:
    """加载 autonomous 场景前的门（loader 的 promotion_gate 实现）。

    过门静默返回；不过门抛 PromotionError，消息列全差项。
    scenario 可传场景名或带 .name 的场景对象（loader 注入点传对象）。
    """
    name = getattr(scenario, "name", scenario)
    check = check_promotion(str(name), eval_dir=Path(eval_dir),
                            min_samples=min_samples)
    if not check.ok:
        raise PromotionError(
            f"场景 {name} 未过转正门槛，autonomous 模式拒载"
            "（fail-closed）。差项：\n- " + "\n- ".join(check.unmet()))


def gate_for(eval_dir: Path, *,
             min_samples: int = DEFAULT_MIN_SAMPLES):
    """造一个 loader 用的 promotion_gate 闭包（组装点接线用）。"""
    def gate(scenario) -> None:
        ensure_promoted(scenario, eval_dir=Path(eval_dir),
                        min_samples=min_samples)
    return gate
