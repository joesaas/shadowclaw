# 影子报告 v1（阶段 3 · 历史回放版）

- 生成时间：2026-10-21T07:33:11.814948+00:00
- 驾驭层版本：h-3b9b34e9cf（内容哈希）
- 模型：google/gemini-3.8-flash（经 OpenRouter）
- 形态：历史回放双跑——只给症状简报，ShadowClaw 独立排障后与参照解对照；非真实任务同步双跑。
- 判定口径：程序硬查为验收口径（结论命中=根因关键词组全中；动作覆盖=参照动作关键词命中数）；judge 为模型自评，仅参考，与硬查不一致时以硬查为准。
- ⚠️ 中途停止：提示词 23023 tokens 超过 OpenRouter key 允许的提示上限 12970 tokens（组装提示含原则+手册全文，属设计口径，不缩减），已按纪律停止，未跑案例标 not_run；需 li 调高 key 限额或充值后再跑

## 总览

| 案例 | 结论命中 | 动作覆盖 | 花费 | 偏差数 | run_id |
|---|---|---|---|---|---|
| data-pipeline 健康检查正常但信息页 404 | 运行失败 | - | $0.0000 | - | fba0932452a6 |
| DocReader 鉴权 Key 设了但不生效 | 未跑 | 未跑 | - | - | - |
| data-pipeline 在 EC2 首构建失败，tsc 自身崩溃 | 未跑 | 未跑 | - | - | - |
| GitHub App 建仓与 fork 连续 403 | 未跑 | 未跑 | - | - | - |
| Telegram 自动回复失效，消息被取走却无回复 | 未跑 | 未跑 | - | - | - |
| 写入工具报成功，文件却不在盘上 | 未跑 | 未跑 | - | - | - |

## 原则性错误（验收线：应为 0）

未能判定：本轮没有案例跑完（见上方中途停止原因），不作「无原则性错误」的验收结论。

## 偏差清单（定位到层）

无已跑完案例，未产生偏差清单。

## judge 参考（模型自评口径，不作验收依据）

本轮无可用 judge 结果。

## 信号入账汇总

本轮未写入信号。

## 运行失败

- datapipeline-info-page-404：{'kind': 'step_failed', 'message': 'litellm.APIError: APIError: OpenrouterException - {"error":{"message":"Prompt tokens limit exceeded: 23023 > 12970. To increase, visit https://openrouter.ai/workspaces/default/keys/36a153d0532c73b22148705dceb5da02e577371228c0f65174811bc9f34b38f5 and adjust the key
