"""Isolated subprocess gateway to the bundled GroundWork engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from lens import SUPPORTED_SCHEMA_MAJOR

ENGINE_PATH = Path(__file__).resolve().parent.parent / "engine" / "groundwork.py"
DEFAULT_TIMEOUT = 60


class EngineError(RuntimeError):
    """Raised when the GroundWork engine fails or returns invalid output."""


class UnsupportedSchemaError(EngineError):
    """Raised when an export reports a schema_version unknown to this Lens."""

    def __init__(self, schema_version: str, engine_version: str) -> None:
        self.schema_version = schema_version
        self.engine_version = engine_version
        super().__init__(
            f"This repo uses GroundWork {engine_version or 'unknown'} (schema {schema_version}); "
            f"upgrade GroundWork plugin and Lens to compatible versions."
        )


# Network settings git needs if it has to fetch on demand inside a partial
# clone (for example behind a corporate proxy or with a private CA).
_PASSTHROUGH_ENV = (
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)


def _scrubbed_env(tmp_home: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tmp_home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key in _PASSTHROUGH_ENV:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _run_engine(args: list[str], cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any] | list[Any]:
    with tempfile.TemporaryDirectory(prefix="gw-env-") as tmp_home:
        proc = subprocess.run(
            [sys.executable, str(ENGINE_PATH), *args],
            cwd=str(cwd) if cwd else None,
            env=_scrubbed_env(tmp_home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        if stdout.startswith("{"):
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and data.get("error"):
                    return data
            except json.JSONDecodeError:
                pass
        raise EngineError(stderr or stdout or f"Engine exited with code {proc.returncode}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise EngineError(f"Engine returned non-JSON output: {stdout[:200]}") from exc


_CACHED_PATHS: list[str] | None = None


def get_export_paths() -> list[str]:
    """Return the list of repo paths read by the GroundWork engine."""
    global _CACHED_PATHS
    if _CACHED_PATHS is None:
        res = _run_engine(["export", "--paths", "--json"])
        if not isinstance(res, list):
            raise EngineError("Unexpected --paths output from engine")
        _CACHED_PATHS = [str(p) for p in res]
    return list(_CACHED_PATHS)


def validate_snapshot_schema(snapshot: dict[str, Any]) -> None:
    """Reject unknown schema_version values (Section 5 & N6/E10)."""
    sv = str(snapshot.get("schema_version") or "")
    ev = str(snapshot.get("engine_version") or "")
    major = sv.split(".", 1)[0] if sv else ""
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise UnsupportedSchemaError(sv or "missing", ev)


def run_export(
    repo_dir: Path,
    ref_name: str = "main",
    sub_repos: list[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    """Run groundwork.py export --json and validate schema_version."""
    args = ["export", "--json", "--ref", ref_name]
    for name, path in sub_repos or []:
        args.extend(["--sub-repo", f"{name}={path}"])
    args.append(str(repo_dir))
    res = _run_engine(args)
    if not isinstance(res, dict):
        raise EngineError("Export did not return a JSON object")
    validate_snapshot_schema(res)
    return res


def run_read_doc(repo_dir: Path, rel_path: str, workspace_repo: Path | None = None) -> dict[str, Any]:
    """Run groundwork.py read-doc --json."""
    args = ["read-doc", rel_path, "--repo", str(repo_dir), "--json"]
    if workspace_repo is not None:
        args.extend(["--workspace-repo", str(workspace_repo)])
    res = _run_engine(args)
    if not isinstance(res, dict):
        raise EngineError("read-doc did not return a JSON object")
    return res


def run_edit_doc(
    repo_dir: Path,
    rel_path: str,
    body_text: str,
    expect_hash: str = "",
    change_note: str = "",
    who: str = "",
) -> dict[str, Any]:
    """Run groundwork.py edit-doc --json."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tf:
        tf.write(body_text)
        body_path = Path(tf.name)
    try:
        args = ["edit-doc", rel_path, "--body-file", str(body_path), "--repo", str(repo_dir), "--json"]
        if expect_hash:
            args.extend(["--expect-hash", expect_hash])
        if change_note:
            args.extend(["--change-note", change_note])
        if who:
            args.extend(["--who", who])
        res = _run_engine(args)
        if not isinstance(res, dict):
            raise EngineError("edit-doc did not return a JSON object")
        return res
    finally:
        body_path.unlink(missing_ok=True)


def run_approve(
    repo_dir: Path,
    rel_path: str,
    who: str,
    via: str = "lens",
    expect_hash: str = "",
    change_note: str = "",
    workspace_repo: Path | None = None,
) -> dict[str, Any]:
    """Run groundwork.py approve --json."""
    args = ["approve", rel_path, "--who", who, "--via", via, "--repo", str(repo_dir), "--json"]
    if expect_hash:
        args.extend(["--expect-hash", expect_hash])
    if change_note:
        args.extend(["--change-note", change_note])
    if workspace_repo is not None:
        args.extend(["--workspace-repo", str(workspace_repo)])
    res = _run_engine(args)
    if not isinstance(res, dict):
        raise EngineError("approve did not return a JSON object")
    return res


def run_new_request(
    repo_dir: Path,
    kind: str,
    fields: dict[str, Any],
    attach_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Run groundwork.py new-request --json."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tf:
        json.dump(fields, tf)
        fields_path = Path(tf.name)
    try:
        args = ["new-request", "--kind", kind, "--fields", str(fields_path), "--repo", str(repo_dir), "--json"]
        for ap in attach_paths or []:
            args.extend(["--attach", str(ap)])
        res = _run_engine(args)
        if not isinstance(res, dict):
            raise EngineError("new-request did not return a JSON object")
        return res
    finally:
        fields_path.unlink(missing_ok=True)


def run_answer(
    repo_dir: Path,
    req_id: str,
    answers: dict[str, str],
    answered_by: str,
) -> dict[str, Any]:
    """Run groundwork.py answer --json."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tf:
        json.dump({"answers": answers, "answered_by": answered_by}, tf)
        fields_path = Path(tf.name)
    try:
        args = ["answer", req_id, "--fields", str(fields_path), "--repo", str(repo_dir), "--json"]
        res = _run_engine(args)
        if not isinstance(res, dict):
            raise EngineError("answer did not return a JSON object")
        return res
    finally:
        fields_path.unlink(missing_ok=True)
