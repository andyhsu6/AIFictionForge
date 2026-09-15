# Model Requirements

**AIFictionForge requires a large-context model: a context window of at least 900,000 tokens.**

Almost every core feature feeds book-scale content into the prompt. Chapter generation can inject the whole book, book deconstruction parses an entire imported text, and the creative assistant carries long project history plus tool results on top of that. Below the floor the failure mode is **silent**, not slow: the assistant drops your earliest instructions once its history budget is full, and a batch-analysis summary can report on only part of the book while still sounding complete. That kind of quality regression cannot be recovered after the fact, so the window is a floor rather than a performance preference.

This page documents how the floor is enforced, what the probe measures, and the blind spots it still has. The README keeps only the summary; this is the deep version.

| Context window | Status |
|---|---|
| >= 900,000 tokens | Supported |
| Measured below the floor | Not supported. The save is rejected, and every request that would dispatch that model is refused |
| Probe could not reach a verdict | Also rejected. Unknown counts as unqualified, until you declare `context_window_tokens >= 900,000` in the settings form |

There is no middle tier and no checkbox that waves a verdict through: a model the app has **measured** below the floor stays rejected even if you declare a larger window. The declaration field is an exit for the "probe could not tell" case only.

There is **no implicit default model** either. An account with no configured model does not silently inherit one: AI features stop with `validation.ai_model_not_configured` and send you to Settings. The retired `DEFAULT_MODEL` environment variable is not read by the backend. Configure the model per account in the app, where the window is actually measured.

## What the app measures

Verdicts are cached per **(provider, base URL, model name)** triple. Whenever the model a request is about to use has no verdict on file (a first configuration, or a change to any of those three), the app probes your endpoint and waits for the result before running:

1. **Metadata**: `GET /models/<id>`, reading a context-length field if your gateway exposes one (zero tokens).
2. **Server-side bound**: a minimal prompt with `max_tokens` set to the floor, streamed, relying on the provider's own upper-bound validation. Accepting that budget is evidence of a window at or above the floor (about zero tokens; the stream is cut after the first chunk). A rejection counts as evidence of a *smaller* window only when the gateway itself states a context or prompt bound below the floor. A refusal that merely rejects an oversized request without naming such a number (including one that is only about the **output** cap) is recorded as "could not decide", not as "too small", because being rejected at exactly the floor cannot exclude a window of exactly the floor. "Could not decide" is the state the declaration field exists for.

A stored verdict is reused **indefinitely**: there is no automatic re-check, periodic or otherwise. The settings form has a manual **Re-check** that measures and caches without blocking. Rejecting is the save/dispatch gate's job, and only a change to one of the three triple parts or that manual action re-probes. A third tier (fill near 1M tokens with a needle and read it back) is the only thing that separates "the endpoint accepted the request" from "the model really read the window"; **it is deliberately not wired up in this iteration**, because it would cost about 1M input tokens per user. The form shows three numbers side by side (measured, declared, and the budget actually adopted) next to the floor itself, so you can see which one decided.

Tier 2 records the probe scale it **accepted**, not the model's real window, and the whole-book budget is that recorded number multiplied by 0.6. A model whose real window is 1,000,000 tokens therefore records 900,000 and budgets 540,000 characters instead of 600,000, a 10% smaller injection. The direction is safe: the app injects less of your book than the model could hold, never more.

A first probe can fail (an unreachable gateway, a key that is momentarily invalid), and "could not decide" is not a conclusion. So when the model a request is about to dispatch has no conclusion, or only that non-conclusion, the app probes again and waits (at most once per minute per triple, so a gateway outage cannot turn every request into an outbound probe). **If that dispatch-time re-probe still cannot decide, the result is not stored**: `context_window_gate` in `GET /settings` stays `null`, the card shows "not measured", and the AI request is refused under the "unknown counts as unqualified" rule. That is more honest than recording a measurement that never happened; it does mean the card can read "not measured" while AI is refused, which is expected. A manual Re-check, or a retry once the gateway is reachable, resolves it.

Eligibility is **not** decided by a hardcoded model list. A built-in registry only hints which probe size to start from; it can never qualify or disqualify a model. **You choose the model, and you declare what your own endpoint gives you. You are the authority on it, not our table.**

## Blind spots

> **Blind spot 1: a silent-truncating gateway can pass.** Plenty of compatibility relays do not error on an oversized request, they just cut it down. Such an endpoint answers tier 2 as if it accepted the probe scale and is recorded as qualified. With tier 3 unwired there is no defence, so a model that passed the gate can still lose the beginning of your book. If output quality is worse than the reported window suggests, suspect the gateway, not the model name.

> **Blind spot 2: a gateway that swaps or downgrades your model, and a stale verdict that is never re-checked.** Verdicts are cached per (provider, base URL, model name) triple and are **not** refreshed automatically, not on a UTC schedule, not on dispatch. Requests keep being admitted under the stale verdict until you change one of the three triple parts or run the manual Re-check; a gateway-side downgrade therefore stays invisible until you do.

> **What even a correct verdict cannot tell you**: accepting the probe scale is not the same as *using* the window well. Long-range consistency in the middle of a novel stays a per-model property, so judge a new model on your own text before committing to it.

> Supporting an API protocol is not the same as meeting the window requirement. The integrations listed in the README's Tech Stack (OpenAI/Claude/Gemini SDKs) describe transport compatibility only. Eligibility is decided by the probed or declared window of the specific model you configure.

## Related

- Configuration keys for AI endpoints: [`backend/.env.example`](../backend/.env.example)
- Plan runner runtime semantics: [`plan-runner-semantics.md`](plan-runner-semantics.md)
