"""Deterministic Drive9 validation agent for AgentCore Runtime Instances.

The handler exposes explicit actions so test outcomes do not depend on model
reasoning. All user-supplied paths are relative to the current test run root.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import boto3

try:
    # bedrock-agentcore >= 1.0 exports the runtime app at the package root.
    from bedrock_agentcore import BedrockAgentCoreApp
except ImportError:  # pragma: no cover - compatibility with early SDK builds
    from bedrock_agentcore.runtime import BedrockAgentCoreApp


app = BedrockAgentCoreApp()

AGENT_ID = os.getenv("AGENT_ID", "agent-unknown")
SECRET_ID = os.getenv("DRIVE9_SECRET_ID", "")
TEST_RUN_ID = os.getenv("TEST_RUN_ID", "local-test")
DRIVE9_SERVER = os.getenv("DRIVE9_SERVER", "https://api.drive9.ai")
EXPECTED_ARCHITECTURE = os.getenv("EXPECTED_ARCHITECTURE", "arm64").lower()
SHARED_BASE_REMOTE = f":/agentcore-tests/{TEST_RUN_ID}/shared"
BASE_REMOTE = SHARED_BASE_REMOTE if AGENT_ID in {"agent-a", "agent-b"} else f":/agentcore-tests/{TEST_RUN_ID}/agent-c"
MAX_RESPONSE_BYTES = int(os.getenv("MAX_RESPONSE_BYTES", "1048576"))

_CONTEXT_LOCK = threading.Lock()
_CONTEXT_READY = False
_CONTEXT_NAME = ""
_SECRET_TOKEN = ""
_SECRET_SERVER = ""


def _redact(value: str) -> str:
    if _SECRET_TOKEN:
        value = value.replace(_SECRET_TOKEN, "***REDACTED***")
    return value


def _run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return {
            "command": args[0:3],
            "exit_code": proc.returncode,
            "stdout": _redact(proc.stdout[:MAX_RESPONSE_BYTES]),
            "stderr": _redact(proc.stderr[:MAX_RESPONSE_BYTES]),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": args[0:3],
            "exit_code": 124,
            "stdout": _redact((exc.stdout or "")[:MAX_RESPONSE_BYTES]),
            "stderr": "command timed out",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except FileNotFoundError as exc:
        return {
            "command": args[0:3],
            "exit_code": 127,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def _drive9_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = f"/tmp/drive9-home-{AGENT_ID}"
    env["DRIVE9_SERVER"] = _SECRET_SERVER or DRIVE9_SERVER
    # drive9 accepts bearer tokens through DRIVE9_API_KEY. This avoids passing
    # a credential to `ctx import`, which expects a different JWT format.
    token = _load_secret_token()
    if token:
        env["DRIVE9_API_KEY"] = token
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return env


def _load_secret_token() -> str:
    global _SECRET_TOKEN, _SECRET_SERVER
    if _SECRET_TOKEN:
        return _SECRET_TOKEN
    if not SECRET_ID:
        token = os.getenv("DRIVE9_TEST_TOKEN", "") or os.getenv("DRIVE9_API_KEY", "")
        if not token:
            raise RuntimeError("DRIVE9_SECRET_ID or DRIVE9_TEST_TOKEN is required")
        _SECRET_TOKEN = token.strip()
        return _SECRET_TOKEN

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=SECRET_ID)
    value = response["SecretString"]
    parsed: Any = None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            token = parsed.get("token", parsed.get("api_key"))
        else:
            token = value
        if isinstance(parsed, dict) and parsed.get("server"):
            _SECRET_SERVER = str(parsed["server"]).rstrip("/")
    except (json.JSONDecodeError, KeyError, TypeError):
        token = value
    if not token:
        if isinstance(parsed, dict) and ("token" in parsed or "api_key" in parsed):
            raise RuntimeError("Drive9 secret token is empty")
        raise RuntimeError("Drive9 secret JSON must contain token or api_key")
    _SECRET_TOKEN = token.strip()
    if not _SECRET_TOKEN:
        raise RuntimeError("Drive9 token is empty")
    return _SECRET_TOKEN


def _context_names(output: str) -> list[str]:
    """Extract context names from the CLI's JSON output across CLI versions."""
    try:
        value: Any = json.loads(output)
    except json.JSONDecodeError:
        return []
    if isinstance(value, dict):
        value = value.get("contexts", value.get("items", value))
        if isinstance(value, dict):
            for key in ("name", "context", "contextName"):
                if isinstance(value.get(key), str):
                    return [value[key]]
            # Some CLI releases emit a name -> metadata mapping.
            return [name for name in value if isinstance(name, str)]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("name", "context", "contextName"):
                if isinstance(item.get(key), str):
                    names.append(item[key])
                    break
    return names


def _ensure_context() -> dict[str, Any]:
    global _CONTEXT_READY, _CONTEXT_NAME
    with _CONTEXT_LOCK:
        if _CONTEXT_READY:
            return {
                "status": "ready",
                "agent_id": AGENT_ID,
                "context": _CONTEXT_NAME,
                "credential_source": "DRIVE9_API_KEY",
                "server": _SECRET_SERVER or DRIVE9_SERVER,
            }

        env = _drive9_env()
        # A token from `drive9 token issue --print` is a bearer token, while
        # `drive9 ctx import` consumes a serialized delegated-context JWT. Use
        # the supported environment credential path for Runtime containers.
        probe = _run(["drive9", "fs", "ls", BASE_REMOTE], env=env)
        if probe["exit_code"] != 0:
            raise RuntimeError(f"Drive9 credential/workspace probe failed: {probe['stderr']}")
        _CONTEXT_NAME = AGENT_ID
        _CONTEXT_READY = True
        return {
            "status": "ready",
            "agent_id": AGENT_ID,
            "context": _CONTEXT_NAME,
            "credential_source": "DRIVE9_API_KEY",
            "server": _SECRET_SERVER or DRIVE9_SERVER,
        }


def _safe_component(value: str, field: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise ValueError(f"invalid {field}")
    return value


def _remote(relative: str | None = None) -> str:
    if relative in (None, "", "."):
        return BASE_REMOTE
    raw = str(relative)
    is_remote_absolute = raw.startswith(":/")
    path = PurePosixPath(raw[2:] if is_remote_absolute else raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative and cannot contain '..'")
    normalized = "/".join(part for part in path.parts if part not in ("", "."))
    if not normalized:
        return BASE_REMOTE
    return f":/{normalized}" if is_remote_absolute else f"{BASE_REMOTE}/{normalized}"


def _write_text(relative: str, content: str, *, append: bool = False) -> dict[str, Any]:
    _ensure_context()
    env = _drive9_env()
    destination = _remote(relative)
    if append:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(content)
            local_path = handle.name
        try:
            result = _run(
                ["drive9", "fs", "cp", "--append", local_path, destination],
                env=env,
            )
        finally:
            Path(local_path).unlink(missing_ok=True)
    else:
        result = _run(
            ["drive9", "fs", "cp", "-", destination],
            env=env,
            input_text=content,
        )
    result.update(
        {
            "path": destination,
            "size": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    )
    return result


def _read_text(relative: str) -> dict[str, Any]:
    _ensure_context()
    result = _run(["drive9", "fs", "cat", _remote(relative)], env=_drive9_env())
    if result["exit_code"] == 0:
        raw = result["stdout"].encode("utf-8")
        result.update({"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return result


def _architecture_matches(observed: str) -> bool:
    """Compare uname output using the names exposed by Linux and AgentCore."""
    normalized = observed.lower().strip()
    expected = EXPECTED_ARCHITECTURE.replace("linux_", "").replace("linux/", "")
    if expected in {"arm64", "aarch64"}:
        return normalized in {"arm64", "aarch64"}
    return normalized == expected


def action_preflight(_: dict[str, Any]) -> dict[str, Any]:
    context_error = None
    try:
        context = _ensure_context()
    except Exception as exc:  # returned as evidence for environment diagnosis
        context = None
        context_error = str(exc)
    env = _drive9_env()
    checks = {
        "uname": _run(["uname", "-a"]),
        "architecture": _run(["uname", "-m"]),
        "identity": _run(["id"]),
        "drive9_version": _run(["drive9", "--version"], env=env),
        "dns": _run(
            [
                "getent",
                "hosts",
                urlparse(_SECRET_SERVER or DRIVE9_SERVER).hostname or "api.drive9.ai",
            ]
        ),
        "https": _run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                _SECRET_SERVER or DRIVE9_SERVER,
            ],
            timeout=30,
        ),
        "context": _run(["drive9", "ctx", "show", "--json"], env=env),
        "workspace_list": _run(["drive9", "fs", "ls", BASE_REMOTE], env=env),
        "fuse_filesystems": _run(["sh", "-c", "grep -i fuse /proc/filesystems || true"]),
        "capabilities": _run(["capsh", "--print"]),
        "drive9_fuse_doctor": _run(["drive9", "doctor", "fuse"], env=env),
    }
    # Bearer-token authentication intentionally does not require a local
    # delegated context. `ctx show` remains diagnostic only.
    required_checks = ("drive9_version", "dns", "https", "workspace_list")
    https_code = checks["https"]["stdout"].strip()
    checks_ok = all(checks[name]["exit_code"] == 0 for name in required_checks)
    checks_ok = checks_ok and bool(re.fullmatch(r"[1-5][0-9][0-9]", https_code))
    return {
        "status": "PASS"
        if context_error is None
        and _architecture_matches(checks["architecture"]["stdout"].strip())
        and checks_ok
        else "FAIL",
        "agent_id": AGENT_ID,
        "test_run_id": TEST_RUN_ID,
        "expected_architecture": EXPECTED_ARCHITECTURE,
        "python_architecture": platform.machine(),
        "dev_fuse_exists": Path("/dev/fuse").exists(),
        "context_init": context,
        "context_error": context_error,
        "checks": checks,
    }


def action_write(payload: dict[str, Any]) -> dict[str, Any]:
    result = _write_text(str(payload["path"]), str(payload.get("content", "")))
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_read(payload: dict[str, Any]) -> dict[str, Any]:
    result = _read_text(str(payload["path"]))
    expected = payload.get("expected_content")
    content_match = expected is None or result.get("stdout") == str(expected)
    return {
        "status": "PASS" if result["exit_code"] == 0 and content_match else "FAIL",
        "content_match": content_match,
        "result": result,
    }


def action_append(payload: dict[str, Any]) -> dict[str, Any]:
    result = _write_text(str(payload["path"]), str(payload.get("content", "")), append=True)
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_list(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    result = _run(["drive9", "fs", "ls", "-l", _remote(payload.get("path", "."))], env=_drive9_env())
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_stat(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    result = _run(["drive9", "fs", "stat", "-o", "json", _remote(str(payload["path"]))], env=_drive9_env())
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_move(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    result = _run(
        ["drive9", "fs", "mv", _remote(str(payload["source"])), _remote(str(payload["destination"]))],
        env=_drive9_env(),
    )
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_copy(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    result = _run(
        ["drive9", "fs", "cp", _remote(str(payload["source"])), _remote(str(payload["destination"]))],
        env=_drive9_env(),
    )
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_delete(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    result = _run(["drive9", "fs", "rm", _remote(str(payload["path"]))], env=_drive9_env())
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_find(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    args = ["drive9", "fs", "find", _remote(payload.get("path", "."))]
    for key, flag in (("name", "-name"), ("newer", "-newer"), ("older", "-older"), ("size", "-size")):
        if payload.get(key) is not None:
            args.extend([flag, str(payload[key])])
    tags = payload.get("tag", [])
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags:
        args.extend(["-tag", str(tag)])
    result = _run(args, env=_drive9_env())
    return {"status": "PASS" if result["exit_code"] == 0 else "FAIL", "result": result}


def action_search(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    query = str(payload["query"])
    result = _run(
        ["drive9", "fs", "grep", "--json", query, _remote(payload.get("path", "."))],
        env=_drive9_env(),
        timeout=180,
    )
    if result["exit_code"] != 0:
        fallback = _run(
            ["drive9", "fs", "grep", query, _remote(payload.get("path", "."))],
            env=_drive9_env(),
            timeout=180,
        )
        result["fallback"] = fallback
        if fallback["exit_code"] == 0:
            return {"status": "PASS", "result": result}
    return {"status": "PASS" if result["exit_code"] == 0 else "FEATURE_UNAVAILABLE", "result": result}


def action_handoff_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = _safe_component(str(payload.get("task_id", "task-001")), "task_id")
    state = {
        "task_id": task_id,
        "producer_agent": AGENT_ID,
        "stage": payload.get("stage", "code-modified"),
        "git_branch": payload.get("git_branch", "feature/drive9-test"),
        "test_status": payload.get("test_status", "not-run"),
        "next_action": payload.get("next_action", "continue validation"),
        "checkpoint_id": str(uuid.uuid4()),
        "created_at": time.time(),
    }
    relative = f"handoff/{task_id}/state.json"
    result = _write_text(relative, json.dumps(state, ensure_ascii=False, indent=2))
    return {
        "status": "PASS" if result["exit_code"] == 0 else "FAIL",
        "handoff_path": relative,
        "state": state,
        "result": result,
    }


def action_handoff_continue(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = _safe_component(str(payload.get("task_id", "task-001")), "task_id")
    state_path = f"handoff/{task_id}/state.json"
    source = _read_text(state_path)
    if source["exit_code"] != 0:
        return {"status": "FAIL", "read_state": source}
    state = json.loads(source["stdout"])
    continuation = {
        "task_id": task_id,
        "consumer_agent": AGENT_ID,
        "source_checkpoint_id": state.get("checkpoint_id"),
        "source_agent": state.get("producer_agent"),
        "result": payload.get("result", "task continued successfully"),
        "completed_at": time.time(),
    }
    result_path = f"handoff/{task_id}/result-{AGENT_ID}.json"
    write = _write_text(result_path, json.dumps(continuation, ensure_ascii=False, indent=2))
    return {
        "status": "PASS" if write["exit_code"] == 0 else "FAIL",
        "source_state": state,
        "result_path": result_path,
        "write_result": write,
    }


def action_permission_test(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    expect_write = bool(payload.get("expect_write_allowed", AGENT_ID != "agent-c"))
    probe = f"permissions/{AGENT_ID}-probe.txt"
    write = _write_text(probe, f"permission probe from {AGENT_ID}\n")
    write_allowed = write["exit_code"] == 0
    delete = None
    if write_allowed:
        delete = _run(["drive9", "fs", "rm", _remote(probe)], env=_drive9_env())
    outside = _run(
        ["drive9", "fs", "cat", ":/agentcore-tests/outside-scope-probe.txt"],
        env=_drive9_env(),
    )
    passed = write_allowed == expect_write and outside["exit_code"] != 0
    return {
        "status": "PASS" if passed else "FAIL",
        "expected_write_allowed": expect_write,
        "actual_write_allowed": write_allowed,
        "write": write,
        "delete": delete,
        "outside_scope_read": outside,
    }


def action_visibility_test(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    target = _remote(str(payload["path"]))
    expect_visible = bool(payload.get("expect_visible", AGENT_ID != "agent-c"))
    result = _run(["drive9", "fs", "ls", "-l", target], env=_drive9_env())
    visible = result["exit_code"] == 0
    passed = visible == expect_visible
    return {
        "status": "PASS" if passed else "FAIL",
        "target": target,
        "expected_visible": expect_visible,
        "actual_visible": visible,
        "result": result,
    }


def _mounted(callback: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    _ensure_context()
    env = _drive9_env()
    mountpoint = Path(f"/mnt/drive9-{AGENT_ID}")
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # AgentCore may make the image filesystem read-only or deny writes to
        # /mnt even when the process runs as root. Treat that as a platform
        # capability limitation, matching the /dev/fuse handling below.
        if exc.errno in {1, 13, 30}:  # EPERM, EACCES, EROFS
            return {
                "status": "PLATFORM_BLOCKED",
                "dev_fuse_exists": Path("/dev/fuse").exists(),
                "mount_log": str(exc),
            }
        raise
    log_path = Path(f"/tmp/drive9-mount-{AGENT_ID}.log")

    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                "drive9",
                "mount",
                "--direct-mount-strict",
                "--supervise-foreground",
                "--debug",
                BASE_REMOTE,
                str(mountpoint),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

        healthy = False
        health: dict[str, Any] | None = None
        for _ in range(30):
            if process.poll() is not None:
                break
            health = _run(["drive9", "mount", "health", str(mountpoint)], env=env, timeout=5)
            if health["exit_code"] == 0:
                healthy = True
                break
            time.sleep(1)

        if not healthy:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            log_handle.flush()
            log_text = log_path.read_text(encoding="utf-8", errors="replace")[-MAX_RESPONSE_BYTES:]
            reason = "PLATFORM_BLOCKED" if not Path("/dev/fuse").exists() or "operation not permitted" in log_text.lower() else "FAIL"
            return {
                "status": reason,
                "dev_fuse_exists": Path("/dev/fuse").exists(),
                "health": health,
                "mount_log": _redact(log_text),
            }

        try:
            callback_result = callback(mountpoint)
            return {
                "status": "PASS",
                "health": health,
                "callback": callback_result,
            }
        finally:
            _run(["drive9", "mount", "drain", "--timeout", "60s", str(mountpoint)], env=env, timeout=75)
            _run(["drive9", "umount", str(mountpoint)], env=env, timeout=30)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()


def action_mount_test(_: dict[str, Any]) -> dict[str, Any]:
    marker = f"mount test from {AGENT_ID} at {time.time()}\n"

    def callback(mountpoint: Path) -> dict[str, Any]:
        target = mountpoint / f"mount-test-{AGENT_ID}.txt"
        with target.open("w", encoding="utf-8") as handle:
            handle.write(marker)
            handle.flush()
            os.fsync(handle.fileno())
        remote_read = _read_text(target.name)
        return {
            "local_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            "remote_read": remote_read,
            "match": remote_read.get("stdout") == marker,
        }

    result = _mounted(callback)
    if result["status"] == "PASS" and not result["callback"]["match"]:
        result["status"] = "FAIL"
    return result


def _git_snapshot(repo: Path) -> dict[str, Any]:
    def git(*args: str) -> dict[str, Any]:
        return _run(["git", "-C", str(repo), *args], timeout=60)

    return {
        "branch": git("branch", "--show-current"),
        "head": git("rev-parse", "HEAD"),
        "status": git("status", "--porcelain=v1"),
        "log": git("log", "-1", "--oneline"),
    }


def action_git_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = _safe_component(str(payload.get("task_id", "git-001")), "task_id")

    def callback(mountpoint: Path) -> dict[str, Any]:
        repo = mountpoint / "git-workspaces" / task_id
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _run(["git", "init", str(repo)])
            _run(["git", "-C", str(repo), "config", "user.email", "agentcore@example.invalid"])
            _run(["git", "-C", str(repo), "config", "user.name", "AgentCore Test"])
            (repo / "README.md").write_text("# Drive9 Git workspace test\n", encoding="utf-8")
            _run(["git", "-C", str(repo), "add", "README.md"])
            _run(["git", "-C", str(repo), "commit", "-m", "base commit"])
        _run(["git", "-C", str(repo), "checkout", "-B", f"feature/{task_id}"])
        (repo / "dirty.txt").write_text(f"uncommitted work from {AGENT_ID}\n", encoding="utf-8")
        return _git_snapshot(repo)

    return _mounted(callback)


def action_git_verify(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = _safe_component(str(payload.get("task_id", "git-001")), "task_id")

    def callback(mountpoint: Path) -> dict[str, Any]:
        repo = mountpoint / "git-workspaces" / task_id
        if not (repo / ".git").exists():
            raise RuntimeError("Git workspace was not found")
        snapshot = _git_snapshot(repo)
        snapshot["dirty_content"] = (repo / "dirty.txt").read_text(encoding="utf-8")
        return snapshot

    return _mounted(callback)


ACTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "preflight": action_preflight,
    "write": action_write,
    "read": action_read,
    "append": action_append,
    "list": action_list,
    "stat": action_stat,
    "move": action_move,
    "copy": action_copy,
    "delete": action_delete,
    "find": action_find,
    "search": action_search,
    "handoff_prepare": action_handoff_prepare,
    "handoff_continue": action_handoff_continue,
    "permission_test": action_permission_test,
    "visibility_test": action_visibility_test,
    "mount_test": action_mount_test,
    "git_prepare": action_git_prepare,
    "git_verify": action_git_verify,
}


@app.entrypoint
def handle_request(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    action = str(payload.get("action", ""))
    if action not in ACTIONS:
        return {
            "status": "FAIL",
            "error": f"unknown action: {action}",
            "available_actions": sorted(ACTIONS),
        }
    try:
        result = ACTIONS[action](payload)
    except Exception as exc:
        result = {"status": "FAIL", "error": _redact(str(exc))}
    result.update(
        {
            "action": action,
            "agent_id": AGENT_ID,
            "test_run_id": TEST_RUN_ID,
            "started_at_epoch": started,
            "finished_at_epoch": time.time(),
        }
    )
    return result


if __name__ == "__main__":
    app.run()
