---
tracker:
  kind: linear
  provider:
    project_slug: "315870bf1953"
    api_key: $OSS_LINEAR_API_KEY
  active_states: ["Backlog", "Todo", "In Progress", "Agent Review", "Human Review", "Rework"]
  terminal_states: ["Closed", "Canceled", "Cancelled", "Duplicate", "Done"]
polling:
  interval_ms: 30000
workspace:
  root: $FACTORY_SYMPHONY_WORKSPACE
agent:
  max_concurrent_agents: 1
hooks:
  before_run: "exit 78"
codex:
  command: "exit 78"
---

Launch this workflow through the factory worker bootstrap. The native orchestrator
selects and supervises execution for issues admitted by the factory coordinator.
