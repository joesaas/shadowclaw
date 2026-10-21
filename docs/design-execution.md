# ShadowClaw 执行层详细设计

> 依据：docs/design.md 的执行层决策（混合编排、隔离副本+事实板）与 docs/tech-selection.md（ADK 底座）。粒度：模块职责、关键数据结构、与 ADK 的边界，照此可直接写代码。

## Step 1 · 模块全景

```
execution/
  units/          agent 单元：规格、工厂、上下文组装
  orchestration/  编排封装：规划者、计划、执行器、五原语
  state/          快照、事实板、任务状态
  trace/          轨迹：结构、记录器、存储、导出
  control/        预算、重试、审批、限额
  models/         模型工厂、价格表
  runtime/        Runner 托管、检查点与恢复
```

设计主线一句话：**ADK 只当「单元的身体」**——单元的运行、工具调用、模型接入归 ADK；编排、状态、轨迹、控制全部是我们自己的层，架在 ADK 之上。这样换框架时只有 units/factory 一处要改。

## Step 2 · agent 单元（units/）

**单元规格 UnitSpec（声明式，由驾驭层注入）**：

- `id`、`role`（干什么的）
- `model`：模型引用（经模型工厂解析到 OpenRouter 具体模型）
- `instruction`：提示词正文
- `tools[]`：工具引用（MCP 工具集或函数工具）
- `memory_view`：记忆视图规格（注入哪些记忆文件/条目，见驾驭层设计）
- `output_contract`：产出结构（自由文本 / 指定 JSON 结构）
- `budget_profile`：单次调用的 token 与花费上限档位

**工厂 factory**：UnitSpec → ADK LlmAgent。工具按引用装配（MCPToolset 或 FunctionTool）；回调统一挂载（轨迹、审批、预算，见 Step 6）。

**上下文组装 context_assembler**：每次调用单元前现拼上下文 = 单元 instruction + 记忆视图内容 + 任务输入 + 事实板快照（Step 4）。不依赖 ADK session 历史做跨调用记忆——隔离语义由组装保证。

**关键取舍**：委派不用 ADK 原生的 agent transfer（它共享 session、上下文互相污染），而是「独立 session 调用」（Step 3 的 Delegate）。代价是多一层调用封装，换来已拍板的隔离语义。

## Step 3 · 编排封装（orchestration/）

**计划 Plan（数据，不是代码）**：

- Plan = 步骤树；Step = {`id`、`primitive`、`unit(s)`、输入映射、验收标准（可选）、预算档位}
- Plan 全文 JSON 可序列化，进轨迹，评估层可事后审计「当时为什么这么拼」

**规划者 Planner**：一个特殊单元，输入是任务描述 + 可用单元清单 + 原语说明，输出是一份 Plan。

**执行器 Orchestrator**：先校验 Plan（结构合法、委派深度 ≤ 2、总预算 ≤ 任务硬顶、引用的单元都存在），校验不过让规划者重出一次，再不过降级为「单单元串行」保底。校验通过后逐步执行，步与步之间过控制面（Step 6）。

**五原语的执行语义**：

1. **串行 Sequence**：按步执行，前步产出按输入映射喂后步。
2. **路由 Route**：路由单元（或规则前置）做分类，产出分支选择 + 理由，理由进轨迹；选中分支继续执行。
3. **并行 Parallel**：多个单元以隔离 session 同时跑，受并发上限约束；部分失败不整单作废——收成功结果，失败清单一并回；汇总方式由 Plan 指定（指定汇总单元，或结构化拼接）。
4. **反思 Reflect**：执行单元产出后过验收：有验收标准时用独立 critic 单元挑错（不让它自己给自己放水），无标准时自评；不合格带意见重做，默认最多 2 轮；轮尽仍不合格，交最好一版并打标。
5. **委派 Delegate**：子调用 = 任务简报 + 父级相关上下文切片（快照）；子级在自己的 session 里跑自己的 Plan；只回结果包 {状态、摘要、产物索引、拟写入事实板的事实、子轨迹引用}。

## Step 4 · 状态（state/）

**任务状态 TaskState**：{任务 id、目标、Plan、每步状态、预算账本、事实板引用、产物索引}，每步结束落检查点（Step 7）。

**事实板 FactBoard**（任务级共享，单写者）：

- 事实 = {`id`、陈述、来源单元、证据引用、状态（proposed / confirmed）、产生于哪一步}
- 单元只能「提议」事实（通过工具 propose_fact）；**只有 Orchestrator 能落板**——汇总、去重、冲突检查都在落板这一关做，兄弟单元永不直写
- 快照随任务状态存 SQLite；组装上下文时按任务相关性截取后注入

## Step 5 · 轨迹（trace/）

**轨迹 Trace**：{`trace_id`、任务 id、场景、起止时间、Plan 全文、事件流、用量汇总、结局}。

**事件 Event**：{序号、时间、步 id、单元 id、类型、载荷、token 入/出、花费、耗时}。事件类型固定枚举：

- 计划类：plan_created、plan_validated、plan_rejected
- 执行类：step_started、step_finished、model_call、tool_call
- 编排类：route_decided、reflect_round、delegate_spawned、delegate_returned
- 状态类：fact_proposed、fact_committed
- 控制类：approval_requested、approval_decided、budget_warning、budget_stopped、error、retry

**记录方式**：ADK 的模型/工具回调是事件来源，Recorder 把 ADK 事件映射为上述结构（映射规则随代码走）；编排类事件由 Orchestrator 直接发。

**存储**：SQLite 两表（traces、events）；每任务可导出一份完整 JSON 文件给评估层。花费：优先用 OpenRouter 返回的用量；缺失时按 token × 价格表估算并打标 estimated。

## Step 6 · 控制面（control/）

- **预算**：任务硬顶 + 单元档位双层账本；每次模型调用前预检、调用后记账；触顶 → 优雅中止，交部分结果并说明。
- **超时与重试**：步级与任务级两层超时；只对模型调用与幂等工具重试，最多 2 次，退避；全部进轨迹。
- **并发**：全局上限默认 4 个并行单元，Plan 可调低不可调高。
- **审批**：挂 ADK 的工具前回调；策略来自驾驭层护栏（工具风险级 × 场景 → 自动 / 先问 / 拒绝）；**默认 fail-closed**：无策略命中时按「先问」处理（muse-agent-adk 评审的教训）。本地阶段「先问」走 adk web 的 UI 中断，上 EC2 后走 Telegram。

## Step 7 · 中断与恢复（runtime/）

- 检查点在**计划步级**：每步结束存 {Plan、步状态、事实板、预算账本} 到 SQLite。
- 恢复 = 读检查点、跳过已完成步、从断步重跑。步内不做细粒度恢复（重跑该步），这是有意的简化。
- ADK session 只当单次调用的工作内存，用完即弃；恢复语义不依赖它（它的事件只追加，做不了我们的检查点）。

## Step 8 · 与 ADK 的边界（适配清单）

| 用 ADK 原生 | 我们自建 |
|---|---|
| LlmAgent 单元运行、工具调用循环 | 编排封装（Plan、Planner、Orchestrator、五原语） |
| FunctionTool / MCPToolset 工具装配 | 状态语义（快照、事实板、任务状态） |
| LiteLlm 接 OpenRouter 多模型 | 轨迹结构与存储（回调只作事件源） |
| 工具前回调（挂审批）、模型回调（挂预算与轨迹） | 预算账本、检查点与恢复 |
| session 作单次调用工作内存 | 场景、记忆视图、护栏策略（驾驭层注入） |

v1 不用 ADK 的 workflow agents（Sequential/Parallel/Loop）：原语统一由 Orchestrator 实现，避免两套编排语义并存；将来固定子流程可局部引入。

## Step 9 · 待确认（li）

1. 委派走独立 session（隔离优先，多一层封装）——按已定语义如此设计，请确认接受这个实现代价。
2. 反思默认：有验收标准用独立 critic、最多 2 轮——可否。
3. Plan 校验失败的保底降级为单单元串行——可否。

## 附：施工纪律（2026-10-21 li 定，全部实现阶段适用）

代码落地遵守六大设计原则，总目标高内聚低耦合：单一职责（编排/轨迹/事实板/检查点各管一摊）、开闭（新原语与新单元靠新增实现+注册接入，不改编排器主体）、里氏替换（Mock/ADK/远程单元同一契约可互换）、接口隔离（单元/存储/审批/模型各用最小 Protocol）、依赖倒置（编排器只依赖抽象，实现注入，组装只在入口一处）、迪米特（单元只与编排器和事实板提议口交互，不触兄弟单元内部）。

防过度设计：只给已知变化方向留口（五原语阶段 1 只实现串行/委派，接口按五原语形状预留；存储只写 SQLite 一种实现+驱动接口）；不建无场景的插件系统、配置框架、事件总线。验收判断：下一个真实需求（阶段 2 接 MCP 工具、阶段 5 补原语）能低成本接入即可，不提前实现。
