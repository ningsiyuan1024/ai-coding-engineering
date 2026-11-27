# AI Coding Workflow → AI Software Factory

## Software Factory Architecture

```mermaid
flowchart TD
    H[Human] --> P[Linear · Shared Issue Pool]
    I[Issue Scout] --> P
    D[Production Lines · Discovery] --> P
    P --> O[Symphony · Always-on Orchestrator]
    O <--> M[Model Planner · Astra Ultra]
    O --> S
    O --> T
    O --> L
    O --> A
    subgraph Lines["Four Independent Production Lines"]
      direction LR
      S["Sylan<br/>Isolated Profile<br/>Native Skills · Worktree · Session<br/>Subagents · HITL · Verification<br/>PR Lifecycle"]
      T["Matt Pocock<br/>Isolated Profile<br/>Native Skills · Worktree · Session<br/>Subagents · HITL · Verification<br/>PR Lifecycle"]
      L["Lauren Tan<br/>Isolated Profile<br/>Native Skills · Worktree · Session<br/>Subagents · HITL · Verification<br/>PR Lifecycle"]
      A["Addy Osmani<br/>Isolated Profile<br/>Native Skills · Worktree · Session<br/>Subagents · HITL · Verification<br/>PR Lifecycle"]
    end
    S -.-> X[OpenTelemetry · Traces / Metrics / Logs / Artifacts]
    T -.-> X
    L -.-> X
    A -.-> X
    X --> E[Experience Store]
    E --> V["Sylan Self-Evolution<br/>Observe → Hypothesize → Experiment → Evaluate → Promote"]
    V --> S
    classDef sylan fill:#64748b,stroke:#475569,color:#fff
    classDef matt fill:#2563eb,stroke:#1d4ed8,color:#fff
    classDef lauren fill:#9333ea,stroke:#7e22ce,color:#fff
    classDef addy fill:#15803d,stroke:#166534,color:#fff
    class S sylan
    class T matt
    class L lauren
    class A addy
```

## 四条独立生产线

我希望通过真实开源贡献，研究不同 AI Coding 工程体系如何影响交付质量、效率与成本，并逐步演进自己的软件工厂。

| 生产线 | 工程体系 | 实践案例 |
| --- | --- | --- |
| [Sylan](workflows/sylan.md) | 自有 Engineering Workflow · Symphony | — |
| [Matt Pocock](workflows/matt.md) | 原生 skills | [Harbor #4219 · Claude Code 用量统计](cases/harbor-4219.md) |
| [Lauren Tan](workflows/pstack.md) | 原生 pstack · Skills / Playbooks / Principles | — |
| [Addy Osmani](workflows/addy.md) | 原生 Agent Skills | — |

四条生产线独立运行，共享任务池、基础设施及观测评测标准。每条生产线的主 Agent 自主选择本体系的 Skills，不预设固定 Skill Chain，也不默认混用其他体系。记录实际 Skill 调用、工程产物、验证结果、人工介入、成本与 PR 结果，事后评估路由和交付质量。

协调器使用 Python 3.11、Git 和 SQLite。Sylan、Matt 和 Addy 使用隔离的 Codex profile。Matt 和 Addy 通过 Codex 原生插件机制安装作者的固定版本。Lauren 保留作者的 Cursor 插件；其子代理模型策略仍待验证。任务领取时固定上游基线、生产线和 Skills 版本。人工作答和 PR 事件会唤醒原生产线。

[架构设计](ARCHITECTURE.md) · [20 项架构决策](DECISIONS.md) · [实施路线与验收](ROADMAP.md)

## 运行工厂

从仓库根目录运行。默认私有状态目录是 `.factory-local/`。所有读取同一任务池的进程须指向同一份 SQLite 数据库。

```powershell
python -m factory status
python -m factory doctor sylan
python -m factory doctor matt
python -m factory doctor lauren
python -m factory doctor addy
python -m unittest discover -s tests -v
```

首次使用某条参考线时，需要从 GitHub 获取作者固定版本，或使用同版本的官方归档。已有 profile 可通过 `python -m factory --profiles <目录> doctor <线名>` 检查。Codex 探针重读原生宿主的模型、认证和 Skills，Cursor 探针检查固定插件的文件和声明。两种检查都将证据写入本机 OTLP 接收端。三条 Codex 生产线可通过 `python -m factory --profiles <目录> login <线名>` 在各自的隔离环境完成 OpenAI 登录。

使用相同的 `--profiles` 运行 `status` 可在一处查看每条线最后一次宿主检查的时间、登录状态、Skills 数量、在手工单和人工决定。宿主检查由 `doctor` 更新，真实工作回合另有独立的执行记录。

Codex 的只读调查槽使用原生 read-only 沙箱。本地写入槽在任务 worktree 内运行，逐次检查模型、推理强度、沙箱和网络设置。Linear 服务凭据由协调器进程持有。Lauren 的调度入口会等待一份能约束主模型及全部子代理的 Cursor 模型策略。

| 操作 | 入口 |
|---|---|
| 从 Linear 入池 | `python -m factory.linear --help`。可调用已安装 Symphony 的 Tracker，或用 `python -m factory import-pool <JSON文件>` 导入连接器回读的工单。 |
| 原生选择与执行 | `python -m factory.dispatch --help`。提供规划回执、当前 Linear 范围和本地仓库基线。调度器在领取前重新核对 GitHub，原生主 Agent 自选任务。 |
| 集中处理人工决定 | `python -m factory inbox` 显示原会话、任务和问题。`python -m factory answer <决定ID> "<答复>"` 让原线在该线程的新一轮继续。 |
| 关联和跟进 PR | `python -m factory track-pr <任务UUID> <线名> <PR地址>`，随后运行 `python -m factory.github_events`。 |
| 调整资源或复议 | `python -m factory.planner --help`。每版决定带原生模型回执、修订号和摘要。 |
| 后台观察 | `python -m factory.dispatch --watch --observe-only`。心跳写在 `.factory-local/dispatcher-status.json`。Windows 可用 `scripts/start-observer.ps1`。 |

**启动 Symphony 工厂实例**。在 Windows 上，从仓库根目录运行 `./scripts/start-factory.ps1 -SymphonyRoot <已编译的Elixir目录> -Profiles <生产线目录>`。可通过 `-Check` 核对部署配置，已有只读 Linear 凭据时可通过 `-PromptForLinearKey` 在本机输入。已有 `OSS_LINEAR_API_KEY` 或 `LINEAR_API_KEY` 的服务进程直接使用该变量。工厂实例在独立 BEAM 内加载 worker 扩展。原生 Orchestrator 监督任务进程，等待人工或上游时归还执行槽。Python 入池程序只负责核对候选和持久化各线的选择。[启动入口](workflows/symphony/worker_boot.exs) · [原生 worker](workflows/symphony/worker_adapter.ex)

初始规划已有桌面 Astra Ultra 会话回执。无人工值守的重新规划使用显式的 `--transport responses`，只发送匿名候选计数、能力选项和预算。服务端返回实际模型、推理强度、请求 ID 与响应 ID 后才可生成新修订。这个 API 入口的账号能力仍待实测。

真实贡献验收会将每项新任务的原生会话、实际 Skills 使用、diff 和测试关联到上游交付。历史宿主探针单独归档。当前生产线的宿主证据和待验收项见实施进展。[实施进展](ROADMAP.md)

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
