# Addy Osmani · Agent Skills

[原始 Agent Skills 仓库](https://github.com/addyosmani/agent-skills)

独立生产线，使用作者原生技能覆盖需求、规划、实现、验证、审查与交付。主 Agent 按任务选择技能，例如 `spec-driven-development`、`planning-and-task-breakdown`、`test-driven-development`、`code-review-and-quality`；这些是候选技能而非固定执行顺序。

## 执行约定

主 Agent 根据任务特征自主选择本生产线的 Skills，不预设固定 Skill Chain，不默认混用其他生产线的 Skills。运行时读取所选 Skill 的原始 SKILL.md，按其要求执行；记录实际加载的 Skill、选择理由、阶段产物、验证命令与结果。事后评估路由是否合理，不以调用数量作为质量目标。

任务分配、模型预算及统一验收细则仍待决策。

独立实跑案例待补。
