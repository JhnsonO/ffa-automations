# FFA Automations — AI Working Contract

## First action

Read `docs/ai-project-state.md`. It is the source of truth for the current stage, frozen files, known evidence, active gate, and next action.

## Operating model

**Three-AI split — effective 8 July 2026 (supersedes 25 June hybrid):**

- **Codex is the generative engine.** Codex writes all code and pushes it to a feature branch — never `main`. Claude drafts every Codex prompt with hard constraints (frozen files, last-good workflow SHA, data contracts).
- **ChatGPT critiques direction and screens trivia.** ChatGPT reviews Codex output for idea/direction problems and trivial defects (typos, missing variables, syntax) before it reaches Claude. Mid-iteration fixes never come to Claude.
- **Claude is the verifier and gate.** Claude enters only at "claimed done": fetch the branch DIFF (not full files), verify against the spec and frozen boundaries, then merge to `main` and dispatch. A bad diff gets a 1-line defect note back to Codex — Claude does not fix it. Claude never iterates code.
- **The user owns product trade-offs and final approval.** A successful workflow is not product acceptance.

Claude must not independently redesign architecture or choose the next roadmap item without a decision-changing reason. If implementation produces a decision rather than a direct coding action, stop and report it.

All Codex output must pass Claude's diff verification against the live repo and frozen boundaries before merging. Claude is the skeptic, not a relay.

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

### Cloning discipline

Do not `git clone` the full repo by default. For single-file reads or single-file pushes, use `gh.sh get`/`gh.sh push` instead — it transfers one file, not the whole tree (code, databases, git history). Only clone when a task genuinely requires editing across multiple files or needs a local working tree (e.g. running tests). Ask the user for confirmation before cloning, stating why a single-file operation isn't sufficient.

### Vast.ai workflows

Any workflow with a Vast.ai element (instance creation and/or termination) must reuse the proven lifecycle block verbatim from `playcam-poc.yml`'s `Launch reliable Vast.ai GPU instance` step and its paired `Terminate Vast.ai instance` step — do not hand-write a new launch/terminate sequence, even a "simpler" one for a lightweight script. This means:

- A `delete_instance()` helper that tries all 3 endpoints (`console.vast.ai/api/v0`, `cloud.vast.ai/api/v0`, `cloud.vast.ai/api/v1`) before giving up, used for every cleanup call site, not just the final step.
- Try up to 5 cheapest matching offers in one dispatch, cleaning up each one that doesn't reach `running` before trying the next — don't fail/require a manual redispatch on the first bad offer.
- Write `instance_id` to `$GITHUB_OUTPUT` only once an instance is confirmed selected (`running` + reachable IP) — at that point exactly one live instance exists and it's always tracked, so the final termination step never has to guess.
- Final termination step prints `::error::...` (not a plain warning) if all endpoints fail, so a leak surfaces as a visible run failure instead of a silent log line.

Adapt the offer *query* (GPU vs CPU-only, resource thresholds) to the script's actual needs — that part is legitimately script-specific. The launch-retry/cleanup/termination *mechanics* are not; copy them. This was a real gap (not just theoretical) as of 5 July 2026 — see `docs/ai-project-state.md` change log for the incident.

## Debug budget

Maximum 3 diagnose→fix→dispatch cycles per chat. After the third cycle, update the state document and hand off to a fresh chat.

## Codex prompt contract

Every Codex prompt Claude drafts must include verbatim:

1. The frozen-files list relevant to the task (from `docs/ai-project-state.md`).
2. For workflow files: the last known-working commit SHA and the exact working dependency/setup block as a hard constraint.
3. The exact data contracts/schemas the code touches.
4. The instruction: "Complete file(s) only — no placeholders, elisions, or 'rest unchanged' markers."

5. The instruction: "Push to a feature branch, never main."

Claude verifies the Codex branch diff against these constraints before merging to `main`.

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
