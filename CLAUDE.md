# FFA Automations — AI Working Contract

## First action

Read `docs/ai-project-state.md`. It is the source of truth for the current stage, frozen files, known evidence, active gate, and next action.

## Operating model

**Pragmatic hybrid — effective 25 June 2026:**

- **ChatGPT is the generative engine.** ChatGPT writes code files, schemas, architecture documents, and bulk output. It has no usage limits and is fast at sustained generation.
- **Claude is the grounded executor and verifier.** Claude reads the live repo, runs tests, pushes commits, dispatches workflows, reviews artifacts against real files, and catches drift against frozen boundaries. Claude cannot be replaced by ChatGPT for anything requiring repo access or execution.
- **The user owns product trade-offs and final approval.** A successful workflow is not product acceptance.

Claude must not independently redesign architecture or choose the next roadmap item without a decision-changing reason. If implementation produces a decision rather than a direct coding action, stop and report it.

ChatGPT output must be verified by Claude against the live repo and frozen boundaries before any commit. Claude is the skeptic, not a relay.

## New-chat bootstrap

Before acting:

1. Read `CLAUDE.md`.
2. Read `docs/ai-project-state.md`.
3. Read only files explicitly needed for the active task.
4. State the current gate and exact files to change in no more than three lines, then proceed.

> **This is mandatory and non-negotiable. Steps 1 and 2 must happen before any other action, including responding to the user's first message.**

Do not request previous chat history or inspect broad logs unless the active task cannot be completed without them.

## Bound the work

- Read only files needed for the requested task.
- Reuse established geometry, schemas, constants, and workflow patterns.
- Do not redesign adjacent systems, refactor frozen production code, or add optional work without a decision-changing reason.
- Keep diagnostics, experiments, and rendering isolated.
- Do not add credentials, API keys, or private tokens to repository files, artifacts, or logs.
- One Claude chat should normally complete one bounded build ticket. Stop after the requested artifact, failed run, or decision-changing result.

## Repo operations

Use `scripts/gh.sh` for all GitHub API work: file reads/pushes, workflow dispatch, run status, failed-run logs, artifacts. It requires `GH_PAT` in the environment. Do not hand-roll curl/Python API boilerplate; if gh.sh lacks an operation, extend gh.sh instead. `gh.sh logs <run_id>` returns the ANSI-stripped error window only — never pull full raw logs into context.

## Debug budget

Maximum 3 diagnose→fix→dispatch cycles per chat. After the third cycle, update the state document and hand off to a fresh chat.

## ChatGPT handoff contract

Every ChatGPT prompt Claude drafts must include verbatim:

1. The frozen-files list relevant to the task (from `docs/ai-project-state.md`).
2. For workflow files: the last known-working commit SHA and the exact working dependency/setup block as a hard constraint.
3. The exact data contracts/schemas the code touches.
4. The instruction: "Return complete file(s) only — no placeholders, elisions, or 'rest unchanged' markers."

Claude verifies all ChatGPT output against the live repo and these constraints before any commit.

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

`docs/ai-project-state.md` is the living source of truth. After every completed task, failed run, dispatch, material finding, or decision:

1. Update the active status and next action in place.
2. Replace or remove any section that now contradicts the new reality.
3. Do not merely append a changelog entry when an earlier section is wrong — fix the earlier section first.
4. Update `CLAUDE.md` only when the operating protocol itself changes.

Before ending any task, verify the state document contains no contradictions involving:

- current active task and gate;
- run IDs and artifact IDs;
- workflow dispatch status;
- GPU or runtime assumptions used by active workflows;
- Stage 1 geometry data contract.
