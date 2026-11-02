# Verification and acceptance coverage

The application is a working initial release. Normal verification uses a durable
local provider and mocked Microsoft HTTP/OAuth responses. It does not establish
live personal-account compatibility or tenant-specific permission approval.

| Requirement | Implementation | Verification |
| --- | --- | --- |
| One engine, both agents | `core.py`, `cli.py`, canonical `engineering-journal` | CLI behavior; separate target-path installer tests |
| Validated schema, stable relationships, UTC | `models.py`, published JSON schema | Round trips, invalid inputs, unsupported schema versions |
| Profile/account isolation | `config.py`, `microsoft/auth.py` | Explicit switches, case collisions, identity/tenant mismatch, retired names |
| Secure delegated authentication | MSAL encrypted persistence | Mocked encryption failure and identity validation; no real sign-in |
| Personal OneDrive | App-folder JSON and conflict-fail uploads | Mocked CRUD/version/identity failures and upload-session semantics |
| Company SharePoint | Seven lists, unique UUID field, multiline JSON | Mocked provisioning/schema/CRUD/pagination/version errors |
| Logging and deduplication | Agent drafts, metadata-only Git capture, stable identities | Approval, digest mismatch, duplicate and explicit update tests |
| Debugging, decisions, debt, projects | Seven-kind model and CLI workflows | Filtering, related records, lexical matching explanations |
| Search and offline cache | SQLite FTS5 and isolated durable drafts | Search, pending sync, remote-success/local-failure recovery |
| Safe deletion/conflicts | ETag checks, pending deletion intents, local tombstones | Stale writes, simulated local delete failure, explicit revival |
| Review/quiz and adaptive intervals | Append-only review histories | Hidden answers, success streaks/reset, reporting projections |
| Word journals | DOCX generation with headings/metadata/references/page fields | OOXML inspection and exclusive-create tests |
| Excel analytics | Nine-sheet dashboard, formulas/tables/charts | Workbook inspection, formula injection, no-overwrite tests |
| Export and recovery | JSON bundle, backup, restore, explicit destination | CLI backup/restore tests; stable ID recovery |
| Optional GitHub metadata | Read-only `gh api` issue/PR metadata | Input validation/local Git tests; current repo publishing is separate |
| Reliability and privacy | Redaction, digest approval, audit metadata, read retries | Quoted credential regressions, hostile transfer URL, no write retries |
| Cross-platform packaging | Python >=3.11, locked dependencies, CI matrix | Local installed CLI; Linux/Windows/macOS CI configured, not yet executed |

## Explicit limits

- Conversation extraction, interpretation, and question generation are performed
  by the active coding agent, using context it can actually access. The standalone
  CLI collects Git filenames/branch/commit IDs. It does not call a separate LLM,
  automatically grade free-text answers, or silently read/upload source code.
- Search is lexical and local; semantic embeddings are an optional future
  extension. There is no external vector database dependency.
- Excel refresh exports a new snapshot, preserving unrelated user workbooks.
- Tombstones protect this profile's local operation queue; there is no distributed
  cross-device deletion ledger or automatic background synchronization.
- Graph read retries use bounded exponential backoff and Retry-After. Writes with
  ambiguous outcomes remain pending for explicit reconciliation, avoiding unsafe
  blind retries. Upload interruption may leave an expiring server-side session.
- Current backup bundles contain committed records. Keep the private profile
  directory separately to retain pending drafts, audit logs, tombstones and
  profile configuration. Tokens remain in the OS-backed encrypted cache.
- Real Claude Code/Codex discovery/invocation and live OneDrive/SharePoint tests
  need a separately configured runtime and authorized test accounts.

## Optional live smoke test

Initialize a dedicated **test** Microsoft profile explicitly before this test.
The optional test only checks access and lists existing records; it never provisions,
uploads, edits, or deletes data. Set `EKM_LIVE_PROFILE`, `EKM_LIVE_HOME` and
`EKM_LIVE_CONFIRM_TEST_ACCOUNT=1`, then run `pytest -m live tests/test_live.py`.
Do not point it at a production profile. No live tests run by default or in CI.
