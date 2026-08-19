"""Secure, transactional remote account setup for the widget protocol."""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

APP = "omarchy-calendar"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_SECRET_BYTES = 16 * 1024
MAX_CLIENT_FILE_PATH_BYTES = 4096
MAX_GOOGLE_CLIENT_FILE_BYTES = 64 * 1024
MAX_ACTIVE_FILE_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_AUTHORIZATION_URL_BYTES = 8192
MAX_CANDIDATE_ENTRIES = 20_000
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 300
SECRET_TIMEOUT_SECONDS = 30
PROCESS_TERMINATION_GRACE_SECONDS = 3
CANDIDATE_CHECK_INTERVAL_SECONDS = 0.25
PROVIDERS = frozenset(("google", "caldav", "icloud"))
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
AUTHORIZATION_URL = re.compile(
    rb"https://accounts\.google\.com/o/oauth2/(?:v2/)?auth[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*"
)
active_process: subprocess.Popen[bytes] | None = None
commit_reached = False
cancellation_requested = False


class SetupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SetupCancelled(SetupError):
    def __init__(self) -> None:
        super().__init__("cancelled", "account setup was cancelled")


def xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home() / fallback


def app_paths() -> dict[str, Path]:
    config = xdg_path("XDG_CONFIG_HOME", ".config") / APP
    data = xdg_path("XDG_DATA_HOME", ".local/share") / APP
    state = xdg_path("XDG_STATE_HOME", ".local/state") / APP
    return {
        "config": config,
        "data": data,
        "state": state,
        "khal_config": config / "khal.conf",
        "vdirsyncer_config": config / "vdirsyncer.conf",
        "profile": config / "remote-profile.json",
        "lock": state / "operation.lock",
    }


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def assert_outside_source(current_paths: dict[str, Path]) -> None:
    root = source_root()
    for name in ("config", "data", "state"):
        try:
            current_paths[name].resolve().relative_to(root)
        except ValueError:
            continue
        raise SetupError("unsafe_path", f"refusing to write {name} inside the plugin source tree")


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def quoted(value: str) -> str:
    assert value
    assert "\x00" not in value
    assert "\n" not in value
    assert "\r" not in value
    return json.dumps(value, ensure_ascii=False)


def khal_config(current_paths: dict[str, Path]) -> str:
    local = current_paths["data"] / "calendars" / "local"
    synced = current_paths["data"] / "calendars" / "synced" / "*"
    return f"""[calendars]
[[local]]
path = {local}
type = calendar

[[synced]]
path = {synced}
type = discover

[locale]
timeformat = %H:%M
dateformat = %Y-%m-%d
longdateformat = %Y-%m-%d
datetimeformat = %Y-%m-%d %H:%M
longdatetimeformat = %Y-%m-%d %H:%M

[default]
default_calendar = local
highlight_event_days = true
print_new = event
"""


def installation_id(current_paths: dict[str, Path]) -> str:
    identity = "\n".join(
        str(current_paths[name].expanduser().absolute()) for name in ("config", "data", "state")
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def secret_attributes(
    provider: str,
    account_id: str,
    slot: str | int,
    *,
    namespace: str | None,
) -> list[str]:
    assert provider in PROVIDERS
    assert HEX_32.fullmatch(account_id)
    slot_text = str(slot)
    assert slot_text in ("0", "1") or HEX_32.fullmatch(slot_text)
    attributes = [
        "service", APP,
        "purpose", "remote-credential",
    ]
    if namespace is not None:
        assert HEX_32.fullmatch(namespace)
        attributes.extend(("installation", namespace))
    attributes.extend((
        "provider", provider,
        "account", account_id,
        "slot", slot_text,
    ))
    return attributes


def vdirsyncer_config(
    current_paths: dict[str, Path],
    request: dict[str, str],
    account_id: str,
    slot: str | int,
    *,
    namespace: str,
    status_path: Path,
    synced_path: Path,
    token_path: Path | None,
) -> str:
    fetch = json.dumps([
        "command",
        "secret-tool",
        "lookup",
        *secret_attributes(request["provider"], account_id, slot, namespace=namespace),
    ])
    provider = request["provider"]
    if provider == "google":
        assert token_path is not None
        remote_lines = (
            'type = "google_calendar"',
            f"token_file = {quoted(str(token_path))}",
            f"client_id = {quoted(request['clientId'])}",
            f"client_secret.fetch = {fetch}",
        )
    else:
        assert token_path is None
        url = request["url"] if provider == "caldav" else "https://caldav.icloud.com/"
        remote_lines = (
            'type = "caldav"',
            f"url = {quoted(url)}",
            f"username = {quoted(request['username'])}",
            f"password.fetch = {fetch}",
        )
    remote = "\n".join(remote_lines)
    return f"""[general]
status_path = {quoted(str(status_path))}

[pair omarchy_calendar]
a = "omarchy_calendar_local"
b = "omarchy_calendar_remote"
collections = ["from a", "from b"]
metadata = ["displayname", "color"]

[storage omarchy_calendar_local]
type = "filesystem"
path = {quoted(str(synced_path))}
fileext = ".ics"

[storage omarchy_calendar_remote]
{remote}
"""


def bounded_string(
    request: dict[str, Any], key: str, *, maximum: int, required: bool = False
) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SetupError("invalid_request", f"{key} must be a string")
    if required and not value.strip():
        raise SetupError("invalid_request", f"{key} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise SetupError("invalid_request", f"{key} exceeds {maximum} bytes")
    if any(character in value for character in "\x00\r\n"):
        raise SetupError("invalid_request", f"{key} contains a forbidden control character")
    return value


def validate_caldav_url(url: str) -> None:
    if url != url.strip() or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in url
    ):
        raise SetupError("invalid_request", "url must not contain whitespace or control characters")
    if "\\" in url or PERCENT_ESCAPE.search(url):
        raise SetupError("invalid_request", "url is malformed")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise SetupError("invalid_request", "url is malformed") from error
    if parsed.scheme != "https" or not parsed.netloc or hostname is None:
        raise SetupError("invalid_request", "url must be an HTTPS CalDAV URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise SetupError("invalid_request", "url must not contain embedded credentials")
    if "#" in url:
        raise SetupError("invalid_request", "url must not contain a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise SetupError("invalid_request", "url port is invalid")
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        try:
            ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
        except UnicodeError as error:
            raise SetupError("invalid_request", "url hostname is malformed") from error
        labels = ascii_hostname.split(".")
        if not ascii_hostname or any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[A-Za-z0-9-]+", label) is None
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        ):
            raise SetupError("invalid_request", "url hostname is malformed")


def google_client_value(installed: dict[str, Any], key: str, maximum: int) -> str:
    value = installed.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SetupError("invalid_client_file", "selected file is not a valid Google Desktop OAuth JSON file")
    if len(value.encode("utf-8")) > maximum or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise SetupError("invalid_client_file", "selected file is not a valid Google Desktop OAuth JSON file")
    return value


def read_google_client_file(path_text: str) -> tuple[str, bytearray]:
    if not Path(path_text).is_absolute() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in path_text
    ):
        raise SetupError("invalid_request", "clientFile must be a bounded absolute path")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(path_text, flags)
    except OSError as error:
        raise SetupError(
            "invalid_client_file", "selected Google OAuth JSON must be a readable regular file"
        ) from error

    raw_buffer = bytearray(MAX_GOOGLE_CLIENT_FILE_BYTES + 1)
    document: Any = None
    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_GOOGLE_CLIENT_FILE_BYTES:
                raise SetupError(
                    "invalid_client_file", "selected Google OAuth JSON must be a bounded regular file"
                )
            offset = 0
            view = memoryview(raw_buffer)
            try:
                while offset < len(raw_buffer):
                    count = os.readv(descriptor, [view[offset:]])
                    if count == 0:
                        break
                    offset += count
            finally:
                view.release()
            if offset > MAX_GOOGLE_CLIENT_FILE_BYTES:
                raise SetupError(
                    "invalid_client_file", "selected Google OAuth JSON exceeds its safety limit"
                )
            del raw_buffer[offset:]
        finally:
            os.close(descriptor)

        try:
            document = json.loads(raw_buffer)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SetupError(
                "invalid_client_file", "selected file is not a valid Google Desktop OAuth JSON file"
            ) from error
        if not isinstance(document, dict) or set(document) != {"installed"}:
            raise SetupError(
                "invalid_client_file", "selected file is not a valid Google Desktop OAuth JSON file"
            )
        installed = document["installed"]
        if not isinstance(installed, dict):
            raise SetupError(
                "invalid_client_file", "selected file is not a valid Google Desktop OAuth JSON file"
            )
        client_id = google_client_value(installed, "client_id", 512)
        client_secret = google_client_value(installed, "client_secret", MAX_SECRET_BYTES)
        secret_buffer = bytearray(client_secret.encode("utf-8"))
        installed["client_secret"] = ""
        client_secret = ""
        return client_id, secret_buffer
    finally:
        if isinstance(document, dict):
            installed_value = document.get("installed")
            if isinstance(installed_value, dict):
                installed_value["client_secret"] = ""
            document.clear()
        raw_buffer[:] = b"\x00" * len(raw_buffer)


def _validate_request_with_inline_secret(raw_request: Any) -> tuple[dict[str, str], bytearray]:
    if not isinstance(raw_request, dict):
        raise SetupError("invalid_request", "request must be a JSON object")
    provider = bounded_string(raw_request, "provider", maximum=16, required=True)
    request_id = bounded_string(raw_request, "requestId", maximum=128, required=True)
    secret = bounded_string(raw_request, "secret", maximum=MAX_SECRET_BYTES, required=True)
    display_name = bounded_string(raw_request, "displayName", maximum=256)
    assert provider is not None
    assert request_id is not None
    assert secret is not None
    if provider not in PROVIDERS:
        raise SetupError("invalid_request", "provider must be google, caldav, or icloud")

    base = {"requestId", "provider", "displayName", "secret"}
    required = set(base) - {"displayName"}
    if provider == "google":
        allowed = base | {"clientId"}
        required.add("clientId")
    elif provider == "caldav":
        allowed = base | {"username", "url"}
        required.update(("username", "url"))
    else:
        allowed = base | {"username"}
        required.add("username")
    unknown = sorted(set(raw_request) - allowed)
    missing = sorted(required - set(raw_request))
    if unknown:
        raise SetupError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise SetupError("invalid_request", f"missing fields: {', '.join(missing)}")

    validated = {"requestId": request_id, "provider": provider}
    if display_name is not None:
        validated["displayName"] = display_name
    if provider == "google":
        client_id = bounded_string(raw_request, "clientId", maximum=512, required=True)
        assert client_id is not None
        validated["clientId"] = client_id
    else:
        username = bounded_string(raw_request, "username", maximum=512, required=True)
        assert username is not None
        validated["username"] = username
        if provider == "caldav":
            url = bounded_string(raw_request, "url", maximum=4096, required=True)
            assert url is not None
            validate_caldav_url(url)
            validated["url"] = url
    secret_buffer = bytearray(secret.encode("utf-8"))
    secret = ""
    return validated, secret_buffer


def validate_request(raw_request: Any) -> tuple[dict[str, str], bytearray]:
    if not isinstance(raw_request, dict) or raw_request.get("provider") != "google" or "clientFile" not in raw_request:
        return _validate_request_with_inline_secret(raw_request)

    provider = bounded_string(raw_request, "provider", maximum=16, required=True)
    request_id = bounded_string(raw_request, "requestId", maximum=128, required=True)
    display_name = bounded_string(raw_request, "displayName", maximum=256)
    client_file = bounded_string(
        raw_request, "clientFile", maximum=MAX_CLIENT_FILE_PATH_BYTES, required=True
    )
    assert provider == "google"
    assert request_id is not None
    assert client_file is not None

    allowed = {"requestId", "provider", "displayName", "clientFile"}
    unknown = sorted(set(raw_request) - allowed)
    missing = sorted({"requestId", "provider", "clientFile"} - set(raw_request))
    if unknown:
        raise SetupError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise SetupError("invalid_request", f"missing fields: {', '.join(missing)}")

    client_id, secret_buffer = read_google_client_file(client_file)
    validated = {
        "requestId": request_id,
        "provider": provider,
        "clientId": client_id,
    }
    if display_name is not None:
        validated["displayName"] = display_name
    return validated, secret_buffer


def account_id_for(request: dict[str, str]) -> str:
    provider = request["provider"]
    if provider == "google":
        identity = request["clientId"]
    elif provider == "caldav":
        identity = f"{request['username']}\n{request['url']}"
    else:
        identity = request["username"]
    return hashlib.sha256(f"{provider}\n{identity}".encode()).hexdigest()[:32]


def read_profile(path: Path, namespace: str) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        profile = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(profile, dict):
        return None
    provider = profile.get("provider")
    account_id = profile.get("accountId")
    slot = profile.get("credentialSlot")
    if provider not in PROVIDERS or not isinstance(account_id, str) or not HEX_32.fullmatch(account_id):
        return None
    if slot in (0, 1):
        profile["secretAttributes"] = secret_attributes(provider, account_id, slot, namespace=None)
        return profile
    if not isinstance(slot, str) or not HEX_32.fullmatch(slot):
        return None
    if profile.get("installationId") != namespace:
        return None
    profile["secretAttributes"] = secret_attributes(provider, account_id, slot, namespace=namespace)
    return profile


def active_credential_from_config(content: bytes | None, namespace: str) -> dict[str, Any] | None:
    if content is None:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    matches = re.findall(r"^(?:password|client_secret)\.fetch\s*=\s*(.+)$", text, re.MULTILINE)
    if len(matches) != 1:
        return None
    try:
        fetch = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(fetch, list) or fetch[:3] != ["command", "secret-tool", "lookup"]:
        return None
    raw_attributes = fetch[3:]
    if len(raw_attributes) not in (10, 12) or any(not isinstance(value, str) for value in raw_attributes):
        return None
    attributes = dict(zip(raw_attributes[::2], raw_attributes[1::2], strict=True))
    expected_keys = {"service", "purpose", "provider", "account", "slot"}
    config_namespace = attributes.get("installation")
    if config_namespace is not None:
        expected_keys.add("installation")
    if set(attributes) != expected_keys:
        return None
    provider = attributes["provider"]
    account_id = attributes["account"]
    slot = attributes["slot"]
    if attributes["service"] != APP or attributes["purpose"] != "remote-credential":
        return None
    if provider not in PROVIDERS or not HEX_32.fullmatch(account_id):
        return None
    if slot not in ("0", "1") and not HEX_32.fullmatch(slot):
        return None
    if config_namespace is not None and config_namespace != namespace:
        return None
    return {
        "provider": provider,
        "accountId": account_id,
        "credentialSlot": slot,
        "secretAttributes": raw_attributes,
    }


def process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    group_id = process.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
    while process_group_exists(group_id) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.05)
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def handle_termination(_signum: int, _frame: Any) -> None:
    global cancellation_requested
    if cancellation_requested:
        return
    process = active_process
    if commit_reached:
        if process is not None:
            terminate_process_group(process)
        return
    cancellation_requested = True
    if process is not None:
        terminate_process_group(process)
    raise SetupCancelled()


def run_process(
    argv: list[str],
    timeout: int,
    *,
    input_buffer: bytearray | None = None,
    output_handler: Callable[[bytes], None] | None = None,
    monitor: Callable[[], None] | None = None,
    error_message: Callable[[], str] | None = None,
) -> None:
    global active_process
    assert argv
    assert timeout > 0
    executable = shutil.which(argv[0])
    if executable is None:
        raise SetupError("missing_command", f"required command is not installed: {argv[0]}")
    resolved_argv = [executable, *argv[1:]]
    capture_output = output_handler is not None
    try:
        process = subprocess.Popen(
            resolved_argv,
            stdin=subprocess.PIPE if input_buffer is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture_output else subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as error:
        raise SetupError("command_failed", f"failed to start {Path(executable).name}") from error
    active_process = process
    selector: selectors.BaseSelector | None = None
    try:
        if input_buffer is not None:
            assert process.stdin is not None
            process.stdin.write(input_buffer)
            process.stdin.close()
            input_buffer[:] = b"\x00" * len(input_buffer)

        if capture_output:
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        next_monitor = time.monotonic()
        output_bytes = 0
        while True:
            now = time.monotonic()
            if now >= deadline:
                terminate_process_group(process)
                raise SetupError("command_timeout", f"{Path(executable).name} timed out")
            if monitor is not None and now >= next_monitor:
                monitor()
                next_monitor = now + CANDIDATE_CHECK_INTERVAL_SECONDS

            if selector is not None and selector.get_map():
                events = selector.select(min(CANDIDATE_CHECK_INTERVAL_SECONDS, deadline - now))
                for key, _mask in events:
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output_bytes += len(chunk)
                    if output_bytes > MAX_COMMAND_OUTPUT_BYTES:
                        raise SetupError("command_output_too_large", f"{Path(executable).name} output exceeded its safety limit")
                    assert output_handler is not None
                    output_handler(chunk)
            elif process.poll() is None:
                time.sleep(min(CANDIDATE_CHECK_INTERVAL_SECONDS, max(0.01, deadline - now)))

            if process.poll() is not None and (selector is None or not selector.get_map()):
                break
        returncode = process.returncode
        assert returncode is not None
        if monitor is not None:
            monitor()
    finally:
        if selector is not None:
            selector.close()
        if input_buffer is not None:
            input_buffer[:] = b"\x00" * len(input_buffer)
        if process.poll() is None or process_group_exists(process.pid):
            terminate_process_group(process)
        active_process = None
    if returncode in (128 + signal.SIGINT, 128 + signal.SIGTERM, -signal.SIGINT, -signal.SIGTERM):
        raise SetupCancelled()
    if returncode != 0:
        message = error_message() if error_message is not None else ""
        if not message:
            message = f"{Path(executable).name} exited unsuccessfully"
        raise SetupError("command_failed", message)


def validate_candidate_bounds(candidate_root: Path) -> None:
    entries = 0
    total_bytes = 0
    pending = [candidate_root]
    while pending:
        directory = pending.pop()
        try:
            children = os.scandir(directory)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise SetupError("candidate_invalid", "candidate calendar data could not be inspected") from error
        with children:
            for child in children:
                entries += 1
                if entries > MAX_CANDIDATE_ENTRIES:
                    raise SetupError("candidate_too_large", "candidate calendar data contains too many entries")
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise SetupError("candidate_invalid", "candidate calendar data could not be inspected") from error
                mode = child_stat.st_mode
                if stat.S_ISDIR(mode):
                    pending.append(Path(child.path))
                elif stat.S_ISREG(mode):
                    total_bytes += child_stat.st_size
                    if total_bytes > MAX_CANDIDATE_BYTES:
                        raise SetupError("candidate_too_large", "candidate calendar data exceeds its byte limit")
                else:
                    raise SetupError("candidate_invalid", "candidate calendar data contains an unsafe file type")


class GoogleAuthorizationDetector:
    def __init__(self, emit_browser: Callable[[str], None]) -> None:
        self.emit_browser = emit_browser
        self.buffer = bytearray()
        self.emitted = False

    def feed(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_COMMAND_OUTPUT_BYTES:
            del self.buffer[:-MAX_COMMAND_OUTPUT_BYTES]
        if self.emitted:
            return
        match = AUTHORIZATION_URL.search(self.buffer)
        if match is None:
            return
        candidate = match.group().decode("ascii")
        if len(candidate.encode("ascii")) > MAX_AUTHORIZATION_URL_BYTES:
            return
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError:
            return
        if (
            parsed.scheme != "https"
            or parsed.hostname != "accounts.google.com"
            or parsed.path not in ("/o/oauth2/auth", "/o/oauth2/v2/auth")
            or not parsed.query
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in (None, 443)
            or PERCENT_ESCAPE.search(candidate)
        ):
            return
        self.emitted = True
        self.emit_browser(candidate)

    def failure_message(self) -> str:
        diagnostic = bytes(self.buffer).decode("utf-8", errors="replace").lower()
        if "access_denied" in diagnostic or "developer-approved testers" in diagnostic:
            return "Add this Google account as a test user in Google Auth Platform, then connect again"
        if "calendar api has not been used" in diagnostic or "calendar api is disabled" in diagnostic:
            return "Enable Google Calendar API in this OAuth client's Google Cloud project, then connect again"
        if "redirect_uri_mismatch" in diagnostic:
            return "Create a Desktop OAuth client, then import its downloaded JSON file"
        if "invalid_client" in diagnostic or "oauth client was not found" in diagnostic:
            return "Google rejected this OAuth client; recreate a Desktop OAuth client and import its JSON file"
        if "invalid_grant" in diagnostic or "invalid token" in diagnostic or "unauthorized" in diagnostic:
            return "Google rejected the authorization token; connect again and approve access in the browser"
        if "unknown error occurred: forbidden" in diagnostic or "403 forbidden" in diagnostic:
            return "Google Calendar denied synchronization; enable Calendar API in the OAuth project and reconnect"
        if "unknown error occurred: not found" in diagnostic or "404 not found" in diagnostic:
            return "Google Calendar was not found; open calendar.google.com once, confirm Calendar API is enabled, and reconnect"
        detail = re.search(r"error: unknown error occurred: ([a-z][a-z0-9 ._-]{0,100})", diagnostic)
        if detail is not None:
            return f"Google Calendar setup failed: {detail.group(1).strip()}"
        return ""

    def clear(self) -> None:
        self.buffer[:] = b"\x00" * len(self.buffer)
        self.buffer.clear()


def clear_secret(attributes: list[str]) -> bool:
    try:
        run_process(["secret-tool", "clear", *attributes], SECRET_TIMEOUT_SECONDS)
    except SetupError:
        return False
    return True


def read_active_file(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_ACTIVE_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > MAX_ACTIVE_FILE_BYTES:
        raise SetupError("active_state_too_large", f"active {path.name} exceeds its safety limit")
    return content


def restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, previous)


def google_token_path(state_path: Path, account_id: str, slot: str | int) -> Path:
    slot_text = str(slot)
    assert slot_text in ("0", "1") or HEX_32.fullmatch(slot_text)
    return state_path / f"google-token-{account_id}-{slot_text}.json"


def profile_bytes(request: dict[str, str], account_id: str, slot: str, namespace: str) -> bytes:
    assert HEX_32.fullmatch(slot)
    assert HEX_32.fullmatch(namespace)
    profile: dict[str, Any] = {
        "version": 2,
        "provider": request["provider"],
        "displayName": request.get("displayName") or request["provider"].capitalize(),
        "accountId": account_id,
        "credentialSlot": slot,
        "installationId": namespace,
    }
    return (json.dumps(profile, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def setup_remote(
    request: dict[str, str],
    secret_buffer: bytearray,
    emit_progress: Callable[[str, str, bool], None],
    emit_browser: Callable[[str], None],
) -> dict[str, Any]:
    global cancellation_requested, commit_reached
    commit_reached = False
    cancellation_requested = False
    current_paths = app_paths()
    assert_outside_source(current_paths)
    for directory in (current_paths["config"], current_paths["data"], current_paths["state"]):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    lock_stream = current_paths["lock"].open("a+b")
    os.chmod(current_paths["lock"], 0o600)
    try:
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("setup_busy", "another account setup or synchronization is already running") from error

        config_path = current_paths["vdirsyncer_config"]
        khal_path = current_paths["khal_config"]
        profile_path = current_paths["profile"]
        namespace = installation_id(current_paths)
        replaces_existing = config_path.is_file()
        old_config = read_active_file(config_path)
        old_profile = read_profile(profile_path, namespace) if replaces_existing else None
        old_credential = old_profile or active_credential_from_config(old_config, namespace)
        old_khal = read_active_file(khal_path)
        old_profile_bytes = read_active_file(profile_path)
        old_was_legacy_google = old_credential is None and old_config is not None and b'type = "google_calendar"' in old_config

        account_id = account_id_for(request)
        slot = secrets.token_hex(16)
        if old_credential is not None:
            while slot == str(old_credential["credentialSlot"]):
                slot = secrets.token_hex(16)
        attributes = secret_attributes(request["provider"], account_id, slot, namespace=namespace)
        active_synced = current_paths["data"] / "calendars" / "synced"
        active_synced_existed = active_synced.exists()
        if active_synced_existed and not active_synced.is_dir():
            raise SetupError("invalid_active_state", "active synchronized calendar path is not a directory")
        active_status = current_paths["state"] / "vdirsyncer-status"
        active_status_existed = active_status.exists()
        if active_status_existed and not active_status.is_dir():
            raise SetupError("invalid_active_state", "active synchronization status path is not a directory")

        emit_progress("storing_secret", "Saving credential", replaces_existing)
        credential_attempted = True
        setup_committed = False
        cleanup_complete = True
        candidate_root: Path | None = None
        final_token: Path | None = None
        final_token_previous: bytes | None = None
        final_token_moved = False
        backup_synced: Path | None = None
        backup_status: Path | None = None
        data_activated = False
        status_activated = False
        try:
            run_process(
                ["secret-tool", "store", "--label=Omarchy Calendar remote credential", *attributes],
                SECRET_TIMEOUT_SECONDS,
                input_buffer=secret_buffer,
            )
            candidate_root = Path(tempfile.mkdtemp(prefix=".remote-candidate-", dir=current_paths["data"]))
            os.chmod(candidate_root, 0o700)
            candidate_synced = candidate_root / "synced"
            candidate_synced.mkdir(mode=0o700)
            candidate_status = candidate_root / "status"
            candidate_status.mkdir(mode=0o700)
            candidate_token = candidate_root / "google-token.json" if request["provider"] == "google" else None
            candidate_config = candidate_root / "vdirsyncer.conf"
            candidate_content = vdirsyncer_config(
                current_paths,
                request,
                account_id,
                slot,
                namespace=namespace,
                status_path=candidate_status,
                synced_path=candidate_synced,
                token_path=candidate_token,
            )
            atomic_write(candidate_config, candidate_content.encode())

            stage_message = "Authorizing Google in your browser" if request["provider"] == "google" else "Discovering calendars"
            emit_progress("authorizing" if request["provider"] == "google" else "discovering", stage_message, replaces_existing)
            authorization_detector = GoogleAuthorizationDetector(emit_browser)
            discover_output = authorization_detector.feed if request["provider"] == "google" else None

            def candidate_monitor() -> None:
                assert candidate_root is not None
                validate_candidate_bounds(candidate_root)

            try:
                run_process(
                    ["vdirsyncer", "-c", str(candidate_config), "discover"],
                    COMMAND_TIMEOUT_SECONDS,
                    output_handler=discover_output,
                    monitor=candidate_monitor,
                    error_message=(authorization_detector.failure_message if request["provider"] == "google" else None),
                )
                emit_progress("syncing", "Performing initial calendar sync", replaces_existing)
                run_process(
                    ["vdirsyncer", "-c", str(candidate_config), "sync"],
                    COMMAND_TIMEOUT_SECONDS,
                    output_handler=(authorization_detector.feed if request["provider"] == "google" else None),
                    monitor=candidate_monitor,
                    error_message=(authorization_detector.failure_message if request["provider"] == "google" else None),
                )
            finally:
                authorization_detector.clear()
            validate_candidate_bounds(candidate_root)
            if candidate_token is not None and not candidate_token.is_file():
                raise SetupError("oauth_failed", "Google authorization did not produce an OAuth token")

            final_content = vdirsyncer_config(
                current_paths,
                request,
                account_id,
                slot,
                namespace=namespace,
                status_path=current_paths["state"] / "vdirsyncer-status",
                synced_path=active_synced,
                token_path=(google_token_path(current_paths["state"], account_id, slot) if candidate_token else None),
            ).encode()
            emit_progress("committing", "Activating remote account", replaces_existing)

            try:
                if candidate_token is not None:
                    final_token = google_token_path(current_paths["state"], account_id, slot)
                    final_token_previous = read_active_file(final_token)
                    final_token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.replace(candidate_token, final_token)
                    final_token_moved = True
                    os.chmod(final_token, 0o600)

                active_synced.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if active_synced_existed:
                    backup_synced = Path(tempfile.mkdtemp(prefix=".synced-backup-", dir=active_synced.parent))
                    backup_synced.rmdir()
                    os.replace(active_synced, backup_synced)
                os.replace(candidate_synced, active_synced)
                data_activated = True
                os.chmod(active_synced, 0o700)

                if active_status_existed:
                    backup_status = Path(tempfile.mkdtemp(prefix=".status-backup-", dir=active_status.parent))
                    backup_status.rmdir()
                    os.replace(active_status, backup_status)
                os.replace(candidate_status, active_status)
                status_activated = True
                os.chmod(active_status, 0o700)

                (current_paths["data"] / "calendars" / "local").mkdir(mode=0o700, parents=True, exist_ok=True)
                atomic_write(khal_path, khal_config(current_paths).encode())
                atomic_write(config_path, final_content)
                atomic_write(profile_path, profile_bytes(request, account_id, slot, namespace))
            except BaseException:
                restore_file(khal_path, old_khal)
                restore_file(config_path, old_config)
                restore_file(profile_path, old_profile_bytes)
                if status_activated:
                    shutil.rmtree(active_status, ignore_errors=True)
                if backup_status is not None:
                    os.replace(backup_status, active_status)
                    backup_status = None
                if data_activated:
                    shutil.rmtree(active_synced, ignore_errors=True)
                if backup_synced is not None:
                    os.replace(backup_synced, active_synced)
                    backup_synced = None
                if final_token_moved and final_token is not None:
                    restore_file(final_token, final_token_previous)
                    final_token_moved = False
                raise
            commit_reached = True
            setup_committed = True
            if backup_synced is not None:
                shutil.rmtree(backup_synced, ignore_errors=True)
                backup_synced = None
            if backup_status is not None:
                shutil.rmtree(backup_status, ignore_errors=True)
                backup_status = None

            if old_credential is not None:
                old_attributes = list(old_credential["secretAttributes"])
                if old_attributes != attributes:
                    cleanup_complete = clear_secret(old_attributes) and cleanup_complete
                if old_credential["provider"] == "google":
                    old_token = google_token_path(
                        current_paths["state"],
                        str(old_credential["accountId"]),
                        old_credential["credentialSlot"],
                    )
                    if old_token != final_token:
                        old_token.unlink(missing_ok=True)
            elif old_was_legacy_google:
                (current_paths["state"] / "google-token.json").unlink(missing_ok=True)

            return {
                "ok": True,
                "provider": request["provider"],
                "displayName": request.get("displayName") or request["provider"].capitalize(),
                "connected": True,
                "replacesExisting": replaces_existing,
                "cleanupComplete": cleanup_complete,
            }
        finally:
            secret_buffer[:] = b"\x00" * len(secret_buffer)
            if candidate_root is not None:
                shutil.rmtree(candidate_root, ignore_errors=True)
            if backup_synced is not None:
                if not active_synced.exists():
                    os.replace(backup_synced, active_synced)
                else:
                    shutil.rmtree(backup_synced, ignore_errors=True)
            if backup_status is not None:
                if not active_status.exists():
                    os.replace(backup_status, active_status)
                else:
                    shutil.rmtree(backup_status, ignore_errors=True)
            if credential_attempted and not setup_committed:
                clear_secret(attributes)
            if final_token_moved and not setup_committed and final_token is not None:
                restore_file(final_token, final_token_previous)
    finally:
        lock_stream.close()


def read_request(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise SetupError("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
    if not raw:
        raise SetupError("invalid_json", "request is empty")
    if not raw.endswith(b"\n"):
        raise SetupError("invalid_json", "request must end with a newline")
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SetupError("invalid_json", "request is not valid UTF-8 JSON") from error
    if not isinstance(request, dict):
        raise SetupError("invalid_request", "request must be a JSON object")
    return request


def encode_line(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise SetupError("response_too_large", "setup response exceeded its protocol limit")
    return encoded + b"\n"


def run_protocol(input_stream: BinaryIO, output_stream: BinaryIO) -> int:
    global cancellation_requested, commit_reached
    commit_reached = False
    cancellation_requested = False
    os.umask(0o077)
    request: dict[str, Any] = {}
    request_id: str | None = None
    replaces_existing: bool | None = None
    final_emitted = False

    def emit(value: dict[str, Any]) -> None:
        output_stream.write(encode_line(value))
        output_stream.flush()

    try:
        request = read_request(input_stream)
        validated, secret_buffer = validate_request(request)
        request_id = validated["requestId"]
        request.pop("secret", None)
        request.clear()
        replaces_existing = app_paths()["vdirsyncer_config"].is_file()

        def progress(stage: str, message: str, replaces_existing: bool) -> None:
            emit({
                "type": "progress",
                "requestId": request_id,
                "stage": stage,
                "message": message,
                "replacesExisting": replaces_existing,
            })

        def browser(url: str) -> None:
            emit({
                "type": "browser",
                "requestId": request_id,
                "url": url,
            })

        try:
            result = setup_remote(validated, secret_buffer, progress, browser)
        finally:
            secret_buffer[:] = b"\x00" * len(secret_buffer)
        response = {"type": "result", "final": True, "requestId": request_id, **result}
    except SetupError as error:
        response = {
            "type": "result",
            "final": True,
            "ok": False,
            "error": {"code": error.code, "message": error.message},
        }
    except Exception:  # noqa: BLE001 - stdout must end with one valid final response.
        response = {
            "type": "result",
            "final": True,
            "ok": False,
            "error": {"code": "internal_error", "message": "unexpected account setup failure"},
        }
    if replaces_existing is not None:
        response.setdefault("replacesExisting", replaces_existing)
    if request_id is None:
        candidate_id = request.get("requestId")
        if isinstance(candidate_id, str) and len(candidate_id.encode()) <= 128:
            request_id = candidate_id
    if request_id is not None:
        response.setdefault("requestId", request_id)
    if not final_emitted:
        emit(response)
        final_emitted = True
    commit_reached = False
    cancellation_requested = False
    return 0


def main() -> int:
    import sys

    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)
    return run_protocol(sys.stdin.buffer, sys.stdout.buffer)
