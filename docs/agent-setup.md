# Claude Code, Codex and Cursor setup

## Plugin installation

Install the Python CLI first (`python -m pip install -e .` from this checkout)
and keep `ekm` on the agent's PATH. The plugin bundles the shared skill, not a
Python environment or credentials. Access to this private repository is required.

| Agent | One-line terminal command |
| --- | --- |
| Claude Code | `claude plugin install ekm --marketplace mallireddy09/engineering-knowledge-system` |
| Codex | `codex plugin marketplace add mallireddy09/engineering-knowledge-system && codex plugin add ekm@ekm` |
| Cursor project skill | `python scripts/install_skills.py --agent cursor` |

The Claude command adds the marketplace if needed. In Claude chat, the equivalent
two steps are `/plugin marketplace add mallireddy09/engineering-knowledge-system`
followed by `/plugin install ekm@ekm`. The plugin skill is namespaced as
`/ekm:engineering-journal`; the project-local skill uses `/engineering-journal`.

Cursor supports private repository marketplaces and `.cursor-plugin` manifests,
but an arbitrary-repository CLI install command is not documented in its current
official CLI reference. The one-line command above installs the same skill into
`.cursor/skills` and works without publishing to Cursor's public marketplace.
For native repository imports, this repo includes `.cursor-plugin/marketplace.json`.
No public marketplace listing or authenticated Cursor runtime test is implied.

## Project-local skill installation

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
| Cursor (`--agent cursor`) | `.cursor/skills/engineering-journal/SKILL.md` | `/engineering-journal` |

Select an agent or install all three into another project:

```bash
python scripts/install_skills.py --agent cursor --target /absolute/path/to/your/project
python scripts/install_skills.py --agent all --target /absolute/path/to/your/project
```

Without `--agent`, the installer retains its original Claude/Codex behavior.
Choose either a marketplace plugin or a project-local copy for an agent to avoid
duplicate discovery. Both routes use the same canonical skill.

## Maintaining the plugin bundle

The installable package is `plugins/ekm`, isolated from source, `.env`, OAuth files
and local data. Its manifests support Claude Code, Codex and Cursor; each ecosystem
has its own marketplace catalog pointing to that same package.

After editing `skill-source/engineering-journal`, synchronize the generated copies:

```bash
python scripts/install_skills.py --package --force
python scripts/install_skills.py --target . --force
python scripts/install_skills.py --package --check
```

Inspect diffs before using `--force`; it replaces edited skill files but refuses
unmanaged extra files. Version plugin manifests together when releasing changes.
For isolated local validation, add this checkout as a marketplace instead of
using the GitHub URL. For example, `codex plugin marketplace add /path/to/EKM`.

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
session automatically. All agents use the same active EKM account profile.
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
legacy installation directories plus Cursor, drift handling, plugin package
consistency, and deterministic CLI workflows.

Official references:
[Claude Code skills](https://code.claude.com/docs/en/skills) and
[OpenAI skills](https://developers.openai.com/codex/skills/).
Plugin references: [Claude installation](https://code.claude.com/docs/en/discover-plugins),
[Codex packaging](https://developers.openai.com/plugins/build/plugins),
[Cursor plugins](https://cursor.com/docs/plugins),
[Cursor CLI](https://cursor.com/docs/cli/reference/parameters), and
[Cursor skill paths](https://cursor.com/docs/skills).

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
