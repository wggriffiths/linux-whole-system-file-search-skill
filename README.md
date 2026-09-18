# Linux Whole-System File Search Skill for Hermes

A Linux-native, read-only Hermes skill for locating files and folders by name,
path, type, size, or modification time.

This is a Linux adaptation of the Windows-oriented
[Everything whole-system file-search skill](https://github.com/ligofff/everything-whole-system-file-search-skill).
It uses the fastest available local backend:

1. `plocate`, when installed and indexed;
2. `locate`, when available;
3. a Python standard-library filesystem scan as a dependency-free fallback.

The skill never opens, edits, moves, or deletes search results.

An optional live watcher detects files and directories being created, deleted,
renamed, or moved in watched directories, then refreshes the filename index
after activity settles. Agents can inspect recent detected changes through
the read-only `watcher_status` MCP tool. The watcher does not monitor edits to
file contents.

## Requirements

- Linux
- Python 3.10 or newer
- No third-party Python packages for the CLI
- Optional: `plocate` or `locate` for fast indexed searches
- The MCP adapter also uses only the Python standard library

## Install

From the project root:

```bash
bash scripts/install.sh
```

When run in an interactive terminal without arguments, this opens a menu. The
menu includes choices for plocate and the optional live index watcher. The
switches remain available for scripted use. The normal install targets
`~/.hermes/skills/` and, when present,
`~/.hermes/profiles/coder/skills/`. It also understands `HERMES_ROOT` (or
`HERMES_HOME`) for a non-default Hermes root. Other modes are available:

```bash
bash scripts/install.sh --normal-only
bash scripts/install.sh --coder-only
bash scripts/install.sh --all-profiles
bash scripts/install.sh --hermes-root /srv/hermes
bash scripts/install.sh --target /path/to/skills --dry-run
bash scripts/install.sh --install-plocate
bash scripts/install.sh --install-watcher
bash scripts/install.sh --uninstall
bash scripts/install.sh --uninstall --remove-plocate
```

The installer updates only this skill's files and does not delete destination
directories. It installs both the CLI and MCP adapter. `--uninstall` removes
only the managed `SKILL.md` and script files; it refuses unrelated directories
and preserves extra files. `--install-plocate` is an explicit opt-in system
change: it uses the available distro package manager, builds the initial
database, and enables/starts `plocate-updatedb.timer` when available. A normal
install never installs packages or runs `updatedb`. `--remove-plocate` is also
explicit and only works alongside `--uninstall`; it removes the optional
system package as well as the skill copies.

The package setup recognizes `apt-get`, `dnf`, `yum`, `pacman`, `zypper`, and
`apk`. It uses `sudo` when not already running as root for package installation
and the optional system timer. The initial search database is built as the
current user at `~/.cache/plocate/plocate.db`; the agent never needs `sudo` to
refresh that database.

`--install-watcher` is a separate opt-in. It installs and enables the
user-level `linux-whole-system-file-search-watcher.service`, which uses Linux
inotify to detect file and directory creation, deletion, renames, and moves in
accessible, watched directories. Content edits and metadata-only changes do
not trigger a refresh. Volatile virtual trees and the Chromium cache/profile
trees are excluded to avoid churn, and some transient files are ignored.
After 30 seconds without more detected activity it refreshes the user-owned
index; during continuous activity it attempts a refresh after five minutes.
It runs as the current
user and never uses `sudo`. The normal daily plocate timer remains as a
fallback. Uninstalling the skill also stops and removes this service when it
was installed by this project.

### MCP

The project includes a stdio MCP server exposing three short-named,
read-only tools: `search_files`, `index_status`, and `watcher_status`. Hermes
can register it with a configuration entry like this (adjust the installed
path if your Hermes root differs):

```yaml
mcp_servers:
  file-search:
    command: python3
    args:
      - /home/YOUR_USER/.hermes/skills/linux-whole-system-file-search/scripts/mcp_server.py
    timeout: 120
    connect_timeout: 30
    enabled: true
    trust: untrusted
```

The coder profile uses the same entry with the script under
`~/.hermes/profiles/coder/skills/`.

`watcher_status` reports whether the optional live watcher is running, the
last 100 detected event batches, events since the last successful refresh, and
the most recent index refresh. Each changed path includes its numeric inotify
`mask` and readable `mask_human` names.

The installer preserves Hermes's existing trust setting and does not provide a
full-trust/write-enablement option. The optional background watcher maintains
the index; no MCP tool runs `updatedb`.

## Usage

```bash
python3 scripts/search.py "report" --count 20
python3 scripts/search.py "*.log" --sort date_modified --descending
python3 scripts/search.py "settings" --type file --project-path /srv/app
python3 scripts/search.py "cache" --min-size 10MiB --modified-after 2026-01-01
python3 scripts/search.py "report" --refresh-if-stale
```

Use `--format json` for structured output. Use `--backend find` when live
filesystem state matters. The fallback prunes `/proc`, `/sys`, `/dev`, and
`/run` by default and reports permission errors; pass `--include-virtual` when
those trees are explicitly needed.

`--refresh-if-stale` is opt-in and only refreshes an old indexed database before
the query. It can take a while and runs `updatedb` as the current user into
`~/.cache/plocate/plocate.db`. The package's system timer may separately keep
the root-managed system database fresh, but it is not required for agent
refreshes.

## Development checks

Run the offline regression suite and syntax checks before committing:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/search.py scripts/mcp_server.py scripts/index_watcher.py
bash -n scripts/install.sh
bash scripts/install.sh --dry-run
bash scripts/install.sh --install-plocate --dry-run
bash scripts/install.sh --install-watcher --dry-run
bash scripts/install.sh --uninstall --remove-plocate --dry-run
```

Tests use temporary directories and mocked index refreshes; they do not install
packages or update your real index. For the interactive smoke test, run the
installer in a terminal and choose `7` to exit.

## License

MIT. See [LICENSE](LICENSE).
