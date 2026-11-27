# AI Coding Workflow → AI Software Factory

## 四条独立生产线

我希望通过真实开源贡献，研究不同 AI Coding 工程体系如何影响交付质量、效率与成本，并逐步演进自己的软件工厂。

| 生产线 | 工程体系 | 实践案例 |
| --- | --- | --- |
| [Sylan](workflows/sylan.md) | 自有 Engineering Workflow · Symphony | — |
| [Matt Pocock](workflows/matt.md) | 原生 skills | [Harbor #4219 · Claude Code 用量统计](cases/harbor-4219.md) |
| [Lauren Tan](workflows/pstack.md) | 原生 pstack · Skills / Playbooks / Principles | — |
| [Addy Osmani](workflows/addy.md) | 原生 Agent Skills | — |

四条生产线独立运行，共享任务池、基础设施及观测评测标准。每条生产线的主 Agent 自主选择本体系的 Skills，不预设固定 Skill Chain，也不默认混用其他体系。记录实际 Skill 调用、工程产物、验证结果、人工介入、成本与 PR 结果，事后评估路由和交付质量。

20 项架构决策已确定，运行时尚未接入。各生产线采用作者原生验证机制，统一观测而非统一质量门禁。

[架构设计](ARCHITECTURE.md) · [20 项架构决策](DECISIONS.md) · [实施路线与验收](ROADMAP.md)

实施策略：Vertical Slice First。先审计现有 Runtime，再让四条生产线分别完成真实开源任务，从首个闭环接入 OpenTelemetry。

## 文章精选

| 主题 | 文章 |
| --- | --- |
| 工作流设计 | [Lauren Tan · pstack](https://github.com/cursor/plugins/tree/005d8f6e8ec057b738adcd08a08b30db0478402d/pstack) |
| 工作流设计 | [Matt Pocock · ask-matt](https://github.com/mattpocock/skills/blob/main/skills/engineering/ask-matt/SKILL.md) |
| 软件工厂 | [LobeHub 一线复盘：实践 Super-Agent 半年后，我们对于 Agent 工程的思考](https://hub.baai.ac.cn/view/59827) |
| 全栈开发实践 | [CodeBuddy 三位一体 AI 全栈开发落地实践](https://mp.weixin.qq.com/s/v92Bl-W9hp65eb5TBfNlrA) |
| 成本与效能 | [Uber · Running a Software Factory Efficiently at Uber Scale](https://www.uber.com/us/en/blog/efficient-software-factory/) |
| 验证、审查与上线 | [美团 · 31 万行代码 AI 重构](https://tech.meituan.com/2026/05/07/Agent-AI-Coding.html) |

<details>
<summary>其余 11 篇文章</summary>

| 主题 | 文章 |
| --- | --- |
| 跨仓开发与集成 | [得物 · Harness + SDD + 多仓全栈开发](https://tech.dewu.com/article?id=221) |
| 跨仓开发与集成 | [AliExpress · Hybrid Coding](https://qcon.infoq.cn/2026/shanghai/track/1966) |
| 任务接入与交付编排 | [腾讯轻量云 · LoopForge](https://developer.cloud.tencent.com/article/2779233) |
| 任务接入与交付编排 | [腾讯游戏 · 把 Loop 接进团队](https://qcon.infoq.cn/2026/shanghai/track/1964) |
| 任务接入与交付编排 | [飞猪 · AI Native 交付大脑](https://qcon.infoq.cn/2026/shanghai/presentation/7263) |
| 项目上下文与研发资产 | [大淘宝 · AI 驱动研发体系的实践和思考](https://ai.aui2.cn/guide/frontier/posts/2026-09-02-ai-driven-rd-harness) |
| 验证、审查与上线 | [蚂蚁 · 60 万行巨石应用可验证交付](https://qcon.infoq.cn/2026/shanghai/presentation/7297) |
| 验证、审查与上线 | [飞猪 QA · 当验证追上生成](https://qcon.infoq.cn/2026/shanghai/presentation/7301) |
| 研发流程改造与效能度量 | [阿里 Qoder · 企业 AI Native 研发流程升级与重塑](https://www.infoq.cn/article/4z7Bbxy92tjAI7MB5evM) |
| 研发流程改造与效能度量 | [去哪儿 · AI 在研发组织落地的三段式路径](https://qcon.infoq.cn/2026/shanghai/presentation/7410) |
| 研发流程改造与效能度量 | [中电金信 · 从个人提效到组织提效](https://www.cnblogs.com/zhongdianjinxin/p/23451672) |

</details>
