# archive/

Scripts kept for reference but **not part of the live pipeline**. None of these are
referenced by any GitHub Actions workflow. They were moved here (not deleted) on
2026-06-14 to keep the repo root focused on active code.

To restore any of these to the root, just `git mv` it back (full history is intact).

| Script | Why it's here |
|--------|---------------|
| `add_kris_tab.py` | One-off — created a single specific session tab (6th Jan) and seeded Kris's timestamps. Already run. |
| `backfill_drive_sources.py` | One-off backfill — pushed GoPro source files from the last 14 days into Drive. Already run. |
| `backfill_index_links.py` | One-off backfill — rewrote the Index tab's Tab Name cells as hyperlinks. Already run. |
| `cleanup_sheet.py` | Superseded by `cleanup_and_sort.py` in the root (which does more: hides processed tabs, sorts the Index). |
| `clip_extractor.py` | Standalone CLI clip-cutter. Superseded by the `process-clips` job inside `sheet_manager.py`, which the `clip-extractor` workflow actually runs. |
| `patch_uploader_progress_and_labels.py` | One-off patch script that was already applied to `gopro_uploader.py`. Nothing left to patch. |

Active equivalents live in the repo root — see the main `README.md` file inventory.
