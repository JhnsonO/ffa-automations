# FFA Automations — AI Working Contract

## First action

Read `docs/ai-project-state.md`. It is the source of truth for the current stage, frozen files, known evidence, active gate, and next action. A pinned "Current Status" block at the top of that file summarizes every active/open track — check it first. Closed, self-contained sagas (e.g. Clip Extractor, Flatcam) are moved to `docs/ai-project-state-archive.md`; only read it if the active task genuinely concerns one of those closed areas.

## Operating model

**Capability-first direct execution — effective 22 August 2026.** This supersedes the old fixed ChatGPT/Codex/Claude role split.

- **The current connected agent owns the bounded task end-to-end when it has the required tools.** If the user says "implement it", "go", "fix it", or equivalent, do the repo work yourself instead of drafting a prompt for another AI.
- **ChatGPT with connected GitHub access may read/write repo files, create feature branches and PRs, merge verified changes, dispatch the OEV harness/workflows, inspect run status/logs/artifacts, and update docs/state.** Do not hand work to Codex or Claude merely because code or GitHub is involved.
- **Claude with equivalent repo/tool access may do the same.** Claude is not a mandatory gate for work already implemented and verified by another connected agent.
- **Codex is optional, not mandatory.** Use it only when the user explicitly wants it or when the current agent genuinely lacks the execution capability needed for the task.
- **Handoff is a capability fallback, not a workflow default.** Only hand work to another tool/agent when the current environment genuinely cannot perform a required action. State the missing capability plainly.
- **The user owns product trade-offs and final product acceptance.** A green workflow proves execution, not that a rendered video is good enough.

For repo changes, use a feature/test branch and verify before merging. Do not edit production Reco or other frozen production paths merely to run an experiment unless the user explicitly authorizes that promotion.

## New-chat bootstrap

Before acting on a repo task:

1. Read `CLAUDE.md`.
2. Read `docs/ai-project-state.md`.
3. Read only files explicitly needed for the active task.
4. State the current gate and exact files to change in no more than three lines, then proceed.

Do not request previous chat history or inspect broad logs unless the active task cannot be completed without them.

## Bound the work

- Read only files needed for the requested task.
- Reuse established geometry, schemas, constants, and workflow patterns.
- Do not redesign adjacent systems, refactor frozen production code, or add optional work without a decision-changing reason.
- Keep diagnostics, experiments, and rendering isolated.
- Do not add credentials, API keys, or private tokens to repository files, artifacts, or logs.
- One chat/session should normally complete one bounded build ticket.

## Repo operations

Choose the repo interface that exists in the current environment:

- **ChatGPT connected mode:** use the connected GitHub tools directly for reads, writes, branches, PRs, workflow/run inspection and artifacts. Do not create handoff prompts simply because a terminal is unavailable.
- **Terminal/Claude/local-agent mode:** use `scripts/gh.sh` for GitHub API work where practical. It requires `GH_PAT` in the environment. Do not hand-roll repeated curl/Python GitHub API boilerplate when `gh.sh` already covers the operation.

For OEV experiment execution, prefer the permanent `OEV — Agent Harness` and `oev/harness-control` request-file trigger. Do not create a new one-shot dispatcher workflow for every test. The harness wraps the proven `oev-runpod-sample-baseline.yml`, retries genuine RunPod allocation/preflight failures, downloads candidate/reference artifacts, and generates standard telemetry.

### Cloning discipline (terminal agents)

Do not `git clone` the full repo by default. For single-file reads or single-file pushes, use the lightest available repo operation. Only clone when a task genuinely requires editing across multiple files or needs a local working tree (e.g. running tests). This restriction does not apply to connected repo tools that already expose file-level reads/writes without cloning.

### Vast.ai workflows

Any workflow with a Vast.ai element (instance creation and/or termination) must reuse the proven lifecycle block verbatim from `playcam-poc.yml`'s `Launch reliable Vast.ai GPU instance` step and its paired `Terminate Vast.ai instance` step — do not hand-write a new launch/terminate sequence, even a "simpler" one for a lightweight script. This means:

- A `delete_instance()` helper that tries all 3 endpoints (`console.vast.ai/api/v0`, `cloud.vast.ai/api/v0`, `cloud.vast.ai/api/v1`) before giving up, used for every cleanup call site, not just the final step.
- Try up to 5 cheapest matching offers in one dispatch, cleaning up each one that doesn't reach `running` before trying the next — don't fail/require a manual redispatch on the first bad offer.
- Write `instance_id` to `$GITHUB_OUTPUT` only once an instance is confirmed selected (`running` + reachable IP) — at that point exactly one live instance exists and it's always tracked, so the final termination step never has to guess.
- Final termination step prints `::error::...` (not a plain warning) if all endpoints fail, so a leak surfaces as a visible run failure instead of a silent log line.

Adapt the offer *query* (GPU vs CPU-only, resource thresholds) to the script's actual needs — that part is legitimately script-specific. The launch-retry/cleanup/termination *mechanics* are not; copy them. This was a real gap (not just theoretical) as of 5 July 2026 — see `docs/ai-project-state.md` change log for the incident.

## Debug budget

Maximum 3 expensive diagnose→fix→GPU-dispatch cycles per chat unless the user explicitly asks to continue. Cheap local/unit/static checks do not count. After the third expensive cycle, update the state document and report the remaining blocker instead of looping blindly.

## Handoff contract (only when genuinely needed)

If the current agent lacks a required capability and another agent/tool must take over, the handoff must include:

1. The frozen-files list relevant to the task (from `docs/ai-project-state.md`).
2. For workflow files: the last known-working commit SHA and the exact working dependency/setup block as a hard constraint.
3. The exact data contracts/schemas the code touches.
4. The instruction: "Complete file(s) only — no placeholders, elisions, or 'rest unchanged' markers."
5. The instruction: "Push to a feature branch, never main."

Do **not** create a handoff prompt when the current connected agent can simply execute the task itself.

## Communication

Do not narrate routine tool calls. Interrupt only for a real blocker, missing decision, unsafe assumption, or evidence that changes the agreed plan.

For a build/task result, return only what matters:

1. **Changed** — files and one-line purpose.
2. **Verified** — exact check/run outcome.
3. **Dispatched** — workflow and artifact, if applicable.
4. **Risk** — only a genuine unresolved risk.

## State update requirement

Update `docs/ai-project-state.md` after meaningful project-state changes when the current environment can safely edit it:

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
