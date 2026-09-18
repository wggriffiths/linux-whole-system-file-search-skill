---
name: linux-whole-system-file-search
description: "Use when finding files or folders anywhere on Linux by name, path, type, size, or modification time; prefer an indexed locate backend and fall back to a read-only filesystem scan."
license: MIT
metadata:
  version: 0.5.0
  author: local
  platforms: [linux]
  hermes:
    tags: [Linux, files, folders, search, locate, plocate, filesystem]
    related_skills: []
---

# Linux Whole-System File Search

Use the bundled `scripts/search.py` when the user asks to locate a file or
folder by name, extension, path, type, size, or modification time. The script
only lists paths and metadata; it never opens, edits, moves, or deletes search
results.

The project also includes `scripts/mcp_server.py`, a stdio MCP adapter exposing
only read-only search, index status, and live watcher status. Index maintenance
is handled by the optional background watcher or the CLI, not by MCP.

## When to use

- Use for machine-wide filename/path searches when the location is unknown.
- Use `--project-path PATH` when the user gives a directory or project scope.
- Use `--backend plocate` or `--backend locate` when the user specifically
  wants an index, and use `--backend find` when fresh filesystem state matters.
- Do not use for searching file contents, Git-aware search, or editing a
  returned file. Locate the path first, then use the separate read/edit
  workflow explicitly.

## Run it

From this skill directory:

```bash
python3 scripts/search.py "report" --count 20
python3 scripts/search.py "*.log" --sort date_modified --descending
python3 scripts/search.py "settings" --type file --project-path /srv/app
python3 scripts/search.py "cache" --min-size 10MiB --modified-after 2026-01-01
```

## Install into Hermes

From a checkout of this project, run:

```bash
bash scripts/install.sh
```

When run without arguments in an interactive terminal, the installer presents
a menu with normal install, plocate install/setup, optional live index watcher,
uninstall, and exit choices. The command-line switches remain available for
scripted use.

The installer targets the normal Hermes skill directory and the existing
`profiles/coder` skill directory. It only updates this skill's `SKILL.md` and
scripts, including the MCP adapter; it does not remove other files. Use
`--normal-only`, `--coder-only`, `--all-profiles`, repeated `--target DIR`, or
`--dry-run` to control the target profiles. `--install-plocate` is an explicit
opt-in that installs the distro package, builds a user-owned initial index as
the current user, and enables or starts the package's system update timer where
available. Package installation and the system timer may use `sudo`; the
agent's index refresh does not. To remove an installed copy, use
`bash scripts/install.sh --uninstall`; uninstalling removes only this skill's
managed files and leaves unrecognized extra files in place. Add
`--remove-plocate` only when the optional system package should also be removed.

`--install-watcher` is another explicit opt-in. It enables a user-level
`linux-whole-system-file-search-watcher.service` that uses inotify to detect
changes in accessible directories. Volatile virtual trees and the Chromium
cache/profile trees are excluded to avoid churn. It waits 30 seconds for a
burst of changes to settle, and then refreshes the user-owned index as the
current user. A refresh is also forced after five minutes of continuous
activity. It does not use `sudo`; the package's daily timer remains a fallback.
Uninstalling the skill removes the watcher service installed by this project.

To register the MCP adapter in Hermes, add a stdio `mcp_servers` entry whose
command is the Hermes Python interpreter and whose argument is the installed
`scripts/mcp_server.py` path. The server exposes only the read-only
`search_files`, `index_status`, and `watcher_status` tools. The installer
preserves the operator's existing trust setting and has no full-trust or
write-enablement option.

The query is passed literally to the selected backend. By default it is a
case-insensitive substring match against the basename. Use `--match-path` to
match the full absolute path, `--case-sensitive` for case-sensitive matching,
`--whole-word` for word boundaries, and `--regex` for a regular expression.
Shell-style `*`, `?`, and `[]` patterns are supported without `--regex`.

When the optional watcher is installed, the MCP `watcher_status` tool reports
its health, the last 100 detected event batches, events since the last
successful refresh, and the latest index refresh. Each changed path includes
the numeric inotify `mask` and readable `mask_human` names.

Useful options:

```text
--count N                 rows to display (default 50)
--offset N                rows to skip
--sort name|path|size|date_modified
--descending              reverse the selected sort
--type file|folder|other|any
--min-size SIZE           e.g. 10MiB, 1GB, 500B
--max-size SIZE           e.g. 2GiB
--modified-after DATE     ISO date/time; date-only values use local midnight
--modified-before DATE    ISO date/time
--backend auto|plocate|locate|find
--timeout SECONDS
--refresh-if-stale        opt-in refresh before indexed search
--refresh-max-age SEC     refresh threshold (default 86400)
--refresh-timeout SEC     refresh timeout (default 900)
--format text|json
--include-virtual         include /proc, /sys, /dev, and /run in find scans
```

## Backend behavior

`auto` prefers `plocate`, then `locate`, then a Python filesystem scan. When a
`--project-path` is supplied in `auto` mode, it scans that path directly rather
than querying a global index and filtering a potentially huge result set. The
indexed backends are fast but can omit files created after the database was
updated; the script uses their existing-entry mode to avoid reporting deleted
paths. The `find` fallback searches `/` (or the project path), does not follow
directory symlinks, and prunes volatile virtual trees by default. It can be
slow and only sees paths permitted to the calling user. A timeout or permission
errors mean the result is not exhaustive; report that to the user.

For faster repeated whole-system searches, install the distribution's
`plocate` package and ensure its normal `updatedb` job is running. Do not
install packages or run `updatedb` unless the user explicitly asks for that
system change. The optional `--refresh-if-stale` CLI mode and the live watcher
run `updatedb` as the current user into the user-owned database under
`~/.cache/plocate/`; they never invoke `sudo` automatically. The package's
system timer may also maintain its separate root-managed database for
system-wide fallback coverage. The MCP adapter does not expose an index
refresh operation.

## Result handling

The text output reports the backend, total matches, displayed range, and one
absolute path per result with type, size, and local ISO modification time. Use
`--offset` to paginate and do not call an indexed result set a live/exhaustive
filesystem inventory. `--format json` is available when structured output is
more useful.

An empty result means only that the selected backend found no matching visible
path; it does not prove that a path cannot exist in an unindexed or inaccessible
location.
