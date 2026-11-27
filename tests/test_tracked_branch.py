"""Following a branch other than the repo's default branch."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lens.app import _valid_branch_name, create_app
from lens.store import _MIGRATIONS, LensStore, project_branch
from lens.wsgi import TestClient
from tests.helpers import commit_all, create_minimal_gw_repo, git_run


def _csrf(html: bytes) -> str:
    m = re.search(rb'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "no CSRF token on page"
    return m.group(1).decode("utf-8")


class TrackedBranchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.app = create_app(td / "data", start_worker=False)
        self.client = TestClient(self.app)
        self.client.get("/demo", follow_redirects=True)

        host_root = td / "host"
        self.repo = create_minimal_gw_repo(host_root / "branchy", name="branchy")
        self.main_sha = commit_all(self.repo, "init")
        git_run(self.repo, ["checkout", "-b", "feature/login"])
        (self.repo / "NOTES.md").write_text("# Feature work\n", encoding="utf-8")
        self.feature_sha = commit_all(self.repo, "feature work")
        git_run(self.repo, ["checkout", "main"])

        self.store = self.app.ctx.store
        self.hid = self.store.upsert_host("local", "Local", str(host_root), str(host_root))
        self.pid = self.store.upsert_project(self.hid, "branchy", "branchy")

    def tearDown(self) -> None:
        self.app.ctx.close()
        self._td.cleanup()


class TestSyncReadsTrackedBranch(TrackedBranchCase):
    def test_default_branch_when_nothing_chosen(self) -> None:
        self.app.ctx.sync_worker.sync_project(self.pid, force=True)
        snap = self.store.get_latest_snapshot(self.pid, "main")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["commit_sha"], self.main_sha)

    def test_chosen_branch_is_read_and_shown(self) -> None:
        self.app.ctx.sync_worker.sync_project(self.pid, force=True)
        self.store.set_project_tracked_branch(self.pid, "feature/login")
        self.app.ctx.sync_worker.sync_project(self.pid)  # no force: the branch change alone must re-read

        proj = self.store.get_project(self.pid)
        self.assertEqual(proj["last_sync_status"], "ok", proj["last_sync_error"])
        self.assertEqual(project_branch(proj), "feature/login")
        snap = self.store.get_latest_snapshot(self.pid, "feature/login")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["commit_sha"], self.feature_sha)

        status, _, body = self.client.get(f"/projects/{self.pid}")
        self.assertEqual(status, 200)
        self.assertIn("Following branch", body.decode("utf-8"))
        self.assertIn("feature/login", body.decode("utf-8"))

    def test_deleted_branch_is_reported(self) -> None:
        self.store.set_project_tracked_branch(self.pid, "gone")
        self.assertIsNone(self.app.ctx.sync_worker.sync_project(self.pid, force=True))
        proj = self.store.get_project(self.pid)
        self.assertEqual(proj["last_sync_status"], "error")
        self.assertIn("'gone'", proj["last_sync_error"])
        self.assertIn("no longer exists", proj["last_sync_error"])


class TestChooseBranchInPicker(TrackedBranchCase):
    def _save(self, branch: str) -> tuple[int, dict[str, str], bytes]:
        _, _, page = self.client.get("/projects/select")
        key = f"{self.hid}:branchy"
        self.assertIn(f'name="branch:{key}"'.encode(), page)
        self.assertIn(b"data-repo-search", page)
        self.assertIn(b'data-repo-name="branchy"', page)
        return self.client.post(
            "/projects/select",
            form={"_csrf": _csrf(page), "repo_keys": key, f"branch:{key}": branch},
        )

    def test_picker_saves_branch(self) -> None:
        self._save("feature/login")
        proj = self.store.get_project(self.pid)
        self.assertEqual(proj["tracked_branch"], "feature/login")
        self.assertEqual(self.store.get_latest_snapshot(self.pid, "feature/login")["commit_sha"], self.feature_sha)

        _, _, page = self.client.get("/projects/select")
        self.assertIn(b'value="feature/login"', page)

    def test_naming_the_default_branch_keeps_following_default(self) -> None:
        self.store.set_project_tracked_branch(self.pid, "feature/login")
        self._save("main")
        self.assertEqual(self.store.get_project(self.pid)["tracked_branch"], "")

    def test_invalid_branch_name_is_refused(self) -> None:
        status, headers, _ = self._save("--upload-pack=evil")
        self.assertIn(status, (302, 303))
        self.assertIn("not%20valid", headers.get("Location", "").replace("+", "%20"))
        self.assertEqual(self.store.get_project(self.pid)["tracked_branch"], "")

    def test_branch_list_api(self) -> None:
        status, _, body = self.client.get(f"/api/branches?host_id={self.hid}&repo=branchy")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["branches"], ["feature/login", "main"])


class TestBranchNameValidation(unittest.TestCase):
    def test_names(self) -> None:
        for ok in ("main", "feature/login", "release-1.2", "user/jo_x"):
            self.assertTrue(_valid_branch_name(ok), ok)
        for bad in ("-x", "--upload-pack=x", "a..b", "a b", "/a", "a/", "a.lock", "a//b", ".hidden", "a;b", ""):
            self.assertFalse(_valid_branch_name(bad), bad)


class TestMigration(unittest.TestCase):
    def test_v1_database_gains_tracked_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            conn = sqlite3.connect(str(data / "lens.db"))
            conn.executescript(_MIGRATIONS[0][1])
            conn.execute("INSERT INTO schema_meta(version) VALUES (1)")
            conn.execute("INSERT INTO hosts(kind, name, base_url, api_url, created_at) VALUES ('local','L','x','x','t')")
            conn.execute("INSERT INTO projects(host_id, repo_full_name, display_name) VALUES (1, 'a/b', 'b')")
            conn.commit()
            conn.close()

            store = LensStore(data)
            proj = store.get_project(1)
            self.assertEqual(proj["tracked_branch"], "")
            self.assertEqual(project_branch(proj), "main")
            self.assertTrue(any((data / "backups").glob("lens-v1-*.db")))
            store.close()


if __name__ == "__main__":
    unittest.main()
