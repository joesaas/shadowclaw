# ShadowClaw

只给 li 自己用的个人 Agent：配他工作中的工具（先行 GitHub、Jira），核心要求是「能像 li 一样思考和解决问题」。成长线：先当影子观察学习，再升格成分身独立干活。

三层架构：**执行层**（编排运行时）→ **驾驭层**（原则 / 方法手册 / 记忆 / 工具 / 护栏 / 场景，文件为正本）→ **评估层**（信号 → 提改动 → 回归对照 → 分级合入，只改驾驭层不改执行层代码）。

设计与决策文档在 `docs/`：

- `docs/design.md` — 三层架构设计定版
- `docs/design-execution.md` / `design-harness.md` / `design-evaluation.md` — 三层模块级详细设计与层间契约
- `docs/tech-selection.md` — 技术选型（Python + Google ADK 底座、OpenRouter 主通道、MCP 优先、SQLite）
- `docs/roadmap.md` — 落地路线图（阶段 0 冷启动提炼已完成，审定材料在 `docs/stage0-*`）

## 阶段 1 已实现（执行层最小回路）

代码在 `src/shadowclaw/`，按三层分包：`execution/`（本阶段实现）、`harness/` 与 `evaluation/`（占位，分别在阶段 2 / 阶段 4 落地）。仓库根的 `harness/` 是驾驭层内容正本（原则、手册），代码只经配置读取其路径，不写死。

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

.venv/bin/python -m pytest            # 25 项测试

.venv/bin/shadowclaw run              # 跑内置 demo 排障场景（离线）
.venv/bin/shadowclaw run --plan plan.json   # 跑一个 JSON 计划文件
.venv/bin/shadowclaw replay <run_id>  # 按轨迹打印时间线复盘
.venv/bin/shadowclaw resume <run_id>  # 从检查点续跑（阶段 1 注册表为 demo 集）
```

配置走环境变量：`SHADOWCLAW_DB_PATH`（默认 `./shadowclaw.db`）、`SHADOWCLAW_HARNESS_DIR`（默认 `./harness`）、`SHADOWCLAW_MODEL`、`SHADOWCLAW_HARNESS_VERSION`、`SHADOWCLAW_BUDGET_HARD_CAP`。
