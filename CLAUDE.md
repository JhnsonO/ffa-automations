# FFA Automations — AI Working Contract

## First action

Read `docs/ai-project-state.md`. It is the source of truth for current stage, frozen files, known evidence, gates, and next action.

## Bound the work

- Read only files needed for the requested task; use targeted search and line ranges.
- Reuse established geometry, schemas, constants, and workflow patterns.
- Do not redesign adjacent systems, refactor frozen production code, or add optional work without a decision-changing reason.
- Keep diagnostics, experiments, and rendering isolated.
- No credentials, API keys, or private tokens in code, commits, artifacts, or responses.

## Communication

Do not narrate routine tool calls. Interrupt only for a real blocker, missing decision, unsafe assumption, or evidence that changes the agreed plan.

For a build/task result, return only:

1. **Changed** — files and one-line purpose.
2. **Verified** — exact check/run outcome.
3. **Dispatched** — workflow and artifact, if applicable.
4. **Risk** — only a genuine unresolved risk.

## State update requirement

Update `docs/ai-project-state.md` in the same commit or immediately after every meaningful change:

- acceptance/rejection of an artifact or test;
- code or workflow addition/change;
- workflow dispatch, completion, or failure;
- new artifact location/identifier;
- updated threshold, data contract, current gate, or do-not-touch rule.

A dispatch is `DISPATCHED — UNVERIFIED` until its result is inspected. Keep the state file compact; replace obsolete detail rather than accumulating chat transcripts.
