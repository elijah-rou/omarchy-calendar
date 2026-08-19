#!/usr/bin/env python3
"""Bounded read-only JSON interface to subscribed ICS calendars."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import shutil
import signal
import sys
from typing import Any

from backend.subscriptions import (
    SubscriptionError,
    load_subscriptions,
    paths,
    run_bounded,
    terminate_process_group,
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EVENTS = 256
MAX_RANGE_DAYS = 366
COMMAND_TIMEOUT_SECONDS = 30
JSON_FIELDS = ("uid", "title", "start", "end", "start-long", "end-long", "location", "description", "calendar", "all-day", "status")


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_keys(request: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(request) - allowed)
    missing = sorted(required - set(request))
    if unknown:
        raise ProtocolError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise ProtocolError("invalid_request", f"missing fields: {', '.join(missing)}")


def bounded_string(request: dict[str, Any], key: str, maximum: int, *, required: bool = False) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value.encode()) > maximum:
        raise ProtocolError("invalid_request", f"{key} is invalid")
    if any(character in value for character in "\x00\r\n"):
        raise ProtocolError("invalid_request", f"{key} is invalid")
    return value


def parse_date(value: str, key: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ProtocolError("invalid_request", f"{key} must be YYYY-MM-DD")
    return parsed


def json_options() -> list[str]:
    result: list[str] = []
    for field in JSON_FIELDS:
        result.extend(("--json", field))
    return result


def normalize_event(row: dict[str, Any]) -> dict[str, Any]:
    all_day_value = row.get("all-day")
    if all_day_value not in ("True", "False"):
        raise ProtocolError("command_failed", "khal returned an invalid event")
    all_day = all_day_value == "True"
    calendar = row.get("calendar", "")
    start = row.get("start-long", row.get("start", ""))
    end = row.get("end-long", row.get("end", ""))
    if isinstance(start, str) and not all_day and len(start) >= 16:
        start = start[:10] + "T" + start[11:]
    if isinstance(end, str) and not all_day and len(end) >= 16:
        end = end[:10] + "T" + end[11:]
    if all_day and isinstance(end, str):
        try:
            # khal 0.14 reports all-day DTEND as an inclusive display date;
            # the backend and QML use RFC 5545's exclusive-end contract.
            end = (dt.date.fromisoformat(end[:10]) + dt.timedelta(days=1)).isoformat()
        except ValueError as error:
            raise ProtocolError("command_failed", "khal returned an invalid event") from error
    event = {
        "uid": row.get("uid", ""), "title": row.get("title", ""), "start": start[:10] if all_day and isinstance(start, str) else start,
        "end": end[:10] if all_day and isinstance(end, str) else end, "location": row.get("location", ""),
        "description": row.get("description", ""), "calendarId": calendar, "calendarName": calendar,
        "status": row.get("status", ""), "allDay": all_day,
    }
    if not all(isinstance(value, str) for key, value in event.items() if key != "allDay"):
        raise ProtocolError("command_failed", "khal returned an invalid event")
    return event


def parse_khal_rows(output: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        text = output.decode()
    except UnicodeDecodeError as error:
        raise ProtocolError("command_failed", "khal returned invalid output") from error
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProtocolError("command_failed", "khal returned malformed JSON") from error
        if not isinstance(rows, list):
            raise ProtocolError("command_failed", "khal returned malformed JSON")
        for row in rows:
            if not isinstance(row, dict):
                raise ProtocolError("command_failed", "khal returned malformed JSON")
            if row:
                events.append(normalize_event(row))
                if len(events) > MAX_EVENTS:
                    raise ProtocolError("result_too_large", f"result exceeds {MAX_EVENTS} events")
    return events


def request_list(request: dict[str, Any]) -> dict[str, Any]:
    validate_keys(request, {"action", "requestId", "start", "end", "calendars"}, {"action", "requestId", "start", "end"})
    start_text = bounded_string(request, "start", 10, required=True)
    end_text = bounded_string(request, "end", 10, required=True)
    assert start_text is not None and end_text is not None
    days = (parse_date(end_text, "end") - parse_date(start_text, "start")).days + 1
    if days <= 0 or days > MAX_RANGE_DAYS:
        raise ProtocolError("invalid_request", f"inclusive date range must be 1 to {MAX_RANGE_DAYS} days")
    subscriptions = load_subscriptions()
    requested = request.get("calendars", [])
    if not isinstance(requested, list) or len(requested) > 16:
        raise ProtocolError("invalid_request", "calendars must be an array of at most 16 IDs")
    known = {item["id"] for item in subscriptions}
    for index, calendar in enumerate(requested):
        if not isinstance(calendar, str) or calendar not in known:
            raise ProtocolError("invalid_request", f"calendars[{index}] is invalid")
    if not subscriptions:
        return {"ok": True, "events": []}
    khal = shutil.which("khal")
    if khal is None:
        raise ProtocolError("dependency_missing", "khal is not installed")
    arguments = [khal, "-c", str(paths()["khal_config"]), "--no-color", "list", *json_options()]
    for calendar in requested:
        arguments.extend(("--include-calendar", calendar))
    arguments.extend((start_text, end_text))
    try:
        output = run_bounded(arguments, timeout=COMMAND_TIMEOUT_SECONDS, output_limit=MAX_RESPONSE_BYTES)
    except SubscriptionError as error:
        raise ProtocolError(error.code, error.message) from error
    events = parse_khal_rows(output)
    calendar_metadata = {item["id"]: item for item in subscriptions}
    for event in events:
        metadata = calendar_metadata.get(event["calendarId"])
        if metadata is not None:
            event["calendarName"] = metadata["name"]
            if "color" in metadata:
                event["color"] = metadata["color"]
    return {"ok": True, "events": events}


def request_calendars(request: dict[str, Any]) -> dict[str, Any]:
    validate_keys(request, {"action", "requestId"}, {"action", "requestId"})
    return {"ok": True, "calendars": [{"id": item["id"], "name": item["name"], "writable": False, **({"color": item["color"]} if "color" in item else {})} for item in load_subscriptions()]}


def executable_version(name: str) -> str | None:
    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        return run_bounded([executable, "--version"], timeout=5, output_limit=4096).decode(errors="replace").strip()[:256]
    except SubscriptionError:
        return None


def last_refresh() -> dict[str, Any] | None:
    try:
        raw = paths()["status"].read_bytes()
        value = json.loads(raw) if len(raw) <= 64 * 1024 else None
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    # Status is written only from sanitized IDs, codes, and fixed messages.
    return value


def request_status(request: dict[str, Any]) -> dict[str, Any]:
    validate_keys(request, {"action", "requestId"}, {"action", "requestId"})
    subscriptions = load_subscriptions()
    try:
        icalendar_version = importlib.metadata.version("icalendar")
    except importlib.metadata.PackageNotFoundError:
        icalendar_version = None
    return {
        "ok": True, "configured": True, "readOnly": True,
        "subscriptionCount": len(subscriptions), "subscriptions": subscriptions,
        "versions": {"khal": executable_version("khal"), "python-icalendar": icalendar_version},
        "lastRefresh": last_refresh(),
    }


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("requestId")
    if not isinstance(request_id, str) or not request_id or len(request_id.encode()) > 128:
        raise ProtocolError("invalid_request", "requestId must be a nonempty string of at most 128 bytes")
    action = request.get("action")
    handlers = {"list": request_list, "calendars": request_calendars, "status": request_status}
    if action in {"create", "update", "delete"}:
        raise ProtocolError("read_only", "calendar subscriptions are read-only")
    if not isinstance(action, str) or action not in handlers:
        raise ProtocolError("unknown_action", "action must be list, calendars, or status")
    response = handlers[action](request)
    response["requestId"] = request_id
    return response


def encode_response(response: dict[str, Any]) -> bytes:
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return encoded
    fallback: dict[str, Any] = {"ok": False, "error": {"code": "response_too_large", "message": "response exceeds protocol limit"}}
    if isinstance(response.get("requestId"), str):
        fallback["requestId"] = response["requestId"]
    return json.dumps(fallback, separators=(",", ":")).encode()


def main() -> int:
    request: dict[str, Any] = {}
    response: dict[str, Any]
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ProtocolError("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProtocolError("invalid_json", "request is not valid UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise ProtocolError("invalid_request", "request must be a JSON object")
        request = value
        response = dispatch(request)
    except (ProtocolError, SubscriptionError) as error:
        response = {"ok": False, "error": {"code": error.code, "message": error.message}}
    except Exception:  # noqa: BLE001 - protocol output must remain valid on every failure.
        response = {"ok": False, "error": {"code": "internal_error", "message": "unexpected backend failure"}}
    request_id = request.get("requestId")
    if isinstance(request_id, str) and len(request_id.encode()) <= 128:
        response.setdefault("requestId", request_id)
    sys.stdout.buffer.write(encode_response(response) + b"\n")
    return 0


if __name__ == "__main__":
    def stop(signum: int, _frame: Any) -> None:
        from backend import subscriptions
        if subscriptions.active_process is not None:
            terminate_process_group(subscriptions.active_process)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(main())
