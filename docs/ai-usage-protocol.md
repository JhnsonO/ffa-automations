# AI Usage Protocol — Johnson's Working Instruction

One page. How to run ChatGPT (unlimited) + Claude (limited) without burning Claude tokens.

---

## Rule 0 — route the task BEFORE opening either app

Ask one question: **does this task need the repo, tools, or execution?**

- **No** → ChatGPT. Always. No exceptions.
- **Yes** → Claude, but arrive prepared (see loop below).

| Task | Who |
|---|---|
| Write code from a spec | ChatGPT |
| Debug logic from pasted error/output | ChatGPT |
| Draft prompts, docs, plans, messages | ChatGPT |
| Explain a concept, review a pasted diff | ChatGPT |
| Visual review of a rendered artifact (video/frames) | ChatGPT |
| Read/push repo files, dispatch workflows | Claude |
| Pull run status, failed-run logs, artifacts | Claude |
| Verify ChatGPT output against live repo + frozen boundaries | Claude |
| Update `docs/ai-project-state.md` | Claude |
| Decide product trade-offs, approve gates | You |

**The discipline:** never ask Claude a question ChatGPT could answer with pasted context. Claude is hands, not head.

---

## The standard loop (one feature/fix)

1. **Claude, 1 turn:** read state + the one relevant file → produce a tight spec + paste-ready ChatGPT prompt (with the contract block from `CLAUDE.md` included).
2. **ChatGPT, unlimited turns:** write, iterate, argue, revise. ALL back-and-forth happens here.
3. **Claude, 1 turn:** verify final code against live repo + frozen files → push → dispatch if needed → update state → STOP.
4. Run completes → come back in a **fresh chat** with the run ID.

**Never iterate on code inside Claude.** Every "actually change this bit" round-trip in Claude re-carries the whole chat history. Iterate in ChatGPT, land once in Claude.

---

## Debug loop rules (run failures)

This is the one job that can't leave Claude — so make it cheap:

- Claude uses `scripts/gh.sh` for every GitHub operation. No hand-rolled curl/Python API boilerplate. `gh.sh logs <run_id>` returns the error window only — raw logs never enter context.
- **Budget: max 3 diagnose→fix→dispatch cycles per chat.** After cycle 3, update state and open a fresh chat. Cycle 4 in a heavy chat costs more than cycles 1–3 in a fresh one.
- If the fix requires real code (not a one-liner): pull the error window in Claude, hand it to ChatGPT with the file, land the result back in Claude. Same loop as above.
- After dispatch: one status check, then stop. `DISPATCHED — UNVERIFIED`. Don't poll.

---

## Session hygiene

- **One bounded ticket per Claude chat.** Ticket done → state updated → chat over.
- **Fresh chat + state file read beats message 40 of an old chat.** Always.
- **`docs/ai-project-state.md` IS the handoff.** Keep chat-end summaries to 3 lines: gate, run IDs, next action. The 10-section template is only for genuinely messy sessions where the state file can't hold it.
- Start every Claude session with the task in the FIRST message, fully specified (repo, file, gate, what "done" looks like). Vague openers cost a clarification round-trip.

## Model choice (in Claude)

- **Sonnet (default):** patches, run diagnosis, verification, pushes, dispatches — 95% of sessions.
- **Opus (ask first):** genuine architecture decisions, multi-system debugging where the cause is unknown.
- **Haiku:** rarely needed now that `gh.sh` pre-filters logs.

## Anti-patterns (the actual leaks from your history)

1. Iterating code drafts inside Claude instead of ChatGPT.
2. 5–7 debug cycles in a single chat.
3. Raw log dumps pulled into context, then filtered.
4. Re-deriving GitHub API boilerplate every session (use `gh.sh`).
5. Re-explaining project context Claude can read from the state file.
6. Per-response decorations (time, chat weight) — already removed, keep them out.

## Security

- `GH_PAT` lives in Claude preferences + env vars only. Never in repo files, prompts to ChatGPT, or logs.
- **Rotate the current PAT** — it appears in plaintext in old chat transcripts. Replace with a fine-grained PAT scoped to `ffa-automations` only (contents + actions read/write).
- Outstanding from state doc: credential embedded in `playcam/chunked_pipeline.py` still needs rotating and moving to Actions Secrets.
