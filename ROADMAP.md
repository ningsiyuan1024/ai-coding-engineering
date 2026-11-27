# Implementation roadmap

Strategy: vertical slice first. The table below defines the acceptance criteria. The dated checkpoint records what has been exercised.

| Phase | Deliverable | Acceptance evidence |
|---|---|---|
| 0 | Audit current Symphony and coding runtime | Capability matrix with source/config evidence: profile isolation, native installation, model selection, session resume, event hooks, OTel; identify gaps |
| 1 | Four minimal end-to-end production lines | Four isolated profiles and task worktrees; native skills installed at recorded revisions; one real contribution task per line; actual skill/tool/model events and test artifacts exported via OTel |
| 2 | Autonomous operation | Shared Linear pool, exclusive claim, scheduler, HITL pause/resume, PR feedback routed to original line; demonstrate deduplication and recovery |
| 3 | Full observability | Correlated traces, metrics, logs, artifacts and PR outcomes; cost and quality analysis with missing data explicitly marked |
| 4 | Sylan evolution | One evidence-backed hypothesis, baseline/candidate experiment, evaluation and versioned promotion or rejection; rollback demonstrated |

## First implementation tickets

1. Audit runtime and Symphony; record supported vs unsupported capabilities and reproducible probes.
2. Define profile configuration and isolation smoke tests.
3. Install native Matt, Lauren and Addy skills; record revisions; preserve Sylan.
4. Wire minimal OTel collector and event correlation before pilot runs.
5. Run one distinct real issue per line, honoring upstream rules and human decisions; capture evidence of native skill use.
6. Review pilot gaps before expanding infrastructure.

## MVP exit criteria

- Four profiles with no unintended cross-line skill loading; separate task worktrees.
- Three upstream skill sets installed; native entrypoints verified; observed skill execution, not merely installation.
- One real task completed per line with verifiable artifacts and upstream-compliant delivery; merge is tracked, not guaranteed.
- Demonstrate human-decision resume and PR-feedback continuation.
- Capture skill, tool, model and subagent events where exposed, test results, token usage and PR state; mark unavailable fields unknown.

## Implementation checkpoint, 2026-11-27

**Paused at Ning's request.** The Linear pool contains four candidates. The factory has recorded zero pilot claims, zero engineering model turns and zero contribution PRs. Symphony's replaceable worker passed the local supervision and cancellation checks. The most recent complete Python suite passed 144 tests. The final checkpoint includes subsequent integration and handoff edits.

| Production line | Verified before pause | Resume condition |
|---|---|---|
| Sylan | Isolated Codex profile discovered six bundled skills and reopened a native thread across processes. | Authenticate the isolated profile and refresh the live issue pool. |
| Matt | Original Codex marketplace loaded 27 skills. All 190 installed files matched the pinned upstream files. | Authenticate the isolated profile, then observe an actual engineering turn. |
| Addy | A fresh Codex profile went from zero to 26 enabled plugin skills. All 330 installed files matched upstream. | Authenticate the isolated profile, then observe an actual engineering turn. |
| Lauren | Original Cursor plugin verified, with 554 files, 59 declared skills and two agent definitions. | Keep the line parked until Cursor's model policy can be verified for both the lead agent and delegates. |

The first three lines have priority on resumption. The default desktop Codex login and the three engineering profiles have separate authentication state. The service still needs its own Linear read credential. The approved resource plan currently permits one read-only investigation. Its ID and digest are recorded in the [checkpoint receipt](evidence/2026-11-27/factory-checkpoint.json). Resume from the existing state database and recheck upstream ownership before any claim.

| Dependency | Verified on this host | Remaining acceptance |
|---|---|---|
| Native host → isolation → pinned skills | Codex thread reopened across app-server processes. Matt and Addy have author-supported Codex marketplace packages. Lauren has an author-supported Cursor plugin. Earlier Claude SDK probes are archived as historical observations. | Authenticated engineering turns. Verify an actual skill call, subagent activity where selected, and worktree evidence on each pilot. Verify the Cursor policy for all delegated models. |
| Live pool → selection → claim → worktree | Four Linear Backlog records read through the connector and imported by UUID. Exclusive ownership and recovery exercised. Each candidate has a pinned public Git baseline. | Continuous Linear credentials for the local service and four autonomous native claims scoped to live upstream availability. |
| Planner → admission → original session | A desktop planner turn ran with GPT-6.2 Astra at ultra. The isolated app-server advertises the same model and effort. Worktree runner validates ownership before a new or resumed turn. | Authenticate the headless planner and exercise a new resource decision from a live production event. |
| Upstream feedback → original owner | The same real GitHub merge was read twice into an isolated audit ledger. One merge event persisted. A native question and original-thread continuation are covered by adapter and integration tests. | An actual human decision or review on a factory-produced contribution. |
| Execution → OTLP → Sylan experiment | The four host probes produced trace, metric and log receipts. A replay across 13 historical PRs preserved three true alerts and added four false positives with the proposed rule. The candidate was rejected and rollback restored all 13 baseline decisions. | Repeat the experiment using new production-line contributions and measured costs where available. |

The local integration calls the installed Symphony Workflow, Config and Tracker for intake. ADR 014 now selects a replaceable worker for the factory instance. The original Orchestrator remains responsible for supervision and task slots. The observation process can maintain plugin versions and poll tracked PRs while service credentials are being provisioned.

The worker-injection experiment used the actual Orchestrator, its Task.Supervisor and the in-memory tracker. Three monitored handoffs released one execution slot for human and upstream waits, then resumed the original owner. The production bootstrap also exercised the real Python worker for a parked task, cooperative cancellation and forced Windows process-tree termination. A separate workspace protects contribution worktrees from Symphony's terminal cleanup. Continuous live Linear intake and authenticated engineering turns are the remaining deployment checks. [Canonical worker patch](workflows/symphony/worker-injection.patch) · [Production smoke receipt](evidence/2026-11-27/symphony-production-smoke.json) · [Reproducible experiment](experiments/symphony-worker/)

[Host receipts](evidence/2026-11-27/host-discovery.json) · [Isolation controls](evidence/2026-11-27/profile-isolation.json) · [PR event read-back](evidence/2026-11-27/github-event-replay.json) · [Sylan replay and rollback](evidence/2026-11-27/sylan-replay.json)
