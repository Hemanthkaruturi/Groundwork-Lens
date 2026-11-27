"""Local filesystem git host adapter for Demo mode and end-to-end tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from lens.hosts.base import (
    HealthCheckResult,
    HostAdapter,
    HostError,
    PullRequestInfo,
    RepoInfo,
)


class LocalGitAdapter(HostAdapter):
    """Git host adapter backed by local bare/standard git repositories."""

    kind = "local"

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_file = self.root_dir / "_host_state.json"

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.is_file():
            return {"repos": {}, "prs": {}, "comments": [], "protected": {}, "read_only_users": {}, "unreadable": {}}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"repos": {}, "prs": {}, "comments": [], "protected": {}, "read_only_users": {}, "unreadable": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _repo_dir(self, repo_full_name: str) -> Path:
        candidate = self.root_dir / repo_full_name
        if candidate.exists():
            return candidate
        bare = self.root_dir / f"{repo_full_name}.git"
        if bare.exists():
            return bare
        raise HostError(f"Repository {repo_full_name} not found under {self.root_dir}")

    def _git(self, repo_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )

    def set_repo_meta(self, repo_full_name: str, **kwargs: Any) -> None:
        state = self._load_state()
        meta = state.setdefault("repos", {}).setdefault(repo_full_name, {})
        meta.update(kwargs)
        self._save_state(state)

    def set_branch_protected(self, repo_full_name: str, branch: str, protected: bool = True) -> None:
        state = self._load_state()
        state.setdefault("protected", {}).setdefault(repo_full_name, {})[branch] = protected
        self._save_state(state)

    def is_branch_protected(self, repo_full_name: str, branch: str) -> bool:
        state = self._load_state()
        return bool(state.get("protected", {}).get(repo_full_name, {}).get(branch, False))

    def set_user_read_only(self, repo_full_name: str, user_login: str, read_only: bool = True) -> None:
        state = self._load_state()
        ro_list = state.setdefault("read_only_users", {}).setdefault(repo_full_name, [])
        if read_only and user_login not in ro_list:
            ro_list.append(user_login)
        elif not read_only and user_login in ro_list:
            ro_list.remove(user_login)
        self._save_state(state)

    def register_pr(
        self,
        repo_full_name: str,
        head_ref: str,
        title: str,
        base_ref: str = "main",
        number: int | None = None,
    ) -> PullRequestInfo:
        state = self._load_state()
        prs = state.setdefault("prs", {}).setdefault(repo_full_name, [])
        next_num = number or (max([p["number"] for p in prs], default=0) + 1)
        repo_dir = self._repo_dir(repo_full_name)
        sha_res = self._git(repo_dir, ["rev-parse", head_ref])
        head_sha = sha_res.stdout.strip() if sha_res.returncode == 0 else ""
        diff_res = self._git(repo_dir, ["diff", "--name-only", f"{base_ref}..{head_ref}"])
        changed = [ln.strip() for ln in diff_res.stdout.splitlines() if ln.strip()] if diff_res.returncode == 0 else []
        entry = {
            "number": next_num,
            "title": title,
            "head_ref": head_ref,
            "head_sha": head_sha,
            "base_ref": base_ref,
            "web_url": f"http://localhost/local/{repo_full_name}/pull/{next_num}",
            "changed_files": changed,
        }
        prs.append(entry)
        self._save_state(state)
        return PullRequestInfo(**entry)

    def list_repos(self, user_token: str = "") -> list[RepoInfo]:
        repos: list[RepoInfo] = []
        if not self.root_dir.is_dir():
            return repos
        for entry in sorted(self.root_dir.iterdir()):
            if entry.name.startswith("_") or not entry.is_dir():
                continue
            if (entry / ".git").exists() or (entry / "HEAD").is_file():
                name = entry.name[:-4] if entry.name.endswith(".git") else entry.name
                repos.append(self.get_repo_info(name))
        return repos

    def get_repo_info(self, repo_full_name: str, user_token: str = "") -> RepoInfo:
        state = self._load_state()
        unreadable_err = state.get("unreadable", {}).get(repo_full_name)
        if unreadable_err:
            raise HostError(str(unreadable_err))
        meta = state.get("repos", {}).get(repo_full_name, {})
        repo_dir = self._repo_dir(repo_full_name)
        head_res = self._git(repo_dir, ["rev-parse", "--verify", "HEAD"])
        is_empty = head_res.returncode != 0
        default_branch = str(meta.get("default_branch") or "main")
        if not is_empty:
            br = self._git(repo_dir, ["symbolic-ref", "--short", "HEAD"])
            if br.returncode == 0 and br.stdout.strip():
                default_branch = br.stdout.strip()
        return RepoInfo(
            full_name=repo_full_name,
            name=repo_full_name.split("/")[-1],
            default_branch=default_branch,
            web_url=f"http://localhost/local/{repo_full_name}",
            clone_url=str(repo_dir),
            archived=bool(meta.get("archived", False)),
            empty=bool(meta.get("empty", is_empty)),
            private=False,
            can_push=True,
            maintainers=list(meta.get("maintainers", ["Hemanth Karuturi"])),
        )

    def check_repo_access(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        state = self._load_state()
        if repo_full_name in state.get("unreadable", {}):
            return False
        try:
            self._repo_dir(repo_full_name)
            return True
        except HostError:
            return False

    def user_can_push(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        state = self._load_state()
        ro_users = [u.lower() for u in state.get("read_only_users", {}).get(repo_full_name, [])]
        return user_login.lower() not in ro_users

    def list_open_prs(self, repo_full_name: str, user_token: str = "") -> list[PullRequestInfo]:
        state = self._load_state()
        raw_prs = state.get("prs", {}).get(repo_full_name, [])
        repo_dir = self._repo_dir(repo_full_name)
        out: list[PullRequestInfo] = []
        for p in raw_prs:
            sha_res = self._git(repo_dir, ["rev-parse", p["head_ref"]])
            head_sha = sha_res.stdout.strip() if sha_res.returncode == 0 else p.get("head_sha", "")
            diff_res = self._git(repo_dir, ["diff", "--name-only", f"{p['base_ref']}..{p['head_ref']}"])
            changed = [ln.strip() for ln in diff_res.stdout.splitlines() if ln.strip()] if diff_res.returncode == 0 else p.get("changed_files", [])
            out.append(
                PullRequestInfo(
                    number=int(p["number"]),
                    title=str(p["title"]),
                    head_ref=str(p["head_ref"]),
                    head_sha=head_sha,
                    base_ref=str(p["base_ref"]),
                    web_url=str(p.get("web_url") or f"http://localhost/local/{repo_full_name}/pull/{p['number']}"),
                    changed_files=changed,
                )
            )
        return out

    def get_clone_url_with_auth(self, repo_full_name: str, user_token: str = "", write: bool = False) -> str:
        return str(self._repo_dir(repo_full_name))

    def get_web_file_url(self, repo_full_name: str, ref: str, rel_path: str) -> str:
        return f"http://localhost/local/{repo_full_name}/blob/{ref}/{rel_path.lstrip('/')}"

    def open_pr(
        self,
        repo_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        user_token: str = "",
    ) -> PullRequestInfo:
        return self.register_pr(repo_full_name, head_branch, title, base_ref=base_branch)

    def comment_on_pr(self, repo_full_name: str, pr_number: int, body: str, user_token: str = "") -> str:
        state = self._load_state()
        state.setdefault("comments", []).append(
            {"repo": repo_full_name, "pr_number": pr_number, "body": body}
        )
        self._save_state(state)
        return f"http://localhost/local/{repo_full_name}/pull/{pr_number}#comment-{len(state['comments'])}"

    def request_reviewers(self, repo_full_name: str, pr_number: int, logins: list[str], user_token: str = "") -> None:
        state = self._load_state()
        state.setdefault("reviewer_requests", []).append(
            {"repo": repo_full_name, "pr_number": pr_number, "logins": logins}
        )
        self._save_state(state)

    def verify_webhook(self, headers: dict[str, str], payload: bytes, secret: str) -> bool:
        return True

    def health_check(self, public_url: str, sample_repo: str = "") -> HealthCheckResult:
        return HealthCheckResult(
            ok=True,
            checks=[{"name": "Local git host", "ok": True, "detail": str(self.root_dir)}],
        )
