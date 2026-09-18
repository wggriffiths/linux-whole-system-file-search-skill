#!/usr/bin/env python3
"""MCP stdio adapter for the Linux whole-system file search skill.

The adapter implements the MCP JSON-RPC stdio transport with the Python
standard library. Keeping transport I/O synchronous avoids requiring an async
runtime just to expose the read-only search and watcher-status tools to an MCP
client.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Literal


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import search  # noqa: E402  (the bundled module lives beside this adapter)


SERVER_NAME = "file-search"
SERVER_VERSION = "0.5.0"
TOOL_NAME = "search_files"
INDEX_STATUS_TOOL_NAME = "index_status"
WATCHER_STATUS_TOOL_NAME = "watcher_status"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
LATEST_PROTOCOL_VERSION = "2025-11-25"

TOOL_DESCRIPTION = (
    "Search the Linux filesystem for files and folders by name, path, type, "
    "size, or modification time. Results are read-only metadata."
)

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Basename substring, shell glob, or regular expression.",
            },
            "count": {
                "type": "integer",
                "minimum": 0,
                "default": 50,
                "description": "Maximum number of result rows to return.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Number of sorted matches to skip.",
            },
            "sort": {
                "type": "string",
                "enum": ["name", "path", "size", "date_modified"],
                "default": "name",
            },
            "descending": {"type": "boolean", "default": False},
            "entry_type": {
                "type": "string",
                "enum": ["file", "folder", "other", "any"],
                "default": "any",
                "description": "Restrict results to a filesystem entry type.",
            },
            "regex": {"type": "boolean", "default": False},
            "case_sensitive": {"type": "boolean", "default": False},
            "whole_word": {"type": "boolean", "default": False},
            "match_path": {"type": "boolean", "default": False},
            "project_path": {
                "type": "string",
                "description": "Optional directory to search instead of the whole system.",
            },
            "global_search": {
                "type": "boolean",
                "default": False,
                "description": "Ignore project_path and search from /.",
            },
            "min_size": {"type": "string", "description": "Minimum size, e.g. 10MiB."},
            "max_size": {"type": "string", "description": "Maximum size, e.g. 2GiB."},
            "modified_after": {"type": "string", "description": "ISO date/time lower bound."},
            "modified_before": {"type": "string", "description": "ISO date/time upper bound."},
            "backend": {
                "type": "string",
                "enum": ["auto", "plocate", "locate", "find"],
                "default": "auto",
            },
            "timeout": {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": search.DEFAULT_TIMEOUT,
            },
            "include_virtual": {
                "type": "boolean",
                "default": False,
                "description": "Include /proc, /sys, /dev, and /run in live scans.",
            },
        },
        "required": ["query"],
    },
    "annotations": {
        "title": "Search Linux files and folders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}

INDEX_STATUS_TOOL_DEFINITION: dict[str, Any] = {
    "name": INDEX_STATUS_TOOL_NAME,
    "description": "Report plocate, updatedb, database, and timer availability without changing the system.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {
        "title": "Check Linux file index status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}

WATCHER_STATUS_TOOL_DEFINITION: dict[str, Any] = {
    "name": WATCHER_STATUS_TOOL_NAME,
    "description": (
        "Report the optional live file watcher state, including the most recent "
        "changed paths, recent event history, and index refresh. This is read-only."
    ),
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {
        "title": "Check live file watcher",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


def _parse_size(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return search.parse_size(value)
    except argparse.ArgumentTypeError as exc:
        raise ValueError(str(exc)) from exc


def _parse_datetime(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return search.parse_datetime(value)
    except argparse.ArgumentTypeError as exc:
        raise ValueError(str(exc)) from exc


def search_files(
    query: str,
    count: int = 50,
    offset: int = 0,
    sort: Literal["name", "path", "size", "date_modified"] = "name",
    descending: bool = False,
    entry_type: Literal["file", "folder", "other", "any"] = "any",
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    match_path: bool = False,
    project_path: str | None = None,
    global_search: bool = False,
    min_size: str | None = None,
    max_size: str | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    backend: Literal["auto", "plocate", "locate", "find"] = "auto",
    timeout: float = search.DEFAULT_TIMEOUT,
    include_virtual: bool = False,
) -> dict[str, Any]:
    """Search for matching filesystem entries and return structured metadata."""
    values = locals()
    for name, definition in TOOL_DEFINITION["inputSchema"]["properties"].items():
        value = values[name]
        if value is None and name in {"project_path", "min_size", "max_size", "modified_after", "modified_before"}:
            continue
        kind = definition["type"]
        valid = {
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": type(value) is int,
            "number": type(value) in (int, float),
        }[kind]
        if not valid:
            raise ValueError(f"{name} must be a {kind}")
        if "enum" in definition and value not in definition["enum"]:
            raise ValueError(f"invalid {name}: {value!r}")
    if not math.isfinite(timeout):
        raise ValueError("timeout must be finite")
    if not query:
        raise ValueError("query cannot be empty")
    if count < 0 or offset < 0:
        raise ValueError("count and offset must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    parsed_min_size = _parse_size(min_size)
    parsed_max_size = _parse_size(max_size)
    if (
        parsed_min_size is not None
        and parsed_max_size is not None
        and parsed_min_size > parsed_max_size
    ):
        raise ValueError("min_size cannot exceed max_size")

    args = argparse.Namespace(
        query=query,
        count=count,
        offset=offset,
        sort=sort,
        descending=descending,
        type=entry_type,
        regex=regex,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        match_path=match_path,
        project_path=project_path,
        global_search=global_search,
        min_size=parsed_min_size,
        max_size=parsed_max_size,
        modified_after=_parse_datetime(modified_after),
        modified_before=_parse_datetime(modified_before),
        backend=backend,
        timeout=timeout,
        include_virtual=include_virtual,
        format="json",
    )

    try:
        result = search.execute_search(args)
    except search.SearchError as exc:
        raise ValueError(str(exc)) from exc

    total = len(result.entries)
    start = min(offset, total)
    end = min(start + count, total)
    shown = result.entries[start:end]
    return {
        "backend": result.backend,
        "coverage": result.coverage,
        "query": query,
        "total": total,
        "offset": offset,
        "count": count,
        "shown": len(shown),
        "exhaustive": result.backend == "find" and result.permission_errors == 0,
        "elapsed_seconds": round(result.elapsed, 3),
        "permission_errors": result.permission_errors,
        "fallback_reason": result.fallback_reason,
        "results": [search.entry_json(entry) for entry in shown],
    }


def index_status() -> dict[str, Any]:
    """Return local index availability and freshness information."""
    return search.index_status()


def watcher_status() -> dict[str, Any]:
    """Return the live watcher state and its recent event history."""
    return search.watcher_status()


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one MCP JSON-RPC message; return None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(request_id, -32600, "invalid JSON-RPC request")
    if is_notification:
        return None
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object")

    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "ping":
        return None if is_notification else _response(request_id, {})
    if method == "initialize":
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            return _error(request_id, -32602, "protocolVersion must be a string")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return _response(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use search_files to locate Linux files and folders. "
                    "index_status and watcher_status are read-only; the optional "
                    "background watcher maintains the index."
                ),
            },
        )
    if method == "tools/list":
        return None if is_notification else _response(
            request_id,
            {
                "tools": [
                    TOOL_DEFINITION,
                    INDEX_STATUS_TOOL_DEFINITION,
                    WATCHER_STATUS_TOOL_DEFINITION,
                ]
            },
        )
    if method == "tools/call":
        if is_notification:
            return None
        if not isinstance(params, dict):
            return _error(request_id, -32602, "tools/call params must be an object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name not in {
            TOOL_NAME,
            INDEX_STATUS_TOOL_NAME,
            WATCHER_STATUS_TOOL_NAME,
        }:
            return _error(request_id, -32602, f"unknown tool: {tool_name!r}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _response(
                request_id,
                _tool_error("arguments must be an object"),
            )
        try:
            if tool_name == TOOL_NAME:
                payload = search_files(**arguments)
            elif tool_name == INDEX_STATUS_TOOL_NAME:
                payload = index_status(**arguments)
            else:
                payload = watcher_status(**arguments)
        except (TypeError, ValueError, OverflowError, search.SearchError) as exc:
            return _response(request_id, _tool_error(str(exc)))
        return _response(request_id, _tool_result(payload))

    if is_notification:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def run() -> None:
    """Serve newline-delimited MCP JSON-RPC messages over stdin/stdout."""
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                response = _error(None, -32600, "JSON-RPC message must be an object")
            else:
                response = dispatch(message)
        except (json.JSONDecodeError, ValueError) as exc:
            response = _error(None, -32700, f"invalid JSON-RPC message: {exc}")
        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    run()
