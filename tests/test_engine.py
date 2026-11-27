"""Milestone M0 & M1 tests for the bundled GroundWork engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import ENGINE_VERSION, SCHEMA_VERSION
from engine.groundwork_approve import approve_document, evaluate_document
from engine.groundwork_board import build_board
from engine.groundwork_check import run_check
from engine.groundwork_common import body_hash, split_front_matter
from engine.groundwork_doctor import run_doctor
from engine.groundwork_export import EXPORT_PATHS, export_repo
from engine.groundwork_requests import answer_request, new_request
from tests.helpers import commit_all, create_minimal_gw_repo, doc_hash, init_git_repo, make_png_with_text_chunk

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestEngineContractAndFeatures(unittest.TestCase):
    def test_m0_fixture_matches_schema_contract(self) -> None:
        fixture = json.loads((REPO_ROOT / "tests" / "fixtures" / "signalfarming_export_v1.json").read_text("utf-8"))
        self.assertEqual(fixture["schema_version"], SCHEMA_VERSION)
        self.assertEqual(fixture["engine_version"], ENGINE_VERSION)
        self.assertEqual(fixture["kind"], "workspace")
        ids = {it["id"]: it for it in fixture["items"]}
        self.assertIn("signalfarming-costs/003-excel-export", ids)
        self.assertIn("researchTrail/038-named-decision-maker-contacts", ids)
        self.assertIn("researchTrail/bugs/011-discovery-price-stale", ids)
        self.assertEqual(ids["signalfarming-costs/003-excel-export"]["waiting_on"][0]["person"], "Hemanth Karuturi")
        self.assertFalse(ids["signalfarming-costs/003-excel-export"]["waiting_on"][0]["inferred"])
        self.assertTrue(ids["researchTrail/038-named-decision-maker-contacts"]["waiting_on"][0]["inferred"])

    def test_export_paths_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "engine" / "groundwork.py"), "export", "--paths", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        paths = json.loads(proc.stdout)
        self.assertEqual(paths, EXPORT_PATHS)
        self.assertIn("DECISIONS/", paths)
        self.assertIn("requests/", paths)

    def test_git_dates_used_instead_of_mtime_and_board_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = create_minimal_gw_repo(Path(td) / "r")
            (repo / "DECISIONS").mkdir()
            (repo / "DECISIONS" / "RFC-0001-old.md").write_text("---\nid: RFC-0001\ntitle: Old\n---\n\n## 1. Summary\nOld.\n")
            (repo / "DECISIONS" / "RFC-0002-new.md").write_text("---\nid: RFC-0002\ntitle: New\n---\n\n## 1. Summary\nNew.\n")
            commit_all(repo, "old rfc", date="2026-10-01T10:00:00Z")
            (repo / "DECISIONS" / "RFC-0002-new.md").write_text("---\nid: RFC-0002\ntitle: New\n---\n\n## 1. Summary\nUpdated.\n")
            commit_all(repo, "update RFC-0002", date="2026-11-20T10:00:00Z")

            # Touch old file mtime to the future — export & board must still report git date!
            os.utime(repo / "DECISIONS" / "RFC-0001-old.md", (4000000000, 4000000000))
            snap = export_repo(repo)
            by_id = {it["id"]: it for it in snap["items"]}
            self.assertTrue(by_id["RFC-0001"]["last_activity"].startswith("2026-10-01"))
            self.assertTrue(by_id["RFC-0002"]["last_activity"].startswith("2026-11-20"))
            board = build_board(repo)
            self.assertEqual(board["items"][0]["id"], "RFC-0002")

    def test_doctor_all_six_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = init_git_repo(Path(td) / "doc")
            self.assertEqual(run_doctor(r)["stage"], "not-started")
            (r / ".groundwork").mkdir()
            (r / ".groundwork" / "config.json").write_text('{"name":"doc"}')
            (r / "PROJECT.md").write_text("# doc\n")
            self.assertEqual(run_doctor(r)["stage"], "scaffolded")
            (r / "ARCHITECTURE.md").write_text("# arch\n")
            (r / "CONSTITUTION.md").write_text("# const\n")
            self.assertEqual(run_doctor(r)["stage"], "documented")
            (r / "DECISIONS").mkdir()
            (r / "DECISIONS" / "RFC-0001-x.md").write_text("---\nid: RFC-0001\ntitle: X\n---\n\nBody.\n")
            self.assertEqual(run_doctor(r)["stage"], "current")
            (r / ".groundwork" / "approvals.json").write_text('{"DECISIONS/RFC-0001-x.md":{"hash":"x","signers":[]}}')
            self.assertEqual(run_doctor(r)["stage"], "guarded")
            (r / ".groundwork" / "ledger.jsonl").write_text('{"at":"2026-11-01T00:00:00Z","type":"created","item":"doc"}\n')
            self.assertEqual(run_doctor(r)["stage"], "practicing")

    def test_check_rules_each_have_failing_negative_case(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Rule 1: waiting-on-unknown-person
            r1 = create_minimal_gw_repo(Path(td) / "r1")
            (r1 / "specs" / "001-a").mkdir(parents=True)
            (r1 / "specs" / "001-a" / "spec.md").write_text("---\ntitle: A\nrfc: (none)\nbaseline: true\n---\n\nBody.\n")
            (r1 / "specs" / "001-a" / "tasks.md").write_text("- [ ] **T010** — Sign off\n  - **Waiting on:** Mystery Person\n")
            rules1 = [w["rule"] for w in run_check(r1)["warnings"]]
            self.assertIn("waiting-on-unknown-person", rules1)

            # Rule 2: manual-task-missing-waiting-on
            r2 = create_minimal_gw_repo(Path(td) / "r2")
            (r2 / "specs" / "001-a").mkdir(parents=True)
            (r2 / "specs" / "001-a" / "spec.md").write_text("---\ntitle: A\nrfc: (none)\nbaseline: true\n---\n\nBody.\n")
            (r2 / "specs" / "001-a" / "tasks.md").write_text("- [ ] **T901** — Manual browser test by hand\n")
            rules2 = [w["rule"] for w in run_check(r2)["warnings"]]
            self.assertIn("manual-task-missing-waiting-on", rules2)

            # Rule 3: approver-unknown-person
            r3 = create_minimal_gw_repo(Path(td) / "r3")
            (r3 / "DECISIONS").mkdir()
            (r3 / "DECISIONS" / "RFC-0001-x.md").write_text("---\nid: RFC-0001\ntitle: X\napprovers: [Ghost User]\n---\n\nBody.\n")
            rules3 = [w["rule"] for w in run_check(r3)["warnings"]]
            self.assertIn("approver-unknown-person", rules3)

            # Rule 4: request-converted-target-missing
            r4 = create_minimal_gw_repo(Path(td) / "r4")
            (r4 / "requests").mkdir()
            (r4 / "requests" / "REQ-0001-x.md").write_text(
                "---\nid: REQ-0001\nkind: feature\ntitle: X\nstatus: converted\nconverted_to: RFC-9999\n---\n\nBody.\n"
            )
            errs4 = [e["rule"] for e in run_check(r4)["errors"]]
            self.assertIn("request-converted-target-missing", errs4)

            # Rule 5: request-backlink-missing
            r5 = create_minimal_gw_repo(Path(td) / "r5")
            (r5 / "requests").mkdir()
            (r5 / "DECISIONS").mkdir()
            (r5 / "requests" / "REQ-0001-x.md").write_text(
                "---\nid: REQ-0001\nkind: feature\ntitle: X\nstatus: converted\nconverted_to: RFC-0001\n---\n\nBody.\n"
            )
            (r5 / "DECISIONS" / "RFC-0001-x.md").write_text("---\nid: RFC-0001\ntitle: X\n---\n\nBody.\n")
            errs5 = [e["rule"] for e in run_check(r5)["errors"]]
            self.assertIn("request-backlink-missing", errs5)

            # Rule 6: spec-missing-rfc (non-legacy)
            r6 = create_minimal_gw_repo(Path(td) / "r6")
            (r6 / "specs" / "001-a").mkdir(parents=True)
            (r6 / "specs" / "001-a" / "spec.md").write_text("---\ntitle: A\nrfc: RFC-0404\n---\n\nBody.\n")
            errs6 = [e["rule"] for e in run_check(r6)["errors"]]
            self.assertIn("spec-missing-rfc", errs6)

    def test_requests_answers_and_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = create_minimal_gw_repo(Path(td) / "req")
            img = Path(td) / "shot.png"
            img.write_bytes(make_png_with_text_chunk())
            res = new_request(
                repo,
                "feature",
                {
                    "title": "Weekly digest",
                    "requested_by": "Amit Singh",
                    "need": "Leaders want a weekly summary.",
                    "done_looks_like": "Delivered on Mondays.",
                    "product_area": "req",
                },
                attachments=[img],
            )
            self.assertEqual(res["id"], "REQ-0001")
            req_file = repo / res["path"]
            self.assertTrue(req_file.is_file())
            self.assertTrue((repo / "requests" / "REQ-0001-weekly-digest" / "1.png").is_file())

            # Append questions as agent would
            raw = req_file.read_text("utf-8") + "\n## Questions for Amit Singh\n\n1. **Q1** — Which day?\n   - Monday\n   - Friday\n"
            req_file.write_text(raw, encoding="utf-8")
            ans = answer_request(repo, "REQ-0001", {"Q1": "Monday"}, answered_by="Amit Singh")
            self.assertEqual(ans["status"], "interviewing")
            self.assertIn("**Answer:** Monday", req_file.read_text("utf-8"))

    def test_approve_via_lens_vs_terminal_parity_and_stale_change_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = create_minimal_gw_repo(Path(td) / "ap")
            (repo / "DECISIONS").mkdir()
            rfc = repo / "DECISIONS" / "RFC-0001-lens.md"
            rfc.write_text(
                "---\nid: RFC-0001\ntitle: Lens\nstatus: in-review\nowner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n## 1. Summary\nSummary.\n",
                encoding="utf-8",
            )
            commit_all(repo, "add rfc")

            h = doc_hash(rfc)
            # Terminal approval (no via)
            res_term = approve_document(repo, "DECISIONS/RFC-0001-lens.md", who="amit@example.com", expect_hash=h)
            self.assertTrue(res_term["ok"])
            self.assertNotIn("via", res_term["signers"][0])

            # Reset and approve via Lens
            (repo / ".groundwork" / "approvals.json").write_text("{}", encoding="utf-8")
            rfc.write_text(
                "---\nid: RFC-0001\ntitle: Lens\nstatus: in-review\nowner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n## 1. Summary\nSummary.\n",
                encoding="utf-8",
            )
            res_lens = approve_document(repo, "DECISIONS/RFC-0001-lens.md", who="amit@example.com", via="lens", expect_hash=h)
            self.assertTrue(res_lens["ok"])
            self.assertEqual(res_lens["signers"][0]["via"], "lens")
            self.assertEqual(res_lens["body_hash"], res_term["body_hash"])

            # Modify spec after approval -> requires change note & produces section diff
            (repo / "specs" / "001-lens").mkdir(parents=True)
            spec = repo / "specs" / "001-lens" / "spec.md"
            spec.write_text("---\ntitle: Lens Spec\nrfc: RFC-0001\nstatus: in-review\n---\n\n## 1. Summary\nInitial.\n", encoding="utf-8")
            commit_all(repo, "add spec")
            approve_document(repo, "specs/001-lens/spec.md", who="karuturihemanth@gmail.com")
            commit_all(repo, "approve spec")

            spec.write_text("---\ntitle: Lens Spec\nrfc: RFC-0001\nstatus: approved\n---\n\n## 1. Summary\nInitial.\nExtra line.\n", encoding="utf-8")
            commit_all(repo, "edit spec")
            ev = evaluate_document(repo, "specs/001-lens/spec.md")
            self.assertEqual(ev["approval"], "stale")
            self.assertEqual(ev["changes_since_approval"][0]["section"], "1. Summary")
            self.assertIn("Extra line.", ev["changes_since_approval"][0]["added"])

            refused = approve_document(repo, "specs/001-lens/spec.md", who="karuturihemanth@gmail.com")
            self.assertFalse(refused["ok"])
            accepted = approve_document(
                repo,
                "specs/001-lens/spec.md",
                who="karuturihemanth@gmail.com",
                via="lens",
                change_note="Clarified summary",
            )
            self.assertTrue(accepted["ok"])
            self.assertIn("Clarified summary", spec.read_text("utf-8"))

    def test_secrets_redaction_in_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = create_minimal_gw_repo(Path(td) / "sec")
            (repo / "DECISIONS").mkdir()
            (repo / "DECISIONS" / "RFC-0001-s.md").write_text(
                "---\nid: RFC-0001\ntitle: S\n---\n\n## 1. Summary\nSafe line.\napi_key = ghp_1234567890abcdefghijklmnop\n",
                encoding="utf-8",
            )
            commit_all(repo, "add rfc with secret")
            snap = export_repo(repo)
            summary = snap["items"][0]["summary"]
            self.assertIn("Safe line.", summary)
            self.assertNotIn("ghp_", summary)
