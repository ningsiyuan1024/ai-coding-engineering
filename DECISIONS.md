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
