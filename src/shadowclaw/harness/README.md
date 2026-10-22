# harness（代码包）

职责一句话：把驾驭层正本文件（原则 / 手册 / 记忆 / 工具 / 护栏 / 场景，存于仓库根 `harness/`）加载、组装并注入执行层。

| 模块 | 职责 |
|---|---|
| `loader.py` | 解析正本文件（原则/护栏/场景/手册/工具卡/core 记忆）；加载时全量校验（引用缺失、锁定级批准标记）；按内容哈希算 harness 版本号供轨迹打标 |
| `assembler.py` | 场景组装唯一入口：原则（最前）→ 身份 → core 记忆 → 手册（执行单元全文/规划者摘要）→ 工具卡摘要 → 检索命中的记忆；产出护栏策略对象与合并从严的预算顶 |
| `memory.py` | 记忆检索 v1：五个长期记忆文件解析成条目索引，关键词（英文词元 + 中文 bigram）命中排序；core 常驻不走检索 |
| `guardrails.py` | 把护栏矩阵变成执行层 ApprovalPolicy：read→ALLOW、write→ASK（攒批先以 ASK 落地）、irreversible→ASK 且标单独确认、forbidden→DENY、矩阵外 ASK |
| `conflicts.py` | 事实落板冲突检查最小版：同键异值拒落板、标 conflict、留给编排器/人工裁决（经 facts.py 的 commit_guard 挂钩接入） |
| `proactivity.py` | 主动性开关（阶段 5）：条目登记表 + `harness/proactivity.yaml` 读取校验，未登记条目拒载，全部默认 off，不含任何触发器 |

阶段 2 落地；阶段 5 增量：loader 场景 `mode`（shadow/autonomous）与转正门注入点（promotion_gate）、assembler 组装产出带 mode、proactivity 装载。记忆写入的分类器/待批入队属评估层（阶段 4），不在本包。
