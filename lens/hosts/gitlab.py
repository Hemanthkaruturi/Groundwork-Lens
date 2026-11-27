"""GitLab.com and self-managed GitLab host adapter."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

import requests

from lens.hosts.base import (
    AuthError,
    HealthCheckResult,
    HostAdapter,
    HostError,
    PullRequestInfo,
    RateLimitError,
    RepoInfo,
    UserProfile,
)


class GitLabAdapter(HostAdapter):
    """Host adapter for GitLab.com and self-managed GitLab instances."""

    kind = "gitlab"

    def __init__(
        self,
        base_url: str = "https://gitlab.com",
        api_url: str = "",
        sign_in_mode: str = "A",
        credentials: dict[str, Any] | None = None,
        service_token: str = "",
        service_token_expires_at: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = (api_url or f"{self.base_url}/api/v4").rstrip("/")
        self.sign_in_mode = sign_in_mode
        self.credentials = credentials or {}
        self.service_token = service_token
        self.service_token_expires_at = service_token_expires_at
        self.http = session or requests.Session()

    def _headers(self, token: str = "", is_oauth: bool = False) -> dict[str, str]:
        hdrs = {"User-Agent": "GroundWork-Lens/1.0"}
        if token:
            if is_oauth:
                hdrs["Authorization"] = f"Bearer {token}"
            else:
                hdrs["PRIVATE-TOKEN"] = token
        return hdrs

    def _request(
        self,
        method: str,
        path: str,
        token: str = "",
        is_oauth: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        resp = self.http.request(method, url, headers=self._headers(token, is_oauth=is_oauth), timeout=20, **kwargs)
        if resp.status_code == 429:
            reset_iso = ""
            ra = resp.headers.get("Retry-After", "")
            if ra.isdigit():
                reset_iso = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()) + int(ra), tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            raise RateLimitError("GitLab rate limit", reset_at=reset_iso)
        return resp

    def _encode_proj(self, repo_full_name: str) -> str:
        return quote(repo_full_name, safe="")

    def _read_auth(self, user_token: str = "") -> tuple[str, bool]:
        if self.service_token:
            return self.service_token, False
        return user_token, bool(user_token)

    def authorize_url(self, public_url: str, state: str) -> str:
        scope = "read_user read_api" if self.sign_in_mode == "A" else "read_user"
        params = {
            "client_id": self.credentials.get("client_id", ""),
            "redirect_uri": f"{public_url.rstrip('/')}/auth/gitlab/callback",
            "response_type": "code",
            "state": state,
            "scope": scope,
        }
        return f"{self.base_url}/oauth/authorize?{urlencode(params)}"

    def exchange_oauth_code(self, code: str, public_url: str) -> str:
        resp = self.http.post(
            f"{self.base_url}/oauth/token",
            data={
                "client_id": self.credentials.get("client_id", ""),
                "client_secret": self.credentials.get("client_secret", ""),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{public_url.rstrip('/')}/auth/gitlab/callback",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise AuthError(f"GitLab OAuth exchange failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        if "access_token" not in data:
            raise AuthError("GitLab OAuth response missing access_token")
        return str(data["access_token"])

    def get_user_profile(self, token: str, is_oauth: bool = True) -> UserProfile:
        resp = self._request("GET", "/user", token=token, is_oauth=is_oauth)
        if resp.status_code != 200:
            raise AuthError(f"Failed to read GitLab user profile ({resp.status_code})")
        u = resp.json()
        emails: list[str] = []
        if u.get("email"):
            emails.append(str(u["email"]))
        er = self._request("GET", "/user/emails", token=token, is_oauth=is_oauth)
        if er.status_code == 200 and isinstance(er.json(), list):
            for e in er.json():
                if e.get("confirmed_at") and e.get("email") and e["email"] not in emails:
                    emails.append(str(e["email"]))
        return UserProfile(
            login=str(u.get("username") or ""),
            name=str(u.get("name") or u.get("username") or ""),
            verified_emails=emails,
            avatar_url=str(u.get("avatar_url") or ""),
        )

    def _to_repo_info(self, raw: dict[str, Any]) -> RepoInfo:
        perms = raw.get("permissions") or {}
        proj_acc = (perms.get("project_access") or {}).get("access_level", 0)
        grp_acc = (perms.get("group_access") or {}).get("access_level", 0)
        can_push = max(int(proj_acc or 0), int(grp_acc or 0)) >= 30
        return RepoInfo(
            full_name=str(raw["path_with_namespace"]),
            name=str(raw["name"]),
            default_branch=str(raw.get("default_branch") or "main"),
            web_url=str(raw.get("web_url") or f"{self.base_url}/{raw['path_with_namespace']}"),
            clone_url=str(raw.get("http_url_to_repo") or f"{self.base_url}/{raw['path_with_namespace']}.git"),
            archived=bool(raw.get("archived", False)),
            empty=bool(raw.get("empty_repo", False)),
            private=str(raw.get("visibility") or "private") != "public",
            can_push=can_push,
        )

    def list_repos(self, user_token: str = "") -> list[RepoInfo]:
        if self.sign_in_mode == "A" and user_token:
            tok, is_oauth = user_token, True
        else:
            tok, is_oauth = self.service_token, False
        resp = self._request("GET", "/projects?membership=true&per_page=100&order_by=last_activity_at", token=tok, is_oauth=is_oauth)
        if resp.status_code != 200:
            raise HostError(f"Failed to list GitLab projects ({resp.status_code})")
        return [self._to_repo_info(p) for p in resp.json()]

    def get_repo_info(self, repo_full_name: str, user_token: str = "") -> RepoInfo:
        tok, is_oauth = self._read_auth(user_token)
        pid = self._encode_proj(repo_full_name)
        resp = self._request("GET", f"/projects/{pid}", token=tok, is_oauth=is_oauth)
        if resp.status_code == 404:
            raise HostError(f"Project {repo_full_name} not found or inaccessible")
        if resp.status_code != 200:
            raise HostError(f"Failed to read GitLab project {repo_full_name} ({resp.status_code})")
        info = self._to_repo_info(resp.json())
        try:
            mr = self._request("GET", f"/projects/{pid}/members/all?per_page=20", token=tok, is_oauth=is_oauth)
            if mr.status_code == 200 and isinstance(mr.json(), list):
                info.maintainers = [
                    str(m["username"]) for m in mr.json() if int(m.get("access_level") or 0) >= 40 and m.get("username")
                ]
        except HostError:
            pass
        return info

    def check_repo_access(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        if self.sign_in_mode == "C":
            return True
        pid = self._encode_proj(repo_full_name)
        if self.sign_in_mode == "A" and user_token:
            r = self._request("GET", f"/projects/{pid}", token=user_token, is_oauth=True)
            return r.status_code == 200
        if self.sign_in_mode == "B" and user_login:
            r = self._request("GET", f"/projects/{pid}/members/all?query={quote(user_login)}", token=self.service_token)
            if r.status_code == 200:
                if any(str(m.get("username") or "").lower() == user_login.lower() for m in r.json()):
                    return True
            info = self.get_repo_info(repo_full_name)
            return not info.private
        return False

    def user_can_push(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        pid = self._encode_proj(repo_full_name)
        if self.sign_in_mode == "A" and user_token:
            r = self._request("GET", f"/projects/{pid}", token=user_token, is_oauth=True)
            if r.status_code == 200:
                return self._to_repo_info(r.json()).can_push
        if self.sign_in_mode == "B" and user_login and self.service_token:
            r = self._request("GET", f"/projects/{pid}/members/all?query={quote(user_login)}", token=self.service_token)
            if r.status_code == 200:
                for m in r.json():
                    if str(m.get("username") or "").lower() == user_login.lower():
                        return int(m.get("access_level") or 0) >= 30
        return False

    def list_open_prs(self, repo_full_name: str, user_token: str = "") -> list[PullRequestInfo]:
        tok, is_oauth = self._read_auth(user_token)
        pid = self._encode_proj(repo_full_name)
        resp = self._request("GET", f"/projects/{pid}/merge_requests?state=opened&per_page=50", token=tok, is_oauth=is_oauth)
        if resp.status_code != 200:
            return []
        mrs: list[PullRequestInfo] = []
        for raw in resp.json():
            iid = int(raw["iid"])
            cr = self._request("GET", f"/projects/{pid}/merge_requests/{iid}/changes", token=tok, is_oauth=is_oauth)
            files = [str(c.get("new_path") or c.get("old_path")) for c in cr.json().get("changes", [])] if cr.status_code == 200 else []
            mrs.append(
                PullRequestInfo(
                    number=iid,
                    title=str(raw.get("title") or f"MR !{iid}"),
                    head_ref=str(raw["source_branch"]),
                    head_sha=str(raw.get("sha") or ""),
                    base_ref=str(raw.get("target_branch") or "main"),
                    web_url=str(raw.get("web_url") or ""),
                    changed_files=files,
                )
            )
        return mrs

    def get_clone_url_with_auth(self, repo_full_name: str, user_token: str = "", write: bool = False) -> str:
        tok = user_token if (write and user_token) else self.service_token or user_token
        clean_base = self.base_url.replace("https://", "").replace("http://", "")
        scheme = "http" if self.base_url.startswith("http://") else "https"
        if tok:
            return f"{scheme}://oauth2:{tok}@{clean_base}/{repo_full_name}.git"
        return f"{scheme}://{clean_base}/{repo_full_name}.git"

    def get_web_file_url(self, repo_full_name: str, ref: str, rel_path: str) -> str:
        return f"{self.base_url}/{repo_full_name}/-/blob/{ref}/{rel_path.lstrip('/')}"

    def open_pr(
        self,
        repo_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        user_token: str = "",
    ) -> PullRequestInfo:
        tok, is_oauth = (user_token, True) if user_token else (self.service_token, False)
        pid = self._encode_proj(repo_full_name)
        resp = self._request(
            "POST",
            f"/projects/{pid}/merge_requests",
            token=tok,
            is_oauth=is_oauth,
            json={"source_branch": head_branch, "target_branch": base_branch, "title": title, "description": body},
        )
        if resp.status_code not in (200, 201):
            raise HostError(f"Could not open merge request ({resp.status_code}): {resp.text}")
        raw = resp.json()
        return PullRequestInfo(
            number=int(raw["iid"]),
            title=str(raw.get("title") or title),
            head_ref=head_branch,
            head_sha=str(raw.get("sha") or ""),
            base_ref=base_branch,
            web_url=str(raw.get("web_url") or ""),
        )

    def comment_on_pr(self, repo_full_name: str, pr_number: int, body: str, user_token: str = "") -> str:
        tok, is_oauth = (user_token, True) if user_token else (self.service_token, False)
        pid = self._encode_proj(repo_full_name)
        resp = self._request(
            "POST",
            f"/projects/{pid}/merge_requests/{pr_number}/notes",
            token=tok,
            is_oauth=is_oauth,
            json={"body": body},
        )
        if resp.status_code not in (200, 201):
            raise HostError(f"Could not comment on merge request ({resp.status_code})")
        return f"{self.base_url}/{repo_full_name}/-/merge_requests/{pr_number}"

    def request_reviewers(self, repo_full_name: str, pr_number: int, logins: list[str], user_token: str = "") -> None:
        if self.sign_in_mode != "A" or not logins:
            return
        tok, is_oauth = (user_token, True) if user_token else (self.service_token, False)
        pid = self._encode_proj(repo_full_name)
        user_ids: list[int] = []
        for login in logins:
            ur = self._request("GET", f"/users?username={quote(login)}", token=tok, is_oauth=is_oauth)
            if ur.status_code == 200 and ur.json():
                user_ids.append(int(ur.json()[0]["id"]))
        if user_ids:
            self._request(
                "PUT",
                f"/projects/{pid}/merge_requests/{pr_number}",
                token=tok,
                is_oauth=is_oauth,
                json={"reviewer_ids": user_ids},
            )

    def verify_webhook(self, headers: dict[str, str], payload: bytes, secret: str) -> bool:
        if not secret:
            return False
        tok = ""
        for k, v in headers.items():
            if k.lower() == "x-gitlab-token":
                tok = v
                break
        return bool(tok) and hmac.compare_digest(tok, secret)

    def health_check(self, public_url: str, sample_repo: str = "") -> HealthCheckResult:
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []
        ok = True

        if self.service_token:
            try:
                r = self._request("GET", "/personal_access_tokens/self", token=self.service_token)
                if r.status_code == 200:
                    data = r.json()
                    scopes = data.get("scopes", [])
                    checks.append({"name": "GitLab service token", "ok": True, "detail": f"Scopes: {', '.join(scopes)}"})
                    exp = data.get("expires_at") or self.service_token_expires_at
                    if exp:
                        exp_dt = datetime.fromisoformat(f"{exp}T00:00:00+00:00") if len(exp) == 10 else datetime.fromisoformat(exp.replace("Z", "+00:00"))
                        days_left = (exp_dt - datetime.now(timezone.utc)).days
                        if days_left <= 14:
                            warnings.append(f"GitLab service token expires in {max(0, days_left)} day(s)")
                else:
                    checks.append({"name": "GitLab service token", "ok": False, "detail": f"HTTP {r.status_code}"})
                    ok = False
            except Exception as exc:
                checks.append({"name": "GitLab service token", "ok": False, "detail": str(exc)})
                ok = False
        else:
            checks.append({"name": "GitLab service token", "ok": False, "detail": "Not configured"})
            ok = False

        if self.sign_in_mode == "A" and not self.credentials.get("client_id"):
            checks.append({"name": "GitLab OAuth app", "ok": False, "detail": "Client ID missing"})
            ok = False
        else:
            expected_cb = f"{public_url.rstrip('/')}/auth/gitlab/callback"
            checks.append({"name": "Callback URL", "ok": True, "detail": expected_cb})

        return HealthCheckResult(ok=ok, checks=checks, warnings=warnings)
