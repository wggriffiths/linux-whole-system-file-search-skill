#!/usr/bin/env python3
"""Debounced inotify watcher for the user-owned plocate database."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import json
import logging
import math
import os
import select
import signal
import struct
import sys
import tempfile
import time
from collections.abc import Iterator

import search


LOGGER = logging.getLogger("linux_file_search.index_watcher")

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_ONESHOT = 0x80000000

IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000

WATCH_MASK = (
    IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)
CHANGE_MASK = WATCH_MASK
DIRECTORY_CHANGE_MASK = IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
EVENT_HEADER = struct.Struct("iIII")
EVENT_HISTORY_LIMIT = 100
INOTIFY_MASK_NAMES = (
    (IN_ACCESS, "IN_ACCESS"),
    (IN_MODIFY, "IN_MODIFY"),
    (IN_ATTRIB, "IN_ATTRIB"),
    (IN_CLOSE_WRITE, "IN_CLOSE_WRITE"),
    (IN_CLOSE_NOWRITE, "IN_CLOSE_NOWRITE"),
    (IN_OPEN, "IN_OPEN"),
    (IN_MOVED_FROM, "IN_MOVED_FROM"),
    (IN_MOVED_TO, "IN_MOVED_TO"),
    (IN_CREATE, "IN_CREATE"),
    (IN_DELETE, "IN_DELETE"),
    (IN_DELETE_SELF, "IN_DELETE_SELF"),
    (IN_MOVE_SELF, "IN_MOVE_SELF"),
    (IN_UNMOUNT, "IN_UNMOUNT"),
    (IN_Q_OVERFLOW, "IN_Q_OVERFLOW"),
    (IN_IGNORED, "IN_IGNORED"),
    (IN_ISDIR, "IN_ISDIR"),
    (IN_ONESHOT, "IN_ONESHOT"),
)


def mask_human(mask: int) -> str:
    """Return symbolic names for an inotify mask, including combined flags."""
    names = [name for bit, name in INOTIFY_MASK_NAMES if mask & bit]
    return "|".join(names) if names else f"0x{mask:x}"


def normalize_event_history(raw_events: object) -> list[dict[str, object]]:
    """Keep event history JSON-safe and backfill human-readable mask labels."""
    if not isinstance(raw_events, list):
        return []
    normalized: list[dict[str, object]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        raw_paths = event.get("paths")
        if isinstance(raw_paths, list):
            paths: list[dict[str, object]] = []
            for raw_path in raw_paths:
                if not isinstance(raw_path, dict):
                    continue
                path = dict(raw_path)
                mask = path.get("mask")
                if isinstance(mask, int) and not isinstance(mask, bool):
                    path["mask_human"] = mask_human(mask)
                paths.append(path)
            event["paths"] = paths
        normalized.append(event)
    return normalized[-EVENT_HISTORY_LIMIT:]

# plocate indexes file and directory names, not file contents or metadata.
# Watch namespace changes only; watching attribute/access events would cause
# needless refreshes whenever a program merely reads or chmods a file.

# These trees are excluded by the bundled updatedb.conf or are volatile virtual
# filesystems. Watching them only creates noise and cannot improve the
# corresponding database.
USER_CACHE_ROOT = search._user_cache_home()
USER_CONFIG_ROOT = os.path.abspath(
    os.path.expanduser(
        os.environ.get("XDG_CONFIG_HOME", "").strip()
        or os.path.join("~", ".config")
    )
)
PRUNED_ROOTS = frozenset(
    (
        "/proc",
        "/sys",
        "/dev",
        "/run",
        "/tmp",
        "/var/spool",
        "/media",
        "/var/lib/os-prober",
        "/var/lib/ceph",
        "/home/.ecryptfs",
        "/var/lib/schroot",
        os.path.join(USER_CACHE_ROOT, "chromium"),
        os.path.join(USER_CONFIG_ROOT, "chromium"),
    )
)

TRANSIENT_EVENT_PREFIXES = (
    ".channel_directory_",
    ".gateway_",
    ".hb_",
    ".mcp_schema_cache_",
    ".watcher-state.",
    "systemd-private-",
    "etilqs_",
)
TRANSIENT_EVENT_SUFFIXES = (
    ".db-wal",
    ".db-shm",
    ".sqlite-wal",
    ".sqlite-shm",
    "-wal",
    "-shm",
    "-journal",
    ".lock",
)
TRANSIENT_EVENT_NAMES = frozenset(
    (
        "channel_directory.json",
        "gateway.heartbeat",
        "gateway-starts.tmp",
        "gateway-starts.log",
        "gateway.lock",
        "mcp_schema_cache.json",
        "ticker_heartbeat",
        "ticker_last_success",
    )
)
HERMES_VOLATILE_EVENT_PREFIXES = (
    "gateway.loop-tick.",
    ".bundled_manifest_",
    ".gateway.lifecycle_",
    ".pt_keys.",
)
HERMES_VOLATILE_EVENT_NAMES = frozenset(
    (
        "gateway_state.json",
        "gateway.pid",
        "gateway.sock",
        "gateway.lifecycle.json",
        "plugin_toolset_keys.json",
        ".bundled_manifest",
    )
)


def _is_transient_event(path: str) -> bool:
    """Ignore temporary/index-maintenance files, not final renames."""
    normalized = os.path.normpath(path)
    database = os.path.normpath(search._user_index_database_path())
    if normalized == database:
        return True
    database_name = os.path.basename(database)
    if (
        os.path.dirname(normalized) == os.path.dirname(database)
        and os.path.basename(normalized).startswith(database_name + ".")
    ):
        return True
    state_file = os.path.normpath(search.watcher_state_path())
    state_name = os.path.basename(state_file)
    if normalized == state_file or (
        os.path.dirname(normalized) == os.path.dirname(state_file)
        and os.path.basename(normalized).startswith("." + state_name + ".")
    ):
        return True
    name = os.path.basename(normalized)
    hermes_root = os.path.normpath(
        os.environ.get("HERMES_ROOT")
        or os.environ.get("HERMES_HOME")
        or os.path.join(os.path.expanduser("~"), ".hermes")
    )
    if normalized.startswith(hermes_root + os.sep) and (
        name in HERMES_VOLATILE_EVENT_NAMES
        or name.startswith(HERMES_VOLATILE_EVENT_PREFIXES)
    ):
        return True
    if name in TRANSIENT_EVENT_NAMES:
        return True
    if "__pycache__" in normalized.split(os.sep) or ".pyc" in name:
        return True
    if name.startswith(
        (".watcher-state.", "systemd-private-", "etilqs_", ".org.chromium.Chromium.")
    ):
        return True
    return (
        (name.endswith(".tmp") and name.startswith(TRANSIENT_EVENT_PREFIXES))
        or name.endswith(TRANSIENT_EVENT_SUFFIXES)
    )


def _load_libc() -> ctypes.CDLL:
    library = ctypes.util.find_library("c") or None
    libc = ctypes.CDLL(library, use_errno=True)
    if not hasattr(libc, "inotify_init1") or not hasattr(libc, "inotify_add_watch"):
        raise RuntimeError("the running Linux kernel/libc has no inotify support")
    return libc


def _is_pruned(path: str) -> bool:
    normalized = os.path.normpath(path)
    return any(normalized == root or normalized.startswith(root + os.sep) for root in PRUNED_ROOTS)


class InotifyWatcher:
    """Maintain recursive directory watches and coalesce filesystem events."""

    def __init__(
        self,
        root: str,
        quiet_period: float,
        max_delay: float,
        refresh_timeout: float,
    ) -> None:
        self.root = os.path.abspath(os.path.normpath(os.path.expanduser(root)))
        self.quiet_period = quiet_period
        self.max_delay = max_delay
        self.refresh_timeout = refresh_timeout
        self.libc = _load_libc()
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = -1
        self.wd_to_path: dict[int, str] = {}
        self.path_to_wd: dict[str, int] = {}
        self.rescan_requested = False
        self.stop_requested = False
        self.last_event_details: list[str] = []
        self.last_event_paths: list[dict[str, object]] = []
        self.state_file = search.watcher_state_path()
        self.state = self._load_state()
        events = normalize_event_history(self.state.get("events"))
        self.state.update(
            {
                "schema_version": 1,
                "event_history_limit": EVENT_HISTORY_LIMIT,
                "events": events[-EVENT_HISTORY_LIMIT:],
                "pid": os.getpid(),
                "watch_root": self.root,
                "quiet_period_seconds": self.quiet_period,
                "max_delay_seconds": self.max_delay,
                "refresh_timeout_seconds": self.refresh_timeout,
                "running": False,
                "pending_refresh": False,
                "last_event": None,
                "refresh_started_at": None,
                "stopped_at": None,
                "watch_count": 0,
                "started_at": search.local_iso(time.time()),
            }
        )
        self._write_state()
        self._open()

    def _load_state(self) -> dict[str, object]:
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                state = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def _write_state(self) -> None:
        parent = os.path.dirname(self.state_file)
        temporary = None
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700)
            fd, temporary = tempfile.mkstemp(prefix=".watcher-state.", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
            temporary = None
        except OSError as exc:
            LOGGER.warning("could not write watcher state %s: %s", self.state_file, exc)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _update_state(self, **updates: object) -> None:
        self.state.update(updates)
        self._write_state()

    def _open(self) -> None:
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self.wd_to_path.clear()
        self.path_to_wd.clear()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        self.wd_to_path.clear()
        self.path_to_wd.clear()

    def _directory_paths(self, start: str) -> Iterator[str]:
        pending = [start]
        while pending:
            path = pending.pop()
            if _is_pruned(path) or not os.path.isdir(path):
                continue
            yield path
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False) and not _is_pruned(entry.path):
                            pending.append(entry.path)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.ENOENT, errno.ENOTDIR):
                    LOGGER.debug("cannot enumerate %s: %s", path, exc)

    def add_watch(self, path: str) -> bool:
        path = os.path.abspath(os.path.normpath(path))
        if _is_pruned(path) or path in self.path_to_wd or not os.path.isdir(path):
            return False
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            error = ctypes.get_errno()
            if error == errno.ENOSPC:
                LOGGER.warning("inotify watch limit reached while adding %s", path)
            elif error not in (errno.EACCES, errno.ENOENT, errno.ENOTDIR):
                LOGGER.debug("cannot watch %s: %s", path, os.strerror(error))
            return False
        previous = self.wd_to_path.get(wd)
        if previous is not None:
            self.path_to_wd.pop(previous, None)
        self.wd_to_path[wd] = path
        self.path_to_wd[path] = wd
        return True

    def add_tree(self, start: str) -> int:
        added = 0
        for path in self._directory_paths(start):
            added += int(self.add_watch(path))
        return added

    def rebuild_watches(self) -> None:
        self.close()
        self._open()
        added = self.add_tree(self.root)
        self.rescan_requested = False
        LOGGER.info("watching %s (%d directories)", self.root, added)

    def _read_events(self) -> list[tuple[int, int, str]]:
        try:
            data = os.read(self.fd, 1024 * 1024)
        except BlockingIOError:
            return []
        events: list[tuple[int, int, str]] = []
        offset = 0
        while offset + EVENT_HEADER.size <= len(data):
            wd, mask, _cookie, name_length = EVENT_HEADER.unpack_from(data, offset)
            offset += EVENT_HEADER.size
            raw_name = data[offset : offset + name_length]
            offset += name_length
            name = os.fsdecode(raw_name.split(b"\0", 1)[0]) if name_length else ""
            events.append((wd, mask, name))
        return events

    def handle_events(self) -> bool:
        changed = False
        self.last_event_details = []
        self.last_event_paths = []
        for wd, mask, name in self._read_events():
            base = self.wd_to_path.get(wd)
            event_path = os.path.join(base, name) if base and name else base
            if event_path is not None and _is_transient_event(event_path):
                continue
            if event_path is not None and len(self.last_event_details) < 4:
                self.last_event_details.append(f"{event_path} (0x{mask:x})")
                self.last_event_paths.append(
                    {
                        "path": event_path,
                        "mask": mask,
                        "mask_human": mask_human(mask),
                    }
                )
            if mask & IN_Q_OVERFLOW:
                LOGGER.warning("inotify queue overflowed; a full index refresh will repair state")
                self.rescan_requested = True
                changed = True
                continue

            if mask & IN_IGNORED:
                if base is not None:
                    self.wd_to_path.pop(wd, None)
                    self.path_to_wd.pop(base, None)
                self.rescan_requested = True
                changed = True
                continue
            if base is None:
                self.rescan_requested = True
                changed = True
                continue

            path = os.path.join(base, name) if name else base
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                self.add_tree(path)
            if mask & (IN_DELETE_SELF | IN_MOVE_SELF):
                self.rescan_requested = True
            if mask & IN_ISDIR and mask & DIRECTORY_CHANGE_MASK:
                self.rescan_requested = True
            if mask & CHANGE_MASK:
                changed = True
        return changed

    def refresh(self) -> bool:
        LOGGER.info("filesystem changes settled; refreshing user-owned index")
        self._update_state(
            pending_refresh=True,
            refresh_started_at=search.local_iso(time.time()),
        )
        try:
            result = search.refresh_index(timeout=self.refresh_timeout)
        except (search.SearchError, search.SearchTimeout) as exc:
            LOGGER.error("user-owned index refresh failed: %s", exc)
            self._update_state(
                pending_refresh=True,
                last_refresh={
                    "status": "error",
                    "completed_at": search.local_iso(time.time()),
                    "error": str(exc),
                },
            )
            return False
        LOGGER.info(
            "index refreshed in %.1fs: %s",
            float(result.get("elapsed_seconds", 0.0)),
            result.get("database", search._user_index_database_path()),
        )
        refresh_result = dict(result)
        refresh_result["completed_at"] = search.local_iso(time.time())
        succeeded = result.get("status") == "updated"
        updates = {"pending_refresh": not succeeded, "last_refresh": refresh_result}
        if succeeded:
            updates["last_successful_refresh"] = refresh_result
        self._update_state(**updates)
        return succeeded

    def run(self) -> int:
        if not os.path.isdir(self.root):
            LOGGER.error("watch root does not exist or is not a directory: %s", self.root)
            return 2
        self._update_state(running=True, pending_refresh=False, watch_count=0)
        try:
            added = self.add_tree(self.root)
            LOGGER.info("watching %s (%d directories)", self.root, added)
            self._update_state(watch_count=len(self.wd_to_path))
            poller = select.poll()
            poller.register(self.fd, select.POLLIN)
            dirty_since: float | None = None
            last_event: float | None = None
            last_event_log: float | None = None
            while not self.stop_requested:
                now = time.monotonic()
                wait_seconds = 1.0
                if last_event is not None and dirty_since is not None:
                    quiet_due = last_event + self.quiet_period
                    maximum_due = dirty_since + self.max_delay
                    wait_seconds = max(0.0, min(quiet_due, maximum_due) - now)
                poller.poll(max(1, int(wait_seconds * 1000)))
                changed = self.handle_events()
                if changed:
                    now = time.monotonic()
                    was_dirty = dirty_since is not None
                    dirty_since = dirty_since or now
                    last_event = now
                    if not was_dirty or last_event_log is None or now - last_event_log >= 30:
                        details = "; ".join(self.last_event_details) or "(event details unavailable)"
                        LOGGER.info("filesystem change detected; refresh debounce active: %s", details)
                        last_event_log = now
                    event = {
                        "detected_at": search.local_iso(time.time()),
                        "paths": list(self.last_event_paths),
                    }
                    events = normalize_event_history(self.state.get("events"))
                    events = [*events, event][-EVENT_HISTORY_LIMIT:]
                    self._update_state(
                        events=events,
                        pending_refresh=True,
                        last_event=event,
                        watch_count=len(self.wd_to_path),
                    )

                if last_event is None or dirty_since is None:
                    continue
                now = time.monotonic()
                quiet_due = now - last_event >= self.quiet_period
                maximum_due = now - dirty_since >= self.max_delay
                if quiet_due or maximum_due:
                    succeeded = self.refresh()
                    dirty_since = None if succeeded else time.monotonic()
                    last_event = dirty_since
                    last_event_log = None
                    if self.rescan_requested:
                        poller.unregister(self.fd)
                        self.rebuild_watches()
                        poller.register(self.fd, select.POLLIN)
                        self._update_state(watch_count=len(self.wd_to_path))
        finally:
            self._update_state(
                running=False,
                pending_refresh=False,
                stopped_at=search.local_iso(time.time()),
            )
            self.close()
        return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/", help="directory tree to watch (default: /)")
    parser.add_argument(
        "--quiet-period",
        type=float,
        default=30.0,
        help="seconds without events before refreshing (default: 30)",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=300.0,
        help="maximum seconds to wait during continuous activity (default: 300)",
    )
    parser.add_argument(
        "--refresh-timeout",
        type=float,
        default=search.DEFAULT_INDEX_TIMEOUT,
        help="maximum seconds for updatedb (default: 900)",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or value <= 0 for value in
           (args.quiet_period, args.max_delay, args.refresh_timeout)):
        parser.error("timing values must be positive")
    if args.max_delay < args.quiet_period:
        parser.error("--max-delay must be at least --quiet-period")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s index-watcher: %(message)s",
    )
    if os.geteuid() == 0:
        LOGGER.error("run the watcher as a normal user, not root")
        return 2
    watcher = InotifyWatcher(
        root=args.root,
        quiet_period=args.quiet_period,
        max_delay=args.max_delay,
        refresh_timeout=args.refresh_timeout,
    )

    def stop(_signum: int, _frame: object) -> None:
        watcher.stop_requested = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return watcher.run()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        LOGGER.error("cannot start inotify watcher: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
