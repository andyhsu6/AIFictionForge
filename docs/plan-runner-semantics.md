# Plan Runner: Concurrency, Timing and Interruption Semantics

Once a plan is approved, the server runs its steps back to back without asking again. That changes how the app behaves under load, so the limits below are documented as product behaviour, not as implementation detail. (Relocated from the README, which now links here.)

## Concurrency

- All model calls share one process-wide semaphore of `max_concurrent_requests = 5`. Plans issue calls in the same queue as interactive chat, so a running plan can make unrelated generations slower. Slowdown, not an error, is the visible symptom.
- One plan per user runs at a time; steps inside a plan run strictly in order.
- Per-user background work is served by a single worker. A plan that runs for tens of minutes holds that worker, so a manual generation you start meanwhile waits until the plan finishes (worst case = total plan time).
- Completed steps log their dispatch latency and how many model calls queued while they ran (backend log under `/tmp`, one line per completed step; the failed-step path records its timing in the plan's `step_results` without a per-step log, and a run that recorded any step also logs one closing summary line). `/health` reports `plans_running`, the global count of plans in `running` state (approved-but-not-started plans are not counted, and no user or project identifiers are ever exposed). Each probe runs one extra COUNT filtered by `task_type='agent_plan' AND status='running'`; `background_tasks` has no index on that column pair, so the count scans the table. That is immaterial at current row counts.

## Timing

- Between two steps the runner waits `agent_plan_step_grace_seconds` (default `3.0`). Chapter analysis relies on SQLite WAL making a written row visible to other sessions, and an automated "write, then immediately launch the next step" chain turns that rare race into the normal path. Do not tune this to 0 unless you moved off SQLite.
- Other plan budgets (max steps, wall-clock limit, poll interval) are configurable; see the `AGENT_PLAN_*` keys in [`backend/.env.example`](../backend/.env.example).

## Restart

- A server restart marks in-flight plans as failed and writes a localized interruption note on the plan row, surfaced through the `progress.agent_plan_interrupted` status code: how many steps finished, that the results are not final, and that the plan has to be started again.
- **Plans are never re-sent automatically.** Chapter analysis overwrites existing analysis results, story memories and foreshadowing; JSON import and consistency repair are not idempotent either. Replaying them unattended would gamble with your data.

## Cancellation: what actually stops

- Stopping a plan stops the plan: remaining steps are not started, and the step counter is recorded.
- **A chapter-analysis sub-task already in flight is not cascaded and keeps running to completion.** That task type exposes no cancellation channel today; the plan records it under `uncancellable_sub_tasks` instead of pretending it stopped. Sub-tasks that do support cancellation are cancelled.
- A cancelled or interrupted plan produces no closing summary, and no further LLM call is billed for it.
