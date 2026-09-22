# Johnson OS GitHub bridge

This bridge prepares a fresh encrypted Johnson OS check-in **before** the 07:00 Europe/London ChatGPT task, so the scheduled task only needs GitHub read access.

## Daily flow

1. GitHub Actions schedules the bridge around **06:50 Europe/London**.
   - GitHub cron is UTC, so the workflow has both 05:50 and 06:50 UTC entries.
   - A Europe/London gate makes only the correct GMT/BST run export data.
2. The workflow calls the production Vercel `/api/export` route and validates the response.
3. It encrypts the JSON with AES-256-GCM and uploads a short-lived artifact named `johnson-os-checkin-<run-id>`.
4. At 07:00, the ChatGPT scheduled task finds the newest fresh `johnson-os-checkin-*` artifact, downloads it, decrypts it, and builds the morning brief.
5. The artifact is retained for at most one day. Plain health JSON is never committed.

The scheduled ChatGPT task should **not** update `health-checkin/request.json` or `health-checkin/ack.json`. Those write-based triggers remain available for manual/interactive testing only.

## Manual test flow

To request a fresh export interactively, replace `health-checkin/request.json` on `feature/johnson-os-github-bridge` with:

```json
{
  "operation": "export_checkin",
  "trigger": "<new UTC timestamp or UUID>"
}
```

Then wait for the **Johnson OS Check-in Bridge** workflow and download its encrypted artifact.

## Decryption

Use `CHECKIN_ARTIFACT_KEY`:

- derive a 32-byte key with SHA-256 over the UTF-8 key text;
- decode `iv_b64`, `tag_b64`, and `ciphertext_b64`;
- decrypt with AES-256-GCM.

Never invent missing readings. Check `generated_at` and reject stale snapshots.

## Repository secrets

- `CHECKIN_EXPORT_KEY`: read-only credential shared with the production Vercel export route.
- `CHECKIN_ARTIFACT_KEY`: passphrase used to encrypt/decrypt the short-lived artifact.

The Vercel production project also needs `CHECKIN_EXPORT_KEY`. Secret values must not be committed or printed in workflow logs.
