# AI Usage Protocol — Johnson's Working Instruction

One page. Default rule: **use the AI you are already in and let it execute end-to-end when it has the tools.** Handoffs are for missing capability, not habit.

---

## Rule 0 — capability first, not brand first

Ask one question: **can the current AI actually do the required repo/tool action?**

- **Yes** → let it do the whole bounded task: read, implement, test, debug, push, dispatch, inspect and report.
- **No** → hand off only the missing part to an agent/tool that has that capability.

| Task | Default owner |
|---|---|
| Write or change code | Current connected AI |
| Debug logic from errors/output | Current connected AI |
| Read/push repo files | Current connected AI if GitHub/repo access exists |
| Dispatch workflows / inspect runs / artifacts | Current connected AI if Actions access exists |
| Review rendered video/frames | ChatGPT is fine; human product judgement still wins |
| Architecture / product trade-offs | Current AI can advise; you approve |
| Final product acceptance | You |

**Do not send work to Claude/Codex just because it involves code or GitHub.** If ChatGPT has the connected GitHub tools, ChatGPT should execute it. If Claude has the tools, Claude should execute it. Codex is optional.

---

## The standard loop

For one feature/fix:

1. Tell the current AI the outcome you want — e.g. **"fix this and test it"**.
2. The agent reads `CLAUDE.md` + `docs/ai-project-state.md` and only the files needed for the ticket.
3. It creates/uses a feature or test branch, implements the change, runs cheap checks first, then the expensive workflow only when needed.
4. For OEV follow-cam experiments, use the permanent **`OEV — Agent Harness`** instead of creating a new dispatcher workflow each time.
5. The agent inspects the resulting logs/artifacts and reports the actual result. A green CI run is not visual/product acceptance.
6. Only hand off if a required capability is genuinely unavailable in the current environment.

This replaces the old fixed "ChatGPT writes / Claude pushes" loop.

---

## OEV fast path

The repo now has a permanent experiment harness:

- `oev/harness_profiles.json` — canonical test settings and known-good reference.
- `oev/harness_request.json` — tiny control request.
- `oev/harness-control` — permanent trigger branch.
- `.github/workflows/oev-agent-harness.yml` — dispatch/wait/retry/analyse wrapper.
- `scripts/oev_check.sh` — cheap checks.
- `scripts/oev_analyze_events.py` — standard telemetry comparison.

Normal OEV loop:

**edit experiment branch → cheap checks → update one request file → harness dispatches RunPod → automatic reference comparison → inspect video at the known failure windows.**

The harness automatically retries genuine RunPod allocation/preflight failures. It does **not** retry code/build/render failures, so bad code is not hidden behind expensive redispatches.

---

## Debug loop rules

- Diagnose from the smallest relevant error/log window; do not pull giant raw logs unless necessary.
- Run cheap/unit/static checks before GPU work.
- Maximum **3 expensive diagnose→fix→GPU-dispatch cycles** in one chat unless you explicitly want to continue.
- Do not treat transient RunPod capacity as a code defect; the OEV harness retries that case automatically.
- Do not declare success from CI alone when the task is visual follow-cam quality.

---

## Session hygiene

- One bounded ticket at a time.
- Fresh chat + state file read is better than reconstructing project state from memory.
- `docs/ai-project-state.md` remains the durable project handoff/source of truth.
- Start with the actual task, not a vague opener, when you know what you want done.
- Prefer **"implement it yourself and test it"** over asking one AI to draft a prompt for another.

## Handoffs

Only create a paste-ready prompt when:

1. the current AI genuinely lacks the required repo/tool capability, or
2. you explicitly want a second agent involved.

A handoff should include the exact repo/ref, frozen files, known-good SHA/setup, data contracts, success criteria, and the instruction to push to a feature branch rather than main.

## Security

- Credentials/tokens live in approved secrets/env/connected-tool storage only. Never put them in repo files, prompts, artifacts or logs.
- Prefer fine-grained tokens with only the permissions actually required.
- Existing credential-rotation/security debt documented in `docs/ai-project-state.md` remains separate from this operating model.
