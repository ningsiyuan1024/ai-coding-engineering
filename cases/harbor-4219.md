# 用 Matt Pocock 的审查流程检查 Harbor 的一次改动

Harbor 用同一组任务评估不同 Agent 的表现。这次改动涉及它如何记录 Claude Code 的 token 用量。[项目介绍](https://github.com/harbor-framework/harbor)

Claude Code 可以明确报告缓存写入了 0 个 token，也可能省略这个字段。前者是已知值，后者应当保留为未知。[上游 issue #4219](https://github.com/harbor-framework/harbor/issues/4219)

## 审查发现了什么

在 Windows 工作区，我们审查了 Claude Code 改动 `f548fc1`。需求审查发现，实现从会话日志取值，而终端结果也可能记录缓存写入量。规范审查提出一项维护性意见：两处代码重复判断每个步骤归属哪个模型。

## 哪个决定改变了实现

当时可以只验证会话日志，也可以增加一个终端对照用例。我选择同时验证两种来源，并同意在复现后补上回退逻辑。验收入口是 `ClaudeCode.populate_context_post_run`：给适配器输入日志，再读它写进 `AgentContext` 的值。

## 同一组用例跑了三次

Linux 回放从原改动的父提交 `b0cd8c2` 开始。八个用例覆盖显式零值、缺失值、两种来源同时报告，以及终端结果属于另一个会话等情况。

| 回放版本 | 通过 | 观察 |
| --- | ---: | --- |
| 父提交 | 2/8 | Claude Code 适配器没有填报新字段。 |
| 加入会话日志处理 | 5/8 | 仅终端报告 0、仅终端报告 20、会话缺数但终端报告 20，三项仍失败。 |
| 加入同一次运行的终端回退 | 8/8 | 缺数时可以补全；两种来源都报告时，保留完整的会话值。 |

复审又补了来源冲突、会话文件截断和续跑历史等用例。最终运行 Claude Code 相关的 16 个单元测试文件，266 项通过。测试使用合成日志。上述数字来自 Linux 独立回放，Windows 原提交接受的是静态审查。

[回放基线](https://github.com/ningsiyuan1024/harbor/commit/b0cd8c2462cdfebb89dd6c06c67f8507f2f27512) · [相关 Pi 适配器 PR](https://github.com/harbor-framework/harbor/pull/4392)
