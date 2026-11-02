# EKM — Engineering Knowledge Management

Remember what you built, why you built it, and what you learned.

EKM connects Claude Code, Codex, and Cursor to one Python knowledge engine. Capture
lessons, decisions, debugging investigations and technical debt; search past work;
review questions; and generate Word, Excel, Google Docs and Google Sheets reports.

## Install

Requires Python 3.11+, Git, and access to this private GitHub repository.
Install the shared CLI once:

```bash
git clone https://github.com/mallireddy09/engineering-knowledge-system.git
cd engineering-knowledge-system
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Keep `ekm`
on the PATH used by your agent. The plugin installs the skill; it uses this CLI.

### Claude Code — one-line plugin install

Run in your terminal:

```bash
claude plugin install ekm --marketplace mallireddy09/engineering-knowledge-system
```

### Codex — one-line marketplace and plugin install

```bash
codex plugin marketplace add mallireddy09/engineering-knowledge-system && codex plugin add ekm@ekm
```

### Cursor — one-line skill install

Run from the EKM checkout to install into the current project:

```bash
python scripts/install_skills.py --agent cursor
```

Restart your agent after installation. Cursor's command installs the project skill
at `.cursor/skills/engineering-journal`; native Cursor marketplace metadata is also
included for repository imports. See [agent setup](docs/agent-setup.md) to install
into another project, use local marketplaces, or update an existing installation.

## Connect storage

Choose an account, or start with the local demo:

| Storage | Setup |
| --- | --- |
| Google Drive, Docs and Sheets | [Google setup](docs/google-workspace.md) |
| Personal Microsoft OneDrive | [OneDrive setup](docs/personal-onedrive.md) |
| Company Microsoft SharePoint | [Microsoft setup](docs/microsoft-setup.md) |

```bash
ekm account add demo --type mock && ekm account switch demo --approve && ekm init --approve
```

Google and Microsoft sign-ins stay independent. Switching providers never moves
historical records. Keep `.env` and OAuth credential files private and out of Git.

## Use

Ask your agent: **“Use EKM to log this session's engineering lessons.”**
It drafts the entry, shows the destination and content, and asks before saving.

```bash
ekm search "consumer timeout"
ekm review --due
ekm insights
ekm weekly
ekm export --format word --output exports/journal.docx
ekm export --format excel --output exports/dashboard.xlsx
```

Use `ekm --help` for all commands. Records and pending offline drafts are isolated
by profile; the default local data directory is `~/.local/share/ekm`.

## Development

```bash
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests scripts
python scripts/install_skills.py --package --check
```

Normal tests use mocks and temporary data. Live integration tests require explicit
configuration. Agent runtime discovery still needs a signed-in agent session.

[CLI guide](docs/usage.md) · [Architecture](docs/architecture.md) ·
[Verification](docs/verification.md) · [MIT license](LICENSE)
