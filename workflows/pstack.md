# Lauren Tan · pstack

Lauren 用 `poteto-mode` 接收工程任务。它根据请求选一套 playbook，再在执行中调用相应的 skill。首次配置从 `setup-pstack` 开始。[作者使用说明](https://github.com/cursor/plugins/tree/005d8f6e8ec057b738adcd08a08b30db0478402d/pstack)

| 我交给 Agent 的任务 | 作者的 playbook | 我希望拿回什么 |
| --- | --- | --- |
| 看看这张 issue 是否还有贡献空间 | investigation | 已有实现、证据和剩余问题 |
| 找出这个 bug 的原因并修复 | bug-fix | 可复现的症状、根因和修复前后结果 |
| 为一个适配器增加行为 | feature | 约定范围内的 diff，以及通过实际入口得到的验证结果 |

**我在哪里介入？** 可用实验回答的问题，先由 Agent 比较结果。选择会改变贡献范围时，我看过取舍再决定。比如只承诺修改一个适配器，却发现必须改公共接口，这时值得停下来讨论。[Lauren 的异步监督原则](https://github.com/cursor/plugins/blob/005d8f6e8ec057b738adcd08a08b30db0478402d/pstack/skills/principle-never-block-on-the-human/SKILL.md)

这一页整理作者流程。独立实跑案例待补。
