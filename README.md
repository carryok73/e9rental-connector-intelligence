# Connector Intelligence — E9Rental.ai Hackathon MVP

A local-first Python application for connecting authorized Gmail and Google Drive sandbox accounts, storing records in SQLite, and retrieving cited evidence for business questions. Gmail and Drive are the recommended connectors for this setup. WhatsApp is optional and remains disabled until organizer-approved group IDs and their webhook payload mapping are confirmed. Organizer-specific export schemas must still be supplied before schema compliance can be claimed.

## Architecture and design

- **Streamlit** (`app.py`) displays counts, source records, categories, and cited query results.
- **FastAPI** (`src/connector_intelligence/api.py`) exposes health, query, and WhatsApp Cloud API webhook endpoints.
- **Connectors** normalize source data and keep Google OAuth scopes read-only. WhatsApp uses the standard Cloud API message envelope and requires a configured group-ID path and allowlist before ingestion.
- **SQLite** stores a common `records` table with source ID, parent/thread/group ID, timestamp, title, content, category, and JSON metadata. A trigger-maintained FTS5 index supports text retrieval; unique `(source, source_id)` keys make repeat delivery idempotent.
- **Query engine** uses parameterized SQL for deterministic counts and FTS for evidence lookup. It returns source IDs as citations and an explicit insufficient-evidence response when it finds nothing. It does not call an LLM.
- **Exports** serialize database rows into `outputs/{whatsapp,gmail,drive}_output.json`; organizer schemas can later be implemented behind this export module.

## Repository structure

```text
app.py
src/connector_intelligence/
  api.py config.py db.py exports.py query.py
  connectors/whatsapp.py gmail.py drive.py drive_store.py
outputs/                    generated JSON exports
tests/                       pytest coverage
requirements.txt .env.example AI_USAGE.md
```

## Run locally on Windows

1. Install Python 3.11 or newer, then from PowerShell run `py -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and adjust local paths. The app runs with mock/local data and does not require Google credentials.
4. Run the UI with `streamlit run app.py`, or API with `uvicorn src.connector_intelligence.api:app --reload`.
5. Run tests with `python -m pytest`.

Google OAuth is opt-in. Copy the organizer-approved OAuth desktop client JSON to the configured local path. For Gmail, set `GMAIL_MAILBOX_OWNER` to the sandbox mailbox in `.env` and click **Sync Gmail (read-only)** in the Gmail tab. For Drive, set `GOOGLE_DRIVE_ACCOUNT` to the authorized sandbox account and click **Sync Drive (read-only)** in the Drive tab. Each connector checks the authorized profile against its configured address before listing messages/files. Gmail requests only `gmail.readonly`; Drive requests only `drive.readonly`. Drive uses a separate ignored `drive_token.json`, so it will not replace the Gmail token. No source write operations are implemented. Keep OAuth client secrets and tokens local; `.gitignore` excludes the default credential and token names.

### Connect the Gmail and Drive sandbox accounts

1. **Confirm the sandbox accounts.** Ask the organizer for the exact Gmail mailbox address, Drive account address, and test data location. The addresses may be the same or different. Confirm that the Gmail mailbox contains challenge test messages and the Drive account can read the challenge test files.
2. **Select a Google Cloud project.** Use the organizer's project if supplied; otherwise create or select a project where you have permission to enable APIs and create OAuth clients.
3. **Enable both APIs.** In Google Cloud Console, enable the Gmail API and Google Drive API for that project. Google's official [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) and [Drive Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python) describe the project, API, and desktop OAuth setup.
4. **Configure the OAuth audience.** Set up Google Auth Platform branding and audience. If all test accounts belong to one Google Workspace organization, use its internal audience; otherwise use External/Testing and add each exact sandbox account as a test user. Do not publish the app for this local challenge setup. Google currently limits Testing projects to 100 test users and says their authorizations expire after seven days, so an account may need to reauthorize during a longer hackathon run. See [Manage App Audience](https://support.google.com/cloud/answer/15549945?hl=en).
5. **Create a Desktop app OAuth client.** Download its JSON file. Save it in the project root as `credentials.json`, or set `GOOGLE_CLIENT_SECRETS_FILE` to its local path. Do not paste its contents into chat, source code, or Git. This path is ignored by `.gitignore`.
6. **Set the local configuration.** Copy `.env.example` to `.env` and set the following values to the organizer-provided addresses and local client path:

   ```dotenv
   GOOGLE_CLIENT_SECRETS_FILE=credentials.json
   GOOGLE_TOKEN_FILE=token.json
   GOOGLE_DRIVE_TOKEN_FILE=drive_token.json
   GMAIL_MAILBOX_OWNER=authorized-mailbox@example.test
   GOOGLE_DRIVE_ACCOUNT=authorized-drive@example.test
   REFERENCE_DATE=
   ```

   Keep the Gmail and Drive token paths separate. Gmail requests only `gmail.readonly`; Drive requests only `drive.readonly`. These scopes allow reading mailbox messages and Drive files, respectively; the connector code has no send, edit, move, or delete operations. If the organizer gives you one account for both services, use that address for both account settings.
7. **Start the app from the project directory.** In PowerShell, activate `.venv` and run `python -m streamlit run app.py`. The connection setup panel should report whether the client file and account addresses are configured. Restart Streamlit after changing `.env`.
8. **Authorize and sync Gmail.** Open the Gmail tab and click **Sync Gmail (read-only)**. In the Google sign-in page, choose the exact authorized sandbox mailbox, inspect the requested read-only permission, and consent. The app checks the returned mailbox address before fetching messages. Gmail results are stored locally in SQLite and `outputs/gmail_output.json`.
9. **Authorize and sync Drive.** Open the Drive tab and click **Sync Drive (read-only)**. Choose the exact authorized Drive account and consent to the read-only permission. The app checks the returned account before listing files. Supported extracted text and metadata are stored locally and exported to `outputs/drive_output.json`.
10. **Review results.** Confirm the Gmail and Drive record counts in the dashboard, inspect the connector tabs and JSON exports, then use **Ask a question** to retrieve cross-source evidence. Results are limited to data successfully synchronized into the local database; they do not establish that the complete sandbox has been imported.

If Google reports a testing-user, API-disabled, scope, or account mismatch error, check the selected Cloud project, test-user list, enabled APIs, and exact configured account address. Do not broaden OAuth scopes to get past an error. Google states that authorizations for External apps in Testing expire after seven days; this can require reauthorization during a hackathon.

Gmail sync paginates the mailbox's non-spam/trash message listing, fetches each listed thread once, normalizes message headers/body/attachment metadata, applies deterministic categories and attachment flags, stores idempotently by Gmail message ID, and refreshes `outputs/gmail_output.json`. The export uses the current generic row format from `exports.export_source`; the organizer's exact Gmail schema was not available, so this is not claimed to be schema-compliant. The mailbox owner and reference date can be configured with `GMAIL_MAILBOX_OWNER` and `REFERENCE_DATE`. For unattended/testable operation, `sync_gmail` accepts a service object, owner, reference date, output folder, and five-day duration explicitly.

The query layer supports deterministic unanswered-email count and list questions over stored Gmail rows. It requires valid stored `is_inbound`, `is_unanswered`, `sender`, `unanswered_reference_date`, and `unanswered_days` metadata on every stored Gmail record. The connector's cutoff is inclusive by UTC calendar date: a message on `reference_date - days` qualifies. `REFERENCE_DATE` supplies the date when configured; otherwise `date.today()` uses the host's local date, while message timestamps are normalized to UTC. Results are explicitly limited to stored records because mailbox synchronization completeness is not tracked in the database. Existing rows without the cutoff-duration metadata are reported as incomplete until resynchronized. `is_inbound` distinguishes the configured mailbox owner from other senders; the current data does not distinguish customers from other external senders, so results are inbound emails rather than confirmed customer emails.

Drive sync paginates the authorized user's non-trashed file list, checks the configured account, and records file ID, MIME type, modified time, web link, and related metadata. Binary PDF/DOCX/XLSX/CSV/TXT/Markdown files are downloaded in memory; Google Docs export to text and Google Sheets export to XLSX. Unsupported types such as Slides and inaccessible/empty files are recorded with explicit status and extraction error instead of being reported as successfully extracted. Successfully extracted files receive one deterministic category and a short rationale. Sync upserts by Drive file ID and refreshes `outputs/drive_output.json` through the existing generic exporter. The organizer's exact output schema is unavailable, so the generic format is not claimed to be schema-compliant. Documents' extracted text is stored in SQLite; raw downloads are not written to disk.

### Test Drive with the authorized sandbox account

1. Obtain the organizer-approved OAuth desktop client file and confirm the Google Cloud project has the Drive API enabled. Keep the client file local.
2. Copy `.env.example` to `.env`. Set `GOOGLE_CLIENT_SECRETS_FILE` to the client file path and set `GOOGLE_DRIVE_ACCOUNT` to the exact sandbox account email. Leave `GOOGLE_DRIVE_TOKEN_FILE=drive_token.json` so Drive authorization stays separate from Gmail.
3. Activate the project environment and install dependencies if needed: `py -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, then `python -m pip install -r requirements.txt`.
4. Start the dashboard with `python -m streamlit run app.py`, open the Drive tab, and click **Sync Drive (read-only)**. Complete Google OAuth using the authorized sandbox account and approve the requested `drive.readonly` permission.
5. The app checks the returned Drive account email before listing files. After sync, inspect `outputs/drive_output.json` and the Drive rows in SQLite. The first live run requires the organizer's actual OAuth setup and sandbox account; mocked tests do not verify live access.

## WhatsApp webhook setup

Set `WHATSAPP_VERIFY_TOKEN` to the organizer-approved verification token, `WHATSAPP_APP_SECRET` to the Meta App Secret used for signature validation, `WHATSAPP_SELECTED_GROUPS` to a comma-separated allowlist of approved group IDs, and `WHATSAPP_GROUP_ID_PATH` to the dot-separated path to the group ID inside each message object. The group path is intentionally blank by default: ingestion fails closed until the organizer supplies a representative authorized payload and confirms the exact group identifier. Keep these values in an ignored local `.env` or deployment secret store. The webhook accepts GET subscription verification and POST events signed with `X-Hub-Signature-256`; POST bodies are checked against the raw bytes before JSON parsing. It does not send messages. Only selected group messages are persisted or interpreted; status events and unsupported envelope elements are ignored. JSON output uses the existing generic record schema and is not claimed to meet an unavailable organizer schema.

The local interpretation fields (`summary`, `requests_and_commitments`, `action_items`, `decisions`) use sentence and keyword heuristics, are stored as candidates in message metadata, and require human review. No external LLM is called.

The query layer supports narrow WhatsApp searches for potential decisions, request/commitment candidates, candidate action items, topic-matching messages, and topic-matching evidence that contains a decision candidate. Results return the original stored message and source citation; candidate interpretations are labeled heuristic and are not confirmed facts. Candidate queries use the currently configured selected-group IDs. The organizer's actual group-ID path and authorization list remain unverified, so results describe only stored records admitted under the current configuration and do not establish coverage of all authorized groups. A specific group query must provide an exact configured ID; group names are not resolved.

The current `WHATSAPP_SELECTED_GROUPS` allowlist also applies to WhatsApp reads, dashboard displays and statistics, and WhatsApp JSON exports. An empty allowlist means no WhatsApp records are displayed or exported. Removing a group from the allowlist does not delete its historical SQLite records; those rows remain internal historical storage and are excluded from these current read/export paths. This setting remains dependent on organizer confirmation of the actual group-ID path and authorized IDs, and must not be treated as proof that the listed IDs are the organizer's real groups.

Cross-source evidence search returns exact literal matches separately from qualified topic matches. Put one phrase in double quotes to request an exact literal search; only one complete quoted phrase is supported. Exact matching uses a case-sensitive, punctuation-sensitive substring of the stored title or content, with no normalization; literals must be at least three characters and cannot be a single configured common question word. If one record qualifies as both an exact literal and a topic match, it is classified as an exact literal match and counted once. This establishes only that the literal text occurs in a stored record; it does not validate it as a customer, order, or transaction ID. Topic search uses FTS5 over title/content with normalized terms and the existing minimum two meaningful-term overlap rule. Matching-record counts are computed over all qualified stored rows before the eight-record display limit and are reported by source; they count records, not customers, orders, or transactions. Generic and exact cross-source searches filter WhatsApp rows to the current selected-group allowlist; when it is empty, WhatsApp rows are excluded while Gmail and Drive remain available. Organizer confirmation of the group path and authorized IDs is still required.

## Database and connector behavior

`records` is intentionally source-neutral while metadata holds source-specific fields. IDs remain source-native and are never merged across providers. For WhatsApp, the configured group path and `WHATSAPP_SELECTED_GROUPS` gate message processing before content extraction and database writes; stable message IDs prevent duplicate records. Gmail unanswered-message evaluation compares inbound timestamps against a configurable reference date and checks later messages in the same thread; provide the account's own addresses on normalized messages so replies are distinguished from customer messages.

Drive extraction handles local and downloaded PDF, DOCX, XLSX, TXT, MD, and CSV files, plus Google Docs/Sheets exports. Google Slides, legacy `.doc`/`.xls`, images/scans without OCR, and other unsupported MIME types are not text-extracted and are marked explicitly. Extraction failures are retained in metadata and surfaced in outputs. Classification is deterministic; ambiguous documents use a documented priority order, with general office as the fallback category.

## Initial architecture plan

1. Establish common configuration, SQLite schema, and deduplication.
2. Add isolated source adapters and deterministic classification/extraction.
3. Add modular serialization and evidence retrieval.
4. Add API and dashboard surfaces.
5. Replace placeholders with organizer-approved schemas and validate against sandbox fixtures.

## Dependencies

FastAPI/Uvicorn, Streamlit, official Google API/OAuth clients, python-docx, openpyxl, pypdf, python-dotenv, and pytest/httpx. No background queue, ORM, vector database, or mandatory LLM is introduced.

## Main risks and organizer questions

- What exact group-ID path appears in the organizer-approved WhatsApp webhook fixture, which group IDs are authorized, and are there additional organizer-specific signature requirements or retry expectations beyond Meta's standard HMAC verification?
- Which fields and nesting are required in each output JSON, and how should conversation summaries/commitments/actions be represented?
- Which Gmail account(s), ownership addresses, message fields, and exact five-day boundary/time zone define an unanswered message?
- What Drive sandbox identifiers and Google Docs/Sheets export formats are provided? Are shared-drive files included?
- Are external LLMs permitted, and if so which provider, retention setting, and data subset? LLM calls remain disabled until permission is explicit.
- What constitutes organizer-specific “business critical” attachment evidence beyond the transparent filename/text heuristics?

