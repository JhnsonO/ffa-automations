# AI Usage Protocol — Quick Start

Use the AI account you are already in. It owns a bounded task end to end when it has the needed tools and access. There is no fixed ChatGPT / Claude / Codex role split; do not create handoff prompts by habit. Hand off only for a real capability gap or when Johnson asks.

## Start and finish

1. For substantial or cross-account work, read the private Drive **AI Project Hub — Home**, **AI Shared Rules and Handover**, and the relevant project handover.
2. For this repo, read `CLAUDE.md`, then the current-status summary and only relevant sections of `docs/ai-project-state.md`. The state file is large; do not load its full history by default.
3. Inspect current evidence, make the smallest useful change, run cheap checks first, then verify the resulting artifact or behaviour.
4. Update the canonical board/roadmap and project handover with verified changes, decisions, blockers and the next action. Keep technical detail in the repo; keep the Drive hub as navigation and concise cross-account context.

## OEV work

Use the diagnose-before-fix and falsifiable-test workflow in `CLAUDE.md`. Identify the first failing stage, state what evidence would support or falsify the hypothesis, change one causal variable, test the known failure first, then run the regression set. Do not treat CI or subsystem metrics as product acceptance; Johnson decides whether the camera output is good enough.

For experiments, use the permanent OEV harness and feature/test branches. Do not change frozen production paths or merge unverified work.

## Boundaries

- Keep employer details in approved work systems; never copy confidential work data to Drive or public GitHub.
- Keep credentials out of files, prompts, artifacts and logs.
- Do not turn one-off requests into projects. Create a board, roadmap and handover for clearly new substantial projects; ask first if the work may belong to an existing project.
