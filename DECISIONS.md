# Architecture decisions

Accepted 2026-11-27. These are design decisions, not evidence of implementation.

| ADR | Decision |
|---|---|
| 001 | Four independent lines: Sylan, Matt Pocock, Lauren Tan, Addy Osmani |
| 002 | Main agent autonomously selects skills |
| 003 | Autonomous task claiming |
| 004 | Native skill discovery and loading |
| 005 | Astra Ultra as intended resource decision maker; availability unverified |
| 006 | Native subagent collaboration |
| 007 | Dynamic HITL based on complexity and risk |
| 008 | Native verification and delivery; upstream rules apply |
| 009 | Full execution observability |
| 010 | Three native reference lines; Sylan evolves |
| 011 | Profile + per-task worktree and session isolation |
| 012 | Periodic upstream updates; pin running tasks to their starting revision |
| 013 | Shared Linear pool with human, scout and autonomous discovery |
| 014 | Always-on Symphony orchestrator, on-demand sessions |
| 015 | Initial resource plan and event-driven replanning |
| 016 | Central decision discovery, native session interaction |
| 017 | OpenTelemetry first |
| 018 | Original line owns PR lifecycle; shared event scheduling |
| 019 | Hypothesis-driven Sylan evolution |
| 020 | Vertical slice first |

No common mandatory engineering chain or extra quality gate. Shared infrastructure must not override native behavior. Missing telemetry is unknown, not evidence of compliance.

### ADR 014 implementation choice, 2026-11-27

Maintain a narrow downstream worker extension for the factory's Symphony instance. Symphony retains Linear reconciliation, supervision, cancellation and slot ownership. A production line selects its issue through admission. The worker starts or resumes the native session bound to the SQLite claim. An idle line can wait for a person or an upstream PR without holding an execution slot. The existing Symphony installation supplies the baseline. Its regular Codex runner remains the default in other instances. [Isolated Orchestrator experiment](evidence/2026-11-27/symphony-worker-smoke.json)

### ADR 011 native hosts and model selection, 2026-11-27

Ning excluded Claude from factory execution, superseding the earlier shared-login choice. Sylan, Matt and Addy use separate Codex profiles and have priority for the pilots. Matt and Addy use the upstream authors' Codex marketplace packages. Lauren keeps the canonical Cursor pstack plugin and stays out of automatic claims pending verification of a provider policy covering delegated models as well as the lead agent. Existing account settings belong to the operator.
