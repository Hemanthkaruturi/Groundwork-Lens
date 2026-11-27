"""Bundled sample GroundWork workspace for Demo mode and end-to-end tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from engine.groundwork_common import body_hash, split_front_matter
from lens.hosts.local_git import LocalGitAdapter
from lens.store import LensStore
from lens.sync import SyncWorker


def _git(cwd: Path, args: list[str], commit_date: str = "2026-11-25T14:00:00Z") -> str:
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = commit_date
    env["GIT_COMMITTER_DATE"] = commit_date
    env["GIT_TERMINAL_PROMPT"] = "0"
    res = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Hemanth Karuturi", "-c", "user.email=karuturihemanth@gmail.com", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, ["init", "-b", "main"])
    _git(path, ["config", "receive.denyCurrentBranch", "updateInstead"])


def _commit_all(path: Path, message: str, date: str = "2026-11-25T14:00:00Z") -> None:
    _git(path, ["add", "-A"], commit_date=date)
    _git(path, ["commit", "-m", message], commit_date=date)


def _doc_hash(path: Path) -> str:
    _, body = split_front_matter(path.read_text(encoding="utf-8"))
    return body_hash(body)


def seed_sample_workspace(host_root: Path) -> dict[str, Any]:
    """Create a realistic SignalFarming-like workspace + sub-repos in host_root."""
    if (host_root / "SignalFarming" / ".git").exists():
        return {"workspace": "SignalFarming", "repos": ["researchTrail", "signalfarming-costs", "legacy-scripts"]}

    host_root.mkdir(parents=True, exist_ok=True)
    adapter = LocalGitAdapter(host_root)

    # 1. Sub-repo: signalfarming-costs
    costs = host_root / "signalfarming-costs"
    _init_repo(costs)
    (costs / ".groundwork").mkdir()
    (costs / ".groundwork" / "config.json").write_text(json.dumps({"name": "signalfarming-costs", "kind": "repo"}), encoding="utf-8")
    (costs / "PROJECT.md").write_text(
        "# signalfarming-costs\n\n## Who works on what\n\n| Name | Role | Contact |\n| --- | --- | --- |\n"
        "| Hemanth Karuturi | Owner and sole developer | karuturihemanth@gmail.com, Hemanthkaruturi |\n"
        "| Amit Singh | Product reviewer | amit@signalfarming.example, amitsingh |\n",
        encoding="utf-8",
    )
    (costs / "ARCHITECTURE.md").write_text("# Architecture\nCost dashboard service.\n", encoding="utf-8")
    (costs / "CONSTITUTION.md").write_text("# Constitution\nKeep calculations auditable.\n", encoding="utf-8")
    (costs / "DECISIONS").mkdir()
    rfc12 = costs / "DECISIONS" / "RFC-0012-excel-export.md"
    rfc12.write_text(
        "---\nid: RFC-0012\ntitle: Excel export\nstatus: approved\nrequested_by: Amit Singh\nowner: Hemanth Karuturi\n"
        "approvers: [Amit Singh]\napproved_by: [amit@signalfarming.example]\n---\n\n"
        "## 1. Summary\n\nAdd a Download Excel option to the cost dashboard so finance reviewers can export monthly cost tables.\n\n"
        "## 2. The need\n\nLeaders want offline spreadsheets without running SQL.\n\n"
        "## 3. Done means\n\nOn production, a signed-in viewer picks a range and downloads a valid workbook.\n",
        encoding="utf-8",
    )
    spec3_dir = costs / "specs" / "003-excel-export"
    spec3_dir.mkdir(parents=True)
    spec3 = spec3_dir / "spec.md"
    spec3.write_text(
        "---\ntitle: Excel export\nrfc: RFC-0012\nstatus: approved\nowner: Hemanth Karuturi\nrequested_by: Amit Singh\n---\n\n"
        "## 1. Summary\n\nExport cost breakdown tables as an Excel workbook.\n\n"
        "## Done means\n\nDownloaded workbook matches dashboard numbers.\n",
        encoding="utf-8",
    )
    (spec3_dir / "tasks.md").write_text(
        "# Tasks\n\n"
        "- [x] **T001** — Build workbook serializer\n"
        "- [x] **T002** — Add download button\n"
        "- [ ] **T902** — Run the manual tests in spec section 8\n"
        "  - **Waiting on:** Hemanth Karuturi (browser look and E18/E19 check)\n"
        "  - **Done when:** All manual checks pass live\n",
        encoding="utf-8",
    )
    (spec3_dir / "log.md").write_text(
        "- 2026-11-25T14:12:00Z: 003 deployed. Waiting on owner: spec section 8 by hand (T902). Then handover.\n",
        encoding="utf-8",
    )
    (costs / ".groundwork" / "approvals.json").write_text(
        json.dumps(
            {
                "DECISIONS/RFC-0012-excel-export.md": {
                    "hash": _doc_hash(rfc12),
                    "signers": [{"who": "amit@signalfarming.example", "at": "2026-11-20T09:00:00Z"}],
                },
                "specs/003-excel-export/spec.md": {
                    "hash": _doc_hash(spec3),
                    "signers": [{"who": "karuturihemanth@gmail.com", "at": "2026-11-21T09:00:00Z"}],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (costs / ".groundwork" / "ledger.jsonl").write_text(
        '{"at":"2026-11-20T09:00:00Z","type":"requested","item":"RFC-0012","who":"Amit Singh"}\n',
        encoding="utf-8",
    )
    _commit_all(costs, "spec 003: deploy Excel export and wait for manual tests", date="2026-11-25T14:12:00Z")

    # 2. Sub-repo: researchTrail (with stale doc, open bug, legacy spec, agent-assisted change, open PR)
    rt = host_root / "researchTrail"
    _init_repo(rt)
    (rt / ".groundwork").mkdir()
    (rt / ".groundwork" / "config.json").write_text(json.dumps({"name": "researchTrail", "kind": "repo"}), encoding="utf-8")
    (rt / "PROJECT.md").write_text(
        "# researchTrail\n\n## Who works on what\n\n| Name | Role | Contact |\n| --- | --- | --- |\n"
        "| Hemanth Karuturi | Owner and sole developer | karuturihemanth@gmail.com, Hemanthkaruturi |\n"
        "| Amit Singh | Leadership reviewer | amit@signalfarming.example, amitsingh |\n",
        encoding="utf-8",
    )
    (rt / "ARCHITECTURE.md").write_text("# Architecture\nResearch pipeline.\n", encoding="utf-8")
    (rt / "CONSTITUTION.md").write_text("# Constitution\nCite sources accurately.\n", encoding="utf-8")
    (rt / "DECISIONS").mkdir()
    rfc11 = rt / "DECISIONS" / "RFC-0011-contacts.md"
    rfc11.write_text(
        "---\nid: RFC-0011\ntitle: Named decision-maker contacts\nstatus: approved\nrequested_by: Amit Singh\nowner: Hemanth Karuturi\n---\n\n"
        "## 1. Summary\n\nDisplay verified decision-maker contacts on company cards.\n",
        encoding="utf-8",
    )
    rfc13 = rt / "DECISIONS" / "RFC-0013-alert-digest.md"
    rfc13.write_text(
        "---\nid: RFC-0013\ntitle: Weekly customer alert digest\nstatus: in-review\nrequested_by: Amit Singh\nowner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n"
        "## 1. Summary\n\nSend a weekly email summary when watched companies raise funding.\n\n"
        "## 2. The need\n\nAccount leaders miss updates between meetings.\n\n"
        "## 3. Done means\n\nEvery Monday morning, watched accounts appear in one digest.\n",
        encoding="utf-8",
    )
    s38 = rt / "specs" / "038-named-decision-maker-contacts"
    s38.mkdir(parents=True)
    s38_spec = s38 / "spec.md"
    s38_spec.write_text(
        "---\ntitle: Named decision-maker contacts\nrfc: RFC-0011\nstatus: approved\nowner: Hemanth Karuturi\n---\n\n"
        "## 1. Summary\n\nShow named contacts with verified roles.\n\n## Done means\n\nContacts verified live.\n",
        encoding="utf-8",
    )
    (s38 / "tasks.md").write_text(
        "- [x] **T001** — Build lookup\n- [ ] **T013** — Owner go-live verification\n",
        encoding="utf-8",
    )
    (s38 / "log.md").write_text("- 2026-11-08T11:30:00Z: deployed 2026-11-08... owner go-live (T013) open\n", encoding="utf-8")
    (s38 / "changes.md").write_text("- 2026-11-07 by Hemanth Karuturi (via claude-code): Added fallback for missing handles\n", encoding="utf-8")

    # Legacy baseline spec (E4)
    s02 = rt / "specs" / "002-baseline-crawler"
    s02.mkdir(parents=True)
    (s02 / "spec.md").write_text(
        "---\ntitle: Baseline crawler\nrfc: (none)\nbaseline: true\nstatus: done\nowner: Hemanth Karuturi\n---\n\n## 1. Summary\n\nOriginal crawler before GroundWork.\n",
        encoding="utf-8",
    )
    (s02 / "handover.md").write_text("---\nstatus: done\n---\n\nRunning in production.\n", encoding="utf-8")

    # Bug 011
    (rt / "bugs").mkdir()
    (rt / "bugs" / "011-discovery-price-stale.md").write_text(
        "---\ntitle: Discovery price stale\nstatus: fixed\nseverity: medium\nreported_by: Amit Singh\nowner: Hemanth Karuturi\nfixed_by: Hemanth Karuturi\ncause: design-flaw\n---\n\n"
        "## Expected\n\nPrice refreshes within 15 minutes.\n\n## Actual\n\nPrice stayed stale for 24 hours.\n",
        encoding="utf-8",
    )

    # Request with pending questions
    (rt / "requests").mkdir()
    (rt / "requests" / "REQ-0001-export-filters.md").write_text(
        "---\nid: REQ-0001\nkind: feature\ntitle: Filter contacts by seniority\nrequested_by: Amit Singh\nrequested_at: 2026-11-24T08:00:00Z\nstatus: questions\nconverted_to: \"\"\ndeclined_reason: \"\"\n---\n\n"
        "# REQ-0001: Filter contacts by seniority\n\n"
        "## What is needed, and for whom\n\nAccount managers want to filter to VP+ contacts.\n\n"
        "## What \"done\" would look like\n\nA seniority dropdown filters the contact list.\n\n"
        "## Questions for Amit Singh\n\n"
        "1. **Q1** — Should C-suite and VP be separate options or grouped together?\n"
        "   - Separate options\n"
        "   - Grouped as Executive\n",
        encoding="utf-8",
    )

    (rt / ".groundwork" / "approvals.json").write_text(
        json.dumps(
            {
                "DECISIONS/RFC-0011-contacts.md": {
                    "hash": _doc_hash(rfc11),
                    "signers": [{"who": "karuturihemanth@gmail.com", "at": "2026-11-01T09:00:00Z"}],
                },
                "specs/038-named-decision-maker-contacts/spec.md": {
                    "hash": "stale-hash-from-earlier-version",
                    "signers": [{"who": "karuturihemanth@gmail.com", "at": "2026-11-02T09:00:00Z"}],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (rt / ".groundwork" / "ledger.jsonl").write_text(
        '{"at":"2026-11-01T09:00:00Z","type":"created","item":"researchTrail","who":"Hemanth Karuturi"}\n',
        encoding="utf-8",
    )
    _commit_all(rt, "Initial researchTrail GroundWork state", date="2026-11-08T11:30:00Z")

    # Add a PR branch on researchTrail to demonstrate "in PR #42"
    _git(rt, ["checkout", "-b", "feat/rfc-0014-pricing-alert"])
    (rt / "DECISIONS" / "RFC-0014-pricing-alert.md").write_text(
        "---\nid: RFC-0014\ntitle: Pricing tier change alert\nstatus: in-review\nrequested_by: Amit Singh\nowner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n"
        "## 1. Summary\n\nNotify team when a competitor updates pricing pages.\n",
        encoding="utf-8",
    )
    _commit_all(rt, "RFC-0014: draft pricing tier change alert", date="2026-11-26T08:00:00Z")
    _git(rt, ["checkout", "main"])
    adapter.register_pr("researchTrail", "feat/rfc-0014-pricing-alert", "Add RFC-0014 pricing alert", number=42)

    # 3. Sub-repo without GroundWork (N5)
    legacy_repo = host_root / "legacy-scripts"
    _init_repo(legacy_repo)
    (legacy_repo / "README.md").write_text("# Legacy Scripts\nNo GroundWork yet.\n", encoding="utf-8")
    _commit_all(legacy_repo, "Initial legacy scripts", date="2026-11-10T10:00:00Z")

    # 4. Parent Workspace: SignalFarming
    ws = host_root / "SignalFarming"
    _init_repo(ws)
    (ws / ".groundwork").mkdir()
    (ws / ".groundwork" / "config.json").write_text(
        json.dumps({"name": "SignalFarming", "kind": "workspace"}),
        encoding="utf-8",
    )
    (ws / "PROJECT.md").write_text(
        "# SignalFarming Workspace\n\n## Who works on what\n\n| Name | Role | Contact |\n| --- | --- | --- |\n"
        "| Hemanth Karuturi | Owner and sole developer | karuturihemanth@gmail.com, Hemanthkaruturi |\n"
        "| Amit Singh | Leadership reviewer | amit@signalfarming.example, amitsingh |\n",
        encoding="utf-8",
    )
    (ws / "ARCHITECTURE.md").write_text("# Workspace Architecture\nMulti-repo product suite.\n", encoding="utf-8")
    (ws / "CONSTITUTION.md").write_text("# Workspace Constitution\nNo LLM in status reporting.\n", encoding="utf-8")
    (ws / ".gitmodules").write_text(
        '[submodule "researchTrail"]\n\tpath = researchTrail\n\turl = ../researchTrail\n'
        '[submodule "signalfarming-costs"]\n\tpath = signalfarming-costs\n\turl = ../signalfarming-costs\n'
        '[submodule "legacy-scripts"]\n\tpath = legacy-scripts\n\turl = ../legacy-scripts\n',
        encoding="utf-8",
    )
    (ws / "DECISIONS").mkdir()
    rfc_ws = ws / "DECISIONS" / "RFC-0005-unified-login.md"
    rfc_ws.write_text(
        "---\nid: RFC-0005\ntitle: Unified SSO across apps\nstatus: in-review\nrequested_by: Amit Singh\nowner: Hemanth Karuturi\napprovers: [Amit Singh]\n---\n\n"
        "## 1. Summary\n\nShare one session cookie across SignalFarming apps.\n\n## 3. Done means\n\nLeaders sign in once for both apps.\n",
        encoding="utf-8",
    )
    (ws / ".groundwork" / "approvals.json").write_text("{}", encoding="utf-8")
    (ws / ".groundwork" / "ledger.jsonl").write_text(
        '{"at":"2026-11-22T09:00:00Z","type":"created","item":"SignalFarming","who":"Hemanth Karuturi"}\n',
        encoding="utf-8",
    )
    _commit_all(ws, "Workspace setup", date="2026-11-26T09:00:00Z")

    return {"workspace": "SignalFarming", "repos": ["researchTrail", "signalfarming-costs", "legacy-scripts"]}


def ensure_demo_project(store: LensStore, sync_worker: SyncWorker) -> int:
    """Ensure the demo local host and SignalFarming demo project exist and are synced."""
    demo_root = store.data_dir / "demo_repos"
    seed_sample_workspace(demo_root)

    existing_host = store.get_host_by_kind("local")
    if existing_host and existing_host["name"] == "Demo Workspace":
        host_id = existing_host["id"]
    else:
        host_id = store.upsert_host(
            kind="local",
            name="Demo Workspace",
            base_url=str(demo_root),
            api_url=str(demo_root),
            sign_in_mode="A",
        )

    pid = store.upsert_project(
        host_id=host_id,
        repo_full_name="SignalFarming",
        display_name="SignalFarming (Demo)",
        default_branch="main",
        is_workspace=True,
        is_demo=True,
    )
    rt_pid = store.upsert_project(
        host_id=host_id,
        repo_full_name="researchTrail",
        display_name="researchTrail (Demo)",
        default_branch="main",
        is_workspace=False,
        is_demo=True,
    )

    if store.get_latest_snapshot(pid) is None:
        sync_worker.sync_project(pid, force=True)
    if store.get_latest_snapshot(rt_pid) is None:
        sync_worker.sync_project(rt_pid, force=True)
    return pid
