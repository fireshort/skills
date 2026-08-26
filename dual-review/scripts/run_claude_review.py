#!/usr/bin/env python3
"""Run a Claude Code review in the background and preserve complete output."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import re
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

SYSTEM_GUARD = (
    "You are one tool-free reviewer in a dual-review workflow. Review only the "
    "preflight-approved snapshot supplied in the prompt. Do not modify files, create "
    "plans, call ExitPlanMode, invoke skills, or start other agents. Return the review "
    "directly."
)
DEFAULT_TIMEOUT_SECONDS = 1800
STARTING_GRACE_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 60
JSON_IO_ATTEMPTS = 10
MAX_PROMPT_BYTES = 500_000
MAX_BASE64_TRANSPORT_BYTES = 4 * ((MAX_PROMPT_BYTES + 2) // 3) + 4
CLEANUP_EXIT_WAIT_SECONDS = 5
WATCHDOG_EXIT_WAIT_SECONDS = 20
STOP_WAIT_SECONDS = 30
STATE_PREFIX = "dual-review-claude-"
WORK_PREFIX = "dual-review-work-claude-"
START_RECEIPT_PREFIX = "DUAL_REVIEW_START_"
END_RECEIPT_PREFIX = "DUAL_REVIEW_END_"
TOKEN_ENV = "DUAL_REVIEW_OWNERSHIP_TOKEN"
CHILD_MARKER_ENV = "DUAL_REVIEW_CHILD_MARKER"
START_RECEIPT_LABEL = "Before any other text, output this receipt on its own line: "
END_RECEIPT_LABEL = (
    "Finish with exactly this token on its own final line and nothing after it: "
)
LEGACY_END_RECEIPT_LABEL = (
    "At the very end of your response, output this receipt on its own line: "
)
# Only the end label changed during receipt-hardening; accept the prior emitted form.
CLAUDE_QUOTA_PATTERNS = (
    r"\busage limit\b",
    r"\bsession limit\b",
    r"\brate limit(?:ed| exceeded)?\b",
    r"\bquota (?:exceeded|exhausted|reached)\b",
    r"\bout of (?:credits|quota)\b",
    r"\byou(?:'ve| have) hit (?:your|the) [^\r\n]{0,80}\blimit\b",
    r"\b(?:429|resource exhausted)\b[^\r\n]{0,120}\b(?:quota|rate|limit)\b",
)
CLAUDE_STDOUT_QUOTA_PATTERN = re.compile(
    r"\Ayou(?:'ve| have) hit your weekly limit\s*·\s*resets\s+"
    r"(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:am|pm)\s+"
    r"\([a-z0-9_+./-]{1,64}\)\Z",
    flags=re.IGNORECASE,
)
MAX_CLAUDE_QUOTA_STDOUT_BYTES = 512
PROMPT_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
WINDOWS_CREATE_SUSPENDED = 0x00000004
WINDOWS_NATIVE_SUFFIXES = {".com", ".exe"}
POSIX_NATIVE_MAGICS = (
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)


class JsonArgumentParser(argparse.ArgumentParser):
    """Emit machine-readable errors for the public start command."""

    def error(self, message: str) -> None:
        if len(sys.argv) > 1 and sys.argv[1] == "start":
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "state_dir": None,
                        "worker_pid": None,
                        "error_kind": "argument_error",
                        "worker_error": f"ArgumentError: {message}",
                    },
                    ensure_ascii=False,
                )
            )
            raise SystemExit(2)
        super().error(message)

class StartReviewError(RuntimeError):
    """A start failure that can be reported through the public JSON contract."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


class PromptInputError(ValueError):
    """A stdin prompt that cannot be transported losslessly."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


def read_utf8_prompt(*, base64_input: bool = False) -> tuple[str, bytes]:
    """Read one bounded prompt and return its strict UTF-8 payload bytes."""

    if sys.stdin.isatty():
        transport = "base64" if base64_input else "UTF-8"
        raise PromptInputError(
            "invalid_prompt", f"prompt must be piped through stdin as {transport}"
        )
    transport_limit = (
        MAX_BASE64_TRANSPORT_BYTES if base64_input else MAX_PROMPT_BYTES
    )
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is None:
        text = sys.stdin.read(transport_limit + 1)
        try:
            transport_raw = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise PromptInputError(
                "invalid_prompt", f"stdin prompt is not valid Unicode: {exc}"
            ) from exc
    else:
        transport_raw = binary_stream.read(transport_limit + 1)
    if len(transport_raw) > transport_limit:
        error_kind = (
            "prompt_transport_too_large" if base64_input else "prompt_too_large"
        )
        transport_hint = (
            "; base64 input must be unwrapped (without inserted whitespace)"
            if base64_input
            else ""
        )
        raise PromptInputError(
            error_kind,
            f"stdin transport exceeds {transport_limit} bytes{transport_hint}",
        )
    if base64_input:
        compact_transport = b"".join(transport_raw.split())
        if compact_transport != transport_raw:
            raise PromptInputError(
                "invalid_prompt", "stdin prompt must be unwrapped base64 without whitespace"
            )
        try:
            raw = base64.b64decode(compact_transport, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PromptInputError(
                "invalid_prompt",
                f"stdin prompt must be unwrapped base64 of UTF-8 bytes: {exc}",
            ) from exc
    else:
        raw = transport_raw
    if len(raw) > MAX_PROMPT_BYTES:
        raise PromptInputError(
            "prompt_too_large",
            f"prompt is {len(raw)} bytes; maximum is {MAX_PROMPT_BYTES}",
        )
    try:
        prompt = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PromptInputError(
            "invalid_prompt",
            "decoded prompt must be strict UTF-8; check the producer encoding, "
            f"PowerShell $OutputEncoding, or use --prompt-base64-stdin: {exc}",
        ) from exc
    if not prompt.strip():
        raise PromptInputError("invalid_prompt", "stdin prompt is empty")
    return prompt, raw


class ReviewStopped(RuntimeError):
    """Raised when the owning worker accepts a caller cancellation request."""


class ReceiptMissing(RuntimeError):
    """Raised when Claude does not return both exact delivery receipts."""


class EmptyReview(RuntimeError):
    """Raised when Claude returns receipts but no review body."""


def receipt_line_matches(line: str, receipt: str, labels: tuple[str, ...]) -> bool:
    """Accept a bare receipt or one of the explicit labels sent to Claude."""

    normalized = line.strip().strip("`*_ ")
    return normalized == receipt or any(
        normalized == label + receipt for label in labels
    )


def frame_review_prompt(
    prompt_raw: bytes, start_receipt: str, end_receipt: str
) -> bytes:
    """Frame the approved prompt without normalizing any approved bytes."""

    return (
        (START_RECEIPT_LABEL + start_receipt + "\n\n").encode("utf-8")
        + prompt_raw
        + ("\n\n" + END_RECEIPT_LABEL + end_receipt + "\n").encode("utf-8")
    )


def write_stdout_bytes(data: bytes) -> None:
    """Write exact bytes when stdout exposes its binary buffer."""

    binary_stream = getattr(sys.stdout, "buffer", None)
    if binary_stream is not None:
        binary_stream.write(data)
        binary_stream.flush()
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for attempt in range(JSON_IO_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == JSON_IO_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: Path) -> dict[str, Any]:
    for attempt in range(JSON_IO_ATTEMPTS):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"state file is not a JSON object: {path}")
            return data
        except (PermissionError, json.JSONDecodeError):
            if attempt == JSON_IO_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def read_text_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def validate_helper_state_dir(state_dir: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    if state_dir.parent != temp_root or not state_dir.name.startswith(STATE_PREFIX):
        raise SystemExit(
            f"state directory must be a direct child of {temp_root} "
            f"with prefix {STATE_PREFIX}: {state_dir}"
        )


def validate_helper_work_dir(work_dir: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    is_junction = getattr(work_dir, "is_junction", lambda: False)()
    if (
        not work_dir.is_absolute()
        or work_dir.is_symlink()
        or is_junction
        or work_dir.parent.resolve() != temp_root
        or not work_dir.name.startswith(WORK_PREFIX)
    ):
        raise SystemExit(
            f"work directory must be a non-symlink direct child of {temp_root} "
            f"with prefix {WORK_PREFIX}: {work_dir}"
        )


def validate_runtime_security() -> None:
    if sys.version_info < (3, 10):
        raise StartReviewError(
            "insecure_python_runtime",
            "Python 3.10 or newer is required",
        )
    if os.name == "nt" and (
        sys.implementation.name != "cpython" or sys.version_info < (3, 12, 4)
    ):
        raise StartReviewError(
            "insecure_python_runtime",
            "Windows requires CPython 3.12.4 or newer so mode 0o700 creates a "
            "private state-directory ACL",
        )


def validate_native_claude_binary(claude: str) -> None:
    path = Path(claude)
    if os.name == "nt":
        if path.suffix.lower() not in WINDOWS_NATIVE_SUFFIXES:
            raise StartReviewError(
                "unsupported_claude_launcher",
                "Windows requires a native Claude Code .exe or .com executable; "
                "command/script shims cannot prove empty --tools preservation",
            )
        return
    try:
        with path.open("rb") as executable:
            magic = executable.read(4)
    except OSError as exc:
        raise StartReviewError(
            "unsupported_claude_launcher",
            f"cannot inspect Claude Code executable header: {exc}",
        ) from exc
    if magic not in POSIX_NATIVE_MAGICS:
        raise StartReviewError(
            "unsupported_claude_launcher",
            "POSIX requires a native ELF or Mach-O Claude Code executable; shell "
            "and script wrappers cannot prove empty --tools preservation",
        )


def create_private_directories() -> tuple[Path, Path]:
    state_dir = Path(tempfile.mkdtemp(prefix=STATE_PREFIX)).resolve()
    work_dir: Path | None = None
    try:
        work_dir = Path(tempfile.mkdtemp(prefix=WORK_PREFIX)).resolve()
        if os.name != "nt":
            os.chmod(state_dir, 0o700)
            os.chmod(work_dir, 0o700)
        return state_dir, work_dir
    except BaseException:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        raise


def remove_private_directories(state_dir: Path, work_dir: Path | None) -> None:
    if work_dir is not None and work_dir.exists():
        validate_helper_work_dir(work_dir)
        if not work_dir.is_dir():
            raise OSError(f"work path is not a directory: {work_dir}")
        shutil.rmtree(work_dir)
    shutil.rmtree(state_dir)


def validate_claude_contract(claude: str) -> None:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed: subprocess.CompletedProcess[str] | None = None
    last_error: BaseException | None = None
    for attempt in range(2):
        try:
            completed = subprocess.run([claude, "--help"], **options)
            break
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
    if completed is None:
        raise StartReviewError(
            "cli_contract",
            f"cannot verify Claude Code CLI contract after retry: {last_error}",
        )
    help_text = " ".join((completed.stdout or "").split())

    def mentions_flag(flag: str) -> bool:
        return (
            re.search(
                rf"(?<![\w-]){re.escape(flag)}(?=$|[\s,=\[\]<>])",
                help_text,
            )
            is not None
        )

    required_flags = [
        "--safe-mode",
        "--permission-mode",
        "--tools",
        "--append-system-prompt",
        "--no-session-persistence",
        "--no-chrome",
        "--output-format",
    ]
    missing = [flag for flag in required_flags if not mentions_flag(flag)]
    if not mentions_flag("--print") and not mentions_flag("-p"):
        missing.append("--print/-p")
    normalized_help = help_text.lower()
    if "dontask" not in normalized_help:
        missing.append("dontAsk permission mode")
    has_empty_marker = any(
        marker in normalized_help
        for marker in (
            'use ""',
            "use ''",
            'set to ""',
            "set to ''",
            'pass ""',
            "empty string",
        )
    )
    if "disable all tools" not in normalized_help or not has_empty_marker:
        missing.append('explicit `--tools ""` disable-all semantics')
    if completed.returncode != 0 or missing:
        detail = ", ".join(missing) if missing else f"exit {completed.returncode}"
        excerpt = help_text[:1000]
        version = "unknown"
        try:
            version_result = subprocess.run([claude, "--version"], **options)
            version = " ".join((version_result.stdout or "").split())[:200]
        except (OSError, subprocess.TimeoutExpired) as exc:
            version = f"unavailable: {type(exc).__name__}: {exc}"
        raise StartReviewError(
            "cli_contract",
            f"Claude Code CLI contract is incompatible: {detail}; "
            f"version: {version}; help: {excerpt}",
        )

def read_linux_process_stat(pid: int) -> tuple[str, int, str]:
    """Return Linux process state, process group, and start-time marker."""

    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split()
    if closing < 0 or len(fields) <= 19:
        raise ValueError(f"invalid /proc stat record for PID {pid}")
    return fields[0], int(fields[2]), fields[19]


def linux_process_is_zombie(pid: int) -> bool | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        state, _, _ = read_linux_process_stat(pid)
    except (OSError, ValueError):
        return None
    return state in {"Z", "X", "x"}


def process_is_running(pid: int) -> bool | None:
    """Return True, False, or None when permissions prevent a reliable probe."""
    if os.name == "nt":
        synchronize = 0x00100000
        wait_object_0 = 0
        wait_timeout = 258
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return False
            return None
        try:
            wait_result = kernel32.WaitForSingleObject(handle, 0)
            if wait_result == wait_timeout:
                return True
            if wait_result == wait_object_0:
                return False
            return None
        finally:
            kernel32.CloseHandle(handle)

    if sys.platform.startswith("linux"):
        try:
            state, _, _ = read_linux_process_stat(pid)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return None
        return state not in {"Z", "X", "x"}

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def process_identity(pid: int) -> str | None:
    """Return a creation marker for PID-reuse checks, or None if unavailable."""
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong),
            ]

        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            marker = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return f"windows:{marker}"
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            _, _, start_time = read_linux_process_stat(pid)
            return f"linux:{start_time}"
        except (OSError, ValueError):
            return None
    try:
        completed = subprocess.run(
            ["ps", "ww", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    marker = (completed.stdout or "").strip()
    return f"ps:{marker}" if completed.returncode == 0 and marker else None


def process_has_child_marker(pid: int, marker: str) -> bool | None:
    """Verify the random marker inherited only by the recorded Claude process."""

    if not marker:
        return None
    expected = f"{CHILD_MARKER_ENV}={marker}"
    if sys.platform.startswith("linux"):
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return None
        return expected.encode("utf-8") in environment
    try:
        completed = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return expected in (completed.stdout or "")


def owned_process_state(pid: int, expected_identity: str | None) -> bool | None:
    """Return whether the exact recorded process is running."""
    if not pid:
        return False
    running = process_is_running(pid)
    if running is not True:
        return running
    if not expected_identity:
        return None
    current_identity = process_identity(pid)
    if current_identity is None:
        return None
    return current_identity == expected_identity


def read_owned_state(state_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    owner_path = state_dir / "owner.json"
    launch_path = state_dir / "launch.json"
    if not owner_path.exists() or not launch_path.exists():
        raise SystemExit(f"state directory is invalid: {state_dir}")
    owner = read_json(owner_path)
    launch = read_json(launch_path)
    state_token = owner.get("token")
    if (
        owner.get("helper") != "dual-review"
        or not isinstance(state_token, str)
        or len(state_token) < 32
        or launch.get("token") != state_token
    ):
        raise SystemExit("state ownership token is missing or mismatched")
    if int(launch.get("protocol_version") or 0) >= 5:
        owner_work_dir = owner.get("work_dir")
        if (
            not isinstance(owner_work_dir, str)
            or launch.get("work_dir") != owner_work_dir
        ):
            raise SystemExit("work directory ownership is missing or mismatched")
        validate_helper_work_dir(Path(owner_work_dir))
    return owner, launch

class JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JobObjectBasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ThreadID", ctypes.c_ulong),
        ("th32OwnerProcessID", ctypes.c_ulong),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
    ]


def create_kill_on_close_job(process_pid: int) -> int | None:
    if os.name != "nt":
        return None
    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000
    process_set_quota = 0x0100
    process_terminate = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    process_handle = kernel32.OpenProcess(
        process_set_quota | process_terminate, False, process_pid
    )
    if not process_handle:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "OpenProcess for job assignment failed")
    try:
        information = JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        if not kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    finally:
        kernel32.CloseHandle(process_handle)
    return int(job)


def resume_suspended_process(process_pid: int) -> None:
    if os.name != "nt":
        return
    th32cs_snapthread = 0x00000004
    thread_suspend_resume = 0x0002
    invalid_handle_value = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32First.restype = ctypes.c_int
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32Next.restype = ctypes.c_int
    kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel32.ResumeThread.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
    if snapshot == invalid_handle_value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    resumed = 0
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == process_pid:
                thread_handle = kernel32.OpenThread(
                    thread_suspend_resume, False, entry.th32ThreadID
                )
                if not thread_handle:
                    raise OSError(ctypes.get_last_error(), "OpenThread failed")
                try:
                    previous_count = kernel32.ResumeThread(thread_handle)
                    if previous_count == 0xFFFFFFFF:
                        raise OSError(ctypes.get_last_error(), "ResumeThread failed")
                    resumed += 1
                finally:
                    kernel32.CloseHandle(thread_handle)
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    if resumed == 0:
        raise RuntimeError("suspended Claude process had no resumable thread")


def close_job(job_handle: int | None) -> None:
    if os.name != "nt" or job_handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(ctypes.c_void_p(job_handle))


def terminate_popen(process: subprocess.Popen[Any]) -> str | None:
    errors: list[str] = []
    if process.poll() is not None:
        return None
    try:
        process.terminate()
    except OSError as exc:
        errors.append(f"terminate failed: {type(exc).__name__}: {exc}")
    try:
        process.wait(timeout=5)
        return "; ".join(errors) or None
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError as exc:
        errors.append(f"kill failed: {type(exc).__name__}: {exc}")
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"wait after kill failed: {type(exc).__name__}: {exc}")
    return "; ".join(errors) or None


def process_group_is_running(pgid: int) -> bool | None:
    if os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None

    if not sys.platform.startswith("linux"):
        return True

    saw_group_member = False
    unreadable = False
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            state, member_pgid, _ = read_linux_process_stat(int(entry.name))
        except FileNotFoundError:
            continue
        except PermissionError:
            unreadable = True
            continue
        except (OSError, ValueError):
            continue
        if member_pgid != pgid:
            continue
        saw_group_member = True
        if state not in {"Z", "X", "x"}:
            return True
    if unreadable:
        return None
    if saw_group_member:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return None


def terminate_claude(
    process: subprocess.Popen[Any],
    pgid: int | None,
    expected_identity: str | None = None,
    child_marker: str | None = None,
) -> None:
    if os.name == "nt":
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return
    if pgid is None:
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return

    def refuse_group_signal(reason: str) -> None:
        was_running = process.poll() is None
        termination_error = terminate_popen(process)
        if termination_error:
            detail = f"; direct child termination failed: {termination_error}"
        elif was_running:
            detail = "; direct child terminated"
        else:
            detail = "; direct child already exited"
        raise RuntimeError(reason + detail)

    process.poll()
    group_state = process_group_is_running(pgid)
    if group_state is False:
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return
    if group_state is None:
        refuse_group_signal("cannot verify Claude process group before termination")
    current_identity = process_identity(process.pid)
    if not expected_identity or current_identity != expected_identity:
        refuse_group_signal("refusing group signal because Claude identity is unavailable")
    marker_verified = (
        process_has_child_marker(process.pid, child_marker or "") is True
    )
    if not marker_verified and linux_process_is_zombie(process.pid) is not True:
        refuse_group_signal("refusing group signal because child marker is unavailable")

    def finish_if_group_stopped(phase: str) -> bool:
        process.poll()
        group_state = process_group_is_running(pgid)
        if group_state is None:
            refuse_group_signal(f"cannot verify Claude process group {phase}")
        if group_state is not False:
            return False
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if finish_if_group_stopped("after SIGTERM"):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        termination_error = terminate_popen(process)
        if termination_error:
            raise RuntimeError(termination_error)
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if finish_if_group_stopped("after SIGKILL"):
            return
        time.sleep(0.1)
    refuse_group_signal("Claude process group survived SIGKILL")


def write_start_failure(state_dir: Path, exc: BaseException) -> None:
    error = f"{type(exc).__name__}: {exc}"
    try:
        write_json(
            state_dir / "result.json",
            {
                "state": "failed",
                "return_code": None,
                "completed_at": time.time(),
                "stdout_path": str(state_dir / "stdout.txt"),
                "stderr_path": str(state_dir / "stderr.txt"),
                "worker_error": error,
                "safe_to_cleanup": True,
            },
        )
    except OSError:
        pass


def _start_review(args: argparse.Namespace) -> int:
    validate_runtime_security()
    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        raise StartReviewError("invalid_cwd", f"cwd is not a directory: {cwd}")
    if args.timeout <= 0:
        raise StartReviewError("invalid_timeout", "timeout must be greater than zero")

    public_transport = "base64-stdin-v1"
    try:
        prompt, prompt_raw = read_utf8_prompt(base64_input=True)
    except PromptInputError as exc:
        raise StartReviewError(exc.error_kind, str(exc)) from exc
    prompt_bytes = len(prompt_raw)
    prompt_sha256 = hashlib.sha256(prompt_raw).hexdigest()

    claude = shutil.which(args.claude_bin)
    if not claude:
        raise StartReviewError(
            "claude_missing", f"Claude Code executable not found: {args.claude_bin}"
        )
    claude = str(Path(claude).resolve())
    validate_native_claude_binary(claude)
    validate_claude_contract(claude)

    state_dir, work_dir = create_private_directories()
    worker: subprocess.Popen[Any] | None = None
    state_created = True
    state_token = secrets.token_urlsafe(32)
    try:
        write_json(
            state_dir / "owner.json",
            {
                "helper": "dual-review",
                "token": state_token,
                "created_at": time.time(),
                "work_dir": str(work_dir),
            },
        )
        launch = {
            "state": "starting",
            "protocol_version": 5,
            "prompt_transport": public_transport,
            "worker_prompt_transport": "utf8-bytes-v1",
            "worker_record_before_child": True,
            "cwd": str(cwd),
            "claude": claude,
            "worker_executable": sys.executable,
            "worker_identity": None,
            "token": state_token,
            "work_dir": str(work_dir),
            "created_at": time.time(),
            "timeout_seconds": args.timeout,
            "prompt_bytes": prompt_bytes,
            "prompt_sha256": prompt_sha256,
        }
        write_json(state_dir / "launch.json", launch)

        worker_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_worker",
            "--state-dir",
            str(state_dir),
            "--claude-bin",
            claude,
            "--timeout",
            str(args.timeout),
        ]
        worker_env = os.environ.copy()
        worker_env[TOKEN_ENV] = state_token
        popen_options: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "close_fds": True,
            "env": worker_env,
        }
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_options["start_new_session"] = True

        bootstrap_path = state_dir / "bootstrap.log"
        with bootstrap_path.open("a", encoding="utf-8", errors="replace") as bootstrap:
            worker = subprocess.Popen(
                worker_command,
                stdout=bootstrap,
                stderr=bootstrap,
                **popen_options,
            )
        worker_identity = process_identity(worker.pid)
        if worker_identity is None:
            raise RuntimeError("cannot establish worker process identity")
        launch.update(
            {
                "state": "running",
                "worker_pid": worker.pid,
                "worker_identity": worker_identity,
            }
        )
        write_json(state_dir / "launch.json", launch)
        if worker.stdin is None:
            raise RuntimeError("worker stdin pipe was not created")
        worker.stdin.write(prompt_raw)
        worker.stdin.flush()
        worker.stdin.close()
        del prompt
        del prompt_raw
        write_json(state_dir / "go.json", {"state": "go", "created_at": time.time()})
    except BaseException as exc:
        cleanup_errors: list[str] = []
        worker_stopped = worker is None
        if worker is not None:
            termination_error = terminate_popen(worker)
            if termination_error:
                cleanup_errors.append(termination_error)
            worker_stopped = worker.poll() is not None
            if not worker_stopped:
                cleanup_errors.append(
                    "worker termination could not be confirmed; state directory preserved"
                )
        cleaned_up = False
        if state_created and worker_stopped:
            write_start_failure(state_dir, exc)
            try:
                remove_private_directories(state_dir, work_dir)
                cleaned_up = True
            except OSError as cleanup_exc:
                cleanup_errors.append(
                    f"state cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        error = f"{type(exc).__name__}: {exc}"
        if cleanup_errors:
            error += "; cleanup: " + "; ".join(cleanup_errors)
        print(
            json.dumps(
                {
                    "state": "failed",
                    "state_dir": (
                        None if cleaned_up or not state_created else str(state_dir)
                    ),
                    "worker_pid": worker.pid if worker is not None else None,
                    "state_created": state_created,
                    "cleaned_up": cleaned_up,
                    "error_kind": "startup_error",
                    "worker_error": error,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "state": "running",
                "state_dir": str(state_dir),
                "worker_pid": worker.pid,
            },
            ensure_ascii=False,
        )
    )
    return 0

def start_review(args: argparse.Namespace) -> int:
    try:
        return _start_review(args)
    except BaseException as exc:
        error_kind = (
            exc.error_kind
            if isinstance(exc, StartReviewError)
            else "start_validation_error"
        )
        print(
            json.dumps(
                {
                    "state": "failed",
                    "state_dir": None,
                    "worker_pid": None,
                    "error_kind": error_kind,
                    "worker_error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 1


def run_worker(args: argparse.Namespace) -> int:
    token = os.environ.pop(TOKEN_ENV, "")
    if len(token) < 32:
        raise SystemExit("worker ownership token is missing")
    state_dir = Path(args.state_dir).resolve()
    validate_helper_state_dir(state_dir)
    go_path = state_dir / "go.json"
    cancel_path = state_dir / "cancel.json"
    stdout_path = state_dir / "stdout.txt"
    stderr_path = state_dir / "stderr.txt"
    result_path = state_dir / "result.json"
    worker_path = state_dir / "worker.json"
    worker_identity: str | None = None
    process: subprocess.Popen[bytes] | None = None
    claude_pgid: int | None = None
    claude_identity: str | None = None
    child_marker: str | None = None
    job_handle: int | None = None
    watchdog: subprocess.Popen[Any] | None = None
    watchdog_identity: str | None = None
    watchdog_write_fd: int | None = None
    prompt_bytes: int | None = None
    receipt_verified = False
    started_at = time.time()
    feeder_errors: list[str] = []
    ownership_verified = False

    def release_watchdog() -> str | None:
        nonlocal watchdog, watchdog_write_fd
        if watchdog_write_fd is not None:
            active_write_fd = watchdog_write_fd
            watchdog_write_fd = None
            try:
                os.close(active_write_fd)
            except OSError as exc:
                return f"failed to close POSIX watchdog pipe: {type(exc).__name__}: {exc}"
        if watchdog is None:
            return None
        active_watchdog = watchdog
        try:
            return_code = active_watchdog.wait(timeout=WATCHDOG_EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            termination_error = terminate_popen(active_watchdog)
            if active_watchdog.poll() is not None:
                watchdog = None
            detail = f"; {termination_error}" if termination_error else ""
            return (
                "POSIX watchdog did not finish within "
                f"{WATCHDOG_EXIT_WAIT_SECONDS} seconds{detail}"
            )
        watchdog = None
        if return_code != 0:
            return f"POSIX watchdog exited with code {return_code}"
        return None

    try:
        owner, launch = read_owned_state(state_dir)
        if (
            owner.get("helper") != "dual-review"
            or owner.get("token") != token
            or launch.get("token") != token
        ):
            raise RuntimeError("state ownership token mismatch")
        work_dir = Path(owner["work_dir"])
        ownership_verified = True
        worker_identity = process_identity(os.getpid())
        if worker_identity is None:
            raise RuntimeError("cannot establish worker process identity")
        write_json(
            worker_path,
            {
                "state": "bootstrapping",
                "worker_pid": os.getpid(),
                "worker_identity": worker_identity,
                "claude_pid": None,
                "claude_pgid": None,
                "claude_identity": None,
                "watchdog_pid": None,
                "watchdog_identity": None,
                "token": token,
                "started_at": started_at,
                "heartbeat_at": time.time(),
                "return_code": None,
            },
        )
        prompt, prompt_raw = read_utf8_prompt()
        prompt_bytes = len(prompt_raw)
        prompt_sha256 = hashlib.sha256(prompt_raw).hexdigest()

        go_deadline = time.monotonic() + STARTING_GRACE_SECONDS
        while not go_path.exists():
            if time.monotonic() >= go_deadline:
                raise TimeoutError("parent did not release worker startup")
            time.sleep(0.1)
        _, launch = read_owned_state(state_dir)
        current_worker_identity = process_identity(os.getpid())
        if (
            launch.get("token") != token
            or launch.get("protocol_version") != 5
            or launch.get("prompt_transport") != "base64-stdin-v1"
            or launch.get("worker_prompt_transport") != "utf8-bytes-v1"
            or launch.get("worker_record_before_child") is not True
            or launch.get("worker_identity") != current_worker_identity
            or launch.get("prompt_bytes") != prompt_bytes
            or launch.get("prompt_sha256") != prompt_sha256
            or current_worker_identity is None
            or current_worker_identity != worker_identity
        ):
            raise RuntimeError("worker process identity or stdin prompt metadata mismatch")
        if not work_dir.is_dir() or any(work_dir.iterdir()):
            raise RuntimeError(
                "private Claude work directory is missing, invalid, or not empty"
            )
        write_json(
            worker_path,
            {
                "state": "spawning",
                "worker_pid": os.getpid(),
                "worker_identity": worker_identity,
                "claude_pid": None,
                "claude_pgid": None,
                "claude_identity": None,
                "watchdog_pid": None,
                "watchdog_identity": None,
                "token": token,
                "started_at": started_at,
                "heartbeat_at": time.time(),
                "return_code": None,
            },
        )
        start_receipt = START_RECEIPT_PREFIX + secrets.token_hex(16)
        end_receipt = END_RECEIPT_PREFIX + secrets.token_hex(16)
        delivered_prompt_bytes = frame_review_prompt(
            prompt_raw, start_receipt, end_receipt
        )
        del prompt
        del prompt_raw
        go_path.unlink(missing_ok=True)

        if cancel_path.exists():
            cancel = read_json(cancel_path)
            if cancel.get("token") != token:
                raise RuntimeError("cancel token mismatch")
            raise ReviewStopped("review stopped by caller")
        command = [
            args.claude_bin,
            "-p",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--append-system-prompt",
            SYSTEM_GUARD,
            "--no-session-persistence",
            "--no-chrome",
            "--output-format",
            "text",
        ]
        child_marker = secrets.token_urlsafe(32)
        claude_env = os.environ.copy()
        claude_env[CHILD_MARKER_ENV] = child_marker
        child_options: dict[str, Any] = {"env": claude_env}
        if os.name == "nt":
            child_options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | WINDOWS_CREATE_SUSPENDED
            )
        else:
            child_options["start_new_session"] = True

        write_json(
            worker_path,
            {
                "state": "spawning_child",
                "worker_pid": os.getpid(),
                "worker_identity": worker_identity,
                "claude_pid": None,
                "claude_pgid": None,
                "claude_identity": None,
                "watchdog_pid": None,
                "watchdog_identity": None,
                "token": token,
                "started_at": started_at,
                "heartbeat_at": time.time(),
                "return_code": None,
            },
        )

        with stdout_path.open("wb") as stdout_file:
            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=work_dir,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    **child_options,
                )
                if os.name != "nt":
                    claude_pgid = process.pid
                write_json(
                    worker_path,
                    {
                        "state": "child_created",
                        "worker_pid": os.getpid(),
                        "worker_identity": worker_identity,
                        "claude_pid": process.pid,
                        "claude_pgid": claude_pgid,
                        "claude_identity": None,
                        "watchdog_pid": None,
                        "watchdog_identity": None,
                        "token": token,
                        "started_at": started_at,
                        "heartbeat_at": time.time(),
                        "return_code": None,
                    },
                )
                claude_identity = process_identity(process.pid)
                if claude_identity is None:
                    raise RuntimeError("cannot establish Claude process identity")
                if os.name == "nt":
                    job_handle = create_kill_on_close_job(process.pid)
                    resume_suspended_process(process.pid)
                else:
                    watchdog_read_fd, watchdog_write_fd = os.pipe()
                    watchdog_command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "_watchdog",
                        "--read-fd",
                        str(watchdog_read_fd),
                        "--claude-pid",
                        str(process.pid),
                        "--claude-pgid",
                        str(claude_pgid),
                        "--claude-identity",
                        claude_identity,
                        "--state-dir",
                        str(state_dir),
                    ]
                    watchdog_env = os.environ.copy()
                    watchdog_env[TOKEN_ENV] = token
                    watchdog_env[CHILD_MARKER_ENV] = child_marker
                    try:
                        watchdog = subprocess.Popen(
                            watchdog_command,
                            stdin=subprocess.DEVNULL,
                            stdout=stderr_file,
                            stderr=stderr_file,
                            close_fds=True,
                            pass_fds=(watchdog_read_fd,),
                            start_new_session=True,
                            env=watchdog_env,
                        )
                        watchdog_identity = process_identity(watchdog.pid)
                        if watchdog_identity is None:
                            raise RuntimeError("cannot establish watchdog process identity")
                    finally:
                        os.close(watchdog_read_fd)
                if process.stdin is None:
                    raise RuntimeError("Claude stdin pipe was not created")
                claude_started_monotonic = time.monotonic()
                write_json(
                    worker_path,
                    {
                        "state": "supervised",
                        "worker_pid": os.getpid(),
                        "worker_identity": worker_identity,
                        "claude_pid": process.pid,
                        "claude_pgid": claude_pgid,
                        "claude_identity": claude_identity,
                        "watchdog_pid": (
                            watchdog.pid if watchdog is not None else None
                        ),
                        "watchdog_identity": watchdog_identity,
                        "token": token,
                        "started_at": started_at,
                        "heartbeat_at": time.time(),
                        "return_code": None,
                    },
                )

                def feed_prompt() -> None:
                    try:
                        assert process is not None and process.stdin is not None
                        process.stdin.write(delivered_prompt_bytes)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError) as exc:
                        feeder_errors.append(f"{type(exc).__name__}: {exc}")
                    finally:
                        if process is not None and process.stdin is not None:
                            try:
                                process.stdin.close()
                            except OSError as exc:
                                feeder_errors.append(
                                    f"stdin close failed: {type(exc).__name__}: {exc}"
                                )

                feeder = threading.Thread(target=feed_prompt, daemon=True)
                feeder.start()

                while True:
                    return_code = process.poll()
                    elapsed = time.monotonic() - claude_started_monotonic
                    write_json(
                        worker_path,
                        {
                            "state": "running" if return_code is None else "exited",
                            "worker_pid": os.getpid(),
                            "worker_identity": worker_identity,
                            "claude_pid": process.pid,
                            "claude_pgid": claude_pgid,
                            "claude_identity": claude_identity,
                            "watchdog_pid": watchdog.pid if watchdog is not None else None,
                            "watchdog_identity": watchdog_identity,
                            "token": token,
                            "started_at": started_at,
                            "heartbeat_at": time.time(),
                            "return_code": return_code,
                        },
                    )
                    if return_code is not None:
                        break
                    if cancel_path.exists():
                        cancel = read_json(cancel_path)
                        if cancel.get("token") != token:
                            raise RuntimeError("cancel token mismatch")
                        raise ReviewStopped("review stopped by caller")
                    if watchdog is not None and watchdog.poll() is not None:
                        raise RuntimeError("POSIX watchdog exited before Claude")
                    if elapsed >= args.timeout:
                        raise TimeoutError(
                            f"Claude review exceeded {args.timeout} seconds"
                        )
                    time.sleep(2)
                feeder.join(timeout=5)
                if feeder.is_alive():
                    raise RuntimeError(
                        "prompt feeder did not finish; prompt may be truncated"
                    )
                if feeder_errors:
                    raise RuntimeError(
                        "failed to deliver complete prompt: " + "; ".join(feeder_errors)
                    )

        if return_code == 0:
            review_output = stdout_path.read_bytes().decode("utf-8", errors="strict")
            output_lines = review_output.splitlines(keepends=True)
            line_contents = [line.rstrip("\r\n") for line in output_lines]
            nonempty_indexes = [
                index for index, line in enumerate(line_contents) if line.strip()
            ]
            start_labels = (
                START_RECEIPT_LABEL,
            )
            end_labels = (
                END_RECEIPT_LABEL,
                LEGACY_END_RECEIPT_LABEL,
            )
            if (
                len(nonempty_indexes) < 2
                or not receipt_line_matches(
                    line_contents[nonempty_indexes[0]], start_receipt, start_labels
                )
                or not receipt_line_matches(
                    line_contents[nonempty_indexes[-1]], end_receipt, end_labels
                )
            ):
                raise ReceiptMissing(
                    "Claude output did not return accepted start and end receipts"
                )
            first_index = nonempty_indexes[0]
            last_index = nonempty_indexes[-1]
            cleaned_output = "".join(output_lines[first_index + 1 : last_index])
            if not cleaned_output.strip():
                raise EmptyReview("Claude returned receipts but no review body")
            stdout_path.write_bytes(cleaned_output.encode("utf-8", errors="strict"))
            receipt_verified = True
        watchdog_error = release_watchdog()
        result: dict[str, Any] = {
            "state": "done" if return_code == 0 and receipt_verified else "failed",
            "return_code": return_code,
            "completed_at": time.time(),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "receipt_verified": receipt_verified,
            "prompt_bytes": prompt_bytes,
        }
        if return_code != 0:
            result["failure_reason"] = "claude_exit"
        if watchdog_error:
            result["supervision_error"] = watchdog_error
        write_json(result_path, result)
        return 0
    except BaseException as exc:
        if not ownership_verified:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        cleanup_errors: list[str] = []
        stopped_by_caller = isinstance(exc, ReviewStopped)
        failure_reason = (
            "stopped_by_caller"
            if stopped_by_caller
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "output_encoding"
            if isinstance(exc, UnicodeDecodeError)
            else "receipt_missing"
            if isinstance(exc, ReceiptMissing)
            else "empty_review"
            if isinstance(exc, EmptyReview)
            else "worker_error"
        )
        if process is not None:
            try:
                terminate_claude(process, claude_pgid, claude_identity, child_marker)
            except Exception as cleanup_exc:
                cleanup_errors.append(
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        watchdog_error = release_watchdog()
        if watchdog_error:
            cleanup_errors.append(watchdog_error)
        error = f"{type(exc).__name__}: {exc}"
        if cleanup_errors:
            error += "; cleanup: " + "; ".join(cleanup_errors)
        with stderr_path.open("a", encoding="utf-8", errors="replace") as stderr_file:
            stderr_file.write(error + "\n")
        write_json(
            worker_path,
            {
                "state": "failed",
                "worker_pid": os.getpid(),
                "worker_identity": worker_identity,
                "claude_pid": process.pid if process is not None else None,
                "claude_pgid": claude_pgid,
                "claude_identity": claude_identity,
                "watchdog_pid": watchdog.pid if watchdog is not None else None,
                "watchdog_identity": watchdog_identity,
                "token": token,
                "started_at": started_at,
                "heartbeat_at": time.time(),
                "worker_error": error,
                "failure_reason": failure_reason,
                "stopped_by_caller": stopped_by_caller,
            },
        )
        write_json(
            result_path,
            {
                "state": "failed",
                "return_code": process.returncode if process is not None else None,
                "completed_at": time.time(),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "worker_error": error,
                "failure_reason": failure_reason,
                "stopped_by_caller": stopped_by_caller,
                "receipt_verified": receipt_verified,
                "prompt_bytes": prompt_bytes,
            },
        )
        return 1
    finally:
        release_watchdog()
        if ownership_verified:
            go_path.unlink(missing_ok=True)
        close_job(job_handle)


def run_watchdog(args: argparse.Namespace) -> int:
    if os.name == "nt":
        return 0
    token = os.environ.pop(TOKEN_ENV, "")
    child_marker = os.environ.pop(CHILD_MARKER_ENV, "")
    if len(token) < 32:
        return 2
    if len(child_marker) < 32:
        return 2
    state_dir = Path(args.state_dir).resolve()
    try:
        validate_helper_state_dir(state_dir)
    except SystemExit:
        return 2
    owner = read_json(state_dir / "owner.json")
    launch = read_json(state_dir / "launch.json")
    if (
        owner.get("helper") != "dual-review"
        or owner.get("token") != token
        or launch.get("token") != token
    ):
        return 2

    try:
        while os.read(args.read_fd, 1):
            pass
    finally:
        os.close(args.read_fd)

    group_state = process_group_is_running(args.claude_pgid)
    if group_state is False:
        return 0
    if group_state is None:
        return 2
    current_identity = process_identity(args.claude_pid)
    if current_identity != args.claude_identity:
        return 2
    marker_verified = process_has_child_marker(args.claude_pid, child_marker)
    if marker_verified is not True and linux_process_is_zombie(
        args.claude_pid
    ) is not True:
        return 2

    try:
        os.killpg(args.claude_pgid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        group_state = process_group_is_running(args.claude_pgid)
        if group_state is False:
            return 0
        if group_state is None:
            return 2
        time.sleep(0.1)
    try:
        os.killpg(args.claude_pgid, signal.SIGKILL)
    except ProcessLookupError:
        return 0
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        group_state = process_group_is_running(args.claude_pgid)
        if group_state is False:
            return 0
        if group_state is None:
            return 2
        time.sleep(0.1)
    return 2

def get_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    validate_helper_state_dir(state_dir)
    owner, launch = read_owned_state(state_dir)
    state_token = owner["token"]
    result_path = state_dir / "result.json"
    if result_path.exists():
        print(json.dumps(read_json(result_path), ensure_ascii=False))
        return 0

    worker_pid = int(launch.get("worker_pid") or 0)
    worker_identity = launch.get("worker_identity")
    if not worker_pid:
        age = time.time() - float(launch.get("created_at", 0))
        if launch.get("state") == "starting" and age <= STARTING_GRACE_SECONDS:
            print(
                json.dumps(
                    {"state": "starting", "state_dir": str(state_dir)},
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "state": "failed",
                    "return_code": None,
                    "worker_error": "worker did not start within the grace period",
                    "bootstrap_error": read_text_tail(state_dir / "bootstrap.log"),
                    "state_dir": str(state_dir),
                },
                ensure_ascii=False,
            )
        )
        return 0

    worker_state = owned_process_state(worker_pid, worker_identity)
    worker_path = state_dir / "worker.json"
    if worker_state is True:
        worker = read_json(worker_path) if worker_path.exists() else {}
        if worker and worker.get("token") != state_token:
            raise SystemExit("worker ownership token differs")
        heartbeat_at = float(worker.get("heartbeat_at") or 0)
        last_progress = heartbeat_at or float(launch.get("created_at", 0))
        if time.time() - last_progress > HEARTBEAT_STALE_SECONDS:
            print(
                json.dumps(
                    {
                        "state": "unknown",
                        "worker_pid": worker_pid,
                        "claude_pid": worker.get("claude_pid"),
                        "heartbeat_at": heartbeat_at or None,
                        "state_dir": str(state_dir),
                        "worker_error": "worker heartbeat is stale",
                        "stale_heartbeat": True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "state": "running",
                    "worker_pid": worker_pid,
                    "claude_pid": worker.get("claude_pid"),
                    "heartbeat_at": heartbeat_at or None,
                    "state_dir": str(state_dir),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if worker_state is None:
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "worker_pid": worker_pid,
                    "state_dir": str(state_dir),
                    "worker_error": "cannot verify worker identity with current permissions",
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not worker_path.exists():
        if launch.get("worker_record_before_child") is True:
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "orphaned": True,
                        "safe_to_cleanup": True,
                        "worker_pid": worker_pid,
                        "state_dir": str(state_dir),
                        "worker_error": "worker exited before spawning a Claude child",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "state_dir": str(state_dir),
                    "worker_error": "worker exited before child supervision state was recorded",
                },
                ensure_ascii=False,
            )
        )
        return 0

    worker = read_json(worker_path)
    if worker.get("token") != state_token:
        raise SystemExit("worker ownership token differs")
    recorded_worker_identity = worker.get("worker_identity")
    if (
        recorded_worker_identity is not None
        and worker_identity is not None
        and recorded_worker_identity != worker_identity
    ):
        raise SystemExit("worker process identity differs from launch state")
    if worker.get("state") == "spawning_child":
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "state_dir": str(state_dir),
                    "worker_error": "worker exited during Claude child creation; residual child state is unknown",
                },
                ensure_ascii=False,
            )
        )
        return 0
    claude_pid = int(worker.get("claude_pid") or 0)
    claude_state = owned_process_state(claude_pid, worker.get("claude_identity"))
    if claude_state is True:
        print(
            json.dumps(
                {
                    "state": "running",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "claude_pid": claude_pid,
                    "state_dir": str(state_dir),
                    "worker_error": "worker exited while Claude is still running",
                },
                ensure_ascii=False,
            )
        )
        return 0
    if claude_state is None:
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "claude_pid": claude_pid,
                    "state_dir": str(state_dir),
                    "worker_error": "cannot verify Claude child identity",
                },
                ensure_ascii=False,
            )
        )
        return 0

    watchdog_pid = int(worker.get("watchdog_pid") or 0)
    watchdog_state = owned_process_state(
        watchdog_pid, worker.get("watchdog_identity")
    )
    claude_pgid = int(worker.get("claude_pgid") or 0)
    group_state = (
        process_group_is_running(claude_pgid)
        if os.name != "nt" and claude_pgid
        else False
    )
    if watchdog_state is True or group_state is True:
        print(
            json.dumps(
                {
                    "state": "running",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "claude_pid": claude_pid,
                    "watchdog_pid": watchdog_pid or None,
                    "claude_pgid": claude_pgid or None,
                    "state_dir": str(state_dir),
                    "worker_error": "worker exited while supervision cleanup is still active",
                },
                ensure_ascii=False,
            )
        )
        return 0
    if watchdog_state is None or group_state is None:
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "orphaned": True,
                    "worker_pid": worker_pid,
                    "claude_pid": claude_pid,
                    "watchdog_pid": watchdog_pid or None,
                    "claude_pgid": claude_pgid or None,
                    "state_dir": str(state_dir),
                    "worker_error": "cannot determine residual supervision state",
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "state": "failed",
                "return_code": None,
                "worker_error": "worker and Claude exited without writing result.json",
                "bootstrap_error": read_text_tail(state_dir / "bootstrap.log"),
                "stdout_path": str(state_dir / "stdout.txt"),
                "stderr_path": str(state_dir / "stderr.txt"),
                "state_dir": str(state_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0

def detect_claude_quota_failure(text: str) -> str | None:
    # stderr and worker errors are trusted failure channels, so they accept several
    # known quota forms. Preserve physical line boundaries: folding the whole stream
    # would let a status code on one line combine with unrelated quota prose later.
    for line in text.splitlines():
        normalized = " ".join(line.lower().split())
        for pattern in CLAUDE_QUOTA_PATTERNS:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return pattern
    return None


def classify_claude_quota_result(
    result: dict[str, Any], stderr_text: str, worker_error: str, stdout_text: str = ""
) -> str | None:
    if result.get("state") != "failed" or result.get("failure_reason") != "claude_exit":
        return None
    matched = detect_claude_quota_failure("\n".join((stderr_text, worker_error)))
    if matched is not None:
        return matched
    # Claude Code reports subscription exhaustion as one exact stdout status line.
    # Do not broadly scan stdout: a partial review may legitimately discuss limits.
    nonempty_lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if (
        result.get("receipt_verified") is not True
        and len(nonempty_lines) == 1
        and CLAUDE_STDOUT_QUOTA_PATTERN.fullmatch(nonempty_lines[0])
    ):
        return CLAUDE_STDOUT_QUOTA_PATTERN.pattern
    return None


def read_complete_quota_stdout(path: Path) -> str:
    try:
        # A genuine Claude subscription banner is tiny. The cap prevents a partial
        # review body from entering the stricter one-line stdout classification path.
        if path.stat().st_size > MAX_CLAUDE_QUOTA_STDOUT_BYTES:
            return ""
        return path.read_bytes().decode("utf-8", errors="strict")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def validated_prompt_sha256(value: Any) -> str | None:
    if isinstance(value, str) and PROMPT_SHA256_PATTERN.fullmatch(value):
        return value
    return None


def classify_failure(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    validate_helper_state_dir(state_dir)
    _, launch = read_owned_state(state_dir)
    result_path = state_dir / "result.json"
    if not result_path.exists():
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "quota_limited": False,
                    "reason": "result_missing",
                },
                ensure_ascii=False,
            )
        )
        return 3
    result = read_json(result_path)
    failure_reason = result.get("failure_reason")
    prompt_sha256 = validated_prompt_sha256(launch.get("prompt_sha256"))
    matched = classify_claude_quota_result(
        result,
        read_text_tail(state_dir / "stderr.txt", limit=8000),
        str(result.get("worker_error") or ""),
        read_complete_quota_stdout(state_dir / "stdout.txt"),
    )
    print(
        json.dumps(
            {
                "state": result.get("state"),
                "quota_limited": matched is not None and prompt_sha256 is not None,
                "failure_reason": failure_reason,
                "matched_pattern": matched,
                "prompt_sha256": prompt_sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


def print_result(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    validate_helper_state_dir(state_dir)
    read_owned_state(state_dir)
    result_path = state_dir / "result.json"
    result = read_json(result_path) if result_path.exists() else None
    stdout_path = state_dir / "stdout.txt"
    stderr_path = state_dir / "stderr.txt"
    output_decode_failed = False
    if stdout_path.exists():
        stdout_raw = stdout_path.read_bytes()
        try:
            stdout_raw.decode("utf-8", errors="strict")
            stdout_output = stdout_raw
        except UnicodeDecodeError as exc:
            output_decode_failed = True
            stdout_output = stdout_raw.decode(
                "utf-8", errors="replace"
            ).encode("utf-8")
            sys.stderr.write(
                "\n--- OUTPUT ENCODING ERROR ---\n"
                f"Claude stdout is not valid UTF-8; replacement text follows: {exc}\n"
            )
        write_stdout_bytes(stdout_output)
    if stderr_path.exists():
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        if stderr:
            sys.stderr.write("\n--- CLAUDE STDERR ---\n")
            sys.stderr.write(stderr)
    if result is None:
        bootstrap = read_text_tail(state_dir / "bootstrap.log")
        if bootstrap:
            sys.stderr.write(bootstrap)
        print(
            "review has no result.json; output above may be partial",
            file=sys.stderr,
        )
        return 3
    return 4 if output_decode_failed else 0


def stop_review(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    validate_helper_state_dir(state_dir)
    owner, launch = read_owned_state(state_dir)
    state_token = owner["token"]
    result_path = state_dir / "result.json"
    if result_path.exists():
        result = read_json(result_path)
        print(
            json.dumps(
                {"state": result.get("state"), "already_terminal": True},
                ensure_ascii=False,
            )
        )
        return 0

    worker_pid = int(launch.get("worker_pid") or 0)
    worker_state = owned_process_state(worker_pid, launch.get("worker_identity"))
    if worker_state is not True:
        print(
            json.dumps(
                {
                    "state": "unknown",
                    "state_dir": str(state_dir),
                    "worker_error": "worker is not verifiably running; no signal was sent",
                },
                ensure_ascii=False,
            )
        )
        return 2

    write_json(
        state_dir / "cancel.json",
        {"token": state_token, "requested_at": time.time()},
    )
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if result_path.exists():
            result = read_json(result_path)
            print(
                json.dumps(
                    {
                        "state": result.get("state"),
                        "stopped": result.get("stopped_by_caller") is True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if owned_process_state(worker_pid, launch.get("worker_identity")) is False:
            break
        time.sleep(0.2)

    if result_path.exists():
        result = read_json(result_path)
        print(
            json.dumps(
                {
                    "state": result.get("state"),
                    "stopped": result.get("stopped_by_caller") is True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "state": "unknown",
                "state_dir": str(state_dir),
                "worker_error": "worker did not acknowledge cancellation; no PID signal was sent",
            },
            ensure_ascii=False,
        )
    )
    return 2

def refuse_cleanup(
    reason: str, state_dir: Path, work_dir: Path | None = None
) -> None:
    raise SystemExit(
        json.dumps(
            {
                "state": "cleanup_refused",
                "state_dir": str(state_dir),
                "work_dir": str(work_dir) if work_dir is not None else None,
                "reason": reason,
            },
            ensure_ascii=False,
        )
    )


def _cleanup_review_impl(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    if not state_dir.is_dir():
        refuse_cleanup("state directory does not exist", state_dir)
    try:
        validate_helper_state_dir(state_dir)
    except SystemExit as exc:
        refuse_cleanup(str(exc), state_dir)

    owner_path = state_dir / "owner.json"
    if not owner_path.exists():
        refuse_cleanup("owner.json is missing", state_dir)
    owner = read_json(owner_path)
    state_token = owner.get("token")
    if (
        owner.get("helper") != "dual-review"
        or not isinstance(state_token, str)
        or len(state_token) < 32
    ):
        refuse_cleanup("owner token is invalid", state_dir)
    work_dir_value = owner.get("work_dir")
    work_dir: Path | None = None
    if work_dir_value is not None:
        if not isinstance(work_dir_value, str):
            refuse_cleanup("work directory is invalid", state_dir)
        work_dir = Path(work_dir_value)
        try:
            validate_helper_work_dir(work_dir)
        except SystemExit as exc:
            refuse_cleanup(str(exc), state_dir)

    launch_path = state_dir / "launch.json"
    if not launch_path.exists():
        remove_private_directories(state_dir, work_dir)
        print(
            json.dumps(
                {
                    "state": "cleaned",
                    "state_dir": str(state_dir),
                    "work_dir": str(work_dir) if work_dir is not None else None,
                    "incomplete": True,
                },
                ensure_ascii=False,
            )
        )
        return 0

    try:
        _, launch = read_owned_state(state_dir)
    except SystemExit as exc:
        refuse_cleanup(str(exc), state_dir, work_dir)
    worker_pid = int(launch.get("worker_pid") or 0)
    worker_identity = launch.get("worker_identity")
    result_path = state_dir / "result.json"
    if (
        launch.get("state") == "starting"
        and not result_path.exists()
        and time.time() - float(launch.get("created_at") or 0)
        <= STARTING_GRACE_SECONDS
    ):
        refuse_cleanup("worker startup grace is active", state_dir, work_dir)
    worker_state = owned_process_state(worker_pid, worker_identity)
    if result_path.exists() and worker_state is True:
        deadline = time.monotonic() + CLEANUP_EXIT_WAIT_SECONDS
        while time.monotonic() < deadline and worker_state is True:
            time.sleep(0.1)
            worker_state = owned_process_state(worker_pid, worker_identity)

    worker_path = state_dir / "worker.json"
    stale = False
    if not worker_path.exists():
        safe_without_record = (
            bool(worker_pid)
            and isinstance(worker_identity, str)
            and isinstance(launch.get("protocol_version"), int)
            and launch.get("protocol_version") >= 2
            and launch.get("worker_record_before_child") is True
        )
        start_failure_safe = False
        if result_path.exists():
            result = read_json(result_path)
            start_failure_safe = result.get("safe_to_cleanup") is True
        if worker_state is not False:
            refuse_cleanup(
                "worker identity is running or unknown", state_dir, work_dir
            )
        if not safe_without_record and not start_failure_safe:
            refuse_cleanup(
                "worker.json is missing and child state is unknown",
                state_dir,
                work_dir,
            )
        worker: dict[str, Any] = {}
        stale = safe_without_record and not start_failure_safe
    else:
        worker = read_json(worker_path)
        recorded_worker_pid = int(worker.get("worker_pid") or 0)
        recorded_worker_identity = worker.get("worker_identity")
        if (
            worker.get("token") != state_token
            or not recorded_worker_pid
            or not isinstance(recorded_worker_identity, str)
            or (
                worker_pid
                and recorded_worker_pid != worker_pid
            )
            or (
                worker_identity is not None
                and recorded_worker_identity != worker_identity
            )
        ):
            refuse_cleanup("worker ownership differs", state_dir, work_dir)
        if not worker_pid:
            worker_pid = recorded_worker_pid
            worker_identity = recorded_worker_identity
            worker_state = owned_process_state(worker_pid, worker_identity)
            if result_path.exists() and worker_state is True:
                deadline = time.monotonic() + CLEANUP_EXIT_WAIT_SECONDS
                while time.monotonic() < deadline and worker_state is True:
                    time.sleep(0.1)
                    worker_state = owned_process_state(worker_pid, worker_identity)
        stale = (
            launch.get("state") == "starting"
            and worker.get("state") == "bootstrapping"
            and worker_state is False
        )
        if worker.get("state") == "spawning_child" and not result_path.exists():
            refuse_cleanup(
                "worker exited during Claude child creation", state_dir, work_dir
            )

    claude_pid = int(worker.get("claude_pid") or 0)
    claude_state = owned_process_state(claude_pid, worker.get("claude_identity"))
    claude_pgid = int(worker.get("claude_pgid") or 0)
    group_state = (
        process_group_is_running(claude_pgid)
        if os.name != "nt" and claude_pgid
        else False
    )
    watchdog_pid = int(worker.get("watchdog_pid") or 0)
    watchdog_state = owned_process_state(
        watchdog_pid, worker.get("watchdog_identity")
    )
    if (
        worker_state is not False
        or claude_state is not False
        or group_state is not False
        or watchdog_state is not False
    ):
        refuse_cleanup(
            "owned process state is running or unknown", state_dir, work_dir
        )

    remove_private_directories(state_dir, work_dir)
    output: dict[str, Any] = {
        "state": "cleaned",
        "state_dir": str(state_dir),
        "work_dir": str(work_dir) if work_dir is not None else None,
    }
    if stale:
        output["stale"] = True
    print(json.dumps(output, ensure_ascii=False))
    return 0

def validated_work_dir_for_report(state_dir: Path) -> Path | None:
    try:
        owner = read_json(state_dir / "owner.json")
        work_dir_value = owner.get("work_dir")
        if not isinstance(work_dir_value, str):
            return None
        work_dir = Path(work_dir_value)
        validate_helper_work_dir(work_dir)
        return work_dir
    except (Exception, SystemExit):
        return None


def cleanup_review(args: argparse.Namespace) -> int:
    raw_state_dir = Path(args.state_dir)
    try:
        state_dir = raw_state_dir.resolve()
    except Exception as exc:
        refuse_cleanup(
            f"{type(exc).__name__}: {exc}", raw_state_dir
        )
    try:
        return _cleanup_review_impl(args)
    except SystemExit:
        raise
    except Exception as exc:
        work_dir = validated_work_dir_for_report(state_dir)
        refuse_cleanup(
            f"{type(exc).__name__}: {exc}", state_dir, work_dir
        )

def sweep_review(args: argparse.Namespace) -> int:
    if args.older_than <= 0:
        raise SystemExit("older-than must be greater than zero")
    temp_root = Path(tempfile.gettempdir()).resolve()
    now = time.time()
    cleaned: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    skipped_recent = 0
    try:
        candidates = list(temp_root.iterdir())
    except OSError as exc:
        raise SystemExit(f"cannot scan system temp directory: {exc}") from exc
    for candidate in candidates:
        if not candidate.name.startswith(STATE_PREFIX):
            continue
        try:
            state_dir = candidate.resolve()
            validate_helper_state_dir(state_dir)
            if not state_dir.is_dir():
                continue
            owner_path = state_dir / "owner.json"
            if not owner_path.exists():
                refused.append(
                    {
                        "state_dir": str(state_dir),
                        "reason": "owner.json is missing",
                    }
                )
                continue
            owner = read_json(owner_path)
            created_at = float(owner.get("created_at") or 0)
            if created_at <= 0:
                raise ValueError("owner.json created_at is missing or invalid")
            if now - created_at < args.older_than:
                skipped_recent += 1
                continue
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cleanup_review(argparse.Namespace(state_dir=str(state_dir)))
            cleaned_output = output.getvalue().strip().splitlines()
            cleaned.append(
                json.loads(cleaned_output[-1])
                if cleaned_output
                else {"state": "cleaned", "state_dir": str(state_dir)}
            )
        except SystemExit as exc:
            refused.append({"state_dir": str(candidate), "reason": str(exc)})
        except Exception as exc:
            refused.append(
                {
                    "state_dir": str(candidate),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    print(
        json.dumps(
            {
                "state": "sweep_complete",
                "older_than": args.older_than,
                "cleaned": cleaned,
                "refused": refused,
                "skipped_recent": skipped_recent,
            },
            ensure_ascii=False,
        )
    )
    return 0

def run_self_test(_: argparse.Namespace) -> int:
    """Run portable regressions without invoking Claude or writing prompt snapshots."""

    if not __debug__:
        raise SystemExit("self-test requires assertions enabled; do not use python -O")

    checks: list[str] = []
    assert detect_claude_quota_failure("You've hit your usage limit") is not None
    assert detect_claude_quota_failure("HTTP 429: rate limit exceeded") is not None
    assert detect_claude_quota_failure("authentication required") is None
    assert detect_claude_quota_failure("tool contract changed") is None
    assert detect_claude_quota_failure("HTTP 429: unrelated failure\nquota noted later") is None
    quota_result = {
        "state": "failed",
        "failure_reason": "claude_exit",
        "receipt_verified": False,
    }
    assert (
        classify_claude_quota_result(
            quota_result, "You've hit your usage limit", ""
        )
        is not None
    )
    partial_stdout = "review finding: the API rate limit needs documentation"
    assert detect_claude_quota_failure(partial_stdout) is not None
    assert (
        classify_claude_quota_result(
            quota_result,
            "",
            "",
            "You've hit your weekly limit · resets 8am (Asia/Shanghai)",
        )
        is not None
    )
    assert (
        classify_claude_quota_result(
            quota_result, "TLS connection reset", "", partial_stdout
        )
        is None
    )
    assert (
        classify_claude_quota_result(
            quota_result, "", "", "You've hit your design limit"
        )
        is None
    )
    assert (
        classify_claude_quota_result(
            quota_result,
            "",
            "",
            "You've hit your weekly limit\n- resets are handled incorrectly in this API",
        )
        is None
    )
    with tempfile.TemporaryDirectory(prefix="dual-review-quota-self-test-") as raw_dir:
        quota_path = Path(raw_dir) / "stdout.txt"
        quota_path.write_text(
            "You've hit your weekly limit · resets 8am (Asia/Shanghai)\n",
            encoding="utf-8",
        )
        assert read_complete_quota_stdout(quota_path).startswith("You've hit")
        quota_path.write_bytes(b"x" * (MAX_CLAUDE_QUOTA_STDOUT_BYTES + 1))
        assert read_complete_quota_stdout(quota_path) == ""
    checks.append("quota_failure_classification")
    assert validated_prompt_sha256("a" * 64) == "a" * 64
    assert validated_prompt_sha256("A" * 64) is None
    assert validated_prompt_sha256("a" * 63) is None
    assert validated_prompt_sha256(None) is None
    checks.append("prompt_sha256_validation")


    class BinaryInput:
        def __init__(self, payload: bytes) -> None:
            self.buffer = io.BytesIO(payload)

        def isatty(self) -> bool:
            return False

    original_stdin = sys.stdin
    try:
        prompt_raw = "评审\r\nbytes".encode("utf-8")
        encoded = base64.b64encode(prompt_raw)
        sys.stdin = BinaryInput(encoded)  # type: ignore[assignment]
        _, decoded = read_utf8_prompt(base64_input=True)
        assert decoded == prompt_raw
        checks.append("base64_utf8_bytes")

        sys.stdin = BinaryInput(encoded + b"\n")  # type: ignore[assignment]
        try:
            read_utf8_prompt(base64_input=True)
        except PromptInputError as exc:
            assert exc.error_kind == "invalid_prompt"
        else:
            raise AssertionError("wrapped base64 transport was accepted")
        checks.append("base64_rejects_whitespace")
    finally:
        sys.stdin = original_stdin

    framed = frame_review_prompt(b"alpha\r\nbeta\r\n", "START", "END")
    assert b"alpha\r\nbeta\r\n" in framed
    assert receipt_line_matches("START", "START", ("label: ",))
    assert receipt_line_matches("`START`", "START", ("label: ",))
    assert not receipt_line_matches("extra START", "START", ("label: ",))
    checks.append("framing_and_receipts")

    assert not WORK_PREFIX.startswith(STATE_PREFIX)
    state_dir, work_dir = create_private_directories()
    canonical_temp_root = Path(tempfile.gettempdir()).resolve()
    assert state_dir.parent == canonical_temp_root
    assert work_dir.parent == canonical_temp_root
    token = "s" * 43
    try:
        write_json(
            state_dir / "owner.json",
            {
                "helper": "dual-review",
                "token": token,
                "created_at": time.time(),
                "work_dir": str(work_dir),
            },
        )
        launch = {
            "state": "starting",
            "protocol_version": 5,
            "token": token,
            "work_dir": str(work_dir),
            "worker_pid": 0,
            "worker_identity": None,
            "worker_record_before_child": True,
            "created_at": time.time(),
        }
        write_json(state_dir / "launch.json", launch)
        try:
            cleanup_review(argparse.Namespace(state_dir=str(state_dir)))
        except SystemExit as exc:
            assert "startup grace" in str(exc)
        else:
            raise AssertionError("active starting state was cleaned")
        write_json(
            state_dir / "worker.json",
            {
                "state": "bootstrapping",
                "worker_pid": 2_147_483_647,
                "worker_identity": "self-test-nonexistent-worker",
                "claude_pid": None,
                "claude_pgid": None,
                "claude_identity": None,
                "watchdog_pid": None,
                "watchdog_identity": None,
                "token": token,
                "started_at": time.time(),
                "heartbeat_at": time.time(),
                "return_code": None,
            },
        )
        launch["created_at"] = time.time() - STARTING_GRACE_SECONDS - 1
        write_json(state_dir / "launch.json", launch)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert cleanup_review(argparse.Namespace(state_dir=str(state_dir))) == 0
        cleanup_result = json.loads(output.getvalue())
        assert cleanup_result.get("stale") is True
        assert not state_dir.exists() and not work_dir.exists()
        checks.append("paired_directories_and_starting_grace")

        broken_state_dir, broken_work_dir = create_private_directories()
        try:
            (broken_state_dir / "owner.json").write_text("{", encoding="utf-8")
            try:
                cleanup_review(
                    argparse.Namespace(state_dir=str(broken_state_dir))
                )
            except SystemExit as exc:
                refusal = json.loads(str(exc))
                assert refusal.get("state") == "cleanup_refused"
                assert refusal.get("state_dir") == str(broken_state_dir)
                assert refusal.get("work_dir") is None
                assert "JSONDecodeError" in refusal.get("reason", "")
            else:
                raise AssertionError("corrupt state did not refuse cleanup")
        finally:
            if broken_state_dir.exists():
                shutil.rmtree(broken_state_dir, ignore_errors=True)
            if broken_work_dir.exists():
                shutil.rmtree(broken_work_dir, ignore_errors=True)
        checks.append("structured_cleanup_refusal")
    finally:
        if state_dir.exists():
            shutil.rmtree(state_dir, ignore_errors=True)
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    if os.name != "nt":
        def launch_ready_process_group(marker: str) -> tuple[subprocess.Popen[str], int]:
            child_env = os.environ.copy()
            child_env[CHILD_MARKER_ENV] = marker
            leader = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys,time; "
                    "child=subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)']); "
                    "print(child.pid, flush=True); time.sleep(60)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=child_env,
                text=True,
            )
            assert leader.stdout is not None
            selector = selectors.DefaultSelector()
            try:
                selector.register(leader.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=5), "descendant readiness timed out"
                child_pid = int(leader.stdout.readline().strip())
                assert process_is_running(child_pid) is True
                return leader, child_pid
            except BaseException:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                leader.wait(timeout=5)
                raise
            finally:
                selector.close()
                leader.stdout.close()

        child_marker = secrets.token_urlsafe(32)
        process, child_pid = launch_ready_process_group(child_marker)
        try:
            identity = process_identity(process.pid)
            assert identity is not None
            terminate_claude(process, process.pid, identity, child_marker)
            assert process.poll() is not None
            assert process_is_running(child_pid) is False
            assert process_group_is_running(process.pid) is False
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        checks.append("posix_group_termination_with_descendant")

        exited_leader, exited_child_pid = launch_ready_process_group(child_marker)
        exited_identity = process_identity(exited_leader.pid)
        assert exited_identity is not None
        exited_leader.terminate()
        exited_leader.wait(timeout=5)
        assert process_is_running(exited_child_pid) is True
        try:
            terminate_claude(
                exited_leader,
                exited_leader.pid,
                exited_identity,
                child_marker,
            )
        except RuntimeError as exc:
            assert "identity is unavailable" in str(exc)
            assert process_is_running(exited_child_pid) is True
        else:
            raise AssertionError("reaped leader allowed a process-group signal")
        finally:
            try:
                os.killpg(exited_leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        checks.append("posix_reaped_leader_fails_closed")

        if sys.platform.startswith("linux"):
            zombie = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while (
                    linux_process_is_zombie(zombie.pid) is not True
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                assert linux_process_is_zombie(zombie.pid) is True
                assert process_is_running(zombie.pid) is False
            finally:
                zombie.wait(timeout=5)
            checks.append("linux_zombie_detection")

    print(json.dumps({"state": "passed", "checks": checks}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a detached Claude review")
    start.add_argument(
        "--cwd",
        required=True,
        help="repository under review; recorded only, Claude runs in private temp state",
    )
    start.add_argument(
        "--prompt-base64-stdin",
        action="store_true",
        required=True,
        help="read unwrapped base64 of the approved UTF-8 prompt from stdin",
    )
    start.add_argument("--claude-bin", default="claude")
    start.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    start.set_defaults(func=start_review)

    status = commands.add_parser("status", help="print review state as JSON")
    status.add_argument("--state-dir", required=True)
    status.set_defaults(func=get_status)

    classify = commands.add_parser(
        "classify-failure", help="classify a terminal Claude failure as quota-related"
    )
    classify.add_argument("--state-dir", required=True)
    classify.set_defaults(func=classify_failure)

    result = commands.add_parser("result", help="print complete Claude stdout")
    result.add_argument("--state-dir", required=True)
    result.set_defaults(func=print_result)

    stop = commands.add_parser("stop", help="stop a running review")
    stop.add_argument("--state-dir", required=True)
    stop.set_defaults(func=stop_review)

    cleanup = commands.add_parser("cleanup", help="remove a terminal temp state")
    cleanup.add_argument("--state-dir", required=True)
    cleanup.set_defaults(func=cleanup_review)

    sweep = commands.add_parser("sweep", help="clean old terminal helper states")
    sweep.add_argument("--older-than", type=int, default=86400)
    sweep.set_defaults(func=sweep_review)

    self_test = commands.add_parser(
        "self-test", help="run portable helper regression checks"
    )
    self_test.set_defaults(func=run_self_test)

    worker = commands.add_parser("_worker", help="internal worker")
    worker.add_argument("--state-dir", required=True)
    worker.add_argument("--claude-bin", required=True)
    worker.add_argument("--timeout", type=int, required=True)
    worker.set_defaults(func=run_worker)

    watchdog = commands.add_parser("_watchdog", help="internal POSIX watchdog")
    watchdog.add_argument("--read-fd", type=int, required=True)
    watchdog.add_argument("--claude-pid", type=int, required=True)
    watchdog.add_argument("--claude-pgid", type=int, required=True)
    watchdog.add_argument("--claude-identity", required=True)
    watchdog.add_argument("--state-dir", required=True)
    watchdog.set_defaults(func=run_watchdog)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace", newline="\n")
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
