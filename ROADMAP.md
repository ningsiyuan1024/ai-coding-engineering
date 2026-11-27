# Implementation roadmap

Strategy: vertical slice first. All items pending runtime verification.

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

No fixed timeline or cost budget is assumed. Start with audit; do not claim runtime deployment from documentation alone.
