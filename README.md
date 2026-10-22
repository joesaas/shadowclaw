# ShadowClaw

只给 li 自己用的个人 Agent：配他工作中的工具（先行 GitHub、Jira），核心要求是「能像 li 一样思考和解决问题」。成长线：先当影子观察学习，再升格成分身独立干活。

三层架构：**执行层**（编排运行时）→ **驾驭层**（原则 / 方法手册 / 记忆 / 工具 / 护栏 / 场景，文件为正本）→ **评估层**（信号 → 提改动 → 回归对照 → 分级合入，只改驾驭层不改执行层代码）。

设计与决策文档在 `docs/`：

- `docs/design.md` — 三层架构设计定版
- `docs/design-execution.md` / `design-harness.md` / `design-evaluation.md` — 三层模块级详细设计与层间契约
- `docs/tech-selection.md` — 技术选型（Python + Google ADK 底座、OpenRouter 主通道、MCP 优先、SQLite）
- `docs/roadmap.md` — 落地路线图（阶段 0 冷启动提炼已完成，审定材料在 `docs/stage0-*`）

## 阶段 1 已实现（执行层最小回路）

代码在 `src/shadowclaw/`，按三层分包：`execution/`（阶段 1）、`harness/`（阶段 2）、`evaluation/`（阶段 3 地基 + 阶段 4 回路）。仓库根的 `harness/` 是驾驭层内容正本，代码只经配置读取其路径，不写死。

执行层模块（一个模块一个职责，接口为最小 Protocol，组装只在 CLI 入口）：

| 模块 | 职责 |
|---|---|
| `contracts.py` | Unit / TrajectoryStore / ApprovalPolicy / CheckpointStore 最小接口契约 |
| `plan.py` | Plan / Step 数据类（JSON 可序列化）+ 校验器（深度 ≤2、预算、单元存在；route/parallel/reflect 报「未实现」而非「非法」）+ 降级构造 |
| `units.py` | 单元注册表（开闭接入）；AdkUnit（LlmAgent 为身体，每次调用开一次性 session）；MockUnit（脚本化确定性输出） |
| `orchestrator.py` | 只管编排：计划校验→重出一次→降级保底；串行执行；delegate 独立上下文子执行；审批挂钩；预算硬顶；步级检查点 |
| `facts.py` | 事实板：单元只提议、编排器才落板（单写者），落板去重 |
| `trajectory.py` | 固定 15 种事件枚举 + SQLite 存储 + 复盘导出；每条事件带 run_id、时间戳、驾驭层版本号；花费缺失按价格表估算并标 estimated |
| `approval.py` | 审批策略：默认 fail-closed——无规则命中 ASK，无人值守时 ASK 等同 DENY 并落轨迹 |
| `checkpoint.py` | 计划步级检查点（SQLite）；resume 从未完成步整步续跑，不依赖 ADK session |
| `context.py` | 上下文组装：instruction + 事实板已提交快照 + 本步输入，不依赖 session 历史（隔离语义在此保证） |
| `config.py` | 只读环境变量 / 默认值（DB 路径、harness 目录、模型规格、价格表、预算硬顶），无配置框架 |
| `demo.py` | 内置验收场景：排障任务（triage 定界 → delegate 委派查日志 → 落板 → summarize 出结论），全程离线确定性 |
| `cli.py` | 入口组装点：`run` / `replay` / `resume` 三个子命令 |

五原语阶段 1 只实现串行与委派，其余三原语已在 Plan 数据形状与校验口径上预留。

### 跑法

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest            # 40 项测试（阶段 1 的 25 + 阶段 2 的 15）

.venv/bin/shadowclaw run              # 跑内置 demo 排障场景（离线）
.venv/bin/shadowclaw run --plan plan.json   # 跑一个 JSON 计划文件
.venv/bin/shadowclaw replay <run_id>  # 按轨迹打印时间线复盘
.venv/bin/shadowclaw resume <run_id>  # 从检查点续跑（阶段 1 注册表为 demo 集）
```

配置走环境变量：`SHADOWCLAW_DB_PATH`（默认 `./shadowclaw.db`）、`SHADOWCLAW_HARNESS_DIR`（默认 `./harness`）、`SHADOWCLAW_MODEL`、`SHADOWCLAW_HARNESS_VERSION`、`SHADOWCLAW_BUDGET_HARD_CAP`。

## 阶段 2 已实现（驾驭层 v1 + GitHub 接入，Jira 暂缓）

**内容正本**（仓库根 `harness/`，阶段 0 审定材料落地）：

| 正本 | 内容 |
|---|---|
| `memory/` | core.md（核心常驻）+ facts（19）/ projects（18）/ decisions（4）共 41 条事实 + inferences 13 条审定推断（带置信度与依据；驳回的 2 条不入）+ people（按审定口径暂空）+ proposals/（待批队列机制） |
| `guardrails/guardrails.md` | 锁定级护栏：三级审批矩阵（read 自动 / write 攒批确认 / irreversible 单独确认）+ 预算顶 + 红线，frontmatter 带批准标记 |
| `tools/github.md` | GitHub 工具卡：MCP 接入、凭证只写引用（`GITHUB_TOKEN` 环境变量名）、每个操作标风险级 |
| `scenarios/troubleshooting.yaml` | 排障场景：组装唯一入口（原则全量 / 手册全文 / core+检索记忆 / GitHub 工具卡上限 read / 护栏合并从严） |

**代码**（`src/shadowclaw/harness/` 五模块 + 执行层增量）：

| 模块 | 职责 |
|---|---|
| `harness/loader.py` | 解析正本 + 加载时全量校验（引用缺失报错、锁定级文件校验批准标记缺失拒载）；按内容哈希算 harness 版本号（原则+护栏+场景文件） |
| `harness/assembler.py` | 场景组装：原则（最前）→ 身份 → core 记忆 → 手册（执行单元全文 / 规划者摘要）→ 工具卡摘要 → 检索记忆；产出护栏策略对象与合并从严的预算顶 |
| `harness/memory.py` | 记忆检索 v1：条目索引 + 关键词（英文词元 + 中文 bigram）命中排序；向量后置 |
| `harness/guardrails.py` | 护栏矩阵 → 执行层 ApprovalPolicy：read→ALLOW、write→ASK（攒批先以 ASK 落地）、irreversible→ASK 且标单独确认、forbidden→DENY、矩阵外 ASK |
| `harness/conflicts.py` | 事实落板冲突检查最小版：同键异值拒落板并标 conflict、留待人工裁决（经 `facts.py` 的 `commit_guard` 挂钩接入，编排器新增可选注入点） |
| `execution/github_tools.py` | GitHub MCP 接入工厂（McpToolset + 远程端点 + 工具按风险级过滤挂载）；无 `GITHUB_TOKEN` 返回 None 并明确告警；FakeGitHub 假工具走相同注册路径供测试/demo |
| `execution/demo2.py` | 阶段 2 验收演示：排障场景组装后跑「data-pipeline CI 失败排障」——脚本化 LLM 经假工具真调用只读集、落板出结论、写步骤（打标签）被护栏 ASK 拦下，全程轨迹带真实版本号 |

版本号来源已换：`Config.from_env()` 不再用 `stage1-dev` 占位——未显式设置 `SHADOWCLAW_HARNESS_VERSION` 时按 harness 内容哈希现算（目录不存在时降级 `unversioned` 并在轨迹留痕）。直接构造 `Config` 的默认值保留占位（阶段 1 单测夹具口径）。

### 跑法（阶段 2 增量）

```bash
.venv/bin/shadowclaw demo2            # 阶段 2 验收演示（离线，FakeGitHub）
.venv/bin/python -m pytest            # 40 项测试（阶段 1 的 25 + 阶段 2 的 15）
```

**如实标注的出入**：GitHub 真实联调缺 token——本机无 `GITHUB_TOKEN`，MCP 工厂只验证了「无 token 诚实降级」与「有 token 能构造 toolset（惰性连接，未发起会话）」，端点连通性与真实工具清单未验；工具名与风险分级按官方 GitHub MCP 的常见命名与工具卡口径维护，联调时以服务端实际清单为准校对。Jira 按 li 决定暂缓（见 roadmap 阶段 2 补注）。

## 阶段 3 已实现（影子模式试运行 · 历史回放版）

形态：回放双跑——每个案例只给 ShadowClaw「当时的症状简报」（不给答案），它经排障场景组装（原则 + 手册 + 记忆）后用真实模型独立排障，产出与参照解（li 当时的实际根因与关键动作）对照，偏差定位到层。真·同步双跑（真实任务进行时同跑）不在本轮，待真实任务。

| 件 | 内容 |
|---|---|
| `evaluation/signals.py` | 信号存储：JSONL 追加写，字段按评估层设计 Step 2；correction/outcome 缺出处引用拒存 |
| `evaluation/regression.py` | 回归集加载 + 程序硬查（结论命中=根因关键词组全中、动作覆盖、编造/未核实断言两类原则性错误硬查） |
| `evaluation/shadow.py` | 影子运行器：场景组装 → 真实模型求解（LiteLlm 走 OpenRouter）→ 硬查 → judge 自评（仅参考）→ 偏差归因 → 信号入账；每案例调用 ≤8，遇额度错误立即停 |
| `evaluation/regression/cases/` | 回归集 v1：6 个真实历史案例（DocReader .env 未进进程、EC2 Node 版本过旧、GitHub App 403 能力边界、Telegram hook log() 传数组、write 假成功、data-pipeline 信息页 404），简报只写症状、参照解带出处 |
| `reports/shadow-report-v1.md` | 影子报告 v1 |

### 跑法（阶段 3 增量）

```bash
.venv/bin/shadowclaw shadow           # 真实回放（需 OpenRouter key：环境变量或保险库替身）
.venv/bin/shadowclaw shadow --mock    # 离线验流程（脚本答复固定未命中，只验 mechanics）
.venv/bin/python -m pytest            # 48 项测试（阶段 1 的 25 + 阶段 2 的 15 + 阶段 3 的 8）
```

## 阶段 4 已实现（评估层回路转满第一轮）

形态：信号 → 提案 → 回归对照 → 整轮判定 → 分级合入 → 迭代报告，整条回路建成并用阶段 3 的真实信号（32 条）实跑转满一轮。judge 与硬查一致率 67%＜70%，本轮权重归零：运行器不调用、报告不引用其判定，只计数。

| 件 | 内容 |
|---|---|
| `evaluation/proposals.py` | ChangeProposal 字段级实现（目标文件/diff/四级 class 含 infra/证据信号 id/归因/预期效果含副作用/状态机）+ 提案生成器（规则写死：按 harness 版本切分、先定位到层才提改动；judge 信号只计数不作证据）+ 落盘 `evaluation/proposals/`（提案 JSON + 索引 + 信号处置账，幂等） |
| `evaluation/iterate.py` + CLI `iterate` | 回路运行器：基线跑 6 案 → 生成提案 → lesson 级应用到工作副本 → 候选跑 → 分项对比（命中/覆盖/原则错误）→ 任一退步整轮拒绝；通过才合入正本（合入前备份到轮次目录，可回滚）；structural/locked 只生成转 pending_li；答复全文按轮次存档 |
| 判定仪表修复（infra） | 编造核查合法来源扩到全部组装材料（手册全文 + core 记忆），数值 SI 换算同族比对（相对误差 ≤5%）+ 建议性语境豁免；certainty 去脆：关键词未中且结论段语义核查（案例 `semantic_groups` 规则同义组）也未中才成立 |
| `reports/iteration-report-001.md` | 迭代报告 001 |

### 跑法（阶段 4 增量）

```bash
.venv/bin/shadowclaw iterate          # 真实转一轮（需 OpenRouter key；两轮回归预算上限 $1）
.venv/bin/shadowclaw iterate --mock   # 离线验回路 mechanics（不合入正本）
.venv/bin/python -m pytest            # 61 项测试（48 + 阶段 4 的 13）
```

**第一轮结果（如实）**：基线 6/6 命中、原则性错误 0（阶段 3 第二轮的三条仪表误报在本轮基线未再现——新仪表下 write 案 0 编造标记、ec2/github 0 certainty 标记）；生成提案 3 条（infra 修复 2 条已随代码落地 merged；lesson 手册补条 1 条）；候选跑出现两处退步（github 命中由真变假、telegram 覆盖 3/4→2/4），按预注册规则**整轮拒绝**，手册正本未改动，lesson 提案以 rejected + 负教训注记存档。每格均为单次采样，模型随机性是退步的可能成因之一——判定口径不因此放松，待后续轮次攒样本再看。

**测法升级（2026-10-21，li 拍板 (a)+(c)：先解决测不准，拒绝门槛不动）**：根因命中判定改为「结论段优先、全答复兜底」（结论段抽不到时在全答复用同义组核查，仍须全中，严格度不变，两侧同等适用）；新增离线重判 `shadowclaw rejudge <round_dir>`（当前仪表对存档答复对称重判，零花费）；iterate 两侧支持 `--samples N`（聚合口径：命中取严格多数、覆盖取中位数.5 向下取整、原则错误任一轮即记）。第 001 轮离线重判（`evaluation/iterations/001/rejudge-001.md`）：github 退步经语义兜底消除（属仪表漏接，源于其生成的 correction 候选 sig-78a40fd166 标注「经重判否定」），telegram 覆盖退步仍成立，整轮拒绝维持；原 rejected 判定记录不改、手册正本不动，是否翻案待 li 定。

**如实标注**：本轮真实回放被 OpenRouter key 的上限拦下，未产生效果结论——组装提示词 23,023 tokens 超过该 key 允许的提示上限 12,970 tokens（首次尝试另遇 402 预留额度，压小 max_tokens 预留后仍被提示上限拦下）。组装提示含原则 + 手册全文是设计口径，不为省额度缩减；需 li 调高 key 限额或充值后重跑 `shadowclaw shadow` 即可续。报告中 6 案例为 1 失败 + 5 未跑，原则性错误一项标「未能判定」，不作验收通过结论。信号账本本轮未写入（无跑完案例）。
