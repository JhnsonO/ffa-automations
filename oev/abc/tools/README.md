# OEV-006 annotation tools

Published as private claude.ai artifacts (owner: Johnson):

- **OEV Match Annotator** (`annotator_template.html`): load a window's left and
  right clips (local files, never uploaded), click where the camera should aim,
  mark goals, shots, counterattacks, long passes, near-side balls, restarts and
  neighbour-pitch balls, draw our pitch / neighbour pitch outlines. Saves each
  window to the artifact database (collection `annotations`, doc = window id) and
  to the browser as a backup; Show/Save/Copy JSON for export.
- **Aylestone Pitch Outline** (`outline_review_template.html`): draft outline on
  both camera stills, drag to correct, approve / save corrections / reject
  (collection `outlines`, doc `aylestone`).

Templates contain placeholders (`__EX_LEFT__`, `__RV_LEFT__`, `__DRAFT__`, ...)
filled with base64 stills / the draft JSON at publish time.

GoPro 4K clips are HEVC; browsers without HEVC support show a one-line ffmpeg
command for a 1280-wide H.264 proxy. Coordinates are fractions of the frame, so
proxies annotate identically.

Annotator JSON (`oev-abc-annot-camera/1`) -> scorer annotation:

    python3 scripts/oev_abc_annot_convert.py --annotation win.json \
        --events <window events.jsonl> --out win_scorer.json
    python3 scripts/oev_abc_score.py --trace <arm trace.csv> --annotation win_scorer.json \
        --arms oev/abc/arms.json --arm A0 --out a0.json
