"""SQLite snapshot store, sessions, hosts, and automatic migrations."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lens.crypto import KeyManager

SCHEMA_VERSION = 2

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_url TEXT NOT NULL,
            sign_in_mode TEXT NOT NULL DEFAULT 'A',
            credentials_enc TEXT NOT NULL DEFAULT '',
            service_token_enc TEXT NOT NULL DEFAULT '',
            service_token_expires_at TEXT NOT NULL DEFAULT '',
            webhook_secret_enc TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            repo_full_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            default_branch TEXT NOT NULL DEFAULT 'main',
            is_workspace INTEGER NOT NULL DEFAULT 0,
            is_demo INTEGER NOT NULL DEFAULT 0,
            is_public INTEGER NOT NULL DEFAULT 0,
            idle_days INTEGER NOT NULL DEFAULT 14,
            last_sync_at TEXT NOT NULL DEFAULT '',
            last_sync_status TEXT NOT NULL DEFAULT 'pending',
            last_sync_error TEXT NOT NULL DEFAULT '',
            rate_limited_until TEXT NOT NULL DEFAULT '',
            last_refused_push_error TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            removed_groundwork_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE,
            UNIQUE (host_id, repo_full_name)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            ref_name TEXT NOT NULL,
            pr_number INTEGER NOT NULL DEFAULT 0,
            pr_title TEXT NOT NULL DEFAULT '',
            pr_url TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            has_groundwork INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_proj_ref ON snapshots(project_id, ref_name, created_at DESC);
        CREATE TABLE IF NOT EXISTS user_projects (
            user_key TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            PRIMARY KEY (user_key, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            host_id INTEGER NOT NULL DEFAULT 0,
            user_login TEXT NOT NULL,
            user_name TEXT NOT NULL,
            verified_emails_json TEXT NOT NULL DEFAULT '[]',
            user_token_enc TEXT NOT NULL DEFAULT '',
            sign_in_mode TEXT NOT NULL DEFAULT 'A',
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_demo INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_kind TEXT NOT NULL,
            event_type TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS send_backs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            doc_path TEXT NOT NULL,
            by_name TEXT NOT NULL,
            note TEXT NOT NULL,
            pr_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE projects ADD COLUMN tracked_branch TEXT NOT NULL DEFAULT '';
        """,
    ),
]


def project_branch(project: dict[str, Any]) -> str:
    """The branch Lens reads for a project: the one a person picked, else the host's default."""
    return str(project.get("tracked_branch") or project.get("default_branch") or "main")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LensStore:
    """Thread-safe SQLite snapshot and session store."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "lens.db"
        self.keys = KeyManager(self.data_dir)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            current_ver = 0
            if cur is not None:
                row = self._conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
                if row is not None:
                    current_ver = int(row["version"])

            pending = [(v, sql) for v, sql in _MIGRATIONS if v > current_ver]
            if not pending:
                return

            if current_ver > 0 and self.db_path.is_file():
                backups_dir = self.data_dir / "backups"
                backups_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                shutil.copy2(self.db_path, backups_dir / f"lens-v{current_ver}-{ts}.db")

            for ver, sql in pending:
                self._conn.executescript(sql)
                self._conn.execute("DELETE FROM schema_meta")
                self._conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (ver,))
            self._conn.commit()

    # Settings
    def get_setting(self, key: str) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, is_secret FROM settings WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["is_secret"]:
                return self.keys.decrypt_json(row["value_json"])
            return json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value_json, is_secret) VALUES (?, ?, 0) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, is_secret=0",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def get_secret_setting(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, is_secret FROM settings WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return ""
            if row["is_secret"]:
                return self.keys.decrypt_str(row["value_json"])
            return str(json.loads(row["value_json"]))

    def set_secret_setting(self, key: str, value: str) -> None:
        enc = self.keys.encrypt_str(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value_json, is_secret) VALUES (?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, is_secret=1",
                (key, enc),
            )
            self._conn.commit()

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()

    # Hosts
    def upsert_host(
        self,
        kind: str,
        name: str,
        base_url: str,
        api_url: str,
        sign_in_mode: str = "A",
        credentials: dict[str, Any] | None = None,
        service_token: str = "",
        service_token_expires_at: str = "",
        webhook_secret: str = "",
        host_id: int | None = None,
    ) -> int:
        cred_enc = self.keys.encrypt_json(credentials or {}) if credentials else ""
        tok_enc = self.keys.encrypt_str(service_token) if service_token else ""
        wh_enc = self.keys.encrypt_str(webhook_secret) if webhook_secret else ""
        with self._lock:
            if host_id is not None:
                self._conn.execute(
                    """
                    UPDATE hosts SET kind=?, name=?, base_url=?, api_url=?, sign_in_mode=?,
                        credentials_enc=?, service_token_enc=?, service_token_expires_at=?, webhook_secret_enc=?
                    WHERE id=?
                    """,
                    (kind, name, base_url, api_url, sign_in_mode, cred_enc, tok_enc, service_token_expires_at, wh_enc, host_id),
                )
                self._conn.commit()
                return host_id
            cur = self._conn.execute(
                """
                INSERT INTO hosts(kind, name, base_url, api_url, sign_in_mode, credentials_enc,
                    service_token_enc, service_token_expires_at, webhook_secret_enc, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, name, base_url, api_url, sign_in_mode, cred_enc, tok_enc, service_token_expires_at, wh_enc, _utc_now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _hydrate_host(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "name": row["name"],
            "base_url": row["base_url"],
            "api_url": row["api_url"],
            "sign_in_mode": row["sign_in_mode"],
            "credentials": self.keys.decrypt_json(row["credentials_enc"], {}) if row["credentials_enc"] else {},
            "service_token": self.keys.decrypt_str(row["service_token_enc"]) if row["service_token_enc"] else "",
            "service_token_expires_at": row["service_token_expires_at"],
            "webhook_secret": self.keys.decrypt_str(row["webhook_secret_enc"]) if row["webhook_secret_enc"] else "",
            "created_at": row["created_at"],
        }

    def list_hosts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM hosts ORDER BY id ASC").fetchall()
            return [h for r in rows if (h := self._hydrate_host(r)) is not None]

    def get_host(self, host_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
            return self._hydrate_host(row)

    def get_host_by_kind(self, kind: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM hosts WHERE kind = ? ORDER BY id ASC LIMIT 1", (kind,)).fetchone()
            return self._hydrate_host(row)

    # Projects
    def upsert_project(
        self,
        host_id: int,
        repo_full_name: str,
        display_name: str,
        default_branch: str = "main",
        is_workspace: bool = False,
        is_demo: bool = False,
        is_public: bool = False,
        archived: bool = False,
    ) -> int:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM projects WHERE host_id = ? AND repo_full_name = ?",
                (host_id, repo_full_name),
            ).fetchone()
            if existing:
                pid = int(existing["id"])
                self._conn.execute(
                    """
                    UPDATE projects SET display_name=?, default_branch=?, is_workspace=?,
                        is_demo=?, is_public=?, archived=?
                    WHERE id=?
                    """,
                    (display_name, default_branch, int(is_workspace), int(is_demo), int(is_public), int(archived), pid),
                )
                self._conn.commit()
                return pid
            cur = self._conn.execute(
                """
                INSERT INTO projects(host_id, repo_full_name, display_name, default_branch,
                    is_workspace, is_demo, is_public, archived)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (host_id, repo_full_name, display_name, default_branch, int(is_workspace), int(is_demo), int(is_public), int(archived)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None

    def list_projects(self, include_demo: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            if include_demo:
                rows = self._conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM projects WHERE is_demo = 0 ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def update_project_sync(
        self,
        project_id: int,
        status: str,
        error: str = "",
        rate_limited_until: str = "",
        removed_groundwork_at: str | None = None,
    ) -> None:
        with self._lock:
            if removed_groundwork_at is not None:
                self._conn.execute(
                    """
                    UPDATE projects SET last_sync_at=?, last_sync_status=?, last_sync_error=?,
                        rate_limited_until=?, removed_groundwork_at=?
                    WHERE id=?
                    """,
                    (_utc_now(), status, error, rate_limited_until, removed_groundwork_at, project_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE projects SET last_sync_at=?, last_sync_status=?, last_sync_error=?,
                        rate_limited_until=?
                    WHERE id=?
                    """,
                    (_utc_now(), status, error, rate_limited_until, project_id),
                )
            self._conn.commit()

    def set_project_idle_days(self, project_id: int, idle_days: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE projects SET idle_days = ? WHERE id = ?", (max(1, idle_days), project_id))
            self._conn.commit()

    def set_project_tracked_branch(self, project_id: int, branch: str) -> None:
        """Pin the branch Lens reads. An empty string follows the host's default branch."""
        with self._lock:
            self._conn.execute("UPDATE projects SET tracked_branch = ? WHERE id = ?", (branch.strip(), project_id))
            self._conn.commit()

    def set_project_default_branch(self, project_id: int, branch: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE projects SET default_branch = ? WHERE id = ?", (branch, project_id))
            self._conn.commit()

    def set_project_refused_push(self, project_id: int, msg: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE projects SET last_refused_push_error = ? WHERE id = ?", (msg, project_id))
            self._conn.commit()

    # User followed projects
    def follow_project(self, user_key: str, project_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_projects(user_key, project_id) VALUES (?, ?)",
                (user_key, project_id),
            )
            self._conn.commit()

    def unfollow_project(self, user_key: str, project_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM user_projects WHERE user_key = ? AND project_id = ?",
                (user_key, project_id),
            )
            self._conn.commit()

    def list_followed_projects(self, user_key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.* FROM projects p
                JOIN user_projects up ON up.project_id = p.id
                WHERE up.user_key = ?
                ORDER BY p.id ASC
                """,
                (user_key,),
            ).fetchall()
            return [dict(r) for r in rows]

    # Snapshots
    def store_snapshot(
        self,
        project_id: int,
        ref_name: str,
        commit_sha: str,
        snapshot: dict[str, Any],
        pr_number: int = 0,
        pr_title: str = "",
        pr_url: str = "",
    ) -> int:
        gw_stage = str((snapshot.get("groundwork") or {}).get("stage") or "not-started")
        has_gw = int(gw_stage != "not-started" or bool(snapshot.get("items")))
        payload = json.dumps(snapshot, separators=(",", ":"))
        with self._lock:
            # Remove older snapshots for same ref & commit to avoid duplicates
            self._conn.execute(
                "DELETE FROM snapshots WHERE project_id = ? AND ref_name = ? AND commit_sha = ? AND pr_number = ?",
                (project_id, ref_name, commit_sha, pr_number),
            )
            cur = self._conn.execute(
                """
                INSERT INTO snapshots(project_id, ref_name, pr_number, pr_title, pr_url, commit_sha, snapshot_json, has_groundwork, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, ref_name, pr_number, pr_title, pr_url, commit_sha, payload, has_gw, _utc_now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def clear_stale_pr_snapshots(self, project_id: int, active_pr_numbers: list[int]) -> None:
        with self._lock:
            if not active_pr_numbers:
                self._conn.execute("DELETE FROM snapshots WHERE project_id = ? AND pr_number > 0", (project_id,))
            else:
                placeholders = ",".join("?" for _ in active_pr_numbers)
                self._conn.execute(
                    f"DELETE FROM snapshots WHERE project_id = ? AND pr_number > 0 AND pr_number NOT IN ({placeholders})",
                    (project_id, *active_pr_numbers),
                )
            self._conn.commit()

    def get_latest_snapshot(self, project_id: int, ref_name: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if ref_name:
                row = self._conn.execute(
                    "SELECT * FROM snapshots WHERE project_id = ? AND ref_name = ? AND pr_number = 0 ORDER BY id DESC LIMIT 1",
                    (project_id, ref_name),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM snapshots WHERE project_id = ? AND pr_number = 0 ORDER BY id DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["data"] = json.loads(row["snapshot_json"])
            return out

    def get_last_groundwork_snapshot(self, project_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE project_id = ? AND pr_number = 0 AND has_groundwork = 1 ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["data"] = json.loads(row["snapshot_json"])
            return out

    def get_open_pr_snapshots(self, project_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.* FROM snapshots s
                JOIN (
                    SELECT ref_name, pr_number, MAX(id) AS max_id
                    FROM snapshots
                    WHERE project_id = ? AND pr_number > 0
                    GROUP BY ref_name, pr_number
                ) latest ON s.id = latest.max_id
                ORDER BY s.pr_number ASC
                """,
                (project_id,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["data"] = json.loads(r["snapshot_json"])
                out.append(d)
            return out

    # Sessions
    def create_session(
        self,
        session_id: str,
        host_id: int,
        user_login: str,
        user_name: str,
        verified_emails: list[str],
        user_token: str = "",
        sign_in_mode: str = "A",
        is_admin: bool = False,
        is_demo: bool = False,
        ttl_seconds: int = 86400 * 7,
    ) -> None:
        tok_enc = self.keys.encrypt_str(user_token) if user_token else ""
        exp = int(time.time()) + ttl_seconds
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions(id, host_id, user_login, user_name, verified_emails_json,
                    user_token_enc, sign_in_mode, is_admin, is_demo, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    host_id,
                    user_login,
                    user_name,
                    json.dumps(verified_emails),
                    tok_enc,
                    sign_in_mode,
                    int(is_admin),
                    int(is_demo),
                    exp,
                    _utc_now(),
                ),
            )
            self._conn.commit()

    def get_follower_token_for_project(self, host_id: int, project_id: int) -> str:
        """OAuth token of the most recent live session of someone who follows the project.

        Used only for background sync on hosts that have no service token
        (GitLab Mode A). Restricted to followers, so Lens only reads a project
        with the token of a person who chose to see it.
        """
        now = int(time.time())
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.user_token_enc FROM sessions s
                JOIN user_projects up
                  ON up.user_key = (CAST(s.host_id AS TEXT) || ':' || s.user_login)
                 AND up.project_id = ?
                WHERE s.host_id = ? AND s.is_demo = 0 AND s.expires_at >= ? AND s.user_token_enc != ''
                ORDER BY s.created_at DESC
                LIMIT 5
                """,
                (project_id, host_id, now),
            ).fetchall()
        for row in rows:
            try:
                tok = self.keys.decrypt_str(row["user_token_enc"])
            except Exception:
                continue
            if tok:
                return tok
        return ""

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) < int(time.time()):
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self._conn.commit()
                return None
            return {
                "id": row["id"],
                "host_id": int(row["host_id"]),
                "user_login": row["user_login"],
                "user_name": row["user_name"],
                "verified_emails": json.loads(row["verified_emails_json"]),
                "user_token": self.keys.decrypt_str(row["user_token_enc"]) if row["user_token_enc"] else "",
                "sign_in_mode": row["sign_in_mode"],
                "is_admin": bool(row["is_admin"]),
                "is_demo": bool(row["is_demo"]),
                "expires_at": int(row["expires_at"]),
            }

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()

    # Webhooks & Send-backs
    def record_webhook_delivery(self, host_kind: str, event_type: str, repo_full_name: str, status: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO webhook_deliveries(host_kind, event_type, repo_full_name, status, detail, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (host_kind, event_type, repo_full_name, status, detail, _utc_now()),
            )
            self._conn.commit()

    def list_webhook_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def record_send_back(self, project_id: int, doc_path: str, by_name: str, note: str, pr_url: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO send_backs(project_id, doc_path, by_name, note, pr_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, doc_path, by_name, note, pr_url, _utc_now()),
            )
            self._conn.commit()

    def list_send_backs(self, project_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM send_backs WHERE project_id = ? AND resolved = 0 ORDER BY id DESC",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def disk_usage_bytes(self) -> int:
        total = 0
        for p in self.data_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
        return total
