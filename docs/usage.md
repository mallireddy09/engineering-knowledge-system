# CLI workflows

All reads operate on the local cache unless a command explicitly says otherwise.
Run `ekm refresh` to fetch a complete remote snapshot. Commands never infer task
completion from modified files or commits.

## Profiles and storage

```bash
ekm account add local --type mock
ekm account list
ekm account switch local --approve
ekm account status
ekm init --approve
ekm check
```

Adding a profile never activates it. Use `account switch` to explicitly change
the active destination. `--profile NAME` may precede a command to read another
profile, but does not bypass the switch requirement for writes. A profile's
destination is immutable; configure a new profile instead of silently retargeting
existing knowledge.

Microsoft profiles need an application ID and appropriate account permissions:

```bash
ekm account add personal --type personal --client-id APP_ID
ekm account switch personal --approve
ekm account login
ekm init --approve

ekm account add work --type company --client-id APP_ID --tenant-id TENANT_ID --site-id SITE_ID
```

Use [Microsoft setup](microsoft-setup.md) before running these commands. `logout`
removes the selected profile's cached sign-in. `account remove NAME --approve`
also removes its configuration; local records remain for recovery and remote data
is unaffected.

## Capture and approve

```bash
ekm log --task TASK-123 --project data-platform --preview-output git-draft.json
```

This creates a metadata-only draft from the selected working tree. The agent
must enrich it from actual session evidence, label inferences, capture tests
performed, and ask concise questions where evidence is missing. Nothing is saved
remotely by this command. Use `--repo /path/to/repo` for another working tree.

```bash
ekm log --input enriched.json --dry-run --preview-output approved-draft.json
ekm log --input approved-draft.json --expected-hash HASH_FROM_PREVIEW --approve
```

The digest binds the exact populated record, configuration, operation, and ETag.
Any change requires a new preview and approval. `--preview-output` creates a new
file and never overwrites an existing draft. Without `--approve`, a terminal
session asks for `yes`; noninteractive use fails safely.

Other record kinds use the same workflow:

```bash
ekm decision import --input decision.json --dry-run
ekm debug import --input investigation.json --approve
ekm debt --input debt.json --approve
ekm project import --input project.json --approve
ekm import --input question.json --approve
```

Records share one schema; `kind` selects `task`, `decision`, `question`,
`debugging`, `debt`, `project`, or `review`. Questions require `question` and
`expected_answer`. Titles must contain text. Unknown keys and unsupported schema
versions are rejected. UUID relationships are explicit. All timestamps include
a timezone and normalize to UTC.

## Update, conflict, and recovery

```bash
ekm show task UUID --remote
# Edit the returned record while retaining its id and created_at.
ekm update --input edited.json --etag 'CURRENT_ETAG' --dry-run
ekm update --input edited.json --etag 'CURRENT_ETAG' --approve
```

Updates never silently replace an existing version. A conflict remains pending
until you inspect the current remote record, decide how to merge, discard the
obsolete pending draft, and submit a newly approved update. No automatic
last-writer-wins resolution is performed.

```bash
ekm log --input draft.json --offline --approve
ekm pending
ekm sync --approve
ekm discard UUID --approve
```

Offline mode queues a reviewed draft locally. It is absent from committed-record
search until sync succeeds. Pending writes retain stable IDs after failures;
retry reconciles a remote success that occurred before the local acknowledgement.
`sync` prints saved, conflicting, and failed IDs and exits unsuccessfully when
some writes still need attention.

## Retrieve engineering memory

```bash
ekm search 'Kafka consumer lag' --project data-platform --technology Kafka
ekm search 'version mismatch' --kind decision
ekm decision list
ekm decision show UUID
ekm debug search 'connection timeout'
ekm debt --priority high --project data-platform
ekm project data-platform
ekm show task UUID --related
```

Similarity is a transparent lexical matching score. A historical investigation
includes its original symptoms, root cause, resolution, project, and references.
Its solution must be verified in the new environment.

## Questions and reviews

```bash
ekm review --due --project data-platform
ekm quiz --topic distributed-systems --difficulty advanced
```

Interactive sessions ask the question, wait for an answer, then reveal the
expected answer and explanation. You or the agent score the answer from 0 to 1
and provide feedback explaining correct, missing, and incorrect points. The
ideal answer is stored as `expected_answer`. Noninteractive listings omit the
expected answer:

```bash
ekm quiz --question UUID
ekm review --question UUID --answer 'My explanation' --score 0.8 \
  --feedback 'Correct failure mode; missing recovery trade-off' --approve
```

The default day intervals are 1, 7, 14, and 30. Consecutive scores of at least 0.7
advance the interval; a lower score resets it. Set a question's `review_schedule`
to customize increasing positive day intervals. Schedule state is derived from
append-only review history, making a partially committed question update
unnecessary. Review history remains intact when questions are edited.

## Reports, files, backup, and optional GitHub

```bash
ekm insights --recurring-issues
ekm insights --learning-gaps
ekm weekly --project data-platform
ekm monthly
ekm export --format word --output exports/journal-2026-11.docx
ekm export --format excel --output exports/dashboard-2026-11.xlsx
ekm export --format json --output exports/records-2026-11.json
ekm export --format word --output exports/upload-journal.docx --upload --approve
ekm backup --output backups/knowledge.json
ekm restore --input backups/knowledge.json --dry-run
ekm restore --input backups/knowledge.json --approve
```

Weekly means the preceding seven days; monthly means the preceding thirty days.
Reports use stored timestamps, never tracker completion guesses. Excel refresh
means exporting a new snapshot after `ekm refresh`; EKM does not overwrite a user
workbook. Existing output files always cause an error. The structured records
remain independent of Word and Excel.

JSON backups include records from the local cache, not token caches or pending
drafts. Refresh before backing up when connected. Restore validates the complete
bundle before saving; stable IDs prevent duplicate creation and conflicts never
overwrite an existing record. A failure midway can be retried, and pending
operations are inspectable. Explicit approval is essential when source and
destination profiles differ.

Deletion protection is maintained as profile-local tombstones and durable delete
intents. It prevents an old pending save from silently recreating a deleted
record on this machine; it is not a globally replicated deletion ledger. To
intentionally restore a deleted UUID, inspect pending operations and use
`ekm revive UUID --approve` before saving, or include `--revive-deleted` in an
explicitly approved restore. A pending deletion must first be resolved or
discarded. Deletion protection is not a substitute for provider retention,
backups, or coordination between multiple machines.

```bash
ekm github owner/repository issues 123
ekm github owner/repository pulls 456
```

This optional integration invokes authenticated `gh` for issue/PR metadata only.
Associate returned URLs/IDs with your draft after review. It never sends local
source code to GitHub. Local Git capture works without GitHub credentials.

For remote deletion, inspect the record first, back it up, then run
`ekm delete KIND UUID --etag CURRENT_ETAG --approve`. Deletion is permanent at the
application layer; provider retention/version policies are outside EKM's control.
