# Johnson OS GitHub bridge

This bridge lets a ChatGPT scheduled task request a fresh Johnson OS check-in through the native GitHub connector.

## Flow

1. Replace `health-checkin/request.json` on `feature/johnson-os-github-bridge` with:
   ```json
   {
     "operation": "export_checkin",
     "trigger": "<new UTC timestamp or UUID>"
   }
   ```
2. Wait for the **Johnson OS check-in bridge** workflow for that commit to finish.
3. Download its `johnson-os-checkin-<run-id>` artifact.
4. Extract `health-checkin/response.enc.json`.
5. Decrypt it with the task's `CHECKIN_ARTIFACT_KEY`:
   - derive a 32-byte key with SHA-256 over the UTF-8 key text;
   - decode `iv_b64`, `tag_b64`, and `ciphertext_b64`;
   - decrypt with AES-256-GCM.
6. Build the morning brief from the decrypted JSON. Never invent missing readings.
7. After the brief is complete, replace `health-checkin/ack.json` with:
   ```json
   {
     "operation": "delete_artifact",
     "artifact_id": <downloaded artifact id>,
     "run_id": <workflow run id>,
     "trigger": "<new UTC timestamp or UUID>"
   }
   ```
8. Confirm the **Johnson OS check-in cleanup** workflow succeeds.

Artifacts are encrypted, retained for at most one day, and normally deleted immediately after the summary is produced. Plain health JSON is never committed.

## Repository secrets

- `CHECKIN_EXPORT_KEY`: read-only credential shared with the production Vercel export route.
- `CHECKIN_ARTIFACT_KEY`: passphrase used to encrypt and decrypt the short-lived artifact.

The Vercel production project also needs `CHECKIN_EXPORT_KEY`. The values must not be committed or printed in workflow logs.
