#!/usr/bin/env python3
"""Run one isolated Antigravity review from an approved prompt snapshot."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import run_claude_review as lifecycle


DEFAULT_MODEL = "claude-opus-4-6-thinking"
DEFAULT_TIMEOUT_SECONDS = 1800
STATE_PREFIX = "dual-review-antigravity-"
WORK_PREFIX = "dual-review-work-antigravity-"
OWNER_FILENAME = "owner.json"
WORK_OWNER_FILENAME = ".dual-review-owner.json"
MAX_PROMPT_BYTES = lifecycle.MAX_PROMPT_BYTES
MAX_BASE64_TRANSPORT_BYTES = lifecycle.MAX_BASE64_TRANSPORT_BYTES
SYSTEM_GUARD = (
    "You are the isolated external reviewer in a dual-review workflow. Review only "
    "the preflight-approved snapshot supplied below. Do not read or modify files, "
    "run commands, browse, call MCP, invoke skills, or start other agents. Do not "
    "create plans or call ExitPlanMode. Put the complete Chinese Critical / Important / "
    "Suggestion review in the structured `review` field and the neutral pass/revise "
    "decision in the structured `decision` field."
)
REVIEW_DECISIONS = ("pass", "revise")
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "review": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": list(REVIEW_DECISIONS)},
    },
    "required": ["review", "decision"],
    "additionalProperties": False,
}
WINDOWS_NATIVE_SUFFIXES = {".com", ".exe"}
POSIX_NATIVE_MAGICS = lifecycle.POSIX_NATIVE_MAGICS
REQUIRED_FLAGS = (
    "--disable-slash-commands",
    "--input-format",
    "--json-schema",
    "--output-format",
    "--model",
    "--print-timeout",
    "--sandbox",
    "--log-file",
)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(
            json.dumps(
                {"state": "failed", "error_kind": "argument_error", "error": message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


class ReviewError(RuntimeError):
    def __init__(
        self, error_kind: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.details = details or {}


def read_prompt_base64() -> bytes:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(MAX_BASE64_TRANSPORT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="strict")
    if len(raw) > MAX_BASE64_TRANSPORT_BYTES:
        raise ReviewError("prompt_too_large", "base64 stdin exceeds transport limit")
    if not raw:
        raise ReviewError("invalid_prompt", "base64 stdin is empty")
    if re.search(rb"\s", raw):
        raise ReviewError("invalid_base64", "base64 stdin must not contain whitespace")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReviewError("invalid_base64", f"invalid base64 stdin: {exc}") from exc
    if not decoded or len(decoded) > MAX_PROMPT_BYTES:
        raise ReviewError(
            "prompt_too_large" if decoded else "invalid_prompt",
            f"decoded prompt must contain 1..{MAX_PROMPT_BYTES} bytes",
        )
    try:
        decoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewError("invalid_utf8", f"prompt is not valid UTF-8: {exc}") from exc
    return decoded


def validate_native_binary(executable: Path) -> None:
    if os.name == "nt":
        if executable.suffix.lower() not in WINDOWS_NATIVE_SUFFIXES:
            raise ReviewError(
                "unsupported_antigravity_launcher",
                "Windows requires a native agy .exe or .com executable",
            )
        return
    try:
        magic = executable.read_bytes()[:4]
    except OSError as exc:
        raise ReviewError(
            "unsupported_antigravity_launcher",
            f"cannot inspect agy executable: {exc}",
        ) from exc
    if magic not in POSIX_NATIVE_MAGICS:
        raise ReviewError(
            "unsupported_antigravity_launcher",
            "POSIX requires a native ELF or Mach-O agy executable",
        )


def resolve_antigravity_binary(requested: str) -> Path:
    direct = Path(requested).expanduser()
    resolved: str | None = None
    if direct.is_absolute() and direct.is_file():
        resolved = str(direct.resolve())
    else:
        resolved = shutil.which(requested)
    if resolved is None and os.name == "nt" and requested == "agy":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            installed = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if installed.is_file():
                resolved = str(installed.resolve())
    if resolved is None:
        raise ReviewError(
            "antigravity_missing",
            "Antigravity CLI was not found on PATH or in LOCALAPPDATA/agy/bin",
        )
    executable = Path(resolved).resolve()
    validate_native_binary(executable)
    return executable


def validate_permission_settings(settings: Any) -> None:
    if not isinstance(settings, dict):
        raise ReviewError("antigravity_permissions", "Antigravity settings must be an object")
    tool_permission = settings.get("toolPermission", "request-review")
    if tool_permission not in {"request-review", "strict"}:
        raise ReviewError(
            "antigravity_permissions",
            f"unsafe Antigravity toolPermission: {tool_permission!r}",
        )
    permissions = settings.get("permissions", {})
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, dict):
        raise ReviewError("antigravity_permissions", "permissions must be an object")
    allowed = permissions.get("allow", [])
    if allowed is None:
        allowed = []
    if not isinstance(allowed, list) or allowed:
        raise ReviewError(
            "antigravity_permissions",
            "permissions.allow must be an empty list for external review",
        )


def validate_antigravity_permissions() -> None:
    if os.name != "nt":
        raise ReviewError(
            "unsupported_platform",
            "Antigravity fallback currently requires Windows AppContainer lifecycle coverage",
        )
    settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings_path.exists():
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(
            "antigravity_permissions",
            f"cannot verify Antigravity permissions settings: {type(exc).__name__}: {exc}",
        ) from exc
    validate_permission_settings(settings)


def validate_state_dir(state_dir: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        state_dir.parent != temp_root
        or not state_dir.name.startswith(STATE_PREFIX)
        or state_dir.is_symlink()
        or getattr(state_dir, "is_junction", lambda: False)()
    ):
        raise ReviewError("invalid_state_dir", f"invalid Antigravity state dir: {state_dir}")


def validate_work_dir(work_dir: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        work_dir.parent != temp_root
        or not work_dir.name.startswith(WORK_PREFIX)
        or work_dir.is_symlink()
        or getattr(work_dir, "is_junction", lambda: False)()
    ):
        raise ReviewError("invalid_work_dir", f"invalid Antigravity work dir: {work_dir}")


def create_private_state() -> tuple[Path, Path]:
    state_dir = Path(tempfile.mkdtemp(prefix=STATE_PREFIX)).resolve()
    work_dir: Path | None = None
    try:
        work_dir = Path(tempfile.mkdtemp(prefix=WORK_PREFIX)).resolve()
        helper_identity = lifecycle.process_identity(os.getpid())
        if helper_identity is None:
            raise ReviewError("process_identity", "cannot establish helper process identity")
        token = secrets.token_urlsafe(32)
        owner = {
                "helper": "dual-review-antigravity",
                "protocol_version": 1,
                "token": token,
                "created_at": time.time(),
                "helper_pid": os.getpid(),
                "helper_identity": helper_identity,
                "agy_pid": None,
                "agy_identity": None,
                "phase": "created",
                "atomic_job": False,
                "work_dir": str(work_dir),
            }
        marker = {
            "helper": "dual-review-antigravity",
            "protocol_version": 1,
            "token": token,
            "state_dir": str(state_dir),
        }
        lifecycle.write_json(work_dir / WORK_OWNER_FILENAME, marker)
        lifecycle.write_json(state_dir / OWNER_FILENAME, owner)
        return state_dir, work_dir
    except BaseException:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        raise


def read_owned_pair(state_dir: Path) -> tuple[dict[str, Any], Path]:
    validate_state_dir(state_dir)
    owner = lifecycle.read_json(state_dir / OWNER_FILENAME)
    if (
        owner.get("helper") != "dual-review-antigravity"
        or owner.get("protocol_version") != 1
        or not isinstance(owner.get("token"), str)
        or len(owner["token"]) < 32
    ):
        raise ReviewError("state_ownership", "invalid Antigravity owner metadata")
    work_dir = Path(str(owner.get("work_dir") or "")).resolve()
    validate_work_dir(work_dir)
    marker = lifecycle.read_json(work_dir / WORK_OWNER_FILENAME)
    if (
        marker.get("helper") != owner["helper"]
        or marker.get("protocol_version") != owner["protocol_version"]
        or marker.get("token") != owner["token"]
        or Path(str(marker.get("state_dir") or "")).resolve() != state_dir
    ):
        raise ReviewError("state_ownership", "state/work ownership markers do not match")
    return owner, work_dir


def update_owner(state_dir: Path, **updates: Any) -> None:
    owner, _ = read_owned_pair(state_dir)
    owner.update(updates)
    lifecycle.write_json(state_dir / OWNER_FILENAME, owner)


def remove_private_state(state_dir: Path, work_dir: Path) -> None:
    _, owned_work_dir = read_owned_pair(state_dir)
    if owned_work_dir != work_dir.resolve():
        raise ReviewError("state_ownership", "cleanup work dir does not match owner")
    if work_dir.exists():
        remove_work_dir(work_dir)
    if state_dir.exists():
        shutil.rmtree(state_dir)


def process_record_is_active(pid_value: Any, identity: Any) -> bool | None:
    try:
        pid = int(pid_value or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    running = lifecycle.process_is_running(pid)
    if running is not True:
        return running
    if not isinstance(identity, str) or not identity:
        return None
    return lifecycle.process_identity(pid) == identity


def cleanup_orphan_state(state_dir: Path) -> dict[str, Any]:
    owner, work_dir = read_owned_pair(state_dir)
    if owner.get("phase") == "spawning_child" and not owner.get("atomic_job"):
        raise ReviewError("state_unknown", "child spawn state lacks an atomic job guarantee")
    for label in ("helper", "agy"):
        active = process_record_is_active(
            owner.get(f"{label}_pid"), owner.get(f"{label}_identity")
        )
        if active is True:
            raise ReviewError("state_active", f"{label} process is still active")
        if active is None:
            raise ReviewError("state_unknown", f"cannot verify {label} process state")
    remove_private_state(state_dir, work_dir)
    return {"state": "cleaned", "state_dir": str(state_dir), "work_dir": str(work_dir)}


def sweep_review(args: argparse.Namespace) -> int:
    if args.older_than <= 0:
        raise ReviewError("invalid_older_than", "older-than must be greater than zero")
    temp_root = Path(tempfile.gettempdir()).resolve()
    cleaned: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    skipped_recent = 0
    now = time.time()
    for candidate in temp_root.iterdir():
        if not candidate.name.startswith(STATE_PREFIX):
            continue
        try:
            state_dir = candidate.resolve()
            validate_state_dir(state_dir)
            owner = lifecycle.read_json(state_dir / OWNER_FILENAME)
            created_at = float(owner.get("created_at") or 0)
            if created_at <= 0:
                raise ReviewError("state_ownership", "invalid created_at")
            if now - created_at < args.older_than:
                skipped_recent += 1
                continue
            cleaned.append(cleanup_orphan_state(state_dir))
        except BaseException as exc:
            refused.append(
                {"state_dir": str(candidate), "reason": f"{type(exc).__name__}: {exc}"}
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


def run_contract_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(command, **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(
            "antigravity_contract",
            f"cannot verify Antigravity CLI contract: {type(exc).__name__}: {exc}",
        ) from exc


def validate_antigravity_contract(executable: Path, model: str) -> None:
    help_result = run_contract_command([str(executable), "--help"], 30)
    help_text = (help_result.stdout + help_result.stderr).decode("utf-8", errors="replace")
    compact_help = " ".join(help_text.split())
    missing = [flag for flag in REQUIRED_FLAGS if flag not in compact_help]
    if help_result.returncode != 0 or missing:
        detail = ", ".join(missing) if missing else f"exit {help_result.returncode}"
        raise ReviewError(
            "antigravity_contract",
            f"Antigravity CLI help contract is incompatible: {detail}",
        )
    models_result = run_contract_command([str(executable), "models"], 90)
    models_text = models_result.stdout.decode("utf-8", errors="replace")
    if models_result.returncode != 0:
        stderr = models_result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise ReviewError(
            "antigravity_models",
            f"agy models failed with exit {models_result.returncode}: {stderr}",
        )
    slugs = {line.split("\t", 1)[0].strip() for line in models_text.splitlines() if line.strip()}
    if model not in slugs:
        raise ReviewError(
            "antigravity_model_unavailable",
            f"required model slug is unavailable: {model}",
        )


def frame_prompt(prompt_raw: bytes) -> str:
    prompt = prompt_raw.decode("utf-8", errors="strict")
    return f"{SYSTEM_GUARD}\n\n{prompt}"


def parse_antigravity_stream(
    stdout_raw: bytes,
    expected_model: str,
    expected_cwd: Path,
) -> str:
    try:
        text = stdout_raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewError("output_encoding", f"agy stdout is not valid UTF-8: {exc}") from exc
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    terminal_seen = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewError(
                "antigravity_protocol",
                f"invalid stream-json line {line_number}: {exc}",
            ) from exc
        event_name = event.get("event")
        if event_name == "init":
            if init is not None or result is not None or line_number != 1:
                raise ReviewError("antigravity_protocol", "init must be the first event")
            init = event.get("init") or {}
        elif event_name == "step_update":
            if init is None or terminal_seen:
                raise ReviewError("antigravity_protocol", "step outside active turn")
            update = event.get("step_update") or {}
            step_type = update.get("step_type")
            if (
                step_type not in {"user_input", "agent_response", "checkpoint", "finish"}
                or update.get("tool_name")
                or update.get("tool_info")
                or update.get("subagent_info")
            ):
                raise ReviewError(
                    "antigravity_tool_use",
                    f"disallowed or unknown step type: {step_type!r}",
                )
            allowed_states = {
                "user_input": {"DONE"},
                "agent_response": {"ACTIVE", "DONE"},
                "checkpoint": {"DONE"},
                "finish": {"DONE"},
            }
            if update.get("state") not in allowed_states[step_type]:
                raise ReviewError(
                    "antigravity_protocol",
                    f"invalid {step_type!r} state: {update.get('state')!r}",
                )
        elif event_name == "result":
            if init is None or result is not None:
                raise ReviewError("antigravity_protocol", "result outside active turn")
            result = event.get("result") or {}
            terminal_seen = True
        else:
            raise ReviewError(
                "antigravity_protocol", f"unknown top-level event: {event_name!r}"
            )
    if init is None or result is None:
        raise ReviewError("antigravity_protocol", "stream lacks init or terminal result")
    if init.get("model") != expected_model:
        raise ReviewError(
            "antigravity_model_mismatch",
            f"expected {expected_model}, got {init.get('model')!r}",
        )
    if Path(str(init.get("cwd", ""))).resolve() != expected_cwd.resolve():
        raise ReviewError(
            "antigravity_workspace_mismatch",
            f"expected private cwd {expected_cwd}, got {init.get('cwd')!r}",
        )
    if init.get("permission_mode") not in {"request-review", "strict"}:
        raise ReviewError(
            "antigravity_permissions",
            f"unsafe runtime permission mode: {init.get('permission_mode')!r}",
        )
    if init.get("json_schema") != REVIEW_SCHEMA:
        raise ReviewError(
            "antigravity_schema_mismatch",
            "agy init did not confirm the required review schema",
        )
    if result.get("status") != "SUCCESS":
        raise ReviewError(
            "antigravity_failed",
            f"agy result status={result.get('status')!r}: {result.get('error') or ''}",
        )
    if result.get("json_schema") != REVIEW_SCHEMA:
        raise ReviewError(
            "antigravity_schema_mismatch",
            "agy result did not echo the required review schema",
        )
    structured = result.get("structured_output")
    if not isinstance(structured, dict) or set(structured) != {"review", "decision"}:
        raise ReviewError(
            "antigravity_structured_output",
            "agy returned an invalid structured review object",
        )
    review = structured.get("review")
    decision = structured.get("decision")
    if (
        not isinstance(review, str)
        or not review.strip()
        or decision not in REVIEW_DECISIONS
    ):
        raise ReviewError(
            "antigravity_structured_output",
            "agy structured review or decision is invalid",
        )
    return f"{review.rstrip()}\n\nDecision：{decision}\n"


def remove_work_dir(work_dir: Path) -> None:
    attempts = 50 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            shutil.rmtree(work_dir)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.1)


def execute_antigravity(
    executable: Path,
    model: str,
    prompt_raw: bytes,
    timeout: int,
    state_dir: Path,
    work_dir: Path,
) -> str:
    process: subprocess.Popen[bytes] | None = None
    process_group: int | None = None
    identity: str | None = None
    job_handle: int | None = None
    child_marker = secrets.token_urlsafe(32)
    try:
        framed = frame_prompt(prompt_raw)
        input_event = (
            json.dumps(
                {"event": "user", "message": {"content": framed}},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        command = [
            str(executable),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--json-schema",
            json.dumps(REVIEW_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--model",
            model,
            "--disable-slash-commands",
            "--sandbox",
            "--print-timeout",
            f"{timeout}s",
            "--log-file",
            str(work_dir / "agy.log"),
        ]
        child_env = os.environ.copy()
        child_env[lifecycle.CHILD_MARKER_ENV] = child_marker
        options: dict[str, Any] = {
            "cwd": work_dir,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": child_env,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        if os.name == "nt":
            # Put this helper in a kill-on-close Job before child creation. Windows
            # atomically inherits Job membership into descendants, so a hard kill
            # cannot strand agy in the Popen -> AssignProcessToJobObject window.
            job_handle = lifecycle.create_kill_on_close_job(os.getpid())
            update_owner(state_dir, phase="spawning_child", atomic_job=True)
        process = subprocess.Popen(command, **options)
        process_group = process.pid if os.name != "nt" else None
        update_owner(state_dir, phase="spawning", agy_pid=process.pid)
        identity = lifecycle.process_identity(process.pid)
        if identity is None:
            raise ReviewError("process_identity", "cannot establish agy process identity")
        update_owner(state_dir, phase="running", agy_identity=identity)
        try:
            stdout_raw, stderr_raw = process.communicate(input=input_event, timeout=timeout + 60)
        except subprocess.TimeoutExpired as exc:
            lifecycle.terminate_claude(process, process_group, identity, child_marker)
            raise ReviewError(
                "antigravity_timeout", f"agy review exceeded {timeout + 60} seconds"
            ) from exc
        except KeyboardInterrupt:
            lifecycle.terminate_claude(process, process_group, identity, child_marker)
            raise
        if process.returncode != 0:
            stderr = stderr_raw.decode("utf-8", errors="replace")[-4000:]
            raise ReviewError(
                "antigravity_exit",
                f"agy exited with {process.returncode}: {stderr}",
            )
        return parse_antigravity_stream(stdout_raw, model, work_dir)
    finally:
        primary_exception = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        # Do not close a Job containing this helper: KILL_ON_JOB_CLOSE would end
        # us before the caller can emit the review/error envelope. The OS closes
        # the raw handle when this short-lived CLI helper exits.
        if process is not None and process.poll() is None:
            error = lifecycle.terminate_popen(process)
            if error:
                cleanup_errors.append(error)
        try:
            remove_private_state(state_dir, work_dir)
        except OSError as exc:
            cleanup_errors.append(
                f"private state cleanup failed: {type(exc).__name__}: {exc}"
            )
        if cleanup_errors:
            primary_kind = (
                primary_exception.error_kind
                if isinstance(primary_exception, ReviewError)
                else type(primary_exception).__name__ if primary_exception else None
            )
            raise ReviewError(
                "cleanup_failed",
                "; ".join(cleanup_errors),
                {
                    "cleanup_failed": True,
                    "state_dir": str(state_dir),
                    "work_dir": str(work_dir),
                    "primary_error_kind": primary_kind,
                    "primary_error": str(primary_exception) if primary_exception else None,
                },
            ) from primary_exception


def validate_prompt_hash(prompt_raw: bytes, expected_sha256: str) -> None:
    actual_sha256 = hashlib.sha256(prompt_raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReviewError("invalid_prompt_hash", "expected prompt SHA-256 is invalid")
    if actual_sha256 != expected_sha256:
        raise ReviewError(
            "prompt_hash_mismatch",
            "fallback prompt bytes do not match the Claude review snapshot",
        )


def run_review(args: argparse.Namespace) -> int:
    try:
        lifecycle.validate_runtime_security()
        validate_antigravity_permissions()
        if args.timeout <= 0:
            raise ReviewError("invalid_timeout", "timeout must be greater than zero")
        prompt_raw = read_prompt_base64()
        validate_prompt_hash(prompt_raw, args.expected_prompt_sha256)
        executable = resolve_antigravity_binary(args.antigravity_bin)
        validate_antigravity_contract(executable, DEFAULT_MODEL)
        state_dir, work_dir = create_private_state()
        review = execute_antigravity(
            executable, DEFAULT_MODEL, prompt_raw, args.timeout, state_dir, work_dir
        )
        lifecycle.write_stdout_bytes(review.encode("utf-8", errors="strict"))
        return 0
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            error_kind = "interrupted"
        elif isinstance(exc, ReviewError):
            error_kind = exc.error_kind
        else:
            error_kind = "antigravity_helper_error"
        payload = {
            "state": "failed",
            "error_kind": error_kind,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if isinstance(exc, ReviewError):
            payload.update(exc.details)
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


def run_self_test(_: argparse.Namespace) -> int:
    if not __debug__:
        raise SystemExit("self-test requires assertions enabled; do not use python -O")
    validate_permission_settings({})
    validate_permission_settings({"toolPermission": "strict", "permissions": {"allow": []}})
    try:
        validate_permission_settings({"permissions": {"allow": ["command(git)"]}})
    except ReviewError as exc:
        assert exc.error_kind == "antigravity_permissions"
    else:
        raise AssertionError("non-empty permissions.allow was not rejected")
    prompt_raw = b"same snapshot"
    prompt_hash = hashlib.sha256(prompt_raw).hexdigest()
    validate_prompt_hash(prompt_raw, prompt_hash)
    try:
        validate_prompt_hash(prompt_raw + b" changed", prompt_hash)
    except ReviewError as exc:
        assert exc.error_kind == "prompt_hash_mismatch"
    else:
        raise AssertionError("prompt hash mismatch was not rejected")
    state_dir, work_dir = create_private_state()
    owner_path = state_dir / OWNER_FILENAME
    owner = lifecycle.read_json(owner_path)
    owner.update(
        {
            "helper_pid": 99999999,
            "helper_identity": "windows:dead",
            "agy_pid": None,
            "agy_identity": None,
        }
    )
    lifecycle.write_json(owner_path, owner)
    cleaned = cleanup_orphan_state(state_dir)
    assert cleaned["state"] == "cleaned"
    assert not state_dir.exists() and not work_dir.exists()
    active_state, active_work = create_private_state()
    try:
        try:
            cleanup_orphan_state(active_state)
        except ReviewError as exc:
            assert exc.error_kind == "state_active"
        else:
            raise AssertionError("active ownership state was cleaned")
    finally:
        remove_private_state(active_state, active_work)
    state_a, work_a = create_private_state()
    state_b, work_b = create_private_state()
    try:
        owner_a = lifecycle.read_json(state_a / OWNER_FILENAME)
        owner_a["work_dir"] = str(work_b)
        lifecycle.write_json(state_a / OWNER_FILENAME, owner_a)
        try:
            cleanup_orphan_state(state_a)
        except ReviewError as exc:
            assert exc.error_kind == "state_ownership"
        else:
            raise AssertionError("cross-paired state/work directories were accepted")
        owner_a["work_dir"] = str(work_a)
        lifecycle.write_json(state_a / OWNER_FILENAME, owner_a)
    finally:
        remove_private_state(state_a, work_a)
        remove_private_state(state_b, work_b)
    if os.name == "nt":
        script_dir = str(Path(__file__).resolve().parent)
        crash_script = (
            "import os,subprocess,sys,time;"
            f"sys.path.insert(0,{script_dir!r});"
            "import run_claude_review as lifecycle;"
            "lifecycle.create_kill_on_close_job(os.getpid());"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
            "creationflags=subprocess.CREATE_NO_WINDOW);"
            "print(p.pid,flush=True);os._exit(97)"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", crash_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert crashed.returncode == 97, crashed.stderr
        inherited_pid = int(crashed.stdout.strip())
        for _ in range(50):
            if lifecycle.process_is_running(inherited_pid) is False:
                break
            time.sleep(0.1)
        assert lifecycle.process_is_running(inherited_pid) is False
    structured_review = {
        "review": "## Critical\n\n无。\n\n## Important\n\n无。\n\n## Suggestion\n\n无。",
        "decision": "pass",
    }
    good = [
        {
            "event": "init",
            "init": {
                "model": DEFAULT_MODEL,
                "cwd": str(Path.cwd()),
                "permission_mode": "request-review",
                "tools": ["run_command"],
                "json_schema": REVIEW_SCHEMA,
            },
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "state": "ACTIVE"},
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "checkpoint", "state": "DONE"},
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "finish", "state": "DONE"},
        },
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": json.dumps(
                    {
                        **structured_review,
                        "toolAction": "Internal rendering metadata",
                        "toolSummary": "Internal rendering metadata",
                    },
                    ensure_ascii=False,
                ),
                "structured_output": structured_review,
                "json_schema": REVIEW_SCHEMA,
            },
        },
    ]
    payload = ("\n".join(json.dumps(item) for item in good) + "\n").encode()
    assert (
        parse_antigravity_stream(payload, DEFAULT_MODEL, Path.cwd())
        == structured_review["review"] + "\n\nDecision：pass\n"
    )
    tool_payload = list(good)
    tool_payload.insert(
        1,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "run_command",
            },
        },
    )
    try:
        parse_antigravity_stream(
            ("\n".join(json.dumps(item) for item in tool_payload) + "\n").encode(),
            DEFAULT_MODEL,
            Path.cwd(),
        )
    except ReviewError as exc:
        assert exc.error_kind == "antigravity_tool_use"
    else:
        raise AssertionError("tool use was not rejected")
    invalid_order = [good[-1], good[0]]
    try:
        parse_antigravity_stream(
            ("\n".join(json.dumps(item) for item in invalid_order) + "\n").encode(),
            DEFAULT_MODEL,
            Path.cwd(),
        )
    except ReviewError as exc:
        assert exc.error_kind == "antigravity_protocol"
    else:
        raise AssertionError("result-before-init protocol was not rejected")
    unknown_payload = list(good)
    unknown_payload.insert(
        1,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool_call",
                "state": "DONE",
                "tool_name": "run_command",
            },
        },
    )
    try:
        parse_antigravity_stream(
            ("\n".join(json.dumps(item) for item in unknown_payload) + "\n").encode(),
            DEFAULT_MODEL,
            Path.cwd(),
        )
    except ReviewError as exc:
        assert exc.error_kind == "antigravity_tool_use"
    else:
        raise AssertionError("unknown step type was not rejected")
    invalid_schema = json.loads(json.dumps(good))
    invalid_schema[0]["init"]["json_schema"] = {"type": "string"}
    try:
        parse_antigravity_stream(
            ("\n".join(json.dumps(item) for item in invalid_schema) + "\n").encode(),
            DEFAULT_MODEL,
            Path.cwd(),
        )
    except ReviewError as exc:
        assert exc.error_kind == "antigravity_schema_mismatch"
    else:
        raise AssertionError("schema mismatch was not rejected")

    def assert_stream_rejected(events: list[dict[str, Any]], error_kind: str) -> None:
        try:
            parse_antigravity_stream(
                ("\n".join(json.dumps(item) for item in events) + "\n").encode(),
                DEFAULT_MODEL,
                Path.cwd(),
            )
        except ReviewError as exc:
            assert exc.error_kind == error_kind, exc.error_kind
        else:
            raise AssertionError(f"stream was not rejected as {error_kind}")

    bad_checkpoint_state = json.loads(json.dumps(good))
    bad_checkpoint_state[2]["step_update"]["state"] = "ACTIVE"
    assert_stream_rejected(bad_checkpoint_state, "antigravity_protocol")
    checkpoint_tool = json.loads(json.dumps(good))
    checkpoint_tool[2]["step_update"]["tool_info"] = {"name": "run_command"}
    assert_stream_rejected(checkpoint_tool, "antigravity_tool_use")
    finish_subagent = json.loads(json.dumps(good))
    finish_subagent[3]["step_update"]["subagent_info"] = {"subagents": [{}]}
    assert_stream_rejected(finish_subagent, "antigravity_tool_use")
    unknown_top_level = json.loads(json.dumps(good))
    unknown_top_level.insert(1, {"event": "checkpoint"})
    assert_stream_rejected(unknown_top_level, "antigravity_protocol")
    bad_result_schema = json.loads(json.dumps(good))
    bad_result_schema[-1]["result"]["json_schema"] = {"type": "string"}
    assert_stream_rejected(bad_result_schema, "antigravity_schema_mismatch")
    for invalid_structured in (
        {"review": structured_review["review"]},
        {**structured_review, "extra": True},
        {"review": "", "decision": "pass"},
        {"review": structured_review["review"], "decision": "maybe"},
    ):
        invalid_output = json.loads(json.dumps(good))
        invalid_output[-1]["result"]["structured_output"] = invalid_structured
        assert_stream_rejected(invalid_output, "antigravity_structured_output")
    print(
        json.dumps(
            {
                "state": "passed",
                "checks": [
                    "stream_protocol",
                    "model_pin",
                    "structured_output_validation",
                    "structured_output_rejection",
                    "checkpoint_finish_validation",
                    "checkpoint_finish_rejection",
                    "tool_use_rejection",
                    "unknown_step_rejection",
                    "strict_event_order",
                    "permission_allow_rejection",
                    "prompt_hash_match",
                    "atomic_job_hard_kill" if os.name == "nt" else "windows_cleanup_skipped",
                    "discoverable_state_lifecycle",
                    "bidirectional_state_ownership",
                    "single_error_envelope",
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one synchronous Antigravity review")
    run.add_argument("--prompt-base64-stdin", action="store_true", required=True)
    run.add_argument("--antigravity-bin", default="agy")
    run.add_argument("--expected-prompt-sha256", required=True)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run.set_defaults(func=run_review)
    sweep = commands.add_parser("sweep", help="clean orphaned Antigravity helper states")
    sweep.add_argument("--older-than", type=int, default=86400)
    sweep.set_defaults(func=sweep_review)
    self_test = commands.add_parser("self-test", help="run offline helper checks")
    self_test.set_defaults(func=run_self_test)
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
