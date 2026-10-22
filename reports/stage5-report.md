# 阶段 5 报告（转正与扩展 · 范围经 li 裁剪）

- 日期：2026-10-22
- 范围：li 2026-10-22 裁剪后三件——补齐三原语、影子转分身的转正机制、主动性开关。
- 测试：pytest **95 passed, 1 failed**（共 96 项 = 原 70 + 阶段 5 新增 26；原 70 项中 1 项因前提变化更新、其余不回归。唯一失败项 `test_rejudge_round_001_archive` 是文末出入 7 的施工事故后果——001 轮基线答复全文被覆盖后，该档案守护测试如实报警，未改测试遮掩）。

## 一、做了什么

### 1. 三原语补齐（执行层五原语齐套）

| 原语 | 实现要点 |
|---|---|
| route | 规则函数（数据形态）：`when: {source: prev_output/input/facts, contains: 子串}`，自上而下首中即选；全不中走 `default`；无 default 则本步失败。选择过程记 `route_decided` 事件：命中规则序号、选中者、候选全集、落选者 |
| parallel | `branches` 一组 sequence 子步并发执行（asyncio 协程池，上限 4）；每分支独立事实板（隔离副本，只带父板快照作种子）；整组结束后编排器按计划顺序把分支已提交事实逐条提议到父板——单写者与冲突检查口径不变。单分支失败记 error 事件并在 `parallel_finished` 里标明，不拖垮整组；全部分支失败才算本步失败；预算触顶不隔离、直接中止整单 |
| reflect | 执行单元后过检查：规则清单（`contains:` / `not_contains:` / `min_length:` / `max_length:` / `fact_proposed`，确定性判定）+ 可选 critic 单元自评（占位接口，回复含「打回」判不过、含「通过」判过、无标记按 fail-closed 判不过）。不过则带逐条理由反馈重跑原单元，`max_rounds` 默认 2，超限按失败处理。每轮记 `reflect_round` 事件（轮次/判定/理由/判定来源）。草稿轮的事实提议只记 proposed、不落真板，通过轮才由编排器落板 |

配套：Plan schema 扩展（Step 新增 route / branches / checklist / max_rounds / critic 字段，to_dict/from_dict 往返保真）；校验与三拒口径一致（结构错 illegal、引用不存在 unknown_unit，逐项有测试）；单元注册表契约未动（Mock/ADK 同契约）；轨迹枚举按执行层设计 Step 5 补入 route_decided / parallel_started / parallel_finished / reflect_round 四种；规划者提示同步——`assembler.planner_view()` 追加五原语说明（与 `plan.PRIMITIVES_GUIDE` 同源）。

**演示证据**（`shadowclaw demo3`，离线确定性，run_id `28bee4185d9d`，status success）：

- route_decided：`{"matched_rule": 0, "chosen": "fd-expert", "candidates": ["fd-expert", "general-expert"], "rejected": ["general-expert"]}`——定界产出含「句柄」，规则命中专家，兜底人选落选留痕。
- parallel_finished：`{"succeeded": ["b-log", "b-metric"], "failed": [{"branch": "b-flaky", "error": "指标通道连接超时（模拟分支失败）"}]}`——失败分支被隔离；两成功分支对「故障域」给出同键异值事实，合并时后到者被冲突检查拒落板（guard 记录 1 条），后续事实板只见先到值。
- reflect_round 两轮：第 1 轮 rejected（四条理由：缺「根因」、含「未证实」、长度 14<30、未提议事实）→ 第 2 轮 pass；最终报告为带反馈重做后的合格版。

### 2. 影子转分身的转正机制

- **场景模式开关**：场景 yaml 新增 `mode`（缺省 shadow；troubleshooting.yaml 已显式写 `mode: shadow`）。`load_harness` → `assemble` 全程带 mode 分流；autonomous 允许真正执行工具调用，**护栏审批一件不减**（write/irreversible 仍 ASK、forbidden 仍 DENY、fail-closed 不变——审批策略对象与模式无关，未动）。
- **转正门**（`evaluation/promotion.py`）：三项须同时满足——①迭代档案（iterations/*/iteration.json）该场景无 regressions 记录；②真实任务影子双跑样本 ≥ 阈值（默认 5）：只数信号账本中 `sample_kind=real 且 origin=live_task` 的 outcome 样本，回放样本（origin=replay_contrast）与 Mock 样本（sample_kind=mock）**不计数**、只在明细里报数，未标记存量按 unknown 不计数（fail-closed）；③批准台账 `evaluation/promotions.jsonl`（只追加）有 li 的批准记录。影子运行写信号时已同步打上 scenario / sample_kind 分类标记。
- **CLI**：`shadowclaw promotion check|approve|list`。approve 即批准动作（docstring 与命令帮助均写明须 li 本人在场执行），写入时附当刻门槛核验快照；写入批准 ≠ 已转正，生效永远以三项实时核验为准。
- **加载即拒**：autonomous 场景在 `load_harness` 时经注入的 `promotion_gate` 核验；未注入 gate 直接拒载、gate 不过门抛 PromotionError 并列出差项（fail-closed）。CLI 的 shadow / iterate 路径已接线 gate。

### 3. 主动性开关

- `harness/proactivity.yaml` + `harness/proactivity.py`：登记表三条（remind_pending 主动提醒未决事项 / propose_iterations 主动提出迭代提案 / sync_progress 主动同步进展），**全部默认 off**；配置文件缺失 = 全关；未登记条目或形状不对 → 加载拒载；`is_enabled` 问未登记条目直接报错。
- 开关只回答「可否被触发」：**未建任何定时/后台触发器**，启用任何一项须 li 单独发令另行接入触发路径。

## 二、转正门槛现状（排障场景，实测原文）

`shadowclaw promotion check troubleshooting`（2026-10-22 实跑，退出码 1）：

```
[差] regression：存在退步记录：轮次 001（2 条退步记录，判定 rejected）（整轮拒绝的轮次在案，转正须先清账或经 li 认定）
[差] samples：真实任务双跑样本 0/5（回放样本 12 条、Mock 样本 0 条、来历未标 0 条均不计数——门槛只认真实任务同步双跑）
[差] approval：台账中无 li 的批准记录（走 shadowclaw promotion approve 写入，须 li 本人在场执行）
结论：场景 troubleshooting 未过转正门槛（差 3 项）
```

三项全差，属预期：排障场景至今只有历史回放、没有真实任务同步双跑；轮次 001 的整轮拒绝记录在案（重判后维持）；台账为空。机制逐项说清差什么，未为演示放水。场景维持 shadow。转正路径也因此明确：先有真实任务双跑攒样本、迭代档案清账，最后 li 在场批准。

## 三、没做什么（如实）

- **li 裁剪项（暂缓）**：Telegram 接入、EC2 迁移、GitHub 真实联调——一件未碰；接入剩余工具（含 Jira）、调研/文档场景转正同未动。
- **真实双跑未发生**：转正机制是代码与台账就绪，样本为 0 是事实；本阶段没有、也不会用 Mock 或回放样本充数（mechanics 测试中的样本都在临时目录，不进真实台账）。
- **autonomous 实际执行未实跑**：没有场景过门，autonomous 路径只经 mechanics 测试验证（临时 harness + 临时台账造全过条件后放行加载）。iterate / rejudge 内部的 `load_harness` 未注入 gate——若将来某场景翻成 autonomous，这两条分析路径会按 fail-closed 拒载，需要用时再接线（宁可拒载，不可绕门）。

## 四、与纪律的出入（如实报）

1. **parallel 用 asyncio 协程池而非线程池**：任务书注「线程池即可」，但单元契约是 async（ADK 单元须在事件循环里跑），线程池需跨线程桥接事件循环、复杂度更高且隔离语义不变，故用 asyncio 并发 + 信号量上限 4。隔离/失败隔离/单写者合并三项语义与口径均按任务书实现。
2. **reflect 超限口径**：设计稿写「轮尽交最好一版并打标」，任务书明确「超限按失败处理」——从任务书，超限判本步失败。
3. **route 的 prev_output 取全局最近一步产出**：跨 delegate 子计划时「最近」含子步产出，此 v1 口径写明在 `_execute_route` docstring，未做帧内隔离（防过度设计；需要时再收紧）。
4. **tests/test_plan.py 一项测试更新**：`test_reserved_primitive_is_not_implemented_not_illegal` 原断言 route 报 not_implemented——阶段 5 实现 route 后前提消失，已改为断言三原语合法形状过校验（测试内注明取代关系）。其余原有 69 项一字未动、全部通过。
5. **顺手修正一处存量文档事实**：`src/shadowclaw/evaluation/README.md` 原写「阶段 4（未实现）」，与已落地的阶段 4 不符，已改为已落地并补阶段 5 一行。
6. **未处理的历史缺口**：harness 版本号仍不含手册哈希（阶段 4 已知缺口）——本阶段给场景文件加了 mode 字段，场景文件在哈希范围内，版本号随之变化（实测 h-3b9b34e9cf → h-59968023b2），手册哈希缺口维持原状待后续。
7. **施工事故（如实披露）**：验收冒烟时误跑 `shadowclaw iterate --mock`（默认轮次 001 + 默认真实 eval 目录），覆盖了轮次 001 档案的 `iteration.json`、`answers-baseline.json`、`signals-baseline.jsonl`，并重写 `evaluation/proposals/dispositions.json`（幂等处置账，无原件可逐字比对）、向轮次 trajectory.db 追加 6 条 mock 运行。处置：`iteration.json` 已按原形状确定性重建（分项据迭代报告与 rejudge-001.md 两处一致记录，run_id/花费据 trajectory.db 真实运行，合计 $0.2572/$0.2815 与原口径吻合），恢复后 promotion check 复测与事故前逐项一致；`answers-baseline.json` 全文与 `signals-baseline.jsonl` 原件**不可复原**（基线判定数据已保全，主信号账本完好）。全过程与恢复口径记录在 `evaluation/iterations/001/RESTORE_NOTE.md`。后果留痕：守护该档案的 `tests/test_rejudge.py::test_rejudge_round_001_archive` 因此失败（重判基线侧与原判对不上），**保留失败状态不改测试**，等 li 决定处置（例如以 rejudge-001.md 为准冻结该档案并调整测试口径，或重跑一轮真实迭代生成新档案）。教训：mock 冒烟必须显式指定临时 eval 目录与轮次号，不得用默认值碰真实档案。
