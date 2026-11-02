# Claude Code and Codex setup

Install EKM in a Python environment and ensure `ekm` is on the PATH used by
your coding agent. From the EKM checkout, synchronize the canonical skill:

```sh
python scripts/install_skills.py --target /absolute/path/to/your/project
python scripts/install_skills.py --target /absolute/path/to/your/project --check
```

This creates both project-local entrypoints from one source:

| Agent | Installed file | Invocation |
| --- | --- | --- |
| Claude Code | `.claude/skills/engineering-journal/SKILL.md` | `/engineering-journal` |
| Codex | `.agents/skills/engineering-journal/SKILL.md` | `$engineering-journal` |

Restart the agent session after installation if the skill is not discovered.
The canonical source is `skill-source/engineering-journal/`. Edit it there,
then rerun the installer. The installer refuses to replace an edited copy
unless `--force` is supplied; inspect those edits before replacing them.
Unknown extra files and symlink destinations are rejected to protect local
customizations. The check operation writes nothing. No home-directory or
global agent settings are changed by project-local installation.

The common skill uses CLI commands, not agent-specific hooks or argument
interpolation. It receives only the conversation and repository context that
the invoking agent can actually access. It cannot read the other agent's
session automatically. Both agents use the same active EKM account profile.
Always inspect `ekm account status` before approving an external write.

## Manual runtime smoke tests

These checks require an authenticated agent session; structural installation
tests do not prove runtime discovery or model behavior.

1. In Claude Code opened at the target project, invoke
   `/engineering-journal` and ask it to draft a lesson without saving.
   Confirm it finds EKM and previews the current destination and payload.
2. In Codex opened at the same project, invoke `$engineering-journal` with
   the same request. Confirm it uses `ekm` and waits for save approval.
3. Use a mock profile and explicitly approve a benign sample. Confirm both
   agents retrieve that record through `ekm search`.
4. Ask for a quiz. Confirm the agent waits for your answer before revealing
   the expected answer and requests approval before recording the review.

No unattended agent runtime test is included in normal CI: it would require
credentials, model access, and control of a live agent session. CI tests both
installation directories, drift handling, and deterministic CLI workflows.

Official references:
[Claude Code skills](https://code.claude.com/docs/en/skills) and
[OpenAI skills](https://developers.openai.com/codex/skills/).

## Export behavior

Word exports include metadata, structured knowledge sections, safe web
references and page numbers. Excel exports contain nine worksheets, tables,
filters, charts and summary formulas. Dates are rendered in UTC. External
strings are literal cell text so knowledge content cannot inject formulas.

Every export creates a new file and refuses to overwrite an existing path.
For dashboard refresh, export a new dated snapshot; unrelated workbook data
is never edited. Formulas recalculate when opened in a spreadsheet engine.
Uploaded reports require a separate approved provider write. Exported files
contain the selected records, so choose the destination and sharing carefully.
