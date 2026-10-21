# 影子报告 v1（阶段 3 · 历史回放版）

- 生成时间：2026-10-21T08:03:50.184119+00:00
- 驾驭层版本：h-3b9b34e9cf（内容哈希）
- 模型：google/gemini-3.8-flash（经 OpenRouter）
- 形态：历史回放双跑——只给症状简报，ShadowClaw 独立排障后与参照解对照；非真实任务同步双跑。
- 判定口径：程序硬查为验收口径（结论命中=根因关键词组全中；动作覆盖=参照动作关键词命中数）；judge 为模型自评，仅参考，与硬查不一致时以硬查为准。

## 总览

| 案例 | 结论命中 | 动作覆盖 | 花费 | 偏差数 | run_id |
|---|---|---|---|---|---|
| data-pipeline 健康检查正常但信息页 404 | ✅ | 2/3 | $0.0483 | 0 | 7eb48c82b2ee |
| DocReader 鉴权 Key 设了但不生效 | ✅ | 4/4 | $0.0519 | 0 | 2b667b0fb81d |
| data-pipeline 在 EC2 首构建失败，tsc 自身崩溃 | ❌ | 3/4 | $0.0569 | 2 | 3587ad68ccda |
| GitHub App 建仓与 fork 连续 403 | ❌ | 2/3 | $0.0539 | 1 | 75c9d20a83da |
| Telegram 自动回复失效，消息被取走却无回复 | ✅ | 2/4 | $0.0522 | 1 | 7bd3ddca3f7b |
| 写入工具报成功，文件却不在盘上 | ✅ | 3/3 | $0.0414 | 2 | e9f9184222c7 |

## 原则性错误（验收线：应为 0）

- [ec2-node-version-too-old] 原则性错误（certainty_without_evidence）——结论未命中参照根因，却以确定口吻下结论且无未证实标记（轨迹 3587ad68ccda）
- [write-false-success] 原则性错误（fabricated_metric）——答复出现量化数值「15.6KB」，简报与材料中无此数（轨迹 e9f9184222c7）
- [write-false-success] 原则性错误（fabricated_metric）——答复出现量化数值「4KB」，简报与材料中无此数（轨迹 e9f9184222c7）

## 偏差清单（定位到层）

- [原则] ec2-node-version-too-old：原则性错误（certainty_without_evidence）。证据：结论未命中参照根因，却以确定口吻下结论且无未证实标记（轨迹 3587ad68ccda）
- [模型] ec2-node-version-too-old：根因判断未命中参照解。证据：未命中的根因关键词组：过旧/过低/低于/太旧/老旧/不支持（轨迹 3587ad68ccda）
- [模型] github-app-no-create-permission：根因判断未命中参照解。证据：未命中的根因关键词组：没有/无此/不具备/未授予/本就无/不支持（轨迹 75c9d20a83da）
- [执行] telegram-hook-log-array：关键动作覆盖不足（2/4）。证据：未覆盖动作：按链路段定位故障段；把 fields 改为对象包裹（轨迹 7bd3ddca3f7b）
- [原则] write-false-success：原则性错误（fabricated_metric）。证据：答复出现量化数值「15.6KB」，简报与材料中无此数（轨迹 e9f9184222c7）
- [原则] write-false-success：原则性错误（fabricated_metric）。证据：答复出现量化数值「4KB」，简报与材料中无此数（轨迹 e9f9184222c7）

按层分布：原则 3 条，执行 1 条，模型 2 条

## judge 参考（模型自评口径，不作验收依据）

- datapipeline-info-page-404：hit=True，score=8，被测答复准确推断出‘EC2 线上运行进程未拉取最新构建/代码未同步’这一核心根因并给出了对应的版本核查与重启建议，但将其作为并列推断且未最终定死。
- docreader-env-not-loaded：hit=True，score=10，被测答复精准命中了服务进程未加载.env/环境变量导致Key为空、auth.required降级为false并穿透为404的根因，且给出的排查与验证动作完全一致。
- ec2-node-version-too-old：hit=True，score=6，被测答复准确识别出服务器Node版本过旧导致tsc对??语法报错而非业务代码问题，但输出不完整且截断在思考草稿阶段，缺失具体修复动作。
- github-app-no-create-permission：hit=True，score=9，被测答复准确判断出该问题是 GitHub App 的权限/能力边界限制而非 Bug 且重试无效，完全命中参照根因，仅因结尾输出截断略微扣分。
- telegram-hook-log-array：hit=True，score=10，被测答复准确指出了 hook 脚本在消费消息后、唤醒 worker 前因 log() 的 fields 参数误传为数组而非对象，在 set -e 约束下崩溃导致破坏性消费后丢消息的完整根因链。
- write-false-success：hit=True，score=10，准确指出大Payload写入假成功、未实际物理落盘的根因，且给出的分段写入与回读核验等动作完全契合参照。
- 与硬查一致率：4/6（校准口径见设计 Step 7，低于 70% 时 judge 权重归零）

## 信号入账汇总

- correction：3 条
- judge：6 条
- outcome：6 条
- correction 均为回放对照产生的候选（非 li 亲口纠正），已在信号 extra 里标 origin 与降权说明。
