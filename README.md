# Engineering Knowledge Management System

EKM captures engineering tasks, decisions, debugging investigations, technical
debt, projects, and recall questions through one Python CLI. Claude Code and
Codex share a canonical `engineering-journal` skill and the same storage logic.

Personal Microsoft accounts use JSON records in an application-owned OneDrive
folder. Company accounts use SharePoint Lists. Every profile has separate
authentication, records, local cache, and pending operations. A durable local
mock provider supports development without Microsoft credentials.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

ekm account add demo --type mock
ekm account switch demo --approve
ekm init --approve
python scripts/install_skills.py --target .
```

Create a small draft, then inspect the destination and redacted record before
approving it:

```json
{
  "kind": "task",
  "title": "Protect consumer recovery from retry storms",
  "task_id": "DEMO-1",
  "project_id": "event-platform",
  "status": "in-progress",
  "technologies": ["Kafka"],
  "implementation_summary": "Bounded retry with jitter after transient failures.",
  "lessons_learned": ["Unbounded concurrent retries amplify downstream failures."],
  "unanswered_questions": ["What is the recovery-time impact at peak load?"]
}
```

Save that JSON as `draft.json` outside this repository:

```bash
ekm log --input draft.json --dry-run --preview-output reviewed.json
# Review the printed payload, profile, operation, and approval_hash.
ekm log --input reviewed.json --approve
ekm search 'Kafka retries' --project event-platform
ekm insights
ekm export --format word --output exports/journal.docx
ekm export --format excel --output exports/dashboard.xlsx
```

`--approve` represents explicit approval of the displayed operation and selected
destination. For automation, also pass `--expected-hash` from the reviewed preview.
The fully populated `reviewed.json` retains UUIDs and timestamps so that the hash
remains stable. Output files are created exclusively; existing files are never
overwritten. Export uses the local cache; run `ekm refresh` when remote data changed.

## What is implemented

- Seven versioned, validated record kinds, stable IDs, UTC timestamps, redaction,
  deduplication, explicit updates with ETags, and recoverable JSON exports.
- Isolated mock, personal OneDrive, and company SharePoint providers; delegated
  OAuth with encrypted token caching and identity binding.
- Approved offline drafts, durable pending writes, explicit sync, conflict
  reporting, metadata-only audit logs, and a rebuildable SQLite full-text index.
- Tasks, decisions, debugging histories, debt and project records; filtered
  search with term-based similarity explanations and related records.
- Questions and append-only reviews; configurable 1/7/14/30-day intervals with
  adaptive scheduling derived from review history. Humans or agents evaluate
  answers; the CLI does not claim automatic authoritative grading.
- Word journals, Excel dashboard snapshots, JSON backup/restore, weekly/monthly
  activity reports, and optional read-only GitHub issue/PR metadata through `gh`.
- A canonical provider-neutral skill and an installer for both agents.

Meaningful extraction and question generation happen in the coding agent using
the conversation available to it. Running `ekm log` by itself creates a Git
metadata draft; it does not read private session APIs, inspect source code, or
invent implementation details or completion status. No separate AI service or
vector database is required. Similarity search uses terms rather than embeddings.

## Microsoft and agent setup

Google Drive, native Google Docs journals and Google Sheets dashboards are also
available as an independent provider. Configure the existing Desktop OAuth app
using the [Google setup guide](docs/google-workspace.md), then run
`ekm auth google login --name google --approve` and `ekm google initialize --approve`.
`ekm provider switch google --approve` and
`ekm provider switch microsoft --approve` only select destinations; they never
migrate records. The same logging, search, quiz, report and export commands work
with either provider, and the same canonical skill serves both coding agents.

For a personal Microsoft account, use the ignored `.env` file in this checkout:
paste your existing public Application (Client) ID into `MICROSOFT_CLIENT_ID`,
then run:

```bash
ekm auth login --name personal --approve
ekm auth status
ekm onedrive initialize --approve
ekm onedrive test --approve
```

See the [personal OneDrive guide](docs/personal-onedrive.md) for the existing-app
localhost/PKCE configuration, delegated permissions, Word/Excel uploads and safe
updates, remote backups, and explicit `sync status/pull/push/resolve` commands.
New personal profiles use `OneDrive/EngineeringKnowledge`; existing AppFolder
profiles retain their original location. Sign-in and the temporary file test run
only when explicitly invoked; normal tests never access your live OneDrive.

Follow [Microsoft setup](docs/microsoft-setup.md) to register a public-client app,
choose delegated permissions, configure a tenant/site, and initialize remote
storage. Provisioning a SharePoint site can require a separate administrator
bootstrap permission from ordinary record CRUD. EKM never automatically elevates
permissions or transfers company records to a personal profile.

See [agent setup](docs/agent-setup.md) for installation and invocation in Claude
Code and Codex, [CLI usage](docs/usage.md) for workflows, and
[architecture](docs/architecture.md) for the data model, reference analysis,
implementation phases, and failure model. `ekm --help` and command-specific
`--help` list every supported flag.

Microsoft operations are covered by mocked service tests. Live personal-account
and company-tenant behavior, consent, provisioning permissions, and actual agent
runtime discovery must be verified with authorized test accounts in your
environment. Structural installation checks alone do not establish runtime
compatibility. This initial release should receive live integration validation
before use as a production knowledge store.

## Data and privacy

The default data root is `~/.local/share/ekm`; set `EKM_HOME` to relocate it. Never
put that directory inside a source repository. Profile switching requires
approval and does not move records. `--profile NAME` selects reads from a named
profile, but writes require that profile to be active.

The local database and approved pending drafts contain engineering knowledge as
plain local data protected by restrictive filesystem permissions. Token caches
require OS-backed encryption and fail closed if it is unavailable. Use encrypted
disks and your organization's retention policy for confidential knowledge.
Redaction catches common credential patterns; review is still necessary for
customer data, company secrets, and sensitive production details. Default Git
capture includes filenames, branch, and recent commit IDs, with no source or diff.

`ekm account remove` signs out and removes profile configuration while retaining
local data for explicit recovery. Remote record deletion requires an ETag and
approval. Backup before deletion. Restore previews every record and requires
explicit destination approval, including any cross-account transfer.

## Development

```bash
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests scripts
python scripts/install_skills.py --target . --check
```

Normal tests use temporary local data and mocked Microsoft services. They do not
authenticate against or modify a live tenant. CI runs the same suite across
supported Python versions. For reproducible development installs, use the
repository's dependency lock file where available.

Inspired by the approved-draft learning workflow in
[swastij/claude-log-skill](https://github.com/swastij/claude-log-skill). EKM is an
original implementation; see the reference analysis for the inspected revision.

License: MIT.
