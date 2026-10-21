# evaluation（代码包）

职责一句话：消费执行层轨迹与 li 的反馈信号，提炼驾驭层改动候选、跑回归对照、按分级门合入。

阶段状态：
- 阶段 3（已落地先行版）：`signals.py` 信号存储（JSONL，字段按评估层设计 Step 2，correction/outcome 缺出处拒存）、`regression.py` 回归集加载与程序硬查、`shadow.py` 影子运行器（历史回放版：只给症状简报、真实模型独立排障、与参照解对照、偏差定位到层、产出影子报告并把信号入账）。回归集数据正本在仓库根 `evaluation/regression/cases/`，信号账本在 `evaluation/signals/`。
- 阶段 4（未实现）：改动提炼（proposer）、对照运行（contrast）、合入门（gate）与迭代报告。
