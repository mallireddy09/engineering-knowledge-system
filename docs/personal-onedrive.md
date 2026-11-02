# Personal Microsoft account and OneDrive

This phase extends EKM's existing shared engine. Both Claude Code and Codex invoke
the same commands. It uses your existing Microsoft Application (Client) ID; no new
registration or client secret is required if that app supports personal accounts.
Mock tests exercise the implementation without signing in to Microsoft. A live
connection is verified only after you complete the interactive sign-in below.

## Configure the existing app

In Microsoft Entra, open **App registrations → your existing application**:

1. Under **Authentication → Supported account types**, select **Personal Microsoft
   accounts only**, or **Accounts in any organizational directory and personal
   Microsoft accounts**. An organization-only registration cannot sign in a
   personal account. Keep using the same app ID after changing supported types.
2. Add the **Mobile and desktop applications** platform with redirect URI
   **`http://localhost`**. The default browser flow uses an available localhost
   port selected by MSAL; do not register an unrelated web callback or supply a
   client secret. Keep the redirect setting in EKM at `http://localhost` unless
   you intentionally need a registered localhost port.
3. Under **API permissions**, configure Microsoft Graph **delegated** permissions
   **`User.Read`** and **`Files.ReadWrite`**. Consent to those permissions when
   signing in. Do not add application permissions, `Files.ReadWrite.All`, or a
   client secret for this desktop CLI.
4. If using the optional device-code flow, enable **Allow public client flows**
   under the app's authentication settings. Browser sign-in uses MSAL's
   authorization-code flow with PKCE, state validation, and loopback handling.

The consumer-specific authority rejects organization accounts. A successful sign-in
validates both the personal tenant and the account identity. EKM does not request
directory-wide application-read permissions to inspect your app registration;
configuration mistakes instead produce safe sign-in diagnostics.

Microsoft documents the desktop redirect and interactive API in
[MSAL PublicClientApplication](https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication)
and the delegated personal-account file permission in
[OneDrive file upload](https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0).

## Paste the Client ID and sign in

The project has an ignored `.env` file. Fill in only the public client ID:

```dotenv
MICROSOFT_CLIENT_ID=YOUR_EXISTING_APPLICATION_CLIENT_ID
MICROSOFT_ACCOUNT_TYPE=personal
MICROSOFT_REDIRECT_URI=http://localhost
MICROSOFT_SCOPES=User.Read Files.ReadWrite
EKM_STORAGE_PROVIDER=onedrive
```

Never paste a password, client secret, access token, or refresh token into this
file. `.env` is not committed; `.env.example` is a public template. EKM reads only
the allowlisted settings and never executes shell commands from `.env`. A process
environment value overrides the corresponding file value. An alternative file can
be supplied with `--env-file /private/path/to/settings.env` on first setup.

From the repository, with the installed virtual environment active:

```bash
ekm auth login --name personal --approve
ekm auth status
```

`--approve` approves creating/activating the displayed personal profile; it does not
approve future file deletions. The browser opens Microsoft's sign-in page. Use
your personal account and complete consent. After sign-in, EKM identifies the user
and reads OneDrive metadata. It does not initialize folders or upload knowledge at
login. Status shows safe account information, granted scopes, expiry/refresh
state, and OneDrive connectivity, never tokens or capability download URLs.

For a terminal without a usable local browser:

```bash
ekm auth login --name personal --flow device --approve
```

Follow the displayed Microsoft device instructions. Token persistence requires the
OS credential store: macOS Keychain, Windows DPAPI, or Linux libsecret. EKM fails
closed instead of saving tokens as plaintext. Expired access tokens refresh
silently when possible; revoked consent or expired refresh sessions require sign-in
again. `ekm auth logout` clears cached accounts while retaining the immutable
profile identity, protecting that profile from accidental reassignment.

## Initialize and verify the owned folder

```bash
ekm onedrive initialize --approve
ekm onedrive test --approve
```

Initialization creates this dedicated tree idempotently:

```text
EngineeringKnowledge/
  database/{tasks,decisions,questions,debugging,technical-debt,projects,reviews}/
  journals/
  reports/
  dashboards/
  backups/
  manifests/
```

A marker binds the root to the application, authenticated account and folder ID.
An existing `EngineeringKnowledge` folder without a matching marker is not
automatically claimed or modified. If a previous initialization was interrupted
between root creation and marker creation, inspect that exact folder first. Only
after confirming it is your intended EKM folder, explicitly run:

```bash
ekm onedrive initialize --adopt-existing --approve
```

The temporary connectivity test requires approval because it writes a unique test
file under the verified EKM folder. It identifies the account, reads OneDrive and
directory metadata, creates and reads its own file, then deletes only that file
using its exact identity/version. It never selects arbitrary existing user files
for cleanup. Do not run live tests against a production account unintentionally.

New `.env`-configured profiles use `OneDrive/EngineeringKnowledge`. Existing
AppFolder profiles retain their original `Apps/<application-name>` location and
permissions; no records are moved automatically. `Files.ReadWrite` is needed for
the requested top-level folder and grants broader Microsoft access than AppFolder;
EKM's owned-root checks restrict its operations to the dedicated folder.

## Log, search, synchronize and recover

Existing capture commands remain the shared execution layer:

```bash
ekm log --input reviewed-task.json --dry-run
ekm log --input reviewed-task.json --approve
ekm log --input offline-task.json --offline --approve
ekm sync status
ekm sync pull
ekm sync push --approve
ekm onedrive sync --approve
ekm search 'consumer retry' --project event-platform
```

The JSON records in OneDrive are the remote source of truth. SQLite is an isolated
cache with durable upload/download queues. Failed synchronization retains local
drafts. Pull publishes a new local snapshot only after validated downloads finish;
ETags and content hashes detect changed/corrupted data. Ambiguous writes are
reconciled using stable UUIDs rather than blindly retried.

Inspect `ekm pending` and `ekm show KIND UUID --remote` when a version conflict is
reported. `ekm sync resolve --help` describes the explicit keep-remote or merged
record workflow. Resolution requires approval and a current remote version; it
never silently chooses last-writer-wins.

For example, inspect the conflict first, then supply the displayed current ETag:

```bash
ekm sync resolve --kind task --id UUID --strategy remote --dry-run
ekm sync resolve --kind task --id UUID --strategy remote --etag 'CURRENT_ETAG' --approve
# Or approve a merged record, preserving its UUID and kind:
ekm sync resolve --kind task --id UUID --strategy merge --input merged.json --etag 'CURRENT_ETAG' --approve
```

The old local draft remains in the profile SQLite `resolution_backups` table.
Completed download jobs are validated before reuse; corrupted staging data is
downloaded again rather than published to the cache. A failed pull preserves the
previous searchable snapshot and all pending local work.

## Word journals and Excel dashboards

Generate locally, inspect the output, then explicitly upload:

```bash
ekm export --format word --output exports/DEMO-1-consumer-retry.docx
ekm onedrive upload --input exports/DEMO-1-consumer-retry.docx --approve
ekm export --format excel --output exports/engineering-dashboard.xlsx
ekm onedrive upload --input exports/engineering-dashboard.xlsx --approve
ekm onedrive metadata --folder dashboards --name engineering-dashboard.xlsx
```

Word files go to `journals`; Excel files go to `dashboards`. Meaningful task-based
filenames make journals easy to find. Reusing an identical upload is safe; a
different document never silently overwrites an existing name. To update a tracked
EKM document, generate a new local file with the same basename and pass the
original ETag:

```bash
ekm onedrive upload --input refreshed/engineering-dashboard.xlsx --etag 'ORIGINAL_ETAG' --approve
ekm onedrive download --folder dashboards --name engineering-dashboard.xlsx --output downloads/dashboard.xlsx
```

Updates check both version and tracked content. If someone modified the remote
workbook, EKM refuses replacement and preserves those changes; it does not claim
to merge arbitrary custom sheets, formulas or charts. Export/upload a new filename
instead. Download destinations are new files and never overwrite existing local
workbooks.

Local files request owner-only POSIX permissions where supported. Windows uses
the destination directory's inherited ACLs; keep exports and backups in a private
user directory. Token persistence independently uses OS-backed encryption.

Creation provenance is local to the profile. Identical-content collision retries
do not claim an untracked existing file as EKM-created. If the local provenance
ledger is missing or an upload succeeded before its local acknowledgement failed,
use a new snapshot filename; EKM will not infer permission to replace that file.

## Backup and restore

```bash
ekm onedrive backup --output backups/knowledge-2026-11-02.json --approve
ekm onedrive download --folder backups --name knowledge-2026-11-02.json --output downloads/recovery.json
ekm restore --input downloads/recovery.json --dry-run
ekm restore --input downloads/recovery.json --approve
```

Backup first pulls a complete remote snapshot, writes a new local JSON bundle, and
uploads approved structured records to the owned `backups` folder. Partial pulls
abort backup rather than silently creating an incomplete snapshot. Backup excludes
token caches and authentication state. Keep the private profile directory separately
if you also need local pending drafts and audit history. Restores validate records
before writes and retain stable IDs; conflicting remote versions require an
explicit merge. Deletion remains an independently approved ETag-aware operation.

## Remaining manual validation

The Client ID, existing app's supported account types/desktop redirect/delegated
permissions, OS credential store, and interactive Microsoft consent must be
configured by the account owner. Mocked tests do not prove tenant policy or live
endpoint behavior. After setup, run the explicitly approved sign-in and temporary
test commands above. Never publish credentials, auth responses, capability URLs,
or a populated `.env` while reporting a failure.

The optional live pytest test additionally requires `EKM_LIVE_HOME`,
`EKM_LIVE_PROFILE`, `EKM_LIVE_CONFIRM_TEST_ACCOUNT=1`, and
`EKM_LIVE_APPROVE_TEMP_FILE=1`. It only creates and deletes its own unique file
inside an already initialized owned root; initialization is a separate approval.
