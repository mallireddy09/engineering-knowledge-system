# Google alongside Microsoft

Google is an additional provider for the existing engine. Microsoft OAuth,
OneDrive layout and record formats, Word/Excel generation and both agent skills
are retained. Provider switching changes only the active profile; it never moves
or deletes historical records. Each profile has an isolated local index, pending
operations and encrypted authentication cache.

## Configure your existing Google Cloud OAuth app

1. In the existing Google Cloud project, enable **Google Drive API**, **Google
   Docs API** and **Google Sheets API**.
2. Configure the OAuth consent screen for an external application. While its
   publishing status is Testing, add your personal Google account as a test user.
3. Use an OAuth client of type **Desktop app**. Download its JSON credentials to
   `credentials.json` in this checkout, or use a private absolute path. A web or
   service-account client is not a substitute for this personal browser flow.
4. Configure the ignored `.env`:

```dotenv
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_CREDENTIALS_FILE=credentials.json
```

The downloaded Desktop JSON file is preferred and contains the client ID and
desktop client secret. Leave the two inline settings blank when using that file.
Alternatively, supply both values through the ignored `.env` or process
environment if no credential JSON exists. Do not paste your Google password or
access/refresh tokens. An inline client ID must match an existing JSON file.
Process environment values override `.env`. Keep the selected private settings
file available; profiles store its absolute path, not its secret contents.

Only these OAuth scopes are requested:

- `https://www.googleapis.com/auth/drive.file`: files created by this app, rather
  than access to the whole Drive.
- `openid` and `https://www.googleapis.com/auth/userinfo.email`: verify the
  immutable Google account subject and display account information.

Both [Docs batchUpdate](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
and [Sheets batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)
accept `drive.file`; separate broad Docs/Sheets scopes are unnecessary. OAuth
uses Google's supported Python libraries, an ephemeral localhost callback, PKCE,
signature/audience/issuer/expiry validation of ID tokens, and Google subject
checks after refresh. See [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).
External apps in Testing can have refresh-token lifetime limits; sign in again
if Google revokes or expires the session.

## Sign in and select a provider

```bash
source .venv/bin/activate
ekm auth google login --name google --approve
ekm auth google status
ekm google initialize --approve
ekm google test

ekm provider list
ekm provider status
ekm provider switch microsoft --approve
ekm provider switch google --approve
```

If no browser window opens, retry `ekm auth google login --no-browser`. Open the
displayed Google authorization link on this machine; the localhost callback still
completes the PKCE flow. This is an authorization request link, not an access token.
Do not share a callback URL containing an authorization code.

The explicit login name adds/activates an isolated Google profile after preview
and approval. Existing Microsoft profiles are never silently repurposed. If more
than one account exists for a provider, select it explicitly, for example
`ekm provider switch microsoft --name personal --approve`.
`ekm --profile google auth google status` reads a named profile without switching
the active destination. `ekm auth google logout` clears the encrypted local Google
session but retains its immutable account binding and all records.

Tokens are stored through the same OS encryption facilities already used by EKM,
in a separate Google cache. There is no plaintext fallback. Google credential JSON
files and `.env` are excluded from Git; Google secrets never enter profile JSON,
knowledge records, or status output. Windows local files inherit directory ACLs;
keep settings, exports and backups in a private user directory.

## Owned Drive storage and existing workflows

Initialization creates an app-owned `EngineeringKnowledge` folder with
`database/{tasks,decisions,questions,debugging,technical-debt,projects,reviews}`,
plus `journals`, `reports`, `dashboards`, `backups` and `manifests`.
Private Drive properties bind every managed file/folder to the OAuth client and
Google subject. IDs and current parents are verified; shortcuts, moved files and
ambiguous duplicate folders are rejected.

With `drive.file`, the app cannot enumerate every unrelated pre-existing file.
An invisible same-named folder is never claimed or overwritten; EKM locates its
own tagged root and pins that Drive ID. Names alone never establish ownership.

Use the same shared commands:

```bash
ekm log --input reviewed-task.json --dry-run
ekm log --input reviewed-task.json --approve
ekm log --input offline-task.json --offline --approve
ekm sync status
ekm sync push --approve
ekm sync pull
ekm search 'consumer lag'
ekm review --due
ekm quiz
ekm decision list
ekm debug search 'timeout'
ekm debt --priority high
ekm insights
ekm weekly
ekm export --format word --output exports/journal.docx
ekm export --format excel --output exports/dashboard.xlsx
```

### Revision and conflict behavior

Google stores a versioned JSON revision envelope containing the unchanged shared
Record schema, parent revision tokens and a deletion flag. Each mutation appends
an immutable file. `StoredRecord.etag` is a provider logical revision token,
**not** an asserted Google HTTP ETag or the Drive `version` number.
Drive's version metadata alone is not an atomic compare-and-swap operation, so
EKM never overwrites an older revision based only on a read-then-write check.

Stable logical IDs, pre-generated binary/folder file IDs and durable local
operation mappings support safe retries. Equivalent revisions collapse logically.
Concurrent divergent updates remain as multiple heads, producing a conflict
instead of choosing a winner. To resolve a remote branch conflict:

```bash
ekm google conflicts
ekm google resolve task UUID --head HEAD_A --head HEAD_B --input merged.json --approve
# Or explicitly delete all those heads from the active view:
ekm google resolve task UUID --head HEAD_A --head HEAD_B --delete --approve
```

Supply every current head shown by `conflicts`; stale head lists are rejected.
The merged record must retain the UUID and kind. Local pending-draft conflicts
are a separate layer and continue to use `ekm sync resolve`. Inspect the queue
after resolving a remote branch; pending drafts are never silently discarded.

Deletion is logical: it appends a tombstone and excludes the record from normal
search/list/exports. Immutable historical revisions remain in the owned Drive
folder. It does not claim to erase all historical personal data. Permanent
history disposal requires separate, explicit management in Drive; no automatic
purge or unrelated-file cleanup is performed by this integration.

## Native Docs and Sheets

```bash
ekm google journal TASK_UUID --approve
ekm google dashboard --approve
```

Journals reuse the existing Word content/heading structure and related-record
content. One native document is tagged by task UUID. The task's `references`
store the document ID and URL without changing the shared record format.
If reference publication conflicts, the already created tagged document is
retained; retry reconciles it rather than creating another document.

Docs updates append a new content snapshot under `requiredRevisionId`, preserving
existing text. A named-range content digest makes identical retries idempotent.
Native Docs/Sheets do not support pre-generated Drive IDs, so tagged operation
discovery and a local pending journal reconcile interrupted creates. If Google
reports an unknown outcome and no tagged file can yet be discovered, EKM refuses
another create. Inspect the owned folder and the specific entry in the profile's
`google-native-operations.json` before explicitly clearing a pending entry; never
blindly delete that recovery journal.

Dashboards reuse the existing Excel model: Overview, Tasks, Projects, Decisions,
Debugging, Technical Debt, Learning Progress, Review History and Technology Trends.
The spreadsheet ID and URL are stored in the Google profile configuration.
Each refresh atomically adds nine fresh snapshot tabs, with formulas, filters,
formatting and charts. Existing tabs/cells/custom formulas remain untouched;
later snapshot tab titles have a suffix. Google's initial blank `Sheet1`, if
present, is preserved as well. This is an append-only refresh, not an in-place
merge. Identical-content retries recognize the completed snapshot marker.
Arbitrary user strings use literal cell values, never executable formulas.

## Uploads, downloads, backup and restore

```bash
ekm google upload --input exports/journal.docx --approve
ekm google metadata GOOGLE_FILE_ID
ekm google download GOOGLE_FILE_ID --output downloads/journal.docx
ekm google backup --output backups/google-records.json --approve
ekm restore --input backups/google-records.json --dry-run
ekm restore --input backups/google-records.json --approve
```

Binary artifacts are new-only; a different file cannot overwrite an existing
owned filename. Downloads verify ownership and parent folders and create a new
local file. Native Docs/Sheets download as DOCX/XLSX, or PDF when the output name
ends with `.pdf`. Backups export validated current records from Drive, excluding
tokens, settings and pending local drafts. Restore uses the existing shared
validation/approval/UUID workflow. Protect the private profile directory
separately to retain pending operations and recovery journals.

## Validation and remaining manual steps

### Implementation map

| Added | Purpose |
| --- | --- |
| `src/ekm/google/auth.py`, `settings.py` | Desktop PKCE, encrypted token isolation, verified account identity and public configuration |
| `src/ekm/google/client.py` | Origin-restricted Drive/Docs/Sheets transport, errors, pagination and read retries |
| `src/ekm/providers/google_drive.py` | Existing StorageProvider contract, owned folders, immutable record revisions, explicit merges and uploads |
| `src/ekm/google/documents.py` | Native journals/dashboards reusing the existing Word/Excel artifact models |
| `src/ekm/cli_google.py` | Google auth, provider selection, journal/dashboard/storage commands |
| `tests/test_google_*.py` | OAuth, transport, storage, native content, CLI and optional live coverage |

Modified shared files are `config.py` (additive Google profile fields), `core.py`
(provider routing and client cleanup), `cli.py` (new command routing),
`cli_microsoft.py` (return the existing auth parser so the Google namespace can
be registered), dependency/lock files, ignore/template files and documentation.
The Microsoft OAuth, OneDrive provider, shared models, StorageProvider interface,
Word generator, Excel generator and canonical agent skills are unchanged.

Normal tests use mocked Google responses and temporary encrypted-cache fakes.
They do not authenticate, create Google files or consume your credentials. The
optional live test requires a separately initialized test profile plus
`EKM_GOOGLE_LIVE_HOME`, `EKM_GOOGLE_LIVE_PROFILE` and
`EKM_GOOGLE_LIVE_CONFIRM_TEST_ACCOUNT=1`; it only reads connectivity/owned records.

You still need to supply your existing Desktop client credentials, enable the
three APIs, configure the consent-screen test user if needed, and complete the
browser consent. The existing Microsoft setup/sign-in is independent and remains
available. Live Google compatibility is unverified until those steps are run.
