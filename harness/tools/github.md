---
tool: github
接入方式: MCP（远程端点 https://api.githubcopilot.com/mcp/，经 ADK McpToolset 挂载）
凭证引用: 环境变量 GITHUB_TOKEN（只写引用，永不写密钥值；由 li 的环境/保险库注入）
---

# 工具卡 · GitHub

工具能不能被某个场景用、用到什么风险级，由场景与护栏决定，不由本卡决定（驾驭层设计 Step 2）。本卡只声明能力与每个操作的风险级。

## 能力清单（操作 × 风险级）

| 操作 | 风险级 |
|---|---|
| get_repository / list_issues / get_issue / list_pull_requests / get_pull_request / list_workflow_runs / get_workflow_run / get_file_contents / search_code | read |
| create_issue / update_issue / add_issue_label / add_issue_comment / create_pull_request / create_branch | write |
| merge_pull_request / push_files / delete_branch | irreversible |
| delete_repository | forbidden |

## 使用说明

- **只读集默认启用**：场景挂载工具时，只读操作随场景直接可用；写操作须护栏策略放行（步骤过审批挂钩）才挂载、才执行；不可逆操作除审批外还须单独确认；forbidden 永不挂载。
- 何时用：查仓库状态、issue/PR 列表与内容、CI 运行结果与日志——排障场景的一手证据来源之一。
- 凭证缺失（GITHUB_TOKEN 未设置）时接入工厂返回空并明确提示，**不许假装已连上**；离线与测试走 FakeMcpGitHub（同注册路径）。

## 已知的坑

- GitHub App / 集成令牌**无建仓与 fork 权限**（403 Resource not accessible by integration，2026-10-14 实测）：建仓须 li 在 GitHub 网页端手动完成。
- 推送大文件走 API 有单参数体积上限，超限时改走网页上传或 git 通道，不要硬塞。
