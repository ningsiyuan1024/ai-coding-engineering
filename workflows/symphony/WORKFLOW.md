---
# Used by intake.exs through Symphony's native Workflow, Config and Tracker.
# From the factory checkout (with elixir on PATH), run once per reconciliation:
# python -m factory.linear --symphony-root <installed Symphony elixir directory> --factory-state <absolute database path>
# The two clients share OSS_LINEAR_API_KEY (or LINEAR_API_KEY). Nothing is installed.
tracker:
  kind: linear
  provider:
    project_slug: "315870bf1953"
    api_key: $OSS_LINEAR_API_KEY
  active_states: ["Backlog"]
  terminal_states: ["Done", "Canceled", "Duplicate"]
workspace:
  root: ".factory-local/symphony-intake"
# This is an intake configuration. A mistaken invocation of the ordinary worker
# launcher must stop before creating a model session.
hooks:
  before_run: "exit 78"
codex:
  command: "exit 78"
---

Use the factory intake entry point in the configuration above. Production-line
sessions are launched by the factory coordinator after a task is claimed.
