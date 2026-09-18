#!/usr/bin/env python3
"""Read-only Linux-wide file search with an indexed and a stdlib fallback."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence


DEFAULT_TIMEOUT = 30.0
DEFAULT_INDEX_TIMEOUT = 900.0
DEFAULT_INDEX_MAX_AGE = 24 * 60 * 60.0
USER_INDEX_DATABASE_ENV = "LINUX_FILE_SEARCH_DATABASE"
WATCHER_STATE_ENV = "LINUX_FILE_SEARCH_WATCHER_STATE"
SYSTEM_INDEX_DATABASE_CANDIDATES = (
    "/var/lib/plocate/plocate.db",
    "/var/lib/mlocate/mlocate.db",
    "/var/lib/locate/locate.db",
)
VIRTUAL_ROOTS = frozenset(("/proc", "/sys", "/dev", "/run"))
SIZE_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}


class SearchError(RuntimeError):
    """A user-actionable search failure."""


class SearchTimeout(SearchError):
    """The search exceeded the requested timeout."""


@dataclass(frozen=True)
class Entry:
    path: str
    entry_type: str
    size: int | None
    modified: float | None


@dataclass
class ScanStats:
    permission_errors: int = 0


@dataclass
class SearchResult:
    backend: str
    coverage: str
    entries: list[Entry]
    elapsed: float
    permission_errors: int = 0
    fallback_reason: str | None = None
    index_refresh: dict[str, object] | None = None


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid size {value!r}; use a number with B, KiB, MiB, GiB, or TiB"
        )
    number = float(match.group(1))
    unit = match.group(2).lower() or "b"
    if unit not in SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"unknown size unit {match.group(2)!r}")
    return math.floor(number * SIZE_UNITS[unit])


def parse_datetime(value: str) -> float:
    """Parse an ISO date/time into a local epoch timestamp."""
    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError("date/time cannot be empty")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date/time {value!r}; use YYYY-MM-DD or an ISO timestamp"
        ) from exc
    if isinstance(parsed, dt.date) and not isinstance(parsed, dt.datetime):
        parsed = dt.datetime.combine(parsed, dt.time.min)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def normalize_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(os.path.expanduser(path)))


def _user_cache_home() -> str:
    cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if not cache_home:
        cache_home = os.path.join(os.path.expanduser("~"), ".cache")
    return normalize_path(cache_home)


def _user_index_database_path() -> str:
    configured = os.environ.get(USER_INDEX_DATABASE_ENV, "").strip()
    if configured:
        return normalize_path(configured)
    return normalize_path(os.path.join(_user_cache_home(), "plocate", "plocate.db"))


def watcher_state_path() -> str:
    configured = os.environ.get(WATCHER_STATE_ENV, "").strip()
    if configured:
        return normalize_path(configured)
    return normalize_path(os.path.join(_user_cache_home(), "plocate", "watcher-state.json"))


def _events_since_refresh(state: dict[str, object]) -> list[object]:
    """Return retained watcher events detected after the last successful refresh."""
    events = state.get("events")
    if not isinstance(events, list):
        return []

    baseline: float | None = None
    last_refresh = state.get("last_successful_refresh") or state.get("last_refresh")
    if isinstance(last_refresh, dict) and last_refresh.get("status") in {
        "updated",
    }:
        completed_at = last_refresh.get("completed_at")
        if isinstance(completed_at, str):
            try:
                baseline = dt.datetime.fromisoformat(
                    completed_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                baseline = None

    if baseline is None:
        return list(events)

    recent: list[object] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        detected_at = event.get("detected_at")
        if not isinstance(detected_at, str):
            recent.append(event)
            continue
        try:
            detected_timestamp = dt.datetime.fromisoformat(
                detected_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            recent.append(event)
            continue
        if detected_timestamp > baseline:
            recent.append(event)
    return recent


def find_updatedb() -> str | None:
    """Find the plocate-aware database updater, preferring Debian's name."""
    for candidate in ("updatedb.plocate", "updatedb"):
        binary = shutil.which(candidate)
        if binary:
            return binary
    for candidate in ("/usr/sbin/updatedb.plocate", "/usr/bin/updatedb"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _index_database_path() -> str:
    candidates = (_user_index_database_path(), *SYSTEM_INDEX_DATABASE_CANDIDATES)
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate
    return candidates[0]


def _timer_state() -> str | None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        completed = subprocess.run(
            [systemctl, "is-active", "plocate-updatedb.timer"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode == 0:
        return "active"
    if completed.returncode == 3:
        return "inactive"
    return "unknown"


def watcher_status() -> dict[str, object]:
    """Return the optional live watcher state without changing the system."""
    state_file = watcher_state_path()
    result: dict[str, object] = {
        "available": False,
        "status": "not_configured",
        "state_file": state_file,
        "running": False,
        "process_alive": None,
    }
    try:
        with open(state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        state_modified = os.path.getmtime(state_file)
    except FileNotFoundError:
        return result
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "unreadable"
        result["error"] = str(exc)
        return result

    if not isinstance(state, dict):
        result["status"] = "invalid"
        result["error"] = "watcher state must be a JSON object"
        return result

    result.update(state)
    result["available"] = True
    result["state_file"] = state_file
    result["state_modified"] = local_iso(state_modified)
    events = result.get("events")
    if not isinstance(events, list):
        events = []
    result["events"] = events
    result["event_count"] = len(events)
    result["events_since_refresh"] = _events_since_refresh(result)

    pid = result.get("pid")
    process_alive: bool | None = None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        process_alive = True
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                if b"index_watcher.py" not in handle.read():
                    process_alive = False
        except (OSError, ProcessLookupError):
            process_alive = False
    result["process_alive"] = process_alive
    declared_running = bool(result.get("running", False))
    result["running"] = declared_running and process_alive is not False
    if declared_running and process_alive is False:
        result["status"] = "stale"
    elif result["running"]:
        result["status"] = "running"
    else:
        result["status"] = "stopped"
    return result


def index_status() -> dict[str, object]:
    """Return local plocate/updatedb availability without changing the system."""
    database = _index_database_path()
    database_exists = os.path.isfile(database) and os.access(database, os.R_OK)
    database_modified = None
    age_seconds = None
    if database_exists:
        try:
            database_modified = os.path.getmtime(database)
            age_seconds = max(0.0, time.time() - database_modified)
        except OSError:
            database_exists = False
    return {
        "plocate": shutil.which("plocate"),
        "updatedb": find_updatedb(),
        "database": database,
        "user_database": _user_index_database_path(),
        "database_exists": database_exists,
        "database_modified": local_iso(database_modified),
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "timer": _timer_state(),
        "watcher": watcher_status(),
    }


def refresh_index(timeout: float = DEFAULT_INDEX_TIMEOUT) -> dict[str, object]:
    """Run updatedb as the current user into a user-owned database."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("index refresh timeout must be positive")
    if not shutil.which("plocate"):
        raise SearchError("plocate is not installed; install it before refreshing its index")
    updatedb = find_updatedb()
    if not updatedb:
        raise SearchError(
            "no updatedb command is available; install plocate first or use the live find backend"
        )

    database = _user_index_database_path()
    database_parent = os.path.dirname(database)
    try:
        os.makedirs(database_parent, mode=0o700, exist_ok=True)
        os.chmod(database_parent, 0o700)
    except OSError as exc:
        raise SearchError(f"could not prepare the user index directory: {exc}") from exc

    started = time.monotonic()
    try:
        lock_fd = os.open(
            database + ".lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise SearchError(f"could not create the index refresh lock: {exc}") from exc

    try:
        with os.fdopen(lock_fd, "r+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {
                    "status": "already_running",
                    "updatedb": updatedb,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            except OSError as exc:
                raise SearchError(f"could not lock the index refresh file: {exc}") from exc
            try:
                completed = subprocess.run(
                    [updatedb, "--require-visibility", "no", "--output", database],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise SearchTimeout(
                    f"{os.path.basename(updatedb)} exceeded the {timeout:g}s index refresh timeout"
                ) from exc
            except OSError as exc:
                raise SearchError(f"could not run {updatedb}: {exc}") from exc
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        # The lock file is intentionally retained so its inode remains stable
        # between concurrent callers; it contains no user data.
        pass

    stderr = completed.stderr.decode(errors="replace").strip()
    if completed.returncode != 0:
        detail = f": {stderr}" if stderr else ""
        raise SearchError(
            f"{os.path.basename(updatedb)} failed with exit code {completed.returncode}{detail}; "
            "the user-owned index was not updated"
        )
    try:
        os.chmod(database, 0o600)
    except OSError:
        pass
    status = index_status()
    return {
        "status": "updated",
        "updatedb": updatedb,
        "database": database,
        "database_modified": status["database_modified"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def maybe_refresh_index(args: argparse.Namespace) -> dict[str, object] | None:
    """Refresh only when explicitly requested and the index is stale."""
    if not getattr(args, "refresh_if_stale", False):
        return None
    backend = getattr(args, "backend", "auto")
    if backend == "find":
        return {"status": "skipped", "reason": "live find backend selected"}
    if backend == "locate":
        return {"status": "skipped", "reason": "refresh is supported for plocate/auto only"}
    if not shutil.which("plocate"):
        return {"status": "skipped", "reason": "plocate is not installed"}
    if backend == "auto" and getattr(args, "project_path", None) and not getattr(
        args, "global_search", False
    ):
        return {"status": "skipped", "reason": "scoped auto search uses a live scan"}

    max_age = getattr(args, "refresh_max_age", DEFAULT_INDEX_MAX_AGE)
    if max_age <= 0:
        raise SearchError("index refresh max age must be positive")
    status = index_status()
    age = status["age_seconds"]
    if status["database_exists"] and isinstance(age, (int, float)) and age <= max_age:
        return {
            "status": "fresh",
            "database": status["database"],
            "age_seconds": age,
            "max_age_seconds": max_age,
        }

    timeout = getattr(args, "refresh_timeout", DEFAULT_INDEX_TIMEOUT)
    try:
        refreshed = refresh_index(timeout=timeout)
    except (SearchError, SearchTimeout) as exc:
        # A search should remain useful if the optional refresh cannot run as
        # the current user; the response reports the refresh failure.
        return {
            "status": "failed",
            "error": str(exc),
            "database": status["database"],
            "age_seconds": age,
            "max_age_seconds": max_age,
        }
    refreshed["max_age_seconds"] = max_age
    return refreshed


def path_is_within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:
        return False


def classify(path: str) -> Entry | None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    mode = metadata.st_mode
    if stat.S_ISDIR(mode):
        entry_type = "folder"
    elif stat.S_ISREG(mode):
        entry_type = "file"
    else:
        entry_type = "other"
    return Entry(
        path=os.path.abspath(path),
        entry_type=entry_type,
        size=metadata.st_size,
        modified=metadata.st_mtime,
    )


def build_matcher(args: argparse.Namespace):
    query = args.query
    flags = 0 if args.case_sensitive else re.IGNORECASE
    compiled = None
    if args.regex:
        try:
            compiled = re.compile(query, flags)
        except re.error as exc:
            raise SearchError(f"invalid regular expression: {exc}") from exc

    def matches(path: str) -> bool:
        candidate = path if args.match_path else os.path.basename(path.rstrip(os.sep))
        if compiled is not None:
            return compiled.search(candidate) is not None
        if any(char in query for char in "*?["):
            if args.case_sensitive:
                return fnmatch.fnmatchcase(candidate, query)
            return fnmatch.fnmatchcase(candidate.casefold(), query.casefold())
        if args.whole_word:
            return re.search(
                rf"(?<!\w){re.escape(query)}(?!\w)", candidate, flags
            ) is not None
        if args.case_sensitive:
            return query in candidate
        return query.casefold() in candidate.casefold()

    return matches


def indexed_command(binary: str, args: argparse.Namespace) -> list[str]:
    executable = os.path.basename(binary)
    command = [binary, "-0", "-e"]
    if executable == "plocate":
        command.extend(("-d", _index_database_path()))
        command.append("-N")
    if not args.match_path:
        command.append("-b")
    if not args.case_sensitive:
        command.append("-i")
    if args.regex:
        command.append("--regex" if executable == "plocate" else "-r")
    command.extend(("--", args.query))
    return command


def run_indexed(binary: str, args: argparse.Namespace) -> list[str]:
    command = indexed_command(binary, args)
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchTimeout(
            f"{os.path.basename(binary)} exceeded the {args.timeout:g}s timeout"
        ) from exc
    except OSError as exc:
        raise SearchError(f"could not run {binary}: {exc}") from exc

    stderr = completed.stderr.decode(errors="replace").strip()
    if completed.returncode not in (0, 1):
        detail = f": {stderr}" if stderr else ""
        raise SearchError(f"{os.path.basename(binary)} failed with exit code "
                          f"{completed.returncode}{detail}")
    if completed.returncode == 1 and stderr:
        raise SearchError(f"{os.path.basename(binary)} reported an error: {stderr}")
    return [os.fsdecode(raw) for raw in completed.stdout.split(b"\0") if raw]


def iter_filesystem_paths(
    root: str,
    include_virtual: bool,
    deadline: float,
    stats: ScanStats,
) -> Iterator[str]:
    """Yield entries without following directory symlinks."""
    if os.path.isfile(root) or os.path.islink(root):
        yield root
        return

    prune = set() if include_virtual else set(VIRTUAL_ROOTS)

    def onerror(_error: OSError) -> None:
        stats.permission_errors += 1

    for current, directories, files in os.walk(
        root,
        topdown=True,
        onerror=onerror,
        followlinks=False,
    ):
        if time.monotonic() > deadline:
            raise SearchTimeout("filesystem scan exceeded the requested timeout")
        current = os.path.abspath(current)
        retained: list[str] = []
        for directory in directories:
            child = os.path.abspath(os.path.join(current, directory))
            if child in prune:
                continue
            retained.append(directory)
            yield child
        directories[:] = retained
        for filename in files:
            if time.monotonic() > deadline:
                raise SearchTimeout("filesystem scan exceeded the requested timeout")
            yield os.path.abspath(os.path.join(current, filename))


def choose_backend(requested: str) -> tuple[str, str | None]:
    if requested == "find":
        return "find", None
    if requested == "plocate":
        binary = shutil.which("plocate")
        if not binary:
            raise SearchError("plocate is not installed; use --backend find or install it")
        return "plocate", binary
    if requested == "locate":
        binary = shutil.which("locate")
        if not binary:
            raise SearchError("locate is not installed; use --backend find or install it")
        return "locate", binary
    binary = shutil.which("plocate")
    if binary:
        return "plocate", binary
    binary = shutil.which("locate")
    if binary:
        return "locate", binary
    return "find", None


def apply_filters(
    paths: Iterable[str],
    args: argparse.Namespace,
    matcher,
    project_path: str | None,
) -> list[Entry]:
    results: list[Entry] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = normalize_path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if project_path is not None and not path_is_within(path, project_path):
            continue
        if not matcher(path):
            continue
        entry = classify(path)
        if entry is None:
            continue
        if args.type != "any" and entry.entry_type != args.type:
            continue
        if args.min_size is not None and (
            entry.size is None or entry.size < args.min_size
        ):
            continue
        if args.max_size is not None and (
            entry.size is None or entry.size > args.max_size
        ):
            continue
        if args.modified_after is not None and (
            entry.modified is None or entry.modified < args.modified_after
        ):
            continue
        if args.modified_before is not None and (
            entry.modified is None or entry.modified > args.modified_before
        ):
            continue
        results.append(entry)
    return results


def sort_entries(entries: list[Entry], args: argparse.Namespace) -> None:
    def key(entry: Entry):
        if args.sort == "name":
            return (os.path.basename(entry.path).casefold(), entry.path.casefold())
        if args.sort == "size":
            return (entry.size is None, entry.size if entry.size is not None else -1,
                    entry.path.casefold())
        if args.sort == "date_modified":
            return (entry.modified is None,
                    entry.modified if entry.modified is not None else float("-inf"),
                    entry.path.casefold())
        return entry.path.casefold()

    entries.sort(key=key, reverse=args.descending)


def execute_search(args: argparse.Namespace) -> SearchResult:
    started = time.monotonic()
    matcher = build_matcher(args)
    index_refresh = maybe_refresh_index(args)
    project_path = None if args.global_search else (
        normalize_path(args.project_path) if args.project_path else None
    )
    # A scoped auto search should not enumerate the whole index and then
    # discard unrelated paths; scan the requested project directly instead.
    if args.backend == "auto" and project_path is not None:
        backend, binary = "find", None
    else:
        backend, binary = choose_backend(args.backend)
    fallback_reason = None
    permission_errors = 0

    indexed_backends = [(backend, binary)] if backend in {"plocate", "locate"} else []
    if args.backend == "auto" and backend == "plocate":
        locate = shutil.which("locate")
        if locate:
            indexed_backends.append(("locate", locate))
    for backend, binary in indexed_backends:
        try:
            candidates = run_indexed(binary, args)  # type: ignore[arg-type]
            entries = apply_filters(candidates, args, matcher, project_path)
            coverage = "indexed snapshot"
        except SearchError as exc:
            if args.backend != "auto":
                raise
            fallback_reason = "; ".join(filter(None, (fallback_reason, str(exc))))
            backend = "find"
            binary = None
        else:
            sort_entries(entries, args)
            return SearchResult(
                backend=backend,
                coverage=coverage,
                entries=entries,
                fallback_reason=fallback_reason,
                elapsed=time.monotonic() - started,
                index_refresh=index_refresh,
            )

    root = project_path or "/"
    if not os.path.exists(root) and not os.path.islink(root):
        raise SearchError(f"search root does not exist: {root}")
    scan_stats = ScanStats()
    deadline = started + args.timeout
    candidates = iter_filesystem_paths(
        root, args.include_virtual, deadline, scan_stats
    )
    entries = apply_filters(candidates, args, matcher, project_path)
    sort_entries(entries, args)
    permission_errors = scan_stats.permission_errors
    return SearchResult(
        backend="find",
        coverage="live filesystem scan",
        entries=entries,
        elapsed=time.monotonic() - started,
        permission_errors=permission_errors,
        fallback_reason=fallback_reason,
        index_refresh=index_refresh,
    )


def human_size(size: int | None) -> str:
    if size is None:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def local_iso(timestamp: float | None) -> str:
    if timestamp is None:
        return "—"
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def entry_json(entry: Entry) -> dict[str, object]:
    return {
        "path": entry.path,
        "type": entry.entry_type,
        "size": entry.size,
        "modified": local_iso(entry.modified),
    }


def emit(result: SearchResult, args: argparse.Namespace) -> None:
    total = len(result.entries)
    start = min(args.offset, total)
    end = min(start + args.count, total)
    shown = result.entries[start:end]
    exhaustive = result.backend == "find" and result.permission_errors == 0

    if args.format == "json":
        payload = {
            "backend": result.backend,
            "coverage": result.coverage,
            "query": args.query,
            "total": total,
            "offset": args.offset,
            "count": args.count,
            "shown": len(shown),
            "exhaustive": exhaustive,
            "elapsed_seconds": round(result.elapsed, 3),
            "permission_errors": result.permission_errors,
            "fallback_reason": result.fallback_reason,
            "index_refresh": result.index_refresh,
            "results": [entry_json(entry) for entry in shown],
        }
        json.dump(payload, sys.stdout, ensure_ascii=True)
        sys.stdout.write("\n")
        return

    print(f"Backend: {result.backend} ({result.coverage})")
    if result.fallback_reason:
        print(f"Indexed backend unavailable; used find fallback: {result.fallback_reason}")
    print(f"Total matches: {total}")
    print(f"Showing: {start + 1 if shown else 0}-{end} of {total}")
    print(f"Elapsed: {result.elapsed:.2f}s")
    if result.permission_errors:
        print(f"Warning: {result.permission_errors} directories could not be read; results are not exhaustive.")
    if result.backend != "find":
        print("Note: indexed results may omit files added after the last database update.")
    if result.index_refresh is not None:
        print(f"Index refresh: {json.dumps(result.index_refresh, ensure_ascii=True)}")
    for entry in shown:
        print(
            f"{entry.path}\t[{entry.entry_type}]\t{human_size(entry.size)}\t"
            f"{local_iso(entry.modified)}"
        )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Read-only Linux-wide file and folder search."
    )
    cli.add_argument("query", help="substring, glob, or regex to search")
    cli.add_argument("--count", type=int, default=50, help="rows to display (default: 50)")
    cli.add_argument("--offset", type=int, default=0, help="rows to skip (default: 0)")
    cli.add_argument(
        "--sort",
        choices=("name", "path", "size", "date_modified"),
        default="name",
        help="sort field (default: name)",
    )
    cli.add_argument("--descending", action="store_true", help="reverse sort order")
    cli.add_argument(
        "--type",
        choices=("file", "folder", "other", "any"),
        default="any",
        help="restrict entry type",
    )
    cli.add_argument("--regex", action="store_true", help="interpret query as a regular expression")
    cli.add_argument("--case-sensitive", action="store_true", help="use case-sensitive matching")
    cli.add_argument("--whole-word", action="store_true", help="match word boundaries for literal queries")
    cli.add_argument("--match-path", action="store_true", help="match against the full absolute path")
    cli.add_argument(
        "--project-path",
        metavar="PATH",
        help="restrict results to PATH; auto mode uses a scoped find scan",
    )
    cli.add_argument(
        "--global",
        dest="global_search",
        action="store_true",
        help="ignore --project-path and search the whole system",
    )
    cli.add_argument("--min-size", type=parse_size, help="minimum size")
    cli.add_argument("--max-size", type=parse_size, help="maximum size")
    cli.add_argument("--modified-after", type=parse_datetime, help="minimum modification time")
    cli.add_argument("--modified-before", type=parse_datetime, help="maximum modification time")
    cli.add_argument(
        "--backend",
        choices=("auto", "plocate", "locate", "find"),
        default="auto",
        help="search backend (default: auto)",
    )
    cli.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout in seconds")
    cli.add_argument(
        "--refresh-if-stale",
        action="store_true",
        help="refresh a stale plocate index before searching (opt-in; runs as current user)",
    )
    cli.add_argument(
        "--refresh-max-age",
        type=float,
        default=DEFAULT_INDEX_MAX_AGE,
        help="refresh threshold in seconds when --refresh-if-stale is used (default: 86400)",
    )
    cli.add_argument(
        "--refresh-timeout",
        type=float,
        default=DEFAULT_INDEX_TIMEOUT,
        help="maximum seconds for an optional index refresh (default: 900)",
    )
    cli.add_argument("--include-virtual", action="store_true", help="include /proc, /sys, /dev, and /run in find scans")
    cli.add_argument("--format", choices=("text", "json"), default="text")
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = parser().parse_args(argv)
    if not args.query:
        print("error: query cannot be empty", file=sys.stderr)
        return 2
    if args.count < 0 or args.offset < 0:
        print("error: --count and --offset must be non-negative", file=sys.stderr)
        return 2
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return 2
    if not math.isfinite(args.refresh_max_age) or args.refresh_max_age <= 0:
        print("error: --refresh-max-age must be positive", file=sys.stderr)
        return 2
    if not math.isfinite(args.refresh_timeout) or args.refresh_timeout <= 0:
        print("error: --refresh-timeout must be positive", file=sys.stderr)
        return 2
    if args.min_size is not None and args.max_size is not None and args.min_size > args.max_size:
        print("error: --min-size cannot exceed --max-size", file=sys.stderr)
        return 2
    if args.global_search and args.project_path:
        print("warning: --global ignores --project-path", file=sys.stderr)
    try:
        result = execute_search(args)
        emit(result, args)
    except SearchTimeout as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 124
    except SearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
