"""Automated tests for every Section 12 edge case (E1-E22) and Section 8.8 situation (N1-N9)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.groundwork_approve import approve_document
from engine.groundwork_check import run_check
from engine.groundwork_common import body_hash, split_front_matter
from engine.groundwork_export import export_repo
from lens.app import create_app
from lens.config import LensConfig
from lens.engine_runner import UnsupportedSchemaError, validate_snapshot_schema
from lens.hosts.local_git import LocalGitAdapter
from lens.images import MAX_IMAGES_PER_REQUEST
from lens.snapshot_merge import format_change_actor, merge_project_snapshots
from lens.store import LensStore
from lens.sync import SyncWorker
from lens.writer import LensWriter, WriteError
from lens.wsgi import TestClient
from tests.helpers import commit_all, create_minimal_gw_repo, doc_hash, init_git_repo, make_png_with_text_chunk


class TestEdgeCasesE1ToE22AndN1ToN9(unittest.TestCase):
    def test_e1_fresh_clone_uses_git_dates_not_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = create_minimal_gw_repo(Path(td) / "src")
            (src / "DECISIONS").mkdir()
            (src / "DECISIONS" / "RFC-0001-a.md").write_text("---\nid: RFC-0001\ntitle: A\n---\n\n## 1. Summary\nA.\n")
            commit_all(src, "rfc1", date="2026-09-01T10:00:00Z")
            clone = Path(td) / "fresh_clone"
            subprocess.run(["git", "clone", str(src), str(clone)], check=True, capture_output=True)
            snap = export_repo(clone)
            self.assertTrue(snap["items"][0]["last_activity"].startswith("2026-09-01"))

    def test_e2_e13_open_pr_snapshots_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_root = Path(td) / "host"
            repo = create_minimal_gw_repo(host_root / "proj")
            commit_all(repo, "init")
            adapter = LocalGitAdapter(host_root)

            # PR 1: touches only source code (skipped by sync filter)
            subprocess.run(["git", "checkout", "-b", "code-only"], cwd=str(repo), check=True, capture_output=True)
            (repo / "app.py").write_text("print(1)\n")
            commit_all(repo, "code change")
            adapter.register_pr("proj", "code-only", "Code PR", number=1)

            # PR 2 & PR 3: touch GroundWork paths
            subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "gw-pr-2"], cwd=str(repo), check=True, capture_output=True)
            (repo / "DECISIONS").mkdir(exist_ok=True)
            (repo / "DECISIONS" / "RFC-0002-pr.md").write_text("---\nid: RFC-0002\ntitle: PR RFC\nstatus: in-review\n---\n\n## 1. Summary\nIn PR.\n")
            commit_all(repo, "pr2")
            adapter.register_pr("proj", "gw-pr-2", "Add RFC 2", number=2)

            subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "gw-pr-3"], cwd=str(repo), check=True, capture_output=True)
            (repo / "DECISIONS").mkdir(exist_ok=True)
            (repo / "DECISIONS" / "RFC-0003-pr.md").write_text("---\nid: RFC-0003\ntitle: PR RFC 3\n---\n\n## 1. Summary\nIn PR 3.\n")
            commit_all(repo, "pr3")
            adapter.register_pr("proj", "gw-pr-3", "Add RFC 3", number=3)
            subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)

            store = LensStore(Path(td) / "data")
            cfg = LensConfig(store)
            cfg.set("pr_cap", 1)  # E13: cap at 1 GW PR
            hid = store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid = store.upsert_project(hid, "proj", "proj")
            worker = SyncWorker(store, cfg)
            worker.sync_project(pid, force=True)

            pr_snaps = store.get_open_pr_snapshots(pid)
            self.assertEqual(len(pr_snaps), 1)
            self.assertEqual(pr_snaps[0]["pr_number"], 2)
            vm = merge_project_snapshots(store.get_project(pid), store.get_latest_snapshot(pid), pr_snaps)
            self.assertEqual(vm["all_items"]["RFC-0002"]["in_pr"]["number"], 2)
            store.close()

    def test_e3_e14_workspace_reads_subrepo_main_directly_and_isolates_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_root = Path(td) / "host"
            sub_ok = create_minimal_gw_repo(host_root / "sub-ok", name="sub-ok")
            (sub_ok / "DECISIONS").mkdir()
            (sub_ok / "DECISIONS" / "RFC-0001-live.md").write_text("---\nid: RFC-0001\ntitle: Live on main\n---\n\n## 1. Summary\nLive.\n")
            commit_all(sub_ok, "sub ok live")

            ws = create_minimal_gw_repo(host_root / "ws", name="ws")
            (ws / ".groundwork" / "config.json").write_text('{"name":"ws","kind":"workspace"}')
            (ws / ".gitmodules").write_text(
                '[submodule "sub-ok"]\n\tpath = sub-ok\n\turl = ../sub-ok\n'
                '[submodule "sub-broken"]\n\tpath = sub-broken\n\turl = ../sub-broken\n'
            )
            commit_all(ws, "ws init")

            adapter = LocalGitAdapter(host_root)
            adapter._save_state({"repos": {}, "prs": {}, "comments": [], "protected": {}, "read_only_users": {}, "unreadable": {"sub-broken": "access denied"}})

            store = LensStore(Path(td) / "data")
            cfg = LensConfig(store)
            hid = store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid = store.upsert_project(hid, "ws", "ws", is_workspace=True)
            worker = SyncWorker(store, cfg)
            snap = worker.sync_project(pid, force=True)
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertIn("sub-ok/RFC-0001", [i["id"] for i in snap["items"]])
            self.assertIn("sub-broken", snap["sub_repo_errors"])
            store.close()

    def test_e4_e5_e6_e7_e8_e9_e12_legacy_readme_timeline_aliases_agent_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = create_minimal_gw_repo(Path(td) / "r")
            (repo / "DECISIONS").mkdir()
            (repo / "DECISIONS" / "README.md").write_text("| RFC | Status |\n| --- | --- |\n| 001 | Fake Approved |\n")
            (repo / "specs").mkdir()
            (repo / "specs" / "README.md").write_text("| Spec | Status |\n| --- | --- |\n| 002 | Fake |\n")
            (repo / ".groundwork" / "active").write_text("local-only\n")
            (repo / ".groundwork" / "bypass.log").write_text("local-only\n")

            # E4 legacy spec
            s_leg = repo / "specs" / "002-old"
            s_leg.mkdir(parents=True)
            (s_leg / "spec.md").write_text("---\ntitle: Legacy feature\nrfc: (none)\nbaseline: true\n---\n\n## 1. Summary\nOld.\n")

            # E6/E7/E8/E9 active feature
            s_act = repo / "specs" / "003-active"
            s_act.mkdir(parents=True)
            (s_act / "spec.md").write_text(
                "---\ntitle: Active work\nrfc: (none)\nbaseline: true\nowner: karuturihemanth@gmail.com\n---\n\n## 1. Summary\nActive.\n"
            )
            # Clear baseline on 003 via RFC so it's non-legacy
            (repo / "DECISIONS" / "RFC-0003-act.md").write_text("---\nid: RFC-0003\ntitle: Active\nstatus: approved\n---\n\n## 1. Summary\nAct.\n")
            (s_act / "spec.md").write_text(
                "---\ntitle: Active work\nrfc: RFC-0003\nowner: karuturihemanth@gmail.com\n---\n\n## 1. Summary\nActive.\n"
            )
            (s_act / "tasks.md").write_text("- [x] **T001** — Done\n- [ ] **T901** — Manual check\n  - **Waiting on:** Hemanthkaruturi (manual check)\n")
            (s_act / "changes.md").write_text("- 2026-11-15 by Hemanth Karuturi (via claude-code): Tweaked retry\n")
            (s_act / "log.md").write_text("- 2026-11-16T10:00:00Z: Deployed to staging\n")
            commit_all(repo, "spec 003: commit work", date="2026-11-16T10:00:00Z")

            snap = export_repo(repo)
            by_id = {it["id"]: it for it in snap["items"]}
            self.assertTrue(by_id["002-old"]["legacy"])
            self.assertFalse(by_id["003-active"]["legacy"])
            # E7 alias resolved
            self.assertEqual(by_id["003-active"]["owner"], "Hemanth Karuturi")
            self.assertEqual(by_id["003-active"]["waiting_on"][0]["person"], "Hemanth Karuturi")
            # E8 agent attribution
            self.assertEqual(format_change_actor(by_id["003-active"]["changes"][0]), "Hemanth Karuturi, using Claude Code")
            # E6 merged timeline sources
            sources = {e["source"] for e in snap["events"]}
            self.assertTrue({"ledger", "change-log", "note", "git"}.issubset(sources))
            # E4: check produces zero errors for legacy spec
            self.assertEqual(run_check(repo)["check_errors"], 0)

    def test_e10_n6_unknown_schema_version(self) -> None:
        with self.assertRaises(UnsupportedSchemaError):
            validate_snapshot_schema({"schema_version": "9.0", "engine_version": "9.0.0"})

    def test_e11_e15_e16_e17_e18_e19_e20_e21_e22_writer_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_root = Path(td) / "host"
            ws = create_minimal_gw_repo(host_root / "ws", name="ws")
            (ws / ".groundwork" / "config.json").write_text('{"name":"ws","kind":"workspace"}')
            (ws / "DECISIONS").mkdir()
            ws_rfc = ws / "DECISIONS" / "RFC-0001-ws.md"
            ws_rfc.write_text("---\nid: RFC-0001\ntitle: WS RFC\nstatus: in-review\napprovers: [Amit Singh]\n---\n\n## 1. Summary\nWS.\n")
            (ws / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = ../sub\n')
            commit_all(ws, "init ws")

            sub = create_minimal_gw_repo(host_root / "sub", name="sub")
            (sub / "DECISIONS").mkdir()
            sub_rfc = sub / "DECISIONS" / "RFC-0002-sub.md"
            sub_rfc.write_text(
                "---\nid: RFC-0002\ntitle: Sub RFC\nstatus: in-review\nworkspace_rfc: RFC-0001\n"
                "owner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n## 1. Summary\nSub.\n"
            )
            commit_all(sub, "init sub")

            # E19: approving sub_rfc before ws_rfc must fail with blocking_rfc
            r_e19 = approve_document(sub, "DECISIONS/RFC-0002-sub.md", who="amit@example.com", workspace_repo=ws)
            self.assertFalse(r_e19["ok"])
            self.assertEqual(r_e19["blocking_rfc"], "RFC-0001")

            # E18: non-named approver refused
            r_e18 = approve_document(ws, "DECISIONS/RFC-0001-ws.md", who="karuturihemanth@gmail.com")
            self.assertFalse(r_e18["ok"])

            # E15: stale expect_hash refused
            r_e15 = approve_document(ws, "DECISIONS/RFC-0001-ws.md", who="amit@example.com", expect_hash="deadbeef")
            self.assertFalse(r_e15["ok"])
            self.assertIn("changed since you opened it", r_e15["error"])

            # E20 + E17: Read-only user on protected branch submits request & approves -> Lens identity + trailer + fallback PR
            adapter = LocalGitAdapter(host_root)
            adapter.set_user_read_only("ws", "amitsingh", True)
            adapter.set_branch_protected("ws", "main", True)

            store = LensStore(Path(td) / "data")
            cfg = LensConfig(store)
            hid = store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid = store.upsert_project(hid, "ws", "ws", is_workspace=True)
            worker = SyncWorker(store, cfg)
            worker.sync_project(pid, force=True)
            writer = LensWriter(store, worker)

            amit = {
                "user_login": "amitsingh",
                "user_name": "Amit Singh",
                "verified_emails": ["amit@example.com"],
                "user_token": "",
                "sign_in_mode": "A",
            }
            # E21: >5 images rejected
            with self.assertRaises(WriteError):
                writer.submit_request(
                    pid,
                    amit,
                    "feature",
                    {"title": "Too many", "need": "N", "done_looks_like": "D", "product_area": "Not sure"},
                    raw_images=[(f"{i}.png", make_png_with_text_chunk()) for i in range(MAX_IMAGES_PER_REQUEST + 1)],
                )

            # E20 + E17 request submission on protected workspace branch
            req_res = writer.submit_request(
                pid,
                amit,
                "bug",
                {
                    "title": "Not sure bug",
                    "expected": "Works",
                    "actual": "Broken",
                    "where_when": "Today",
                    "severity": "medium",
                    "product_area": "Not sure",
                },
                raw_images=[("s.png", make_png_with_text_chunk(b"GPS=1,2"))],
            )
            self.assertFalse(req_res["push"]["pushed"])
            self.assertIsNotNone(req_res["push"]["fallback_pr"])
            self.assertIn("Protected branch", store.get_project(pid)["last_refused_push_error"])

            # E20 + E17 approval on protected branch
            _, ws_body = split_front_matter(ws_rfc.read_text("utf-8"))
            ap_res = writer.approve_document(pid, amit, "DECISIONS/RFC-0001-ws.md", "main", body_hash(ws_body))
            self.assertTrue(ap_res["ok"])
            self.assertFalse(ap_res["push"]["pushed"])
            self.assertEqual(ap_res["approve"]["signers"][0]["via"], "lens")
            # Commit message on fallback branch carries Approved-by trailer
            fb_branch = ap_res["push"]["branch"]
            log_msg = subprocess.run(["git", "log", "-1", "--format=%B", fb_branch], cwd=str(ws), capture_output=True, text=True).stdout
            self.assertIn("Approved-by: Amit Singh <amit@example.com>", log_msg)

            # E22: "Not sure" bug report stays in workspace while bug record lives in sub-repo with backlink
            (ws / "requests").mkdir(exist_ok=True)
            (ws / "requests" / "REQ-0001-not-sure-bug.md").write_text(
                "---\nid: REQ-0001\nkind: bug\ntitle: Not sure bug\nstatus: converted\nconverted_to: sub/bugs/001-fix.md\n---\n\nBody.\n"
            )
            (sub / "bugs").mkdir(exist_ok=True)
            (sub / "bugs" / "001-fix.md").write_text(
                "---\ntitle: Fix\nstatus: open\nseverity: medium\nfrom_request: REQ-0001\n---\n\n## Expected\nOk\n\n## Actual\nBad\n"
            )
            self.assertEqual(run_check(sub)["check_errors"], 0)

            # E11: Concurrent requests on unprotected branch allocate consecutive numbers
            adapter.set_branch_protected("sub", "main", False)
            sub_pid = store.upsert_project(hid, "sub", "sub")
            worker.sync_project(sub_pid, force=True)
            r1 = writer.submit_request(sub_pid, amit, "feature", {"title": "First", "need": "N", "done_looks_like": "D", "product_area": "sub"})
            r2 = writer.submit_request(sub_pid, amit, "feature", {"title": "Second", "need": "N", "done_looks_like": "D", "product_area": "sub"})
            self.assertEqual(r1["request"]["id"], "REQ-0001")
            self.assertEqual(r2["request"]["id"], "REQ-0002")

            store.close()

    def test_n1_to_n9_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_root = Path(td) / "host"
            # N1 & N2: repo without groundwork on main, but PR adds .groundwork/
            n1 = init_git_repo(host_root / "n1")
            (n1 / "README.md").write_text("# Plain repo\n")
            commit_all(n1, "init plain")
            subprocess.run(["git", "checkout", "-b", "add-gw"], cwd=str(n1), check=True, capture_output=True)
            (n1 / ".groundwork").mkdir()
            (n1 / ".groundwork" / "config.json").write_text('{"name":"n1"}')
            (n1 / "PROJECT.md").write_text("# n1\n")
            commit_all(n1, "add groundwork")
            subprocess.run(["git", "checkout", "main"], cwd=str(n1), check=True, capture_output=True)

            adapter = LocalGitAdapter(host_root)
            adapter.register_pr("n1", "add-gw", "Set up GroundWork", number=12)

            # N9: empty repo & archived repo
            init_git_repo(host_root / "empty-repo")
            arch = create_minimal_gw_repo(host_root / "arch-repo", name="arch-repo")
            commit_all(arch, "init arch")
            adapter.set_repo_meta("arch-repo", archived=True)

            # N7: unreadable front matter
            n7 = create_minimal_gw_repo(host_root / "n7-repo", name="n7-repo")
            (n7 / "DECISIONS").mkdir()
            (n7 / "DECISIONS" / "RFC-0001-bad.md").write_text("---\nid: [unclosed\n---\n\nBody.\n")
            commit_all(n7, "bad fm")

            store = LensStore(Path(td) / "data")
            cfg = LensConfig(store)
            hid = store.upsert_host("local", "Local", str(host_root), str(host_root))
            pid_n1 = store.upsert_project(hid, "n1", "n1")
            pid_empty = store.upsert_project(hid, "empty-repo", "empty-repo")
            pid_arch = store.upsert_project(hid, "arch-repo", "arch-repo", archived=True)
            pid_n7 = store.upsert_project(hid, "n7-repo", "n7-repo")

            worker = SyncWorker(store, cfg)
            worker.sync_project(pid_n1, force=True)
            worker.sync_project(pid_empty, force=True)
            worker.sync_project(pid_arch, force=True)
            worker.sync_project(pid_n7, force=True)

            # N2 detected
            vm_n1 = merge_project_snapshots(
                store.get_project(pid_n1),
                store.get_latest_snapshot(pid_n1),
                store.get_open_pr_snapshots(pid_n1),
            )
            self.assertEqual(vm_n1["groundwork"]["stage"], "not-started")
            self.assertEqual(vm_n1["setup_pr"]["number"], 12)

            # N9 empty & archived
            self.assertEqual(store.get_project(pid_empty)["last_sync_status"], "empty")
            self.assertEqual(store.get_project(pid_arch)["last_sync_status"], "archived")

            # N7 unreadable reported
            vm_n7 = merge_project_snapshots(store.get_project(pid_n7), store.get_latest_snapshot(pid_n7), [])
            self.assertEqual(len(vm_n7["groundwork"]["unreadable"]), 1)

            # N8: GroundWork removed from n7-repo -> last GW snapshot retained
            subprocess.run(["git", "rm", "-rf", ".groundwork", "PROJECT.md", "ARCHITECTURE.md", "CONSTITUTION.md", "DECISIONS"], cwd=str(n7), check=True, capture_output=True)
            commit_all(n7, "remove groundwork")
            worker.sync_project(pid_n7, force=True)
            self.assertEqual(store.get_project(pid_n7)["last_sync_status"], "groundwork_removed")
            self.assertIsNotNone(store.get_last_groundwork_snapshot(pid_n7))
            store.close()
