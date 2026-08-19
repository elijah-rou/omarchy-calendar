"""Private, bounded read-only ICS subscription storage and refresh."""

from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
import secrets
import selectors
import shutil
import signal
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SUBSCRIPTIONS = 16
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_EVENTS_PER_FEED = 10_000
MAX_COMPONENTS_PER_FEED = 20_000
MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_DATA_BYTES = 256 * 1024 * 1024
MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 30
KHAL_TIMEOUT_SECONDS = 120
SECRET_TIMEOUT_SECONDS = 10
MAX_COMMAND_OUTPUT = 64 * 1024
Progress = Callable[[str, dict[str, Any]], None]
active_process: subprocess.Popen[bytes] | None = None


class SubscriptionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
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
        "metadata": config / "subscriptions.json",
        "khal_config": config / "khal.conf",
        "calendars": data / "calendars",
        "status": state / "sync-status.json",
        "lock": state / "operation.lock",
    }


def installation_namespace() -> str:
    current = paths()
    material = "\n".join((str(current["config"]), str(current["data"]), str(current["state"])))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def secret_attributes(subscription_id: str) -> list[str]:
    return [
        "service", "omarchy-calendar",
        "purpose", "ics-subscription",
        "installation", installation_namespace(),
        "subscription", subscription_id,
    ]


def ensure_private_directories() -> None:
    current = paths()
    for key in ("config", "data", "state", "calendars"):
        current[key].mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(current[key], 0o700)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def metadata_bytes(subscriptions: list[dict[str, str]]) -> bytes:
    return (json.dumps({"subscriptions": subscriptions}, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def load_subscriptions() -> list[dict[str, str]]:
    path = paths()["metadata"]
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise SubscriptionError("storage_failed", "subscription metadata could not be read") from error
    if len(raw) > 64 * 1024:
        raise SubscriptionError("storage_failed", "subscription metadata exceeds its limit")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SubscriptionError("storage_failed", "subscription metadata is invalid") from error
    if not isinstance(document, dict) or set(document) != {"subscriptions"}:
        raise SubscriptionError("storage_failed", "subscription metadata is invalid")
    values = document["subscriptions"]
    if not isinstance(values, list) or len(values) > MAX_SUBSCRIPTIONS:
        raise SubscriptionError("storage_failed", "subscription metadata is invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or not set(value).issubset({"id", "name", "color"}):
            raise SubscriptionError("storage_failed", "subscription metadata is invalid")
        subscription_id = value.get("id")
        name = value.get("name")
        color = value.get("color")
        if not isinstance(subscription_id, str) or len(subscription_id) != 32 or any(c not in "0123456789abcdef" for c in subscription_id):
            raise SubscriptionError("storage_failed", "subscription metadata is invalid")
        if subscription_id in seen or not isinstance(name, str) or not name:
            raise SubscriptionError("storage_failed", "subscription metadata is invalid")
        if color is not None and not isinstance(color, str):
            raise SubscriptionError("storage_failed", "subscription metadata is invalid")
        item = {"id": subscription_id, "name": name}
        if color is not None:
            item["color"] = color
        result.append(item)
        seen.add(subscription_id)
    return result


def quote_config(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def khal_config(subscriptions: list[dict[str, str]], calendars_root: Path | None = None, *, readonly: bool = True) -> bytes:
    root = calendars_root or paths()["calendars"]
    lines = ["[calendars]"]
    for item in subscriptions:
        lines.extend((
            f"[[{item['id']}]]",
            f"path = {quote_config(str(root / item['id']))}",
            "type = calendar",
            f"readonly = {'True' if readonly else 'False'}",
        ))
        if "color" in item:
            lines.append(f"color = {quote_config(item['color'])}")
    lines.extend(("", "[locale]", "timeformat = %H:%M", "dateformat = %Y-%m-%d", "longdateformat = %Y-%m-%d", "datetimeformat = %Y-%m-%d %H:%M", "longdatetimeformat = %Y-%m-%d %H:%M", ""))
    return "\n".join(lines).encode()


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def run_bounded(argv: list[str], *, input_bytes: bytes | None = None, timeout: int, output_limit: int = MAX_COMMAND_OUTPUT) -> bytes:
    global active_process
    assert argv
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    active_process = process
    assert process.stdout is not None
    assert process.stderr is not None
    if input_bytes is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            pass
    output = bytearray()
    error = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, output)
    selector.register(process.stderr, selectors.EVENT_READ, error)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(process)
                raise SubscriptionError("command_failed", "calendar helper timed out")
            for key, _ in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(key.data) > output_limit:
                    terminate_process_group(process)
                    raise SubscriptionError("command_failed", "calendar helper output exceeded its limit")
        returncode = process.wait(timeout=2)
    finally:
        selector.close()
        if process.poll() is None:
            terminate_process_group(process)
        process.stdout.close()
        process.stderr.close()
        active_process = None
    if returncode != 0:
        raise SubscriptionError("command_failed", "calendar helper failed")
    return bytes(output)


def secret_tool() -> str:
    executable = shutil.which("secret-tool")
    if executable is None:
        raise SubscriptionError("dependency_missing", "Secret Service command is not installed")
    return executable


def store_secret(subscription_id: str, credential: dict[str, str]) -> None:
    payload = json.dumps(credential, ensure_ascii=False, separators=(",", ":")).encode()
    run_bounded(
        [secret_tool(), "store", "--label=Omarchy Calendar ICS subscription", *secret_attributes(subscription_id)],
        input_bytes=payload,
        timeout=SECRET_TIMEOUT_SECONDS,
    )


def lookup_secret(subscription_id: str) -> dict[str, str]:
    raw = run_bounded([secret_tool(), "lookup", *secret_attributes(subscription_id)], timeout=SECRET_TIMEOUT_SECONDS, output_limit=32 * 1024)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SubscriptionError("credential_unavailable", "subscription credential is unavailable") from error
    if not isinstance(value, dict) or not set(value).issubset({"url", "username", "password"}) or not isinstance(value.get("url"), str):
        raise SubscriptionError("credential_unavailable", "subscription credential is unavailable")
    username = value.get("username")
    password = value.get("password")
    if (username is None) != (password is None) or (username is not None and (not isinstance(username, str) or not isinstance(password, str))):
        raise SubscriptionError("credential_unavailable", "subscription credential is unavailable")
    return value


def clear_secret(subscription_id: str) -> bool:
    try:
        run_bounded([secret_tool(), "clear", *secret_attributes(subscription_id)], timeout=SECRET_TIMEOUT_SECONDS)
        return True
    except SubscriptionError:
        return False


def validated_url(value: str) -> urllib.parse.SplitResult:
    if not isinstance(value, str) or not value or len(value.encode()) > 8192 or value != value.strip():
        raise SubscriptionError("invalid_request", "url must be a bounded HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise SubscriptionError("invalid_request", "url must be a valid HTTPS URL") from error
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise SubscriptionError("invalid_request", "url must be a private HTTPS URL without userinfo or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise SubscriptionError("invalid_request", "url has an invalid port")
    if any(ord(character) < 0x20 or character == "\\" for character in value):
        raise SubscriptionError("invalid_request", "url contains an invalid character")
    hexadecimal = set("0123456789abcdefABCDEF")
    for index, character in enumerate(value):
        if character == "%" and (index + 2 >= len(value) or value[index + 1] not in hexadecimal or value[index + 2] not in hexadecimal):
            raise SubscriptionError("invalid_request", "url contains an invalid percent escape")
    try:
        parsed.hostname.encode("idna")
    except UnicodeError as error:
        raise SubscriptionError("invalid_request", "url has an invalid hostname") from error
    return parsed


def origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int]:
    assert parsed.hostname is not None
    return parsed.scheme, parsed.hostname.lower(), parsed.port or 443


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.redirects = 0

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> urllib.request.Request:
        self.redirects += 1
        if self.redirects > MAX_REDIRECTS:
            raise SubscriptionError("fetch_failed", "calendar feed exceeded the redirect limit")
        target = urllib.parse.urljoin(req.full_url, newurl)
        parsed = validated_url(target)
        headers_copy = dict(req.headers)
        if origin(urllib.parse.urlsplit(req.full_url)) != origin(parsed):
            headers_copy.pop("Authorization", None)
        return urllib.request.Request(target, headers=headers_copy, method="GET")


def fetch_feed(credential: dict[str, str]) -> bytes:
    url = credential["url"]
    validated_url(url)
    headers = {"Accept": "text/calendar, application/ics;q=0.9, */*;q=0.1", "User-Agent": "omarchy-calendar/1"}
    username = credential.get("username")
    if username is not None:
        token = base64.b64encode(f"{username}:{credential['password']}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), SafeRedirectHandler())
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if response.geturl() and urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise SubscriptionError("fetch_failed", "calendar feed redirected to an unsafe URL")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_FEED_BYTES:
                raise SubscriptionError("feed_too_large", "calendar feed exceeds 16 MiB")
            content = response.read(MAX_FEED_BYTES + 1)
    except SubscriptionError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ssl.SSLError, http.client.HTTPException, OSError, ValueError) as error:
        raise SubscriptionError("fetch_failed", "calendar feed could not be fetched") from error
    if len(content) > MAX_FEED_BYTES:
        raise SubscriptionError("feed_too_large", "calendar feed exceeds 16 MiB")
    return content


def validate_icalendar(content: bytes) -> int:
    try:
        import icalendar
    except ImportError as error:
        raise SubscriptionError("dependency_missing", "python-icalendar is not installed") from error
    try:
        calendar = icalendar.Calendar.from_ical(content)
    except Exception as error:
        raise SubscriptionError("invalid_calendar", "calendar feed is not valid iCalendar") from error
    if getattr(calendar, "name", None) != "VCALENDAR":
        raise SubscriptionError("invalid_calendar", "calendar feed is not a VCALENDAR")
    events = 0
    for components, component in enumerate(calendar.walk(), start=1):
        if components > MAX_COMPONENTS_PER_FEED:
            raise SubscriptionError("feed_too_large", "calendar feed has too many entries")
        if getattr(component, "name", None) == "VEVENT":
            events += 1
            if events > MAX_EVENTS_PER_FEED:
                raise SubscriptionError("feed_too_large", "calendar feed has too many events")
    return events


def directory_size(root: Path, maximum: int = MAX_CANDIDATE_BYTES) -> int:
    total = 0
    if not root.exists():
        return total
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SubscriptionError("candidate_failed", "calendar importer produced unsafe data")
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            os.chmod(path, 0o600)
            total += path.stat().st_size
            if total > maximum:
                raise SubscriptionError("candidate_too_large", "imported calendar exceeds its limit")
        else:
            raise SubscriptionError("candidate_failed", "calendar importer produced unsafe data")
    return total


def validate_total_data(candidate: Path, replacing_id: str) -> None:
    total = directory_size(candidate)
    calendars = paths()["calendars"]
    if calendars.exists():
        for child in calendars.iterdir():
            if child.name != replacing_id:
                total += directory_size(child, MAX_TOTAL_DATA_BYTES)
                if total > MAX_TOTAL_DATA_BYTES:
                    raise SubscriptionError("candidate_too_large", "total calendar data exceeds its limit")


def import_candidate(item: dict[str, str], content: bytes, parent: Path) -> Path:
    khal = shutil.which("khal")
    if khal is None:
        raise SubscriptionError("dependency_missing", "khal is not installed")
    candidate_root = parent / "calendars"
    candidate_dir = candidate_root / item["id"]
    candidate_dir.mkdir(mode=0o700, parents=True)
    config = parent / "khal.conf"
    config.write_bytes(khal_config([item], candidate_root, readonly=False))
    os.chmod(config, 0o600)
    feed = parent / "feed.ics"
    feed.write_bytes(content)
    os.chmod(feed, 0o600)
    run_bounded([khal, "-c", str(config), "--no-color", "import", "--batch", "--include-calendar", item["id"], str(feed)], timeout=KHAL_TIMEOUT_SECONDS)
    directory_size(candidate_dir)
    return candidate_dir


def replace_directory(candidate: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}-old-{secrets.token_hex(8)}")
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(candidate, destination)
    except BaseException:
        if had_destination:
            os.replace(backup, destination)
        raise
    if had_destination:
        shutil.rmtree(backup)


def refresh_one(item: dict[str, str]) -> int:
    credential = lookup_secret(item["id"])
    content = fetch_feed(credential)
    count = validate_icalendar(content)
    ensure_private_directories()
    with tempfile.TemporaryDirectory(prefix=".refresh-", dir=paths()["data"]) as temporary:
        candidate = import_candidate(item, content, Path(temporary))
        validate_total_data(candidate, item["id"])
        replace_directory(candidate, paths()["calendars"] / item["id"])
    return count


def sanitized_status(results: list[dict[str, Any]], attempted: bool = True) -> dict[str, Any]:
    ok = all(result["ok"] for result in results)
    return {
        "attempted": attempted,
        "ok": ok,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "subscriptions": results,
    }


def write_status(status: dict[str, Any]) -> None:
    atomic_write(paths()["status"], (json.dumps(status, separators=(",", ":")) + "\n").encode())


def acquire_lock() -> Any:
    ensure_private_directories()
    lock_path = paths()["lock"]
    stream = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise SubscriptionError("busy", "another subscription operation is already running")
    return stream


def add_subscription(request: dict[str, Any], progress: Progress) -> dict[str, Any]:
    allowed = {"action", "requestId", "name", "color", "url", "username", "password"}
    validate_request_keys(request, allowed, {"action", "name", "url"})
    name = bounded_text(request, "name", 256, required=True)
    color = bounded_text(request, "color", 64)
    url = bounded_text(request, "url", 8192, required=True)
    username = bounded_text(request, "username", 1024)
    password = bounded_text(request, "password", 16 * 1024, allow_controls=True)
    if (username is None) != (password is None):
        raise SubscriptionError("invalid_request", "username and password must be supplied together")
    if username is not None and ":" in username:
        raise SubscriptionError("invalid_request", "username must not contain a colon")
    assert name is not None and url is not None
    validated_url(url)
    lock = acquire_lock()
    subscription_id = secrets.token_hex(16)
    item = {"id": subscription_id, "name": name}
    if color is not None:
        item["color"] = color
    credential = {"url": url}
    if username is not None:
        assert password is not None
        credential.update({"username": username, "password": password})
    stored = False
    committed = False
    destination = paths()["calendars"] / subscription_id
    try:
        subscriptions = load_subscriptions()
        if len(subscriptions) >= MAX_SUBSCRIPTIONS:
            raise SubscriptionError("subscription_limit", f"at most {MAX_SUBSCRIPTIONS} subscriptions are allowed")
        progress("storing", {"subscriptionId": subscription_id})
        store_secret(subscription_id, credential)
        stored = True
        progress("fetching", {"subscriptionId": subscription_id})
        content = fetch_feed(credential)
        if len(content) > MAX_FEED_BYTES:
            raise SubscriptionError("feed_too_large", "calendar feed exceeds 16 MiB")
        count = validate_icalendar(content)
        progress("importing", {"subscriptionId": subscription_id})
        ensure_private_directories()
        with tempfile.TemporaryDirectory(prefix=".add-", dir=paths()["data"]) as temporary:
            candidate = import_candidate(item, content, Path(temporary))
            validate_total_data(candidate, subscription_id)
            replace_directory(candidate, destination)
        updated = [*subscriptions, item]
        try:
            atomic_write(paths()["metadata"], metadata_bytes(updated))
            atomic_write(paths()["khal_config"], khal_config(updated))
            committed = True
        except BaseException:
            shutil.rmtree(paths()["calendars"] / subscription_id, ignore_errors=True)
            atomic_write(paths()["metadata"], metadata_bytes(subscriptions))
            atomic_write(paths()["khal_config"], khal_config(subscriptions))
            raise
        return {"subscription": item, "events": count}
    finally:
        lock.close()
        if not committed:
            shutil.rmtree(destination, ignore_errors=True)
        if stored and not committed:
            clear_secret(subscription_id)


def remove_subscription(request: dict[str, Any], progress: Progress) -> dict[str, Any]:
    validate_request_keys(request, {"action", "requestId", "id"}, {"action", "id"})
    subscription_id = bounded_text(request, "id", 32, required=True)
    assert subscription_id is not None
    lock = acquire_lock()
    try:
        subscriptions = load_subscriptions()
        matching = [item for item in subscriptions if item["id"] == subscription_id]
        if not matching:
            raise SubscriptionError("not_found", "subscription was not found")
        updated = [item for item in subscriptions if item["id"] != subscription_id]
        destination = paths()["calendars"] / subscription_id
        backup = destination.with_name(f".{subscription_id}-removed-{secrets.token_hex(8)}")
        try:
            if destination.exists():
                os.replace(destination, backup)
            atomic_write(paths()["metadata"], metadata_bytes(updated))
            atomic_write(paths()["khal_config"], khal_config(updated))
        except BaseException:
            if backup.exists():
                os.replace(backup, destination)
            atomic_write(paths()["metadata"], metadata_bytes(subscriptions))
            atomic_write(paths()["khal_config"], khal_config(subscriptions))
            raise
        shutil.rmtree(backup, ignore_errors=True)
        progress("clearing", {"subscriptionId": subscription_id})
        cleanup = clear_secret(subscription_id)
        return {"removed": matching[0], "cleanupComplete": cleanup}
    finally:
        lock.close()


def refresh_subscriptions(progress: Progress = lambda _stage, _details: None) -> dict[str, Any]:
    lock = acquire_lock()
    try:
        subscriptions = load_subscriptions()
        results: list[dict[str, Any]] = []
        for item in subscriptions:
            progress("refreshing", {"subscriptionId": item["id"]})
            try:
                count = refresh_one(item)
                results.append({"id": item["id"], "ok": True, "events": count})
            except SubscriptionError as error:
                results.append({"id": item["id"], "ok": False, "error": {"code": error.code, "message": error.message}})
        status = sanitized_status(results)
        write_status(status)
        return status
    finally:
        lock.close()


def validate_request_keys(request: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(request) - allowed)
    missing = sorted(required - set(request))
    if unknown:
        raise SubscriptionError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise SubscriptionError("invalid_request", f"missing fields: {', '.join(missing)}")


def bounded_text(request: dict[str, Any], key: str, maximum: int, *, required: bool = False, allow_controls: bool = False) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value.encode()) > maximum:
        raise SubscriptionError("invalid_request", f"{key} is invalid")
    if not allow_controls and any(character in value for character in "\x00\r\n"):
        raise SubscriptionError("invalid_request", f"{key} is invalid")
    if "\x00" in value:
        raise SubscriptionError("invalid_request", f"{key} is invalid")
    return value


def dispatch(request: dict[str, Any], progress: Progress) -> dict[str, Any]:
    request_id = request.get("requestId")
    if not isinstance(request_id, str) or not request_id or len(request_id.encode()) > 128:
        raise SubscriptionError("invalid_request", "requestId must be a nonempty string of at most 128 bytes")
    action = request.get("action")
    if action == "list":
        validate_request_keys(request, {"action", "requestId"}, {"action"})
        result: dict[str, Any] = {"subscriptions": load_subscriptions()}
    elif action == "add":
        result = add_subscription(request, progress)
    elif action == "remove":
        result = remove_subscription(request, progress)
    elif action == "refresh":
        validate_request_keys(request, {"action", "requestId"}, {"action"})
        result = {"refresh": refresh_subscriptions(progress)}
    else:
        raise SubscriptionError("unknown_action", "action must be list, add, remove, or refresh")
    return result


def encode_line(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise SubscriptionError("response_too_large", "response exceeds protocol limit")
    return encoded + b"\n"


def handle_termination(signum: int, _frame: Any) -> None:
    if active_process is not None:
        terminate_process_group(active_process)
    raise SystemExit(128 + signum)


def subscriptions_main() -> int:
    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)
    request: dict[str, Any] = {}
    request_id: str | None = None

    def emit_progress(stage: str, details: dict[str, Any]) -> None:
        value = {"type": "progress", "final": False, "stage": stage, **details}
        if request_id is not None:
            value["requestId"] = request_id
        os.write(sys.stdout.fileno(), encode_line(value))

    import sys
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise SubscriptionError("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SubscriptionError("invalid_json", "request is not valid UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise SubscriptionError("invalid_request", "request must be a JSON object")
        request = value
        candidate_id = request.get("requestId")
        if isinstance(candidate_id, str) and len(candidate_id.encode()) <= 128:
            request_id = candidate_id
        result = dispatch(request, emit_progress)
        response = {"type": "result", "final": True, "ok": True, **result}
    except SubscriptionError as error:
        response = {"type": "result", "final": True, "ok": False, "error": {"code": error.code, "message": error.message}}
    except Exception:  # noqa: BLE001 - protocol output must remain valid on every failure.
        response = {"type": "result", "final": True, "ok": False, "error": {"code": "internal_error", "message": "unexpected subscription failure"}}
    if request_id is not None:
        response["requestId"] = request_id
    sys.stdout.buffer.write(encode_line(response))
    sys.stdout.buffer.flush()
    return 0
