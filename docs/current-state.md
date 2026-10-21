# FFA Automations — Current state

Reviewed: 21 October 2026. Use this compact entry point first. [ai-project-state.md](ai-project-state.md) retains the detailed dated engineering log and superseded approaches; read only the task-relevant sections.

## Scope and source of truth
This repo contains FFA uploader/relay automation plus OEV experiment infrastructure. Keep their states separate. The public OEV prototype page is an overview, not authoritative product acceptance. The all-project hub is https://johnson-project-hub.airyibis1024.chatgpt.site and requires Johnson's sign-in.

## FFA board
| Stage | Item | Evidence / condition |
| --- | --- | --- |
| Implemented and unit-tested | Camera-neutral YouTube title/description | Main commit a85d003, 15 October; 9 metadata tests recorded passing |
| Needs live confirmation | Observe a new upload with revised metadata | No post-change upload validation is recorded in the current top log entry |
| Open | GoPro refresh succeeds without resuming original upload | [#53](https://github.com/JhnsonO/ffa-automations/issues/53) |
| Needs revalidation | Relay scheduling gaps and runner separation | Older 24 August entries are proposals/diagnoses, not current proof of runtime failure |
| Parked | Credential review or rotation | Johnson explicitly deferred key work on 21 October |

## OEV milestone evidence
The repository's M1 acceptance gate [#28](https://github.com/JhnsonO/ffa-automations/issues/28) remains open, last updated 6 August, with zero comments at this review. BOM [#24](https://github.com/JhnsonO/ffa-automations/issues/24) and housing [#35](https://github.com/JhnsonO/ffa-automations/issues/35) also remain open. This is evidence of incomplete tracking, not proof that physical work did or did not happen. Require an accepted clip/checklist and a current hardware update before committing new delivery dates.

## Roadmap
1. **Now — reconcile evidence:** observe the next uploader result, revalidate active workflow/runner issues from current logs before changing infrastructure.
2. **Next — bounded fixes:** address confirmed GoPro resume or relay failures with the smallest testable change. Retain existing authentication and working upload behavior.
3. **Later — operational simplification:** review unused workflows and dedicated media-runner scope using current evidence and authorization. Optional issue queues are for explicit capability handoff, never automatic model routing.

## AI working rules
Read CLAUDE.md and the private Drive shared rules before substantial work. The current AI owns the task if it has the tools. No fixed ChatGPT/Claude/Codex roles. Keep frozen production paths, experimental branches and approval boundaries intact. Keep credentials out of new notes, screenshots and logs. Update canonical issues and the relevant current summary after each substantive task.

## Exact next action
Confirm a new uploader result's title/description and inspect current evidence for issue #53 before choosing a repair. For OEV product work, continue from the current private OEV roadmap through the shared hub; old milestone dates are not current commitments.
