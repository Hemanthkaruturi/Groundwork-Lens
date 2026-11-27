"""Background git sync worker with blobless sparse checkouts and open PR snapshots."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lens.config import LensConfig
from lens.engine_runner import EngineError, get_export_paths, run_export
from lens.hosts import adapter_for_host
from lens.hosts.base import HostAdapter, HostError, PullRequestInfo, RateLimitError
from lens.store import LensStore


def _run_git(cwd: Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
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


def _touches_groundwork_paths(changed_files: list[str], export_paths: list[str]) -> bool:
    if not changed_files:
        return True  # Conservative when file list is unavailable
    for cf in changed_files:
        norm = cf.lstrip("/")
        for ep in export_paths:
            ep_clean = ep.rstrip("/")
            if norm == ep_clean or norm.startswith(ep_clean + "/"):
                return True
    return False


def _parse_gitmodules_entries(gitmodules_text: str, workspace_full_name: str) -> list[dict[str, str]]:
    """Parse .gitmodules into [{name, path, repo_full_name}]."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    ws_owner = workspace_full_name.rsplit("/", 1)[0] if "/" in workspace_full_name else ""

    def _finalize(cur: dict[str, str]) -> None:
        if not cur.get("path"):
            return
        name = Path(cur["path"]).name
        url = cur.get("url", "")
        full_name = name
        if url.startswith(("http://", "https://")):
            parsed = urlparse(url)
            full_name = parsed.path.strip("/")
            if full_name.endswith(".git"):
                full_name = full_name[:-4]
        elif url.startswith((".", "..")):
            full_name = f"{ws_owner}/{name}" if ws_owner else name
        elif "/" in url and not url.startswith("/"):
            # e.g. git@github.com:org/repo.git
            part = url.split(":", 1)[-1].strip("/")
            if part.endswith(".git"):
                part = part[:-4]
            full_name = part
        elif ws_owner:
            full_name = f"{ws_owner}/{name}"
        entries.append({"name": name, "path": cur["path"], "repo_full_name": full_name})

    for raw in (gitmodules_text or "").splitlines():
        s = raw.strip()
        if re.match(r'^\[submodule\s+"[^"]+"\]$', s):
            _finalize(current)
            current = {}
            continue
        if "=" in s:
            k, v = [x.strip() for x in s.split("=", 1)]
            current[k] = v
    _finalize(current)
    return entries


class SyncWorker:
    """Polls tracked projects via git ls-remote and stores engine JSON snapshots."""

    def __init__(self, store: LensStore, config: LensConfig) -> None:
        self.store = store
        self.config = config
        self.clones_dir = store.data_dir / "clones"
        self.clones_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._project_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last_manual_refresh: dict[int, float] = {}

    def _lock_for(self, project_id: int) -> threading.Lock:
        with self._locks_guard:
            if project_id not in self._project_locks:
                self._project_locks[project_id] = threading.Lock()
            return self._project_locks[project_id]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="lens-sync-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                for proj in self.store.list_projects(include_demo=True):
                    if self._stop_event.is_set():
                        break
                    try:
                        self.sync_project(proj["id"])
                    except Exception:
                        pass
            except Exception:
                pass
            interval = self.config.poll_interval_seconds()
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def can_manual_refresh(self, project_id: int) -> tuple[bool, int]:
        """Enforce 1-minute cooldown on manual 'Refresh now' button (Section 10.7)."""
        last = self._last_manual_refresh.get(project_id, 0.0)
        elapsed = time.time() - last
        if elapsed < 60.0:
            return False, max(1, int(60 - elapsed))
        return True, 0

    def trigger_manual_refresh(self, project_id: int) -> tuple[bool, str]:
        ok, wait_s = self.can_manual_refresh(project_id)
        if not ok:
            return False, f"Refresh is limited to once per minute. Try again in {wait_s}s."
        self._last_manual_refresh[project_id] = time.time()
        self.sync_project(project_id, force=True)
        return True, "Refreshed"

    def ensure_mirror(self, mirror_dir: Path, clone_url: str, export_paths: list[str]) -> None:
        """Clone blobless + sparse-checkout repository or update remote URL."""
        if not (mirror_dir / ".git").exists():
            mirror_dir.parent.mkdir(parents=True, exist_ok=True)
            res = _run_git(
                mirror_dir.parent,
                ["clone", "--filter=blob:none", "--no-checkout", clone_url, mirror_dir.name],
            )
            if res.returncode != 0:
                # Clean partial directory and retry without partial-clone filter
                if mirror_dir.exists():
                    shutil.rmtree(mirror_dir, ignore_errors=True)
                res2 = _run_git(mirror_dir.parent, ["clone", "--no-checkout", clone_url, mirror_dir.name])
                if res2.returncode != 0:
                    raise HostError(res2.stderr.strip() or "git clone failed")
            _run_git(mirror_dir, ["sparse-checkout", "init", "--no-cone"])
            _run_git(mirror_dir, ["sparse-checkout", "set", *export_paths])
        else:
            _run_git(mirror_dir, ["remote", "set-url", "origin", clone_url])

    def _ls_remote_sha(self, repo_dir: Path, clone_url: str, ref: str) -> str:
        res = _run_git(repo_dir, ["ls-remote", clone_url, f"refs/heads/{ref}", f"refs/pull/*/head", "HEAD"])
        if res.returncode != 0:
            return ""
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and (parts[1] == f"refs/heads/{ref}" or (ref == "HEAD" and parts[1] == "HEAD")):
                return parts[0]
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                return parts[0]
        return ""

    def _fetch_and_checkout_worktree(
        self,
        mirror_dir: Path,
        fetch_ref: str,
        worktree_dir: Path,
        export_paths: list[str],
    ) -> str:
        res = _run_git(mirror_dir, ["fetch", "--force", "origin", fetch_ref])
        if res.returncode != 0:
            raise HostError(res.stderr.strip() or f"git fetch {fetch_ref} failed")
        sha_res = _run_git(mirror_dir, ["rev-parse", "FETCH_HEAD"])
        sha = sha_res.stdout.strip()
        wt_res = _run_git(mirror_dir, ["worktree", "add", "--detach", str(worktree_dir), sha])
        if wt_res.returncode != 0:
            raise HostError(wt_res.stderr.strip() or "git worktree add failed")
        # Ensure sparse checkout inside worktree only materializes GroundWork paths
        _run_git(worktree_dir, ["sparse-checkout", "init", "--no-cone"])
        _run_git(worktree_dir, ["sparse-checkout", "set", *export_paths])
        return sha

    def mirror_dir_for(self, project_id: int, sub_name: str = "") -> Path:
        if sub_name:
            safe_sub = re.sub(r"[^A-Za-z0-9_.-]+", "_", sub_name)
            return self.clones_dir / f"project_{project_id}_sub_{safe_sub}"
        return self.clones_dir / f"project_{project_id}"

    def sync_project(self, project_id: int, force: bool = False, user_token: str = "") -> dict[str, Any] | None:
        lock = self._lock_for(project_id)
        with lock:
            proj = self.store.get_project(project_id)
            if proj is None:
                return None
            host = self.store.get_host(proj["host_id"])
            if host is None:
                self.store.update_project_sync(project_id, "error", "Host configuration missing")
                return None

            adapter = adapter_for_host(host)
            export_paths = get_export_paths()

            try:
                repo_info = adapter.get_repo_info(proj["repo_full_name"], user_token=user_token)
            except RateLimitError as exc:
                self.store.update_project_sync(
                    project_id, "rate_limited", str(exc), rate_limited_until=exc.reset_at
                )
                return None
            except HostError as exc:
                self.store.update_project_sync(project_id, "unreachable", str(exc))
                return None

            if repo_info.empty:
                self.store.update_project_sync(project_id, "empty", "This repo is empty")
                return None

            default_branch = repo_info.default_branch or proj["default_branch"] or "main"
            mirror_dir = self.mirror_dir_for(project_id)
            clone_url = adapter.get_clone_url_with_auth(proj["repo_full_name"], user_token=user_token)

            try:
                self.ensure_mirror(mirror_dir, clone_url, export_paths)
            except HostError as exc:
                self.store.update_project_sync(project_id, "unreachable", str(exc))
                return None

            remote_sha = self._ls_remote_sha(mirror_dir, clone_url, default_branch)
            latest_snap = self.store.get_latest_snapshot(project_id, default_branch)
            need_main_export = force or latest_snap is None or (remote_sha and latest_snap["commit_sha"] != remote_sha)

            main_snapshot: dict[str, Any] | None = None
            if need_main_export:
                main_snapshot = self._export_ref_snapshot(
                    project_id=project_id,
                    proj=proj,
                    adapter=adapter,
                    mirror_dir=mirror_dir,
                    ref_name=default_branch,
                    fetch_spec=f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
                    export_paths=export_paths,
                    user_token=user_token,
                )
                # Detect N8: GroundWork was present before, now removed
                gw_stage = str((main_snapshot.get("groundwork") or {}).get("stage") or "not-started")
                prev_gw = self.store.get_last_groundwork_snapshot(project_id)
                if gw_stage == "not-started" and prev_gw is not None and not main_snapshot.get("items"):
                    removed_at = proj.get("removed_groundwork_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    self.store.update_project_sync(
                        project_id,
                        "groundwork_removed",
                        f"GroundWork was removed from this repo on {removed_at}",
                        removed_groundwork_at=removed_at,
                    )
                else:
                    self.store.update_project_sync(
                        project_id,
                        "archived" if repo_info.archived else "ok",
                        "",
                        removed_groundwork_at="",
                    )
            else:
                self.store.update_project_sync(
                    project_id,
                    "archived" if repo_info.archived else "ok",
                    "",
                )
                main_snapshot = latest_snap["data"]

            # Sync open PR heads that touch GroundWork paths (E2, E13, N2), including workspace sub-repos
            try:
                pr_targets: list[tuple[str, str, Path]] = [("", proj["repo_full_name"], mirror_dir)]
                gm_res = _run_git(mirror_dir, ["show", f"refs/remotes/origin/{default_branch}:.gitmodules"])
                if gm_res.returncode == 0:
                    for entry in _parse_gitmodules_entries(gm_res.stdout, proj["repo_full_name"]):
                        pr_targets.append(
                            (
                                entry["name"],
                                entry["repo_full_name"],
                                self.mirror_dir_for(project_id, sub_name=entry["name"]),
                            )
                        )

                existing_pr_snaps = {
                    (s["ref_name"], s["pr_number"]): s for s in self.store.get_open_pr_snapshots(project_id)
                }
                active_pr_nums: list[int] = []
                for sub_name, target_full, target_mirror in pr_targets:
                    try:
                        if not (target_mirror / ".git").exists():
                            sub_url = adapter.get_clone_url_with_auth(target_full, user_token=user_token)
                            self.ensure_mirror(target_mirror, sub_url, export_paths)
                        prs = adapter.list_open_prs(target_full, user_token=user_token)
                    except RateLimitError:
                        raise
                    except Exception:
                        continue
                    gw_prs = [p for p in prs if _touches_groundwork_paths(p.changed_files, export_paths)]
                    gw_prs = gw_prs[: self.config.pr_cap()]
                    for pr in gw_prs:
                        active_pr_nums.append(pr.number)
                        stored_ref = f"{sub_name}:{pr.head_ref}" if sub_name else pr.head_ref
                        prev_pr = existing_pr_snaps.get((stored_ref, pr.number))
                        if not force and prev_pr is not None and pr.head_sha and prev_pr["commit_sha"] == pr.head_sha:
                            continue
                        try:
                            self._export_pr_snapshot(
                                project_id=project_id,
                                proj=proj,
                                adapter=adapter,
                                mirror_dir=target_mirror,
                                pr=pr,
                                export_paths=export_paths,
                                user_token=user_token,
                                sub_name=sub_name,
                            )
                        except Exception:
                            continue
                self.store.clear_stale_pr_snapshots(project_id, active_pr_nums)
            except RateLimitError as exc:
                self.store.update_project_sync(
                    project_id, "rate_limited", str(exc), rate_limited_until=exc.reset_at
                )

            return main_snapshot

    def _export_ref_snapshot(
        self,
        project_id: int,
        proj: dict[str, Any],
        adapter: HostAdapter,
        mirror_dir: Path,
        ref_name: str,
        fetch_spec: str,
        export_paths: list[str],
        user_token: str = "",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="gw-wt-") as tmp_root:
            wt_dir = Path(tmp_root) / "main"
            sha = self._fetch_and_checkout_worktree(mirror_dir, fetch_spec, wt_dir, export_paths)
            sub_worktrees: list[tuple[str, Path]] = []
            sub_mirrors_cleanup: list[tuple[Path, Path]] = []
            sub_errors: dict[str, str] = {}

            try:
                gm_path = wt_dir / ".gitmodules"
                if gm_path.is_file():
                    entries = _parse_gitmodules_entries(
                        gm_path.read_text(encoding="utf-8", errors="replace"),
                        proj["repo_full_name"],
                    )
                    for entry in entries:
                        sub_name = entry["name"]
                        sub_full = entry["repo_full_name"]
                        try:
                            sub_info = adapter.get_repo_info(sub_full, user_token=user_token)
                            sub_url = adapter.get_clone_url_with_auth(sub_full, user_token=user_token)
                            sub_mirror = self.mirror_dir_for(project_id, sub_name=sub_name)
                            self.ensure_mirror(sub_mirror, sub_url, export_paths)
                            sub_wt = Path(tmp_root) / f"sub_{sub_name}"
                            sub_branch = sub_info.default_branch or "main"
                            self._fetch_and_checkout_worktree(
                                sub_mirror,
                                f"+refs/heads/{sub_branch}:refs/remotes/origin/{sub_branch}",
                                sub_wt,
                                export_paths,
                            )
                            sub_worktrees.append((sub_name, sub_wt))
                            sub_mirrors_cleanup.append((sub_mirror, sub_wt))
                        except Exception as exc:
                            # E14: capture per-repo failure without crashing workspace export
                            sub_errors[sub_name] = str(exc)

                snap = run_export(wt_dir, ref_name=ref_name, sub_repos=sub_worktrees or None)
                if sub_errors:
                    snap.setdefault("sub_repo_errors", {}).update(sub_errors)
                    for failed_name in sub_errors:
                        if failed_name not in snap.get("repos", []):
                            snap.setdefault("repos", []).append(failed_name)

                self.store.store_snapshot(
                    project_id=project_id,
                    ref_name=ref_name,
                    commit_sha=sha,
                    snapshot=snap,
                )
                return snap
            finally:
                for sm, sw in sub_mirrors_cleanup:
                    _run_git(sm, ["worktree", "remove", "--force", str(sw)])
                _run_git(mirror_dir, ["worktree", "remove", "--force", str(wt_dir)])

    def _export_pr_snapshot(
        self,
        project_id: int,
        proj: dict[str, Any],
        adapter: HostAdapter,
        mirror_dir: Path,
        pr: PullRequestInfo,
        export_paths: list[str],
        user_token: str = "",
        sub_name: str = "",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="gw-pr-") as tmp_root:
            wt_dir = Path(tmp_root) / "pr"
            fetch_spec = f"+refs/heads/{pr.head_ref}:refs/remotes/origin/{pr.head_ref}"
            try:
                sha = self._fetch_and_checkout_worktree(mirror_dir, fetch_spec, wt_dir, export_paths)
            except HostError:
                sha = self._fetch_and_checkout_worktree(
                    mirror_dir, f"+refs/pull/{pr.number}/head:refs/remotes/origin/pr-{pr.number}", wt_dir, export_paths
                )
            try:
                snap = run_export(wt_dir, ref_name=pr.head_ref)
                if sub_name:
                    for it in snap.get("items") or []:
                        if not str(it.get("id", "")).startswith(f"{sub_name}/"):
                            it["id"] = f"{sub_name}/{it['id']}"
                        it["repo"] = sub_name
                stored_ref = f"{sub_name}:{pr.head_ref}" if sub_name else pr.head_ref
                self.store.store_snapshot(
                    project_id=project_id,
                    ref_name=stored_ref,
                    commit_sha=sha,
                    snapshot=snap,
                    pr_number=pr.number,
                    pr_title=pr.title,
                    pr_url=pr.web_url,
                )
                return snap
            finally:
                _run_git(mirror_dir, ["worktree", "remove", "--force", str(wt_dir)])
