"""Restricted, read-only browser for Frappe bench and site log files.

The API intentionally accepts only signed identifiers returned by ``list_logs``;
callers never provide filesystem paths. Downloads use a streaming Werkzeug
response so file contents are not accumulated in memory.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import stat
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import frappe
from werkzeug.wrappers import Response


_CHUNK_SIZE = 64 * 1024
_MAX_TAIL_RESPONSE = 1024 * 1024
# Leave generous room for Frappe's JSON envelope and pathological escaping.
_TAIL_JSON_BUDGET = _MAX_TAIL_RESPONSE - 16 * 1024
_DEFAULT_DOWNLOAD_CONFIRM_MB = 50
_ALLOWED_LOG_NAME = re.compile(
    r"^(?:.+\.json\.log(?:\.\d+)?(?:\.gz)?|.+\.log(?:\.\d+)?(?:\.gz)?|.+\.gz)$"
)
_SENSITIVE_LINE = re.compile(
    r"(?:\b(?:authorization|proxy-authorization|password|passwd|pwd|token|"
    r"access[_-]?token|refresh[_-]?token|api[_-]?key|secret|cookie|set-cookie)\b"
    r"\s*[\"']*\s*[:=])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Root:
    path: str
    scope: str
    label: str


class _LogBrowserError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _fail(code: str, message: str, status: int = 400) -> None:
    raise _LogBrowserError(code, message, status)


def _structured_error(error: _LogBrowserError) -> dict[str, Any]:
    frappe.local.response["http_status_code"] = error.status
    return {"ok": False, "error": {"code": error.code, "message": error.message}}


def _unexpected_error() -> dict[str, Any]:
    frappe.local.response["http_status_code"] = 500
    return {
        "ok": False,
        "error": {"code": "internal_error", "message": "The log operation could not be completed."},
    }


def _current_user() -> str:
    return str(getattr(frappe.session, "user", "Guest") or "Guest")


def _require_scope(scope: str) -> None:
    user = _current_user()
    if user == "Guest":
        _fail("authentication_required", "Sign in to access logs.", 401)
    if scope == "bench":
        if user != "Administrator":
            _fail("permission_denied", "Bench logs are restricted to Administrator.", 403)
        return
    try:
        roles = set(frappe.get_roles(user) or ())
    except Exception:
        roles = set()
    if not roles.intersection({"System Manager", "Observability Admin"}):
        _fail("permission_denied", "Site logs require System Manager or Observability Admin.", 403)


def _require_any_log_role() -> None:
    user = _current_user()
    if user == "Guest":
        _fail("authentication_required", "Sign in to access logs.", 401)
    if user == "Administrator":
        return
    try:
        roles = set(frappe.get_roles(user) or ())
    except Exception:
        roles = set()
    if not roles.intersection({"System Manager", "Observability Admin"}):
        _fail("permission_denied", "You do not have permission to access logs.", 403)


def _configured_extra_roots() -> list[tuple[str, str, str]]:
    """Read optional admin-configured roots; plain paths are site-scoped."""
    try:
        settings = frappe.get_single("Observability Settings")
        value = settings.get("log_roots") if hasattr(settings, "get") else getattr(settings, "log_roots", None)
    except Exception:
        return []

    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, (list, tuple)):
        return []

    result: list[tuple[str, str, str]] = []
    for index, item in enumerate(value[:64]):
        if isinstance(item, str):
            raw_path, scope, label = item, "site", f"configured-{index + 1}"
        elif isinstance(item, dict):
            raw_path = item.get("path")
            scope = item.get("scope", "site")
            label = item.get("name") or f"configured-{index + 1}"
        else:
            continue
        if not isinstance(raw_path, str) or not os.path.isabs(raw_path):
            continue
        if scope not in {"site", "bench"}:
            continue
        if not isinstance(label, str):
            label = f"configured-{index + 1}"
        result.append((raw_path, scope, label[:80]))
    return result


def _roots() -> list[_Root]:
    site_path = os.path.realpath(frappe.get_site_path())
    bench_path = os.path.dirname(os.path.dirname(site_path))
    candidates: list[tuple[str, str, str]] = [
        (os.path.join(bench_path, "logs"), "bench", "bench"),
        (frappe.get_site_path("logs"), "site", "site"),
    ]
    candidates.extend(_configured_extra_roots())

    roots: list[_Root] = []
    seen: set[tuple[str, str]] = set()
    for raw_path, scope, label in candidates:
        if scope not in {"bench", "site"} or not os.path.isabs(raw_path):
            continue
        canonical = os.path.realpath(os.path.abspath(raw_path))
        if not os.path.isabs(canonical):
            continue
        identity = (scope, canonical)
        if identity in seen:
            continue
        seen.add(identity)
        roots.append(_Root(canonical, scope, label))
    return roots


def _inside_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except (OSError, ValueError):
        return False


def _identifier_key() -> bytes:
    conf = getattr(frappe.local, "conf", None)
    key = None
    if conf is not None:
        try:
            key = conf.get("encryption_key") or conf.get("secret_key")
        except Exception:
            key = None
    if isinstance(key, str) and key:
        return key.encode("utf-8")
    if isinstance(key, bytes) and key:
        return key
    _fail("configuration_error", "A site encryption key is required for log identifiers.", 503)


def _make_file_id(root_index: int, name: str) -> str:
    payload = json.dumps([root_index, name], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(_identifier_key(), encoded, hashlib.sha256).digest()
    return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _decode_file_id(file_id: str) -> tuple[int, str]:
    if not isinstance(file_id, str) or len(file_id) > 2048:
        _fail("invalid_file_id", "The log identifier is invalid.", 400)
    try:
        encoded_text, signature_text = file_id.split(".", 1)
        encoded = encoded_text.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        supplied = base64.urlsafe_b64decode(signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4))
        expected = hmac.new(_identifier_key(), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            _fail("invalid_file_id", "The log identifier is invalid.", 400)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not isinstance(payload[0], int)
            or isinstance(payload[0], bool)
            or not isinstance(payload[1], str)
        ):
            _fail("invalid_file_id", "The log identifier is invalid.", 400)
        return payload[0], payload[1]
    except _LogBrowserError:
        raise
    except Exception:
        _fail("invalid_file_id", "The log identifier is invalid.", 400)


def _audit(action: str, *, scope: str | None = None, path: str | None = None, byte_count: int = 0,
           outcome: str = "success", file_id: str | None = None) -> None:
    timestamp = datetime.now(timezone.utc)
    request = getattr(frappe.local, "request", None)
    event = {
        "event": "log_access",
        "action": action,
        "outcome": outcome,
        "user": _current_user(),
        "ip": str(getattr(frappe.local, "request_ip", None) or getattr(request, "remote_addr", None) or ""),
        "file": path,
        "scope": scope,
        "bytes": max(0, int(byte_count)),
        "time": timestamp.isoformat(),
    }
    if file_id:
        event["file_id_fingerprint"] = hashlib.sha256(file_id.encode("utf-8", "replace")).hexdigest()[:16]
    message = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    try:
        frappe.get_doc({
            "doctype": "Log Access Entry",
            "time": timestamp.replace(tzinfo=None),
            "user": event["user"],
            "ip": event["ip"][:140],
            "action": action[:80],
            "scope": scope,
            "file": path,
            "bytes": event["bytes"],
            "outcome": outcome[:80],
        }).insert(ignore_permissions=True)
    except Exception:
        # Retain the file-log audit if database auditing is not available.
        pass
    try:
        frappe.logger("log_browser", allow_site=True).info(message)
    except Exception:
        # Preserve an audit record on the configured application logger if the
        # Frappe file logger is unavailable in a test or stripped deployment.
        import logging

        logging.getLogger("frappe_observability.log_browser").info(message)


def _name_allowed(name: str) -> bool:
    return (
        isinstance(name, str)
        and name not in {".", ".."}
        and os.path.basename(name) == name
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and bool(_ALLOWED_LOG_NAME.fullmatch(name))
    )


def _open_listed_file(file_id: str) -> tuple[_Root, str, int, int]:
    root_index, name = _decode_file_id(file_id)
    roots = _roots()
    if root_index < 0 or root_index >= len(roots) or not _name_allowed(name):
        _fail("invalid_file_id", "The log identifier is invalid.", 400)
    root = roots[root_index]
    _require_scope(root.scope)

    # Identifiers are issued only for direct child files. Reject all path syntax
    # regardless of signature validation to keep the root boundary simple.
    if name in {".", ".."} or os.path.basename(name) != name or "/" in name or "\\" in name:
        _fail("invalid_file_id", "The log identifier is invalid.", 400)
    path = os.path.join(root.path, name)
    if not _inside_root(path, root.path):
        _audit("denied", scope=root.scope, outcome="denied", file_id=file_id)
        _fail("unsafe_path", "The selected log file is outside an allowed root.", 400)
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        _audit("view_or_download", scope=root.scope, path=path, outcome="disappeared", file_id=file_id)
        _fail("file_unavailable", "The selected log file no longer exists. Refresh the list and try again.", 404)
    except OSError:
        _fail("file_unavailable", "The selected log file cannot be accessed.", 404)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _audit("denied", scope=root.scope, outcome="denied", file_id=file_id)
        _fail("unsafe_file", "Only regular log files can be accessed.", 400)
    if not _name_allowed(os.path.basename(path)):
        _fail("invalid_file_id", "The log identifier is invalid.", 400)
    return root, path, before.st_size, before.st_mtime_ns


def _open_checked_fd(root: _Root, path: str, expected_mtime_ns: int | None = None) -> tuple[int, os.stat_result]:
    if not _inside_root(path, root.path):
        _fail("unsafe_path", "The selected log file is outside an allowed root.", 400)
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        _fail("file_unavailable", "The selected log file disappeared. Refresh the list and try again.", 404)
    except OSError:
        _fail("file_unavailable", "The selected log file cannot be accessed.", 404)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail("unsafe_file", "Only regular log files can be accessed.", 400)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(root.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        file_fd = os.open(os.path.basename(path), flags, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _fail("file_changed", "The log file changed while it was being opened. Refresh the list and try again.", 409)
        if expected_mtime_ns is not None and before.st_mtime_ns != expected_mtime_ns:
            # A file may grow normally between listing and opening. Do not treat
            # growth as a rotation; the open descriptor is a stable snapshot.
            if before.st_size < 0:
                _fail("file_changed", "The log file changed while it was being opened.", 409)
        return file_fd, opened
    except FileNotFoundError:
        _fail("file_unavailable", "The selected log file disappeared. Refresh the list and try again.", 404)
    except OSError:
        _fail("file_unavailable", "The selected log file cannot be accessed.", 404)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        # Ownership transfers to the caller only after successful return.
        if file_fd is not None and ("opened" not in locals() or not stat.S_ISREG(opened.st_mode) or
                                    (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            os.close(file_fd)


def _directory_listing(root: _Root, root_index: int) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        with os.scandir(root.path) as directory:
            for entry in directory:
                name = entry.name
                if not _name_allowed(name) or entry.is_symlink():
                    continue
                try:
                    item_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(item_stat.st_mode):
                    continue
                path = os.path.join(root.path, name)
                if not _inside_root(path, root.path):
                    continue
                total_bytes += item_stat.st_size
                lower_name = name.lower()
                entries.append({
                    "id": _make_file_id(root_index, name),
                    "name": name,
                    "path": path,
                    "scope": root.scope,
                    "size_bytes": item_stat.st_size,
                    "size_human": _human_size(item_stat.st_size),
                    "modified": datetime.fromtimestamp(item_stat.st_mtime, timezone.utc).isoformat(),
                    "compressed_or_rotated": lower_name.endswith(".gz") or bool(re.search(r"\.log\.\d+(?:\.gz)?$", lower_name)),
                })
    except FileNotFoundError:
        # A missing log directory is normal on a fresh bench.
        return [], 0
    except OSError:
        return [], 0
    entries.sort(key=lambda item: (item["scope"], item["name"].casefold()))
    return entries, total_bytes


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _threshold_bytes() -> int:
    value: Any = _DEFAULT_DOWNLOAD_CONFIRM_MB
    try:
        settings = frappe.get_single("Observability Settings")
        configured = settings.get("download_confirm_threshold") if hasattr(settings, "get") else getattr(settings, "download_confirm_threshold", None)
        if configured not in (None, ""):
            value = float(configured)
    except Exception:
        value = _DEFAULT_DOWNLOAD_CONFIRM_MB
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            number = float(_DEFAULT_DOWNLOAD_CONFIRM_MB)
    except (TypeError, ValueError):
        number = float(_DEFAULT_DOWNLOAD_CONFIRM_MB)
    return int(number * 1024 * 1024)


def _is_confirmed(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}


def _redact(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    patterns = []
    try:
        settings = frappe.get_cached_doc("Observability Settings")
        raw_patterns = settings.get("redaction_patterns") if hasattr(settings, "get") else None
        if isinstance(raw_patterns, str):
            raw_patterns = json.loads(raw_patterns)
        if isinstance(raw_patterns, (list, tuple)):
            patterns = [re.compile(pattern[:200], re.IGNORECASE) for pattern in raw_patterns[:20]
                        if isinstance(pattern, str) and pattern]
    except Exception:
        patterns = []
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        if _SENSITIVE_LINE.search(line) or any(pattern.search(line) for pattern in patterns):
            ending = "\n" if line.endswith("\n") else ""
            if line.endswith("\r\n"):
                ending = "\r\n"
            output.append("[REDACTED]" + ending)
        else:
            output.append(line)
    return "".join(output)


def _tail_payload(root: _Root, path: str, end_offset: int, limit: int) -> tuple[dict[str, Any], int]:
    file_fd, opened = _open_checked_fd(root, path)
    size = opened.st_size
    end = min(max(0, end_offset), size)
    read_size = min(max(0, limit), end)
    try:
        with os.fdopen(file_fd, "rb", buffering=0) as file_handle:
            while True:
                start = end - read_size
                file_handle.seek(start)
                raw = file_handle.read(read_size)
                text = _redact(raw)
                payload = {
                    "ok": True,
                    "text": text,
                    "start_offset": start,
                    "end_offset": start + len(raw),
                    "file_size": size,
                    "bytes": len(raw),
                    "has_more": start > 0,
                    "next_offset": start,
                    "redacted": True,
                }
                encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                # The framework adds a small response envelope. Leave additional headroom
                # so pathological UTF-8 replacement and escaping remain below 1 MiB.
                if encoded_size <= _TAIL_JSON_BUDGET or read_size == 0:
                    return payload, len(raw)
                read_size = max(0, read_size // 2)
    except FileNotFoundError:
        _fail("file_unavailable", "The selected log file disappeared. Refresh the list and try again.", 404)
    except OSError:
        _fail("file_unavailable", "The selected log file cannot be read.", 404)


def _safe_download_name(name: str, gzip_output: bool = False) -> str:
    output = name + (".gz" if gzip_output else "")
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", output)[:180] or "log-download"
    encoded = quote(output, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _raw_chunks(file_fd: int, expected_size: int) -> Iterator[bytes]:
    with os.fdopen(file_fd, "rb", buffering=0) as source:
        remaining = expected_size
        while remaining > 0:
            chunk = source.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                # A concurrent truncation yields a short Content-Length, which
                # clients can detect; an open descriptor survives log rotation.
                return
            remaining -= len(chunk)
            yield chunk


def _gzip_chunks(file_fd: int, expected_size: int) -> Iterator[bytes]:
    compressor = zlib.compressobj(level=6, wbits=16 + zlib.MAX_WBITS)
    with os.fdopen(file_fd, "rb", buffering=0) as source:
        remaining = expected_size
        while remaining > 0:
            chunk = source.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            compressed = compressor.compress(chunk)
            if compressed:
                yield compressed
        final = compressor.flush()
        if final:
            yield final


def _streaming_response(file_fd: int, size: int, name: str, *, gzip_output: bool = False) -> Response:
    response_name = name + (".gz" if gzip_output else "")
    content_type = "application/gzip" if gzip_output else (mimetypes.guess_type(name)[0] or "application/octet-stream")
    body = _gzip_chunks(file_fd, size) if gzip_output else _raw_chunks(file_fd, size)
    response = Response(body, status=200, content_type=content_type, direct_passthrough=True)
    if not gzip_output:
        response.content_length = size
    response.headers["Content-Disposition"] = _safe_download_name(name, gzip_output)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Log-Download-Warning"] = "Raw log data may contain secrets and personal data."
    response.headers["X-Log-Source-Bytes"] = str(size)
    response.headers["X-Log-Compression"] = "gzip" if gzip_output else "none"
    return response


@frappe.whitelist(allow_guest=False)
def list_logs() -> dict[str, Any]:
    """List allowed direct-child log files visible to the current user's role."""
    try:
        _require_any_log_role()
        entries: list[dict[str, Any]] = []
        totals: dict[str, int] = {"bench": 0, "site": 0}
        free_space: dict[str, int] = {}
        roots = _roots()
        for root_index, root in enumerate(roots):
            try:
                _require_scope(root.scope)
            except _LogBrowserError:
                continue
            root_entries, total = _directory_listing(root, root_index)
            entries.extend(root_entries)
            totals[root.scope] = totals.get(root.scope, 0) + total
            try:
                free_space[root.scope] = max(free_space.get(root.scope, 0), shutil.disk_usage(root.path).free)
            except OSError:
                pass
        entries.sort(key=lambda item: (item["scope"], item["name"].casefold(), item["path"].casefold()))
        _audit("list", byte_count=0)
        return {
            "ok": True,
            "files": entries,
            "totals": {scope: {"size_bytes": size, "size_human": _human_size(size)} for scope, size in totals.items()},
            "disk_free_bytes": free_space,
            "raw_download_warning": "Downloaded logs may contain secrets and personal data.",
            "download_confirmation_threshold_bytes": _threshold_bytes(),
        }
    except _LogBrowserError as error:
        try:
            _audit("list", outcome="denied")
        except Exception:
            pass
        return _structured_error(error)
    except Exception:
        return _unexpected_error()


@frappe.whitelist(allow_guest=False)
def tail_log(file_id: str, offset: int | str | None = None, limit: int | str | None = None) -> dict[str, Any]:
    """Return one redacted byte-offset page; ``offset`` is its exclusive end."""
    try:
        root, path, size, _mtime_ns = _open_listed_file(file_id)
        _require_scope(root.scope)
        if offset in (None, ""):
            end_offset = size
        else:
            try:
                end_offset = int(offset)
            except (TypeError, ValueError):
                _fail("invalid_offset", "Offset must be a non-negative byte position.", 400)
            if end_offset < 0:
                _fail("invalid_offset", "Offset must be a non-negative byte position.", 400)
            end_offset = min(end_offset, size)
        requested_limit = _MAX_TAIL_RESPONSE
        if limit not in (None, ""):
            try:
                requested_limit = int(limit)
            except (TypeError, ValueError):
                _fail("invalid_limit", "Limit must be a positive byte count.", 400)
            if requested_limit <= 0:
                _fail("invalid_limit", "Limit must be a positive byte count.", 400)
        requested_limit = min(requested_limit, _MAX_TAIL_RESPONSE)
        payload, byte_count = _tail_payload(root, path, end_offset, requested_limit)
        _audit("tail", scope=root.scope, path=path, byte_count=byte_count, file_id=file_id)
        return payload
    except _LogBrowserError as error:
        if error.code in {"invalid_file_id", "unsafe_path", "unsafe_file", "permission_denied"}:
            try:
                _audit("tail", outcome="denied", file_id=file_id if isinstance(file_id, str) else None)
            except Exception:
                pass
        return _structured_error(error)
    except Exception:
        return _unexpected_error()


def _download(file_id: str, confirm: Any, *, gzip_output: bool) -> Response | dict[str, Any]:
    action = "download_gzip" if gzip_output else "download"
    file_fd: int | None = None
    handed_to_response = False
    try:
        root, path, _listed_size, listed_mtime_ns = _open_listed_file(file_id)
        _require_scope(root.scope)
        file_fd, opened = _open_checked_fd(root, path, listed_mtime_ns)
        size = opened.st_size
        threshold = _threshold_bytes()
        if size > threshold and not _is_confirmed(confirm):
            os.close(file_fd)
            _audit(action, scope=root.scope, path=path, byte_count=0, outcome="confirmation_required", file_id=file_id)
            return {
                "ok": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "This log exceeds the download confirmation threshold.",
                    "size_bytes": size,
                    "threshold_bytes": threshold,
                },
                "raw_download_warning": "Downloaded logs may contain secrets and personal data.",
            }
        _audit(action, scope=root.scope, path=path, byte_count=size, file_id=file_id)
        response = _streaming_response(file_fd, size, os.path.basename(path), gzip_output=gzip_output)
        handed_to_response = True
        return response
    except _LogBrowserError as error:
        if file_fd is not None and not handed_to_response:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if error.code in {"invalid_file_id", "unsafe_path", "unsafe_file", "permission_denied"}:
            try:
                _audit(action, outcome="denied", file_id=file_id if isinstance(file_id, str) else None)
            except Exception:
                pass
        return _structured_error(error)
    except Exception:
        if file_fd is not None and not handed_to_response:
            try:
                os.close(file_fd)
            except OSError:
                pass
        return _unexpected_error()


@frappe.whitelist(allow_guest=False)
def download_log(file_id: str, confirm: bool | str = False) -> Response | dict[str, Any]:
    """Stream raw bytes in 64 KiB chunks. The response warns that contents are unredacted."""
    return _download(file_id, confirm, gzip_output=False)


@frappe.whitelist(allow_guest=False)
def download_log_gzip(file_id: str, confirm: bool | str = False) -> Response | dict[str, Any]:
    """Stream a gzip-compressed copy without buffering the complete file."""
    return _download(file_id, confirm, gzip_output=True)
