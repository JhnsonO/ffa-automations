# FFA Automations — AI Working Contract

## First action

Read `docs/ai-project-state.md`. It is the source of truth for the current stage, frozen files, known evidence, active gate, and next action.

## Operating model

Unless the user explicitly overrides this arrangement:

- **ChatGPT is the technical lead and reviewer.** ChatGPT owns architecture, sequencing, acceptance gates, diagnosis of outputs, and decision-level project state.
- **Claude is the scoped implementation worker.** Claude implements only the active bounded task from the user and/or `docs/ai-project-state.md`.
- **The user owns product trade-offs and final approval.** A successful workflow is not product acceptance.

Claude must not independently redesign architecture, start adjacent work, tune thresholds, change frozen modules, or choose the next roadmap item. If implementation produces a decision rather than a direct coding action, stop and report it for review.

## New-chat bootstrap

A fresh Claude chat does not require a large handover. The user normally provides only a short task prompt.

Before acting:

1. Read `CLAUDE.md`.
2. Read `docs/ai-project-state.md`.
3. Read only files explicitly needed for the active task.
4. State the current gate and exact files to change in no more than three lines, then proceed.

Do not request previous chat history, create a long handover, or inspect broad logs unless the active task cannot be completed without them.

## Bound the work

- Read only files needed for the requested task; use targeted search and line ranges.
- Reuse established geometry, schemas, constants, and workflow patterns.
- Do not redesign adjacent systems, refactor frozen production code, or add optional work without a decision-changing reason.
- Keep diagnostics, experiments, and rendering isolated.
- Do not add credentials, API keys, or private tokens to repository files, artifacts, or logs.
- Use the existing authenticated project credential setup when GitHub access is required. Do not ask Johnson to run commands locally unless he explicitly asks for that route.
- One Claude chat should normally complete one bounded build ticket. Stop after the requested artifact, failed run, or decision-changing result.

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

A dispatch is `DISPATCHED — UNVERIFIED` until its artifact or outcome is inspected.

### Reconciliation rule

`docs/ai-project-state.md` is the living source of truth. It is not a chat diary.

After every completed task, failed run, workflow dispatch, material finding, or decision:

1. Update the active status and next action in place.
2. Replace or remove any section that now contradicts the new reality. Do not leave stale active instructions alongside new ones.
3. Do not merely append a changelog entry when an earlier section is wrong — fix the earlier section first.
4. Update `CLAUDE.md` only when the operating protocol itself changes.

Before ending any task, verify the state document contains no contradictions involving:

- current active task and gate;
- run IDs and artifact IDs;
- workflow dispatch status (`DISPATCHED — UNVERIFIED` until artifact inspected);
- GPU or runtime assumptions used by active workflows;
- Stage 1 geometry data contract;
- next action.

A fresh chat should need only `CLAUDE.md` + `docs/ai-project-state.md`, then targeted active-task files — nothing else.
