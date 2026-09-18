# AGENTS.md

## Project purpose

This project is the source checkout for the Linux adaptation of the Windows
Everything whole-system file-search skill. It provides a read-only filename
search CLI and an MCP stdio adapter for Hermes.

## Source of truth and profile sync

- Edit files in this checkout: `workspace/linux-whole-system-file-search-skill/`.
- Do not hand-edit the installed copies under `skills/` or
  `profiles/coder/skills/`.
- After source changes, run `bash scripts/install.sh` from this project root to
  synchronize the normal and coder profile copies.
- The installer preserves unrelated files in destination directories.
- With no arguments in a terminal, the installer presents a menu; keep the
  command-line switches available for automation and tests.

## Backends and indexing

- `auto` prefers `plocate`, then `locate`, then the live Python filesystem
  fallback.
- A normal skill install must not install packages or run `updatedb`.
- `--install-plocate` is the explicit opt-in path: install the distro package,
  build the initial index as the current user under `~/.cache/plocate/`, and
  enable/start `plocate-updatedb.timer` when the package provides it. Package
  and system-timer setup may require sudo; the agent refresh must not.
- `--refresh-if-stale` is a CLI-only opt-in. It and the watcher run `updatedb`
  as the current user into the user-owned database; never add an automatic
  sudoers rule.
- Keep the live `find` fallback for newly created files and for requests that
  require current filesystem state.
- `--install-watcher` is a separate opt-in: it installs a user-level inotify
  service that debounces filesystem events and refreshes the user-owned index
  as the current user. It must not use sudo or run the watcher as root. Exclude
  the user's Chromium cache and profile trees from the watch tree to avoid
  cache churn. Keep the daily package timer as a fallback, and stop/remove the
  managed service during a normal uninstall.

## MCP contract

The MCP server exposes these short tool names:

- `search_files` — search and return read-only metadata.
- `index_status` — inspect index availability/freshness and watcher state.
- `watcher_status` — report the live watcher, recent event history, and
  most recent changed paths.

Preserve newline-delimited JSON-RPC stdio compatibility. All exposed MCP tools
are read-only and must retain the read-only annotations
(`readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false`). The
MCP adapter must not expose an index-refresh operation; the internal refresh
function remains available to the optional user-level watcher and CLI only.
The watcher state retains at most 100 event batches in `events` and reports the
subset detected after the last successful refresh as `events_since_refresh`.
Each path record must retain `mask` and include the symbolic `mask_human` value.

The installer preserves the operator's existing Hermes trust setting and must
not add or modify trust/write-enablement options. Do not silently change
unrelated Hermes trust entries.

## Validation

At minimum, run:

```bash
python3 -m py_compile scripts/search.py scripts/mcp_server.py scripts/index_watcher.py
bash scripts/install.sh --dry-run
bash scripts/install.sh --install-plocate --dry-run
bash scripts/install.sh --install-watcher --dry-run
bash scripts/install.sh --uninstall --remove-plocate --dry-run
```

For the interactive menu, run `bash scripts/install.sh` in a terminal and
choose the exit option during a non-mutating smoke test.

For MCP changes, initialize the server with an MCP client and verify tool
listing plus calls to search and index status. If the installed MCP process is
already running, restart the Hermes gateway after synchronizing the installed
copy so it reloads the adapter.
