"""Small shared helpers: errors, logging, hashing, subprocesses."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


class BuildError(RuntimeError):
    """A condition that must stop the build (fail closed)."""


_T0 = time.monotonic()


def info(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:6.1f}s] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:6.1f}s] WARNING: {msg}", flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str:
    """Read a source file as UTF-8 (falling back to latin-1) with LF line endings."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, obj, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        data = json.dumps(obj, ensure_ascii=False, indent=1)
    path.write_text(data, encoding="utf-8", newline="\n")


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:  # pragma: no cover - the build runs on Windows
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: float = 300,
        input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command as an argument list (no shell); kill the whole process tree on timeout."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=full_env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(
            input=input_text.encode("utf-8") if input_text is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        proc.communicate()
        raise BuildError(f"timed out after {timeout:.0f}s: {' '.join(map(str, cmd))[:200]}")
    res = subprocess.CompletedProcess(cmd, proc.returncode,
                                      out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    if check and res.returncode != 0:
        raise BuildError(f"command failed ({res.returncode}): {' '.join(map(str, cmd))[:200]}\n"
                         f"{res.stdout[-2000:]}\n{res.stderr[-2000:]}")
    return res


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)
