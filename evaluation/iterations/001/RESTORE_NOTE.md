# 轮次 001 档案恢复注记（2026-10-22）

**事件**：阶段 5 施工期间，验收冒烟误跑 `shadowclaw iterate --mock`
（默认轮次号 001、默认 eval 目录为真实档案），覆盖了本目录三件文件：
`iteration.json`、`answers-baseline.json`、`signals-baseline.jsonl`；
另 `evaluation/proposals/dispositions.json` 被同次运行的提案生成器
重写（该文件为幂等处置账，内容应与原一致，但无原件可逐字比对），
`trajectory.db` 被追加 6 条 mock 运行记录（原 12 条真实运行完好，
按 started_at 日期可区分）。

**恢复情况**：
- `iteration.json`：已按 persist_round 原形状确定性重建。分项数据
  出处 = reports/iteration-report-001.md 回归对照表与
  rejudge-001.md 原判列（两处一致）；run_id 与逐案花费出自本目录
  trajectory.db 的真实运行记录（基线合计 $0.2571、候选 $0.2815，
  与迭代报告口径吻合；基线前三案 run_id 与覆盖前的原文件头部
  记录逐一对上）。verdict=rejected、两条 regressions 原文按
  compare_case_scores 的生成口径还原。
- `answers-candidate.json`、`signals-candidate.jsonl`、
  `harness_work/`、`rejudge-001.md`、本目录外的主信号账本：未受损。
- **未能恢复**：`answers-baseline.json`（基线答复全文被 mock 文本
  覆盖；逐案判定数据已由上表保全，全文不可复原）、
  `signals-baseline.jsonl`（基线侧轮内信号被 mock 信号覆盖；
  其内容为模板化对照信号，主账本 evaluation/signals/signals.jsonl
  中的同批信号完好）。

**后续（2026-10-22 父代理恢复）**：四件文件已从 GitHub 远端原件
（commit 42a9539e，即事故前一日推送的版本）整件恢复，逐件
git hash-object 与远端 blob SHA 一致；tests/test_rejudge.py::
test_rejudge_round_001_archive 恢复通过，全量 96/96。本注记中
「未能恢复」两项就此撤销。trajectory.db 的 6 条 mock 追加记录
仍在本地库中（按日期可区分），远端版本未受污染。
