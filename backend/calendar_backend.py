#!/usr/bin/env python3
"""Bounded JSON interface to khal and vdirsyncer."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EVENTS = 256
MAX_RANGE_DAYS = 366
COMMAND_TIMEOUT_SECONDS = 30
SYNC_TIMEOUT_SECONDS = 300
JSON_FIELDS = (
    "uid",
    "title",
    "start",
    "end",
    "start-long",
    "end-long",
    "location",
    "description",
    "calendar",
    "all-day",
    "status",
)


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CommandError(Exception):
    def __init__(self, command: str, message: str) -> None:
        super().__init__(message)
        self.command = command
        self.message = message


def xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home() / fallback


def paths() -> dict[str, Path]:
    config = xdg_path("XDG_CONFIG_HOME", ".config") / "omarchy-calendar"
    data = xdg_path("XDG_DATA_HOME", ".local/share") / "omarchy-calendar"
    state = xdg_path("XDG_STATE_HOME", ".local/state") / "omarchy-calendar"
    return {
        "config": config,
        "data": data,
        "state": state,
        "khal_config": config / "khal.conf",
        "vdirsyncer_config": config / "vdirsyncer.conf",
        "sync_status": state / "sync-status.json",
    }


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request", "request must be a JSON object")
    return value


def validate_keys(request: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(request) - allowed)
    missing = sorted(required - set(request))
    if unknown:
        raise ProtocolError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise ProtocolError("invalid_request", f"missing fields: {', '.join(missing)}")


def bounded_string(
    request: dict[str, Any], key: str, *, maximum: int, required: bool = False
) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ProtocolError("invalid_request", f"{key} must be a string")
    if required and not value.strip():
        raise ProtocolError("invalid_request", f"{key} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise ProtocolError("invalid_request", f"{key} exceeds {maximum} bytes")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ProtocolError("invalid_request", f"{key} contains a forbidden control character")
    return value


def parse_date(value: str, key: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DD")
    return parsed


def parse_event_time(value: str, key: str, all_day: bool) -> str:
    if all_day:
        return parse_date(value, key).isoformat()
    if len(value) != 16 or value[10] != "T" or value[13] != ":":
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DDTHH:MM")
    parse_date(value[:10], key)
    try:
        hour = int(value[11:13])
        minute = int(value[14:16])
    except ValueError as error:
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DDTHH:MM") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DDTHH:MM")
    return f"{value[:10]} {hour:02d}:{minute:02d}"


def command_argv(executable: str, *arguments: str) -> list[str]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise CommandError(executable, f"{executable} is not installed")
    return [resolved, *arguments]


def run_command(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    assert argv
    assert timeout > 0
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as error:
        raise CommandError(Path(argv[0]).name, f"command timed out after {timeout} seconds") from error
    except OSError as error:
        raise CommandError(Path(argv[0]).name, str(error)) from error
    if len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise CommandError(Path(argv[0]).name, "command output exceeded the response limit")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise CommandError(Path(argv[0]).name, detail[:2048])
    return result


def khal_argv(*arguments: str) -> list[str]:
    config = paths()["khal_config"]
    if not config.is_file():
        raise CommandError("khal", "calendar is not configured; run omarchy-calendar-setup local")
    return command_argv("khal", "-c", str(config), "--no-color", *arguments)


def json_options() -> list[str]:
    result: list[str] = []
    for field in JSON_FIELDS:
        result.extend(("--json", field))
    return result


def normalize_khal_datetime(value: Any, all_day: bool) -> Any:
    if not isinstance(value, str):
        return value
    if all_day:
        return value[:10]
    if len(value) == 16 and value[4] == "-" and value[7] == "-" and value[10] == " ":
        return value[:10] + "T" + value[11:]
    return value


def normalize_event(row: dict[str, Any]) -> dict[str, Any]:
    all_day_value = row.get("all-day")
    if all_day_value not in ("True", "False"):
        raise CommandError("khal", "khal returned an invalid all-day value")
    all_day = all_day_value == "True"
    calendar = row.get("calendar", "")
    event = {
        "uid": row.get("uid", ""),
        "title": row.get("title", ""),
        "start": normalize_khal_datetime(row.get("start-long", row.get("start", "")), all_day),
        "end": normalize_khal_datetime(row.get("end-long", row.get("end", "")), all_day),
        "location": row.get("location", ""),
        "description": row.get("description", ""),
        "calendarId": calendar,
        "calendarName": calendar,
        "status": row.get("status", ""),
        "allDay": all_day,
    }
    if not all(isinstance(value, str) for key, value in event.items() if key != "allDay"):
        raise CommandError("khal", "khal returned an invalid event field")
    return event


def parse_khal_rows(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError as error:
            raise CommandError("khal", "khal returned malformed JSON") from error
        if not isinstance(rows, list):
            raise CommandError("khal", "khal returned an unexpected JSON value")
        for row in rows:
            if not isinstance(row, dict):
                raise CommandError("khal", "khal returned an unexpected event value")
            if row:
                events.append(normalize_event(row))
                if len(events) > MAX_EVENTS:
                    raise ProtocolError("result_too_large", f"result exceeds {MAX_EVENTS} events")
    return events


def request_list(request: dict[str, Any]) -> dict[str, Any]:
    allowed = {"action", "requestId", "start", "end", "calendars"}
    validate_keys(request, allowed, {"action", "start", "end"})
    start_text = bounded_string(request, "start", maximum=10, required=True)
    end_text = bounded_string(request, "end", maximum=10, required=True)
    assert start_text is not None
    assert end_text is not None
    start = parse_date(start_text, "start")
    end = parse_date(end_text, "end")
    days = (end - start).days
    if days <= 0 or days > MAX_RANGE_DAYS:
        raise ProtocolError("invalid_request", f"date range must be 1 to {MAX_RANGE_DAYS} days")

    calendars = request.get("calendars", [])
    if not isinstance(calendars, list) or len(calendars) > 32:
        raise ProtocolError("invalid_request", "calendars must be an array of at most 32 names")
    arguments = ["list", "--once", *json_options()]
    for index, calendar in enumerate(calendars):
        if not isinstance(calendar, str) or not calendar or len(calendar.encode("utf-8")) > 128:
            raise ProtocolError("invalid_request", f"calendars[{index}] is invalid")
        if any(character in calendar for character in "\x00\r\n"):
            raise ProtocolError("invalid_request", f"calendars[{index}] is invalid")
        arguments.extend(("--include-calendar", calendar))
    arguments.extend((start_text, end_text))
    output = run_command(khal_argv(*arguments), COMMAND_TIMEOUT_SECONDS).stdout
    return {"ok": True, "events": parse_khal_rows(output)}


def run_sync() -> dict[str, Any]:
    configured = paths()["vdirsyncer_config"].is_file()
    if not configured:
        return {"attempted": False, "ok": True}
    started = dt.datetime.now(dt.timezone.utc)
    try:
        argv = command_argv("vdirsyncer", "-c", str(paths()["vdirsyncer_config"]), "sync")
        run_command(argv, SYNC_TIMEOUT_SECONDS)
        sync = {"attempted": True, "ok": True, "at": started.isoformat()}
    except CommandError as error:
        sync = {"attempted": True, "ok": False, "at": started.isoformat(), "error": error.message}
    write_sync_status(sync)
    return sync


def write_sync_status(status: dict[str, Any]) -> None:
    status_path = paths()["sync_status"]
    status_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".sync-status-", dir=status_path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(status, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, status_path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def request_create(request: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action", "requestId", "title", "start", "end", "allDay", "calendar",
        "calendarId", "location", "description", "sync",
    }
    validate_keys(request, allowed, {"action", "title", "start", "end"})
    title = bounded_string(request, "title", maximum=512, required=True)
    start_text = bounded_string(request, "start", maximum=16, required=True)
    end_text = bounded_string(request, "end", maximum=16, required=True)
    calendar = bounded_string(request, "calendar", maximum=128)
    calendar_id = bounded_string(request, "calendarId", maximum=128)
    if calendar and calendar_id and calendar != calendar_id:
        raise ProtocolError("invalid_request", "calendar and calendarId must match")
    calendar = calendar or calendar_id
    location = bounded_string(request, "location", maximum=1024)
    description = bounded_string(request, "description", maximum=8192)
    all_day = request.get("allDay", False)
    sync_requested = request.get("sync", True)
    if not isinstance(all_day, bool):
        raise ProtocolError("invalid_request", "allDay must be a boolean")
    if not isinstance(sync_requested, bool):
        raise ProtocolError("invalid_request", "sync must be a boolean")
    assert title is not None
    assert start_text is not None
    assert end_text is not None
    start = parse_event_time(start_text, "start", all_day)
    end = parse_event_time(end_text, "end", all_day)
    if end <= start:
        raise ProtocolError("invalid_request", "end must be after start")

    arguments = ["new"]
    if calendar:
        arguments.extend(("--calendar", calendar))
    if location:
        arguments.extend(("--location", location))
    arguments.extend(json_options())
    arguments.extend((start, end, title))
    if description:
        arguments.extend(("::", description))
    output = run_command(khal_argv(*arguments), COMMAND_TIMEOUT_SECONDS).stdout
    events = parse_khal_rows(output)
    if len(events) != 1:
        raise CommandError("khal", "khal did not confirm the created event")

    # khal writes the vdir first. A failed remote synchronization must not turn
    # a successful local creation into a failed create response.
    sync = run_sync() if sync_requested else {"attempted": False, "ok": True}
    return {"ok": True, "event": events[0], "sync": sync}


def request_calendars(request: dict[str, Any]) -> dict[str, Any]:
    validate_keys(request, {"action", "requestId"}, {"action"})
    output = run_command(khal_argv("printcalendars"), COMMAND_TIMEOUT_SECONDS).stdout
    names = [line.strip() for line in output.splitlines() if line.strip()]
    if len(names) > 128 or any(len(name.encode("utf-8")) > 128 for name in names):
        raise ProtocolError("result_too_large", "calendar result exceeds protocol limits")
    return {
        "ok": True,
        "calendars": [
            {"id": name, "name": name, "writable": True}
            for name in names
        ],
    }


def executable_version(name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved is None:
        return None
    try:
        output = run_command([resolved, "--version"], 5).stdout.strip()
    except CommandError:
        return None
    return output[:256]


def request_status(request: dict[str, Any]) -> dict[str, Any]:
    validate_keys(request, {"action", "requestId"}, {"action"})
    current_paths = paths()
    last_sync: dict[str, Any] | None = None
    try:
        raw = current_paths["sync_status"].read_bytes()
        if len(raw) <= 16 * 1024:
            candidate = json.loads(raw)
            if isinstance(candidate, dict):
                last_sync = candidate
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return {
        "ok": True,
        "configured": current_paths["khal_config"].is_file(),
        "syncConfigured": current_paths["vdirsyncer_config"].is_file(),
        "versions": {"khal": executable_version("khal"), "vdirsyncer": executable_version("vdirsyncer")},
        "lastSync": last_sync,
    }


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("requestId")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id.encode("utf-8")) > 128
    ):
        raise ProtocolError("invalid_request", "requestId must be a string of at most 128 bytes")
    action = request.get("action")
    if not isinstance(action, str):
        raise ProtocolError("invalid_request", "action must be a string")
    handlers = {
        "list": request_list,
        "create": request_create,
        "calendars": request_calendars,
        "status": request_status,
    }
    handler = handlers.get(action)
    if handler is None:
        raise ProtocolError("unknown_action", "action must be list, create, calendars, or status")
    response = handler(request)
    if request_id is not None:
        response["requestId"] = request_id
    return response


def read_request() -> dict[str, Any]:
    # Quickshell keeps the process write channel open, so the protocol is one
    # newline-delimited JSON request rather than an EOF-delimited document.
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
    if not raw.strip():
        raise ProtocolError("invalid_json", "request is empty")
    try:
        return require_object(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProtocolError("invalid_json", "request is not valid UTF-8 JSON") from error


def emit(response: dict[str, Any]) -> None:
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"ok":false,"error":{"code":"response_too_large","message":"response exceeds protocol limit"}}'
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        response = dispatch(read_request())
    except ProtocolError as error:
        response = {"ok": False, "error": {"code": error.code, "message": error.message}}
    except CommandError as error:
        response = {"ok": False, "error": {"code": "command_failed", "message": error.message, "command": error.command}}
    except Exception:  # noqa: BLE001 - stdout must remain valid protocol on every failure.
        response = {"ok": False, "error": {"code": "internal_error", "message": "unexpected backend failure"}}
    emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
