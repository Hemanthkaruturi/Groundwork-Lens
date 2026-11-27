"""End-to-end container/HTTP server tests covering M2, M3, M4, M5, and self-hosting."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request as UrlReq, build_opener

from lens.app import create_app
from lens.wsgi import QuietWSGIHandler, TestClient, ThreadingWSGIServer
from tests.helpers import commit_all, create_minimal_gw_repo, doc_hash, make_png_with_text_chunk

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_csrf(html_bytes: bytes) -> str:
    m = re.search(rb'name="_csrf"\s+value="([^"]+)"', html_bytes)
    assert m, "CSRF token not found in HTML"
    return m.group(1).decode("ascii")


class TestEndToEnd(unittest.TestCase):
    def test_live_http_server_setup_wizard_demo_and_healthz(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = create_app(Path(td) / "data", start_worker=False)
            setup_code = app.ctx.setup_code
            self.assertIsNotNone(setup_code)

            server = ThreadingWSGIServer(("127.0.0.1", 0), app, handler_class=QuietWSGIHandler)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                opener = build_opener(HTTPCookieProcessor())
                base = f"http://127.0.0.1:{port}"

                # /healthz
                with opener.open(f"{base}/healthz") as r:
                    self.assertEqual(r.status, 200)
                    self.assertTrue(json.loads(r.read())["ok"])

                # / redirects to /setup on fresh install; unlock with setup_code
                with opener.open(f"{base}/setup") as r:
                    setup_html = r.read()
                    self.assertIn(b"Enter one-time setup code", setup_html)
                    csrf = _extract_csrf(setup_html)

                req_unlock = UrlReq(
                    f"{base}/setup/unlock",
                    data=urlencode({"_csrf": csrf, "setup_code": setup_code}).encode("utf-8"),
                    method="POST",
                )
                with opener.open(req_unlock) as r:
                    unlocked_html = r.read()
                    self.assertIn(b"Create GitHub App automatically", unlocked_html)

                # Try demo mode -> seeds real git workspace and renders project home
                with opener.open(f"{base}/demo") as r:
                    demo_html = r.read().decode("utf-8")
                    self.assertIn("Brightwater (Demo)", demo_html)
                    self.assertIn("Excel export", demo_html)
                    self.assertIn("Maya Chen", demo_html)
                    self.assertIn("Discovery price stale", demo_html)
                    self.assertIn("in PR #42", demo_html)
                    self.assertIn("Older work", demo_html)
            finally:
                server.shutdown()
                server.server_close()
                app.ctx.close()

    def test_m4_m5_requests_answers_edit_approve_and_mode_c_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = create_app(Path(td) / "data", start_worker=False)
            client = TestClient(app)

            # Enter demo as Jordan Reyes (leader & named approver on RFC-0013 and requester of REQ-0001)
            st, _, body = client.get("/demo", follow_redirects=True)
            self.assertEqual(st, 200)
            html = body.decode("utf-8")
            self.assertIn("Brightwater (Demo)", html)

            # Find demo workspace project id
            projs = app.ctx.store.list_projects(include_demo=True)
            ws_proj = next(p for p in projs if p["is_workspace"])
            pid = ws_proj["id"]

            # "Waiting on me" filter shows Jordan's approval & question items
            st_mine, _, mine_body = client.get(f"/projects/{pid}?mine=1")
            self.assertEqual(st_mine, 200)
            self.assertIn("Filter contacts by seniority", mine_body.decode("utf-8"))

            # Answer REQ-0001 questions via form
            st_req, _, req_html = client.get(f"/projects/{pid}/item/fieldNotes/REQ-0001")
            self.assertEqual(st_req, 200)
            csrf = _extract_csrf(req_html)
            st_ans, _, ans_html = client.post(
                f"/projects/{pid}/request/REQ-0001/answer",
                form={"_csrf": csrf, "repo_override": "fieldNotes", "answer_Q1": "Separate options"},
                follow_redirects=True,
            )
            self.assertEqual(st_ans, 200)
            self.assertIn("Being discussed", ans_html.decode("utf-8"))

            # Read, Edit, and Approve RFC-0013 as named approver Jordan Reyes
            doc_url = f"/projects/{pid}/doc?path=DECISIONS%2FRFC-0013-alert-digest.md&repo=fieldNotes&ref=main"
            st_doc, _, doc_html = client.get(doc_url)
            self.assertEqual(st_doc, 200)
            doc_text = doc_html.decode("utf-8")
            self.assertIn("Edit document body", doc_text)
            self.assertIn("Approve", doc_text)
            csrf_doc = _extract_csrf(doc_html)
            hash_m = re.search(r'name="expect_hash"\s+value="([^"]+)"', doc_text)
            assert hash_m
            expect_h = hash_m.group(1)

            # Save and approve edited RFC
            st_ed, _, ed_html = client.post(
                f"/projects/{pid}/doc/edit",
                form={
                    "_csrf": csrf_doc,
                    "path": "DECISIONS/RFC-0013-alert-digest.md",
                    "repo": "fieldNotes",
                    "ref": "main",
                    "pr": "0",
                    "expect_hash": expect_h,
                    "body": "## 1. Summary\n\nSend weekly digest every Monday.\n\n## 3. Done means\n\nLeaders receive one concise digest.\n",
                    "action": "save_and_approve",
                },
                follow_redirects=True,
            )
            self.assertEqual(st_ed, 200)
            self.assertIn("Approved", ed_html.decode("utf-8"))

            # Verify approvals.json on disk in demo_repos/fieldNotes contains via: "lens"
            rt_approvals = json.loads((app.ctx.data_dir / "demo_repos" / "fieldNotes" / ".groundwork" / "approvals.json").read_text("utf-8"))
            rfc13_rec = rt_approvals["DECISIONS/RFC-0013-alert-digest.md"]
            self.assertEqual(rfc13_rec["signers"][0]["via"], "lens")
            self.assertEqual(rfc13_rec["signers"][0]["who"], "jordan@brightwater.example")

            # Submit a feature request with PNG screenshot via multipart form
            st_rf, _, rf_html = client.get(f"/projects/{pid}/request/feature")
            self.assertEqual(st_rf, 200)
            csrf_rf = _extract_csrf(rf_html)
            png_bytes = make_png_with_text_chunk(b"GPS=PRIVATE")
            boundary = "----LensTestBoundary7MA4YWxkTrZu0gW"
            mp_parts = [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"_csrf\"\r\n\r\n{csrf_rf}\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"step\"\r\n\r\nsubmit\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nExport PDF summary\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"need\"\r\n\r\nBoard pack PDF.\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"done_looks_like\"\r\n\r\nDownload button works.\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"product_area\"\r\n\r\nbrightwater-costs\r\n".encode(),
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"images\"; filename=\"sketch.png\"\r\n"
                    f"Content-Type: image/png\r\n\r\n"
                ).encode()
                + png_bytes
                + b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
            st_sub, _, sub_html = client.post(
                f"/projects/{pid}/request/feature",
                raw_body=b"".join(mp_parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                follow_redirects=True,
            )
            self.assertEqual(st_sub, 200)
            self.assertIn("Export PDF summary", sub_html.decode("utf-8"))

            # Verify PNG metadata stripped in committed attachment
            committed_img = app.ctx.data_dir / "demo_repos" / "brightwater-costs" / "requests" / "REQ-0001-export-pdf-summary" / "1.png"
            self.assertTrue(committed_img.is_file())
            self.assertNotIn(b"GPS=PRIVATE", committed_img.read_bytes())

            # Admin health page loads cleanly
            st_hl, _, hl_html = client.get("/admin/health")
            self.assertEqual(st_hl, 200)
            self.assertIn("System health", hl_html.decode("utf-8"))

            # Mode C disables approval & edit
            host_id = app.ctx.store.upsert_host(
                "local",
                "ModeC",
                str(app.ctx.data_dir / "demo_repos"),
                str(app.ctx.data_dir / "demo_repos"),
                sign_in_mode="C",
            )
            c_pid = app.ctx.store.upsert_project(host_id, "fieldNotes", "rt-mode-c")
            app.ctx.sync_worker.sync_project(c_pid, force=True)
            app.ctx.config.mark_setup_complete()
            c_client = TestClient(app)
            st_cdoc, _, c_doc_html = c_client.get(f"/projects/{c_pid}/doc?path=DECISIONS%2FRFC-0011-contacts.md")
            self.assertEqual(st_cdoc, 200)
            self.assertIn("turned off in Mode C", c_doc_html.decode("utf-8"))

            app.ctx.close()

    def test_groundwork_lens_repo_itself_passes_check_and_export(self) -> None:
        proc_chk = subprocess.run(
            [sys.executable, str(REPO_ROOT / "engine" / "groundwork.py"), "check", "--json", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=True,
        )
        chk = json.loads(proc_chk.stdout)
        self.assertEqual(chk["check_errors"], 0)

        proc_exp = subprocess.run(
            [sys.executable, str(REPO_ROOT / "engine" / "groundwork.py"), "export", "--json", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=True,
        )
        exp = json.loads(proc_exp.stdout)
        self.assertEqual(exp["schema_version"], "1.0")
        self.assertEqual(exp["name"], "groundwork-lens")
