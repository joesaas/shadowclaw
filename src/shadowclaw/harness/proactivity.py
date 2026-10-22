"""主动性开关（阶段 5）：条目登记 + 读取校验，全部默认 off。

口径（li 拍板：主动性最后开、默认关、逐项启用）：
- 条目是封闭登记表 KNOWN_ITEMS，配置里出现未登记条目 → 拒载
  （HarnessError），不许随手发明新主动行为；
- 配置文件 harness/proactivity.yaml 缺失 = 全部 off（默认关不依赖
  文件存在）；
- 开关只回答「这个机制可不可以被触发」，本模块**不建任何触发器**：
  没有定时、没有后台轮询；将来启用某项时由 li 单独发令另行接入
  触发路径，开关本身不会让任何事情自己发生。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .loader import HarnessError

# 条目登记表：id -> 人话说明。新增条目必须先改这里（走评审），
# 配置文件只能开关已登记条目，不能自创。
KNOWN_ITEMS: dict[str, str] = {
    "remind_pending": "主动提醒未决事项",
    "propose_iterations": "主动提出迭代提案",
    "sync_progress": "主动同步进展",
}


@dataclass
class ProactivityConfig:
    """主动性配置：每个已登记条目一个布尔开关，缺省全 False。"""
    enabled: dict[str, bool] = field(
        default_factory=lambda: {name: False for name in KNOWN_ITEMS})

    def is_enabled(self, item: str) -> bool:
        if item not in KNOWN_ITEMS:
            raise KeyError(f"未登记的主动性条目：{item}"
                           f"（登记表：{'/'.join(KNOWN_ITEMS)}）")
        return self.enabled.get(item, False)

    def enabled_items(self) -> list[str]:
        return [name for name, on in self.enabled.items() if on]


def load_proactivity(root: Path | str) -> ProactivityConfig:
    """读 harness/proactivity.yaml；文件缺失返回全关默认。

    校验（带病拒载）：顶层必须是 items 映射；条目名必须在登记表内；
    每项必须是 {enabled: bool} 形状。
    """
    path = Path(root) / "proactivity.yaml"
    config = ProactivityConfig()
    if not path.exists():
        return config
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    items = raw.get("items")
    if not isinstance(items, dict):
        raise HarnessError(
            f"主动性配置形状不对（顶层须为 items 映射）：{path}")
    for name, spec in items.items():
        if name not in KNOWN_ITEMS:
            raise HarnessError(
                f"主动性配置出现未登记条目 {name!r}，拒载"
                f"（登记表：{'/'.join(KNOWN_ITEMS)}；新条目须先改"
                " proactivity.py 登记表并经评审）")
        if not isinstance(spec, dict) \
                or not isinstance(spec.get("enabled"), bool):
            raise HarnessError(
                f"主动性条目 {name} 形状不对（须为 enabled: true/false）")
        config.enabled[name] = spec["enabled"]
    return config
