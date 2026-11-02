# Record guidance

Run `ekm --help` and the relevant subcommand's help before invoking a command
whose flags you have not checked. The CLI is the execution layer; do not write
SQLite or remote files directly.

All records use a common versioned schema with a `kind`, a `title`, a stable
UUID, and UTC timestamps. The CLI supplies identifiers and timestamps for a
new record. Retain the UUID when editing an existing record and supply the
expected version required by the command. The JSON schema in the repository
is the authority for field types; do not introduce unsupported keys.

A minimal task draft is:

```json
{
  "kind": "task",
  "title": "Handle stale cache versions",
  "task_id": "EXAMPLE-123",
  "project_id": "example-project",
  "status": "in_progress",
  "problem_statement": "A stale client could overwrite a newer record.",
  "implementation_summary": "Require the version observed by the caller before accepting an update.",
  "technologies": ["Python", "SQLite"],
  "tests_performed": ["Verified that a stale version is rejected"],
  "lessons_learned": ["A read before a write does not make the pair atomic."]
}
```

This is an example, not a claim about the user's work. Populate only facts
established in the session. Use `source_agent` to record the invoking agent;
do not claim access to previous conversations that are not available.

Supported record kinds:

| Kind | Capture |
| --- | --- |
| `task` | Problem, requirements, implementation, tests, lessons and follow-ups |
| `decision` | Context, chosen approach, alternatives, rationale and trade-offs |
| `debugging` | Symptoms, timeline, hypotheses, tests, root cause, fix and prevention |
| `debt` | Impact, workaround, proposed solution, priority and owner |
| `project` | Repository, services, dependencies and architecture summary |
| `question` | Question, expected answer, topic, difficulty and task relationship |
| `review` | Answer, score, feedback, question UUID and next review time |

Useful review questions ask about a mechanism, a trade-off, or a failure
scenario from the actual work. Keep an expected answer grounded in verified
knowledge. Keep task IDs from issue trackers distinct from internal UUIDs.
