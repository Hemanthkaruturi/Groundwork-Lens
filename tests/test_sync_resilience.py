"""Regression tests for issue #2: repos wrongly shown as "GroundWork is not set up".

Covers the slow history scan in blobless clones, export failures being
recorded instead of read as "not set up", GitLab Mode A background sync
without a service token, and network settings reaching the engine's git.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import groundwork_git
from engine.groundwork_git import scan_git_history
from lens.app import create_app
from lens.config import LensConfig
from lens.engine_runner import EngineError, _scrubbed_env
from lens.store import LensStore
from lens.sync import SyncWorker
from lens.wsgi import TestClient
from tests.helpers import commit_all, create_minimal_gw_repo, init_git_repo


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _pack_count(repo: Path) -> int:
    return len(list((repo / ".git" / "objects" / "pack").glob("*.pack")))


class TestHistoryScan(unittest.TestCase):
    def test_scan_does_not_fetch_blobs_in_blobless_clone(self) -> None:
        # Each commit deletes one file and adds another: rename detection would
        # have to download both blobs to compare them.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            init_git_repo(src)
            _git(src, "config", "uploadpack.allowFilter", "true")
            _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
            for i in range(1, 16):
                (src / f"f{i}.txt").write_text("\n".join(str(n) for n in range(i * 20)) + "\n")
                if i > 1:
                    (src / f"f{i - 1}.txt").unlink()
                commit_all(src, f"c{i}")

            clone = Path(td) / "clone"
            _git(Path(td), "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{src}", str(clone))
            _git(clone, "config", "gc.auto", "0")
            before = _pack_count(clone)

            file_dates, commits = scan_git_history(clone)

            self.assertEqual(_pack_count(clone), before, "history scan downloaded blobs on demand")
            self.assertGreaterEqual(len(commits), 15)
            self.assertIn("f15.txt", file_dates)

    def test_git_timeout_degrades_to_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            init_git_repo(repo)
            (repo / "a.txt").write_text("a\n")
            commit_all(repo, "a")
            with patch.object(
                groundwork_git.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="git log", timeout=120),
            ):
                self.assertEqual(scan_git_history(repo), ({}, []))


class TestSyncFailuresAreNotNotSetUp(unittest.TestCase):
    def test_export_failure_is_recorded_and_page_does_not_say_not_set_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = create_app(Path(td) / "data", start_worker=False)
            client = TestClient(app)
            client.get("/demo", follow_redirects=True)

            host_root = Path(td) / "host"
            repo = create_minimal_gw_repo(host_root / "big-repo", name="big-repo")
            commit_all(repo, "init")
            hid = app.ctx.store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid = app.ctx.store.upsert_project(hid, "big-repo", "big-repo")

            with patch.object(
                SyncWorker, "_export_ref_snapshot", side_effect=EngineError("git log timed out after 120s")
            ):
                self.assertIsNone(app.ctx.sync_worker.sync_project(pid, force=True))

            proj = app.ctx.store.get_project(pid)
            self.assertEqual(proj["last_sync_status"], "error")
            self.assertIn("timed out", proj["last_sync_error"])

            status, headers, body = client.get(f"/projects/{pid}")
            self.assertEqual(status, 200, headers)
            html = body.decode("utf-8")
            self.assertIn("could not read the GroundWork data", html)
            self.assertNotIn("not set up", html.lower())
            app.ctx.close()

    def test_unsynced_project_is_not_reported_as_not_set_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = create_app(Path(td) / "data", start_worker=False)
            client = TestClient(app)
            client.get("/demo", follow_redirects=True)
            host_root = Path(td) / "host"
            commit_all(create_minimal_gw_repo(host_root / "later-repo", name="later-repo"), "init")
            hid = app.ctx.store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid = app.ctx.store.upsert_project(hid, "later-repo", "later-repo")  # followed, not yet synced

            status, _, body = client.get(f"/projects/{pid}")
            self.assertEqual(status, 200)
            self.assertIn("has not finished reading this repo", body.decode("utf-8"))
            app.ctx.close()


class TestGitLabModeABackgroundToken(unittest.TestCase):
    def test_follower_token_is_used_only_for_followers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = LensStore(Path(td) / "data")
            hid = store.upsert_host("gitlab", "GitLab", "https://gitlab.example.com", "https://gitlab.example.com/api/v4")
            pid = store.upsert_project(hid, "group/app", "app")
            other_pid = store.upsert_project(hid, "group/other", "other")

            store.create_session("s1", hid, "alice", "Alice", [], user_token="glpat-alice")
            store.create_session("s2", hid, "bob", "Bob", [], user_token="glpat-bob")
            store.follow_project(f"{hid}:alice", pid)

            self.assertEqual(store.get_follower_token_for_project(hid, pid), "glpat-alice")
            self.assertEqual(store.get_follower_token_for_project(hid, other_pid), "")
            store.close()

    def test_background_sync_passes_follower_token_to_gitlab(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = LensStore(Path(td) / "data")
            hid = store.upsert_host(
                "gitlab", "GitLab", "https://gitlab.example.com", "https://gitlab.example.com/api/v4", sign_in_mode="A"
            )
            pid = store.upsert_project(hid, "group/app", "app")
            store.create_session("s1", hid, "alice", "Alice", [], user_token="glpat-alice")
            store.follow_project(f"{hid}:alice", pid)
            worker = SyncWorker(store, LensConfig(store))

            seen: dict[str, str] = {}

            def fake_repo_info(self, repo_full_name: str, user_token: str = ""):  # noqa: ANN001
                seen["token"] = user_token
                from lens.hosts.base import HostError

                raise HostError("stop after recording the token")

            with patch("lens.hosts.gitlab.GitLabAdapter.get_repo_info", fake_repo_info):
                worker.sync_project(pid)
            self.assertEqual(seen.get("token"), "glpat-alice")

            # With nobody signed in, the failure explains how to fix it.
            store._conn.execute("DELETE FROM sessions")
            store._conn.commit()
            with patch("lens.hosts.gitlab.GitLabAdapter.get_repo_info", fake_repo_info):
                worker.sync_project(pid)
            proj = store.get_project(pid)
            self.assertEqual(proj["last_sync_status"], "unreachable")
            self.assertIn("service token", proj["last_sync_error"])
            store.close()


class TestEngineEnvironment(unittest.TestCase):
    def test_network_settings_reach_engine_git(self) -> None:
        with patch.dict(
            "os.environ",
            {"GIT_SSL_CAINFO": "/etc/ssl/corp.pem", "HTTPS_PROXY": "http://proxy:3128", "SECRET_TOKEN": "x"},
        ):
            env = _scrubbed_env("/tmp/home")
        self.assertEqual(env["GIT_SSL_CAINFO"], "/etc/ssl/corp.pem")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy:3128")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("SECRET_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
