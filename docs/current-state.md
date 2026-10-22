# FFA Automations — Current state

Project status reviewed: 21 October 2026. Hub link corrected: 22 October 2026. Use this compact entry point first. [ai-project-state.md](ai-project-state.md) retains the detailed dated engineering log and superseded approaches; read only the task-relevant sections.

## Scope and source of truth
This repo contains FFA uploader/relay automation plus OEV experiment infrastructure. Keep their states separate. The public OEV prototype page is an overview, not authoritative product acceptance. The all-project hub is https://jhnsono.github.io/project-hub/; it is a public directory that links to canonical project boards, roadmaps and handovers. Private records still require GitHub or Google sign-in.

## FFA board
| Stage | Item | Evidence / condition |
| --- | --- | --- |
| Implemented and unit-tested | Camera-neutral YouTube title/description | Main commit a85d003, 15 October; 9 metadata tests recorded passing |
| Reviewed output; partial acceptance | Two 14 October uploads reviewed on 15 October | Titles, intro, booking/shop links, hashtags and media ID confirmed; Monday League paragraph/link missing, exact producing version unverified |
| Open | GoPro refresh succeeds without resuming original upload | [#53](https://github.com/JhnsonO/ffa-automations/issues/53) |
| Needs revalidation | Relay scheduling gaps and runner separation | Older 24 August entries are proposals/diagnoses, not current proof of runtime failure |
| Parked | Credential review or rotation | Johnson explicitly deferred key work on 21 October |

## OEV milestone evidence
The repository's M1 acceptance gate [#28](https://github.com/JhnsonO/ffa-automations/issues/28) remains open, last updated 6 August, with zero comments at this review. BOM [#24](https://github.com/JhnsonO/ffa-automations/issues/24) and housing [#35](https://github.com/JhnsonO/ffa-automations/issues/35) also remain open. This is evidence of incomplete tracking, not proof that physical work did or did not happen. Require an accepted clip/checklist and a current hardware update before committing new delivery dates.

## Additional observation, 21 October
The live FFA homepage's YouTube player showed “Football Session in Leicester | Monday 19 Oct 2026 | FFA Leicester”, matching the intended camera-neutral title style. The video's description and the exact uploader version that produced it were not verified; do not equate a matching public title with full uploader acceptance.

## Roadmap
1. **Now — reconcile evidence:** observe the next uploader result, revalidate active workflow/runner issues from current logs before changing infrastructure.
2. **Next — bounded fixes:** address confirmed GoPro resume or relay failures with the smallest testable change. Retain existing authentication and working upload behavior.
3. **Later — operational simplification:** review unused workflows and dedicated media-runner scope using current evidence and authorization. Optional issue queues are for explicit capability handoff, never automatic model routing.

## AI working rules
Read CLAUDE.md and the private Drive shared rules before substantial work. The current AI owns the task if it has the tools. No fixed ChatGPT/Claude/Codex roles. Keep frozen production paths, experimental branches and approval boundaries intact. Keep credentials out of new notes, screenshots and logs. Update canonical issues and the relevant current summary after each substantive task.

## Exact next action
Check the missing Monday League description section and producing-version provenance, then inspect current evidence for issue #53 before choosing a repair. For OEV product work, continue from the current private OEV roadmap through the shared hub; old milestone dates are not current commitments.

## Conversation/status reconciliation — 22 October 2026
The previous top-entry-only summary missed the 15 October conversation review of two real 14 October outputs (standard `47RhOWe7qEc`, POV `LlrFomv76-s`). Revised titles/intro, direct booking and shop links, hashtags and media ID were verified. The separate Monday League paragraph/link was absent. Keep that gap and exact uploader provenance open; do not describe all description verification as unstarted.

Read-only ChatGPT automation records now confirm both 22 October scheduled evening tests completed with successful connector pulls and workout-data availability: run `1b0f7508-b468-832d-b0e4-8051a2e0f0c4` completed 18:00:58Z; `3f508748-ed74-832c-acca-6df24787f24b` completed 18:11:44Z. Delivery status remains unknown. First daily 06:50 Europe/London run and delivery acceptance remain open. GitHub manual artifacts are a separate path. No readings or secrets are recorded here.

Detailed 30-task audit: https://github.com/JhnsonO/project-hub/blob/main/docs/open-task-audit-2026-10-22.md. Hub records completed portions separately and preserves remaining acceptance gates. No credential, production or runner changes were made by this documentation audit.
