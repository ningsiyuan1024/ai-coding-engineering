# AI Coding 工程实践

收录国内团队将 Coding Agent 接入真实研发工作的案例。沿着一条需求看工程实现：从自动接取 Issue、整理规格和项目上下文，到跨仓并行开发、联调验证、人工验收与提交 PR。

| 类别 | 代表文章 | 主要讲什么 |
|---|---|---|
| **全栈开发实践** | [CodeBuddy 三位一体 AI 全栈开发落地实践](https://mp.weixin.qq.com/s/v92Bl-W9hp65eb5TBfNlrA) | 用 CodeBuddy 做 AI 全栈开发的落地案例。 |
| **跨仓开发与集成** | **[得物 · Harness + SDD + 多仓全栈开发](https://tech.dewu.com/article?id=221)**、[AliExpress · Hybrid Coding](https://qcon.infoq.cn/2026/shanghai/track/1966) | 一个需求横跨多个仓库或客户端时，怎样先对齐接口契约，再分配任务、并行实现和联调。 |
| **任务接入与交付编排** | **[腾讯轻量云 · LoopForge](https://developer.cloud.tencent.com/article/2779233)**、[腾讯游戏 · 把 Loop 接进团队](https://qcon.infoq.cn/2026/shanghai/track/1964)、[飞猪 · AI Native 交付大脑](https://qcon.infoq.cn/2026/shanghai/presentation/7263) | LoopForge 从需求进入、派发 SOP 讲到验收和 MR。腾讯游戏讲 Spec 如何变成可并行、可重跑的任务图。飞猪讲产品、研发和测试如何围绕同一份 Spec 交接。 |
| **项目上下文与研发资产** | [大淘宝 · AI 驱动研发体系的实践和思考](https://ai.aui2.cn/guide/frontier/posts/2026-09-02-ai-driven-rd-harness) | 多个业务仓库、团队知识、每轮迭代的 PRD 和验证结果怎样组织，让下一个人或 Agent 接手时能找到依据。 |
| **验证、审查与上线** | [美团 · 31 万行代码 AI 重构](https://tech.meituan.com/2026/05/07/Agent-AI-Coding.html)、[蚂蚁 · 60 万行巨石应用可验证交付](https://qcon.infoq.cn/2026/shanghai/presentation/7297)、[飞猪 QA · 当验证追上生成](https://qcon.infoq.cn/2026/shanghai/presentation/7301) | 美团讲 Pre-PR 和测试风险分级，蚂蚁讲新旧系统的行为比对和流量回放，飞猪 QA 讲接口与 UI 验收。 |
| **研发流程改造与效能度量** | [阿里 Qoder · 企业 AI Native 研发流程升级与重塑](https://www.infoq.cn/article/4z7Bbxy92tjAI7MB5evM)、[去哪儿 · AI 在研发组织落地的三段式路径](https://qcon.infoq.cn/2026/shanghai/presentation/7410)、[中电金信 · 从个人提效到组织提效](https://www.cnblogs.com/zhongdianjinxin/p/23451672) | 从个人使用 Agent 到团队改变交付流程，讲角色分工、推广阶段，以及用什么指标衡量交付是否真的变快。 |
