"""Write flows: requests, answers, document edits, approvals, and send-backs."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from lens.engine_runner import (
    EngineError,
    run_answer,
    run_approve,
    run_edit_doc,
    run_export,
    run_new_request,
    run_read_doc,
)
from lens.hosts import adapter_for_host
from lens.hosts.base import HostAdapter, HostError, PullRequestInfo
from lens.hosts.local_git import LocalGitAdapter
from lens.images import MAX_IMAGES_PER_REQUEST, validate_and_strip_image
from lens.store import LensStore, project_branch
from lens.sync import SyncWorker, _parse_gitmodules_entries


class WriteError(RuntimeError):
    """Raised when a Lens write operation fails validation or permission checks."""


def _run_git(cwd: Path, args: list[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _people_aliases_match(user: dict[str, Any], candidate: str) -> bool:
    cand = (candidate or "").strip().lower()
    if not cand:
        return False
    tokens = {
        str(user.get("user_login") or "").lower(),
        str(user.get("user_name") or "").lower(),
        *(str(e).lower() for e in user.get("verified_emails") or []),
    }
    return cand in tokens


def _match_user_to_people(user: dict[str, Any], people: list[dict[str, Any]]) -> dict[str, Any] | None:
    tokens = {
        str(user.get("user_login") or "").lower(),
        str(user.get("user_name") or "").lower(),
        *(str(e).lower() for e in user.get("verified_emails") or []),
    }
    tokens.discard("")
    for p in people:
        p_tokens = {str(p.get("name") or "").lower(), *(str(a).lower() for a in p.get("aliases") or [])}
        if tokens & p_tokens:
            return p
    return None


def _is_named_approver(user: dict[str, Any], approvers: list[str], people: list[dict[str, Any]]) -> bool:
    matched_person = _match_user_to_people(user, people)
    person_names = {str(user.get("user_name") or "").lower(), str(user.get("user_login") or "").lower()}
    if matched_person:
        person_names.add(matched_person["name"].lower())
        person_names.update(str(a).lower() for a in matched_person.get("aliases") or [])
    for app in approvers:
        if app.strip().lower() in person_names:
            return True
    return False


class LensWriter:
    """Executes all 4 allowed Lens writes via the GroundWork engine and HostAdapter."""

    def __init__(self, store: LensStore, sync_worker: SyncWorker) -> None:
        self.store = store
        self.sync_worker = sync_worker

    def _resolve_target_repo(
        self,
        project: dict[str, Any],
        adapter: HostAdapter,
        repo_override: str = "",
        user_token: str = "",
    ) -> tuple[str, str]:
        """Return (repo_full_name, default_branch) for workspace or single/sub-repo target."""
        if not repo_override or repo_override in ("Several", "Not sure", project["display_name"], project["repo_full_name"]):
            return project["repo_full_name"], project_branch(project)

        snap = self.store.get_latest_snapshot(project["id"])
        # Check workspace .gitmodules in mirror if available
        mirror = self.sync_worker.mirror_dir_for(project["id"])
        if mirror.exists():
            gm_res = _run_git(mirror, ["show", f"HEAD:.gitmodules"])
            if gm_res.returncode == 0:
                for entry in _parse_gitmodules_entries(gm_res.stdout, project["repo_full_name"]):
                    if entry["name"] == repo_override or entry["repo_full_name"] == repo_override:
                        info = adapter.get_repo_info(entry["repo_full_name"], user_token=user_token)
                        return entry["repo_full_name"], info.default_branch or "main"

        if "/" in repo_override:
            info = adapter.get_repo_info(repo_override, user_token=user_token)
            return repo_override, info.default_branch or "main"
        if "/" in project["repo_full_name"]:
            owner = project["repo_full_name"].rsplit("/", 1)[0]
            cand = f"{owner}/{repo_override}"
            info = adapter.get_repo_info(cand, user_token=user_token)
            return cand, info.default_branch or "main"
        return repo_override, "main"

    def _signer_identity(self, user: dict[str, Any], people: list[dict[str, Any]]) -> str:
        """Pick verified email (or login/name) matching PROJECT.md for approvals."""
        emails = [str(e) for e in user.get("verified_emails") or [] if e]
        matched = _match_user_to_people(user, people)
        if matched:
            aliases = [str(a) for a in matched.get("aliases") or []]
            for e in emails:
                if e.lower() in [a.lower() for a in aliases]:
                    return e
            for a in aliases:
                if "@" in a:
                    return a
        if emails:
            return emails[0]
        return str(user.get("user_login") or user.get("user_name") or "Reviewer")

    def _commit_author_and_trailer(
        self,
        adapter: HostAdapter,
        repo_full_name: str,
        user: dict[str, Any],
        trailer_key: str,
    ) -> tuple[str, str, str]:
        """Return (author_name, author_email, trailer_line_or_empty)."""
        name = str(user.get("user_name") or user.get("user_login") or "GroundWork User")
        emails = user.get("verified_emails") or []
        email = str(emails[0]) if emails else f"{user.get('user_login') or 'user'}@users.noreply.groundwork.local"
        can_push = False
        if user.get("sign_in_mode") != "C":
            can_push = adapter.user_can_push(repo_full_name, str(user.get("user_login") or ""), user_token=str(user.get("user_token") or ""))
        if can_push:
            return name, email, ""
        # E20 / Section 9.3: commit as Lens identity, add Requested-by/Answered-by/Approved-by trailer
        return "GroundWork Lens", "lens@groundwork.local", f"{trailer_key}: {name} <{email}>"

    def _prepare_write_worktree(
        self,
        repo_full_name: str,
        target_branch: str,
        adapter: HostAdapter,
        user: dict[str, Any],
    ) -> tuple[Path, Path, str]:
        """Clone fresh temp repo on target_branch and return (tmp_dir, work_dir, write_url)."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="gw-write-"))
        user_tok = str(user.get("user_token") or "")
        can_push = adapter.user_can_push(repo_full_name, str(user.get("user_login") or ""), user_token=user_tok)
        write_url = adapter.get_clone_url_with_auth(
            repo_full_name,
            user_token=user_tok if can_push else "",
            write=can_push,
        )
        work_dir = tmp_dir / "repo"
        res = _run_git(tmp_dir, ["clone", "--branch", target_branch, "--single-branch", write_url, "repo"])
        if res.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise WriteError(f"Could not clone {repo_full_name}@{target_branch}: {res.stderr.strip()}")
        return tmp_dir, work_dir, write_url

    def _push_or_fallback_pr(
        self,
        project: dict[str, Any],
        adapter: HostAdapter,
        work_dir: Path,
        repo_full_name: str,
        target_branch: str,
        fallback_title: str,
        fallback_body: str,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        """Push commit to target_branch, or open fallback PR if push is refused/protected (E17)."""
        is_protected = isinstance(adapter, LocalGitAdapter) and adapter.is_branch_protected(repo_full_name, target_branch)
        push_err = ""
        if not is_protected:
            push_res = _run_git(work_dir, ["push", "origin", f"HEAD:{target_branch}"])
            if push_res.returncode == 0:
                return {"pushed": True, "branch": target_branch, "fallback_pr": None}
            push_err = push_res.stderr.strip() or push_res.stdout.strip()
        else:
            push_err = f"Protected branch {target_branch} refused direct push"

        # Fallback PR/MR (Section 9.9 & E17)
        fb_branch = f"lens/{int(time.time())}-{secrets.token_hex(3)}"
        _run_git(work_dir, ["checkout", "-b", fb_branch])
        fb_push = _run_git(work_dir, ["push", "origin", f"HEAD:{fb_branch}"])
        if fb_push.returncode != 0:
            raise WriteError(f"Push refused ({push_err}) and fallback branch push failed: {fb_push.stderr.strip()}")
        pr = adapter.open_pr(
            repo_full_name=repo_full_name,
            head_branch=fb_branch,
            base_branch=target_branch,
            title=fallback_title,
            body=fallback_body,
            user_token=str(user.get("user_token") or ""),
        )
        self.store.set_project_refused_push(project["id"], f"{repo_full_name}@{target_branch}: {push_err}")
        return {
            "pushed": False,
            "branch": fb_branch,
            "refused_reason": push_err,
            "fallback_pr": {"number": pr.number, "title": pr.title, "web_url": pr.web_url},
        }

    def read_document(
        self,
        project_id: int,
        rel_path: str,
        ref_name: str = "",
        repo_override: str = "",
        user_token: str = "",
    ) -> dict[str, Any]:
        """Read a governed document directly from git via engine read-doc."""
        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)
        target_repo, default_branch = self._resolve_target_repo(project=proj, adapter=adapter, repo_override=repo_override, user_token=user_token)
        branch = ref_name or default_branch

        tmp_dir, work_dir, _ = self._prepare_write_worktree(
            target_repo, branch, adapter, {"user_login": "", "user_token": user_token, "sign_in_mode": "A"}
        )
        ws_tmp: Path | None = None
        try:
            ws_dir: Path | None = None
            if target_repo != proj["repo_full_name"] and proj["is_workspace"]:
                ws_tmp, ws_dir, _ = self._prepare_write_worktree(
                    proj["repo_full_name"],
                    project_branch(proj),
                    adapter,
                    {"user_login": "", "user_token": user_token, "sign_in_mode": "A"},
                )
            doc = run_read_doc(work_dir, rel_path, workspace_repo=ws_dir)
            doc["repo_full_name"] = target_repo
            doc["branch"] = branch
            doc["web_url"] = adapter.get_web_file_url(target_repo, branch, rel_path)
            return doc
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if ws_tmp:
                shutil.rmtree(ws_tmp, ignore_errors=True)

    def submit_request(
        self,
        project_id: int,
        user: dict[str, Any],
        kind: str,
        fields: dict[str, Any],
        raw_images: list[tuple[str, bytes]] | None = None,
    ) -> dict[str, Any]:
        """Create a feature or bug request (REQ-NNNN) with optional stripped screenshots."""
        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)

        if raw_images and len(raw_images) > MAX_IMAGES_PER_REQUEST:
            raise WriteError(f"At most {MAX_IMAGES_PER_REQUEST} images are allowed per request.")

        # Validate & strip metadata from all screenshots before touching git
        clean_images: list[tuple[str, bytes]] = []
        for fname, raw_bytes in raw_images or []:
            clean_bytes, ext, _ = validate_and_strip_image(raw_bytes, fname)
            clean_images.append((ext, clean_bytes))

        product_area = str(fields.get("product_area") or "Not sure")
        target_repo, target_branch = self._resolve_target_repo(
            proj, adapter, repo_override=product_area, user_token=str(user.get("user_token") or "")
        )

        # In Mode C, requester name comes from form input; otherwise verified user name
        requester_name = str(fields.get("requested_by") or user.get("user_name") or user.get("user_login") or "Requester")
        fields_payload = dict(fields)
        fields_payload["requested_by"] = requester_name

        tmp_img_dir = Path(tempfile.mkdtemp(prefix="gw-img-"))
        attach_paths: list[Path] = []
        try:
            for idx, (ext, cbytes) in enumerate(clean_images, start=1):
                p = tmp_img_dir / f"upload_{idx}{ext}"
                p.write_bytes(cbytes)
                attach_paths.append(p)

            tmp_dir, work_dir, _ = self._prepare_write_worktree(target_repo, target_branch, adapter, user)
            try:
                author_name, author_email, trailer = self._commit_author_and_trailer(
                    adapter, target_repo, user, "Requested-by"
                )
                # E11: retry if concurrent push happened before ours
                for attempt in range(3):
                    req_res = run_new_request(work_dir, kind, fields_payload, attach_paths=attach_paths)
                    req_id = req_res["id"]
                    title = req_res["title"]
                    _run_git(work_dir, ["add", "requests", ".groundwork/ledger.jsonl"])
                    msg = f"Request {req_id}: {title} (via GroundWork Lens)"
                    if trailer:
                        msg += f"\n\n{trailer}"
                    c_res = _run_git(
                        work_dir,
                        ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", msg],
                    )
                    if c_res.returncode != 0:
                        raise WriteError(f"Git commit failed: {c_res.stderr.strip()}")

                    is_prot = isinstance(adapter, LocalGitAdapter) and adapter.is_branch_protected(target_repo, target_branch)
                    if not is_prot:
                        push_res = _run_git(work_dir, ["push", "origin", f"HEAD:{target_branch}"])
                        if push_res.returncode == 0:
                            push_info = {"pushed": True, "branch": target_branch, "fallback_pr": None}
                            break
                        err_txt = push_res.stderr + push_res.stdout
                        if attempt < 2 and any(k in err_txt.lower() for k in ("non-fast-forward", "fetch first", "rejected")):
                            _run_git(work_dir, ["reset", "--hard", f"origin/{target_branch}"])
                            _run_git(work_dir, ["clean", "-fd"])
                            _run_git(work_dir, ["pull", "--rebase", "origin", target_branch])
                            continue
                    # Protected branch or non-conflict push refusal -> fallback PR
                    push_info = self._push_or_fallback_pr(
                        project=proj,
                        adapter=adapter,
                        work_dir=work_dir,
                        repo_full_name=target_repo,
                        target_branch=target_branch,
                        fallback_title=f"Request {req_id}: {title}",
                        fallback_body=f"Submitted via GroundWork Lens by {requester_name}.",
                        user=user,
                    )
                    break

                self.sync_worker.sync_project(project_id, force=True, user_token=str(user.get("user_token") or ""))
                return {"ok": True, "request": req_res, "push": push_info, "repo": target_repo}
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        finally:
            shutil.rmtree(tmp_img_dir, ignore_errors=True)

    def submit_answers(
        self,
        project_id: int,
        user: dict[str, Any],
        req_id: str,
        answers: dict[str, str],
        repo_override: str = "",
    ) -> dict[str, Any]:
        """Record requester's answers to interview questions."""
        if user.get("sign_in_mode") == "C":
            raise WriteError("Answering questions requires sign-in (disabled in Mode C).")
        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)
        target_repo, target_branch = self._resolve_target_repo(
            proj, adapter, repo_override=repo_override, user_token=str(user.get("user_token") or "")
        )

        tmp_dir, work_dir, _ = self._prepare_write_worktree(target_repo, target_branch, adapter, user)
        try:
            who = str(user.get("user_name") or user.get("user_login") or "Requester")
            ans_res = run_answer(work_dir, req_id, answers, answered_by=who)
            author_name, author_email, trailer = self._commit_author_and_trailer(
                adapter, target_repo, user, "Answered-by"
            )
            _run_git(work_dir, ["add", "requests", ".groundwork/ledger.jsonl"])
            msg = f"Answer {req_id} questions (via GroundWork Lens)"
            if trailer:
                msg += f"\n\n{trailer}"
            _run_git(
                work_dir,
                ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", msg],
            )
            push_info = self._push_or_fallback_pr(
                project=proj,
                adapter=adapter,
                work_dir=work_dir,
                repo_full_name=target_repo,
                target_branch=target_branch,
                fallback_title=f"Answer {req_id} questions",
                fallback_body=f"Answers submitted via GroundWork Lens by {who}.",
                user=user,
            )
            self.sync_worker.sync_project(project_id, force=True, user_token=str(user.get("user_token") or ""))
            return {"ok": True, "answer": ans_res, "push": push_info}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def edit_document(
        self,
        project_id: int,
        user: dict[str, Any],
        rel_path: str,
        target_ref: str,
        new_body: str,
        expect_hash: str,
        change_note: str = "",
        also_approve: bool = False,
        repo_override: str = "",
        pr_number: int = 0,
    ) -> dict[str, Any]:
        """Edit a governed document's body (only author or named approvers; disabled in Mode C)."""
        if user.get("sign_in_mode") == "C":
            raise WriteError("Editing documents is disabled in Mode C (no login).")
        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)
        target_repo, default_branch = self._resolve_target_repo(
            proj, adapter, repo_override=repo_override, user_token=str(user.get("user_token") or "")
        )
        branch = target_ref or default_branch

        tmp_dir, work_dir, _ = self._prepare_write_worktree(target_repo, branch, adapter, user)
        ws_tmp: Path | None = None
        try:
            ws_dir: Path | None = None
            if target_repo != proj["repo_full_name"] and proj["is_workspace"]:
                ws_tmp, ws_dir, _ = self._prepare_write_worktree(
                    proj["repo_full_name"], project_branch(proj), adapter, user
                )

            doc_info = run_read_doc(work_dir, rel_path, workspace_repo=ws_dir)
            snap = run_export(work_dir, ref_name=branch)
            people = snap.get("people", [])
            approvers = doc_info.get("approvers") or []
            author_name_doc = str(doc_info.get("author") or "")
            is_author = bool(author_name_doc and _people_aliases_match(user, author_name_doc))
            matched_person = _match_user_to_people(user, people)
            if matched_person and author_name_doc.lower() == matched_person["name"].lower():
                is_author = True
            is_named_app = bool(approvers and _is_named_approver(user, approvers, people))
            if not (is_author or is_named_app):
                raise WriteError("Only the document's author and its named approvers may edit it.")

            if doc_info.get("approval") == "approved" and doc_info.get("type") == "spec" and not change_note.strip():
                raise WriteError("Editing an approved spec requires a short 'What changed and why' note.")

            signer = self._signer_identity(user, people)
            edit_res = run_edit_doc(
                work_dir,
                rel_path,
                new_body,
                expect_hash=expect_hash,
                change_note=change_note,
                who=signer,
            )
            if not edit_res.get("ok"):
                raise WriteError(str(edit_res.get("error") or "Edit refused"))

            author_name, author_email, trailer = self._commit_author_and_trailer(
                adapter, target_repo, user, "Edited-by"
            )
            _run_git(work_dir, ["add", rel_path, ".groundwork/ledger.jsonl"])
            doc_label = Path(rel_path).stem
            msg = f"Edit {doc_label} (via GroundWork Lens)"
            if trailer:
                msg += f"\n\n{trailer}"
            _run_git(
                work_dir,
                ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", msg],
            )

            approve_res = None
            if also_approve:
                approve_res = run_approve(
                    work_dir,
                    rel_path,
                    who=signer,
                    via="lens",
                    expect_hash=edit_res["body_hash"],
                    change_note="",
                    workspace_repo=ws_dir,
                )
                if not approve_res.get("ok"):
                    raise WriteError(str(approve_res.get("error") or "Approval refused"))
                _, _, ap_trailer = self._commit_author_and_trailer(adapter, target_repo, user, "Approved-by")
                _run_git(work_dir, ["add", rel_path, ".groundwork/approvals.json", ".groundwork/ledger.jsonl"])
                ap_msg = f"Approve {doc_label} (via GroundWork Lens)"
                if ap_trailer:
                    ap_msg += f"\n\n{ap_trailer}"
                _run_git(
                    work_dir,
                    ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", ap_msg],
                )

            push_info = self._push_or_fallback_pr(
                project=proj,
                adapter=adapter,
                work_dir=work_dir,
                repo_full_name=target_repo,
                target_branch=branch,
                fallback_title=f"Edit {doc_label}" + (" and approve" if also_approve else ""),
                fallback_body=f"Edited via GroundWork Lens by {signer}.",
                user=user,
            )
            self.sync_worker.sync_project(project_id, force=True, user_token=str(user.get("user_token") or ""))
            return {"ok": True, "edit": edit_res, "approve": approve_res, "push": push_info}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if ws_tmp:
                shutil.rmtree(ws_tmp, ignore_errors=True)

    def approve_document(
        self,
        project_id: int,
        user: dict[str, Any],
        rel_path: str,
        target_ref: str,
        expect_hash: str,
        change_note: str = "",
        repo_override: str = "",
        pr_number: int = 0,
    ) -> dict[str, Any]:
        """Approve a governed document via groundwork.py approve --via lens."""
        if user.get("sign_in_mode") == "C":
            raise WriteError("Approving documents is disabled in Mode C (no login).")
        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)
        target_repo, default_branch = self._resolve_target_repo(
            proj, adapter, repo_override=repo_override, user_token=str(user.get("user_token") or "")
        )
        branch = target_ref or default_branch

        tmp_dir, work_dir, _ = self._prepare_write_worktree(target_repo, branch, adapter, user)
        ws_tmp: Path | None = None
        try:
            ws_dir: Path | None = None
            if target_repo != proj["repo_full_name"] and proj["is_workspace"]:
                ws_tmp, ws_dir, _ = self._prepare_write_worktree(
                    proj["repo_full_name"], project_branch(proj), adapter, user
                )

            doc_info = run_read_doc(work_dir, rel_path, workspace_repo=ws_dir)
            snap = run_export(work_dir, ref_name=branch)
            people = snap.get("people", [])
            approvers = doc_info.get("approvers") or []

            if approvers:
                if not _is_named_approver(user, approvers, people):
                    raise WriteError(f"Only named approvers ({', '.join(approvers)}) may approve this document.")
            else:
                if not adapter.user_can_push(target_repo, str(user.get("user_login") or ""), user_token=str(user.get("user_token") or "")):
                    raise WriteError("Write access to the repository is required to approve when no approvers are named.")

            signer = self._signer_identity(user, people)
            ap_res = run_approve(
                work_dir,
                rel_path,
                who=signer,
                via="lens",
                expect_hash=expect_hash,
                change_note=change_note,
                workspace_repo=ws_dir,
            )
            if not ap_res.get("ok"):
                err = str(ap_res.get("error") or "Approval refused")
                raise WriteError(err)

            author_name, author_email, trailer = self._commit_author_and_trailer(
                adapter, target_repo, user, "Approved-by"
            )
            _run_git(work_dir, ["add", rel_path, ".groundwork/approvals.json", ".groundwork/ledger.jsonl"])
            doc_label = Path(rel_path).stem
            msg = f"Approve {doc_label} (via GroundWork Lens)"
            if trailer:
                msg += f"\n\n{trailer}"
            c_res = _run_git(
                work_dir,
                ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", msg],
            )
            if c_res.returncode != 0:
                raise WriteError(f"Git commit failed: {c_res.stderr.strip()}")

            push_info = self._push_or_fallback_pr(
                project=proj,
                adapter=adapter,
                work_dir=work_dir,
                repo_full_name=target_repo,
                target_branch=branch,
                fallback_title=f"Approve {doc_label}",
                fallback_body=f"Approved via GroundWork Lens by {signer}.",
                user=user,
            )
            self.sync_worker.sync_project(project_id, force=True, user_token=str(user.get("user_token") or ""))
            return {"ok": True, "approve": ap_res, "push": push_info}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if ws_tmp:
                shutil.rmtree(ws_tmp, ignore_errors=True)

    def send_back_document(
        self,
        project_id: int,
        user: dict[str, Any],
        rel_path: str,
        target_ref: str,
        pr_number: int,
        note: str,
        repo_override: str = "",
    ) -> dict[str, Any]:
        """Send a document back to the author with a note (PR comment or feedback PR)."""
        if user.get("sign_in_mode") == "C":
            raise WriteError("Sending back documents requires sign-in (disabled in Mode C).")
        clean_note = (note or "").strip()
        if not clean_note:
            raise WriteError("Please enter a note explaining what needs to change.")

        proj = self.store.get_project(project_id)
        if proj is None:
            raise WriteError("Project not found")
        host = self.store.get_host(proj["host_id"])
        assert host is not None
        adapter = adapter_for_host(host)
        target_repo, default_branch = self._resolve_target_repo(
            proj, adapter, repo_override=repo_override, user_token=str(user.get("user_token") or "")
        )
        by_name = str(user.get("user_name") or user.get("user_login") or "Reviewer")
        user_tok = str(user.get("user_token") or "")

        pr_url = ""
        if pr_number > 0:
            pr_url = adapter.comment_on_pr(
                target_repo,
                pr_number,
                f"Sent back from GroundWork Lens by **{by_name}** for `{rel_path}`:\n\n{clean_note}",
                user_token=user_tok,
            )
        else:
            # Document is already on default branch: open a small review note PR
            tmp_dir, work_dir, _ = self._prepare_write_worktree(target_repo, target_ref or default_branch, adapter, user)
            try:
                fb_branch = f"lens/send-back-{int(time.time())}"
                _run_git(work_dir, ["checkout", "-b", fb_branch])
                _run_git(
                    work_dir,
                    [
                        "-c",
                        "user.name=GroundWork Lens",
                        "-c",
                        "user.email=lens@groundwork.local",
                        "commit",
                        "--allow-empty",
                        "-m",
                        f"Send back {Path(rel_path).stem} (via GroundWork Lens)",
                    ],
                )
                _run_git(work_dir, ["push", "origin", f"HEAD:{fb_branch}"])
                pr = adapter.open_pr(
                    repo_full_name=target_repo,
                    head_branch=fb_branch,
                    base_branch=target_ref or default_branch,
                    title=f"Review feedback on {Path(rel_path).stem}",
                    body=f"Sent back by **{by_name}** via GroundWork Lens:\n\n{clean_note}",
                    user_token=user_tok,
                )
                pr_url = pr.web_url
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        self.store.record_send_back(project_id, rel_path, by_name, clean_note, pr_url=pr_url)
        return {"ok": True, "pr_url": pr_url}
