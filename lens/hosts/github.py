"""GitHub and GitHub Enterprise Server host adapter."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
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


def is_publicly_reachable(url: str) -> bool:
    """True when GitHub's servers could plausibly reach ``url``.

    GitHub rejects an App manifest whose webhook URL points at localhost or a
    private network ("Hook url is not supported because it isn't reachable over
    the public Internet"), even when the webhook is inactive.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    if "." not in host and ":" not in host:
        return False  # single-label intranet name
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def build_app_manifest(
    public_url: str,
    app_name: str = "GroundWork Lens",
    webhook_active: bool = False,
    public: bool = False,
) -> dict[str, Any]:
    """Build GitHub App Manifest payload matching Section 10.4.

    ``public`` lets accounts other than the app owner install the app (needed
    when people sign in with, or keep repos under, a different GitHub account).
    The webhook is only included when ``public_url`` is publicly reachable.
    """
    base = public_url.rstrip("/")
    callback_urls = [f"{base}/auth/github/callback"]
    local_fallback = "http://localhost:8080/auth/github/callback"
    if local_fallback not in callback_urls:
        callback_urls.append(local_fallback)
    manifest: dict[str, Any] = {
        "name": app_name,
        "url": base,
        "redirect_url": f"{base}/setup/github/manifest-callback",
        "callback_urls": callback_urls,
        "request_oauth_on_install": False,
        "public": bool(public),
        "default_permissions": {
            "contents": "write",
            "metadata": "read",
            "pull_requests": "write",
            "emails": "read",
        },
        "default_events": [],
    }
    if is_publicly_reachable(base):
        manifest["hook_attributes"] = {"url": f"{base}/webhooks/github", "active": bool(webhook_active)}
        if webhook_active:
            manifest["default_events"] = ["push", "pull_request"]
    return manifest


def app_install_url(base_url: str, slug: str) -> str:
    """Where an owner adds repositories to (or installs) the Lens GitHub App."""
    base = base_url.rstrip("/")
    if slug:
        return f"{base}/apps/{slug}/installations/new"
    return f"{base}/settings/installations"


class AppNotInstalledError(AuthError):
    """The Lens GitHub App exists but is not installed on the requested repository."""


class GitHubAdapter(HostAdapter):
    """Host adapter for GitHub.com and GitHub Enterprise Server."""

    kind = "github"

    def __init__(
        self,
        base_url: str = "https://github.com",
        api_url: str = "https://api.github.com",
        sign_in_mode: str = "A",
        credentials: dict[str, Any] | None = None,
        service_token: str = "",
        service_token_expires_at: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.sign_in_mode = sign_in_mode
        self.credentials = credentials or {}
        self.service_token = service_token
        self.service_token_expires_at = service_token_expires_at
        self.http = session or requests.Session()
        self._install_token_cache: dict[str, tuple[str, float]] = {}

    def _headers(self, token: str = "") -> dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GroundWork-Lens/1.0",
        }
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        return hdrs

    def _request(self, method: str, path_or_url: str, token: str = "", **kwargs: Any) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_url}{path_or_url}"
        resp = self.http.request(method, url, headers=self._headers(token), timeout=20, **kwargs)
        if resp.status_code in (403, 429):
            remaining = resp.headers.get("X-RateLimit-Remaining", "")
            retry_after = resp.headers.get("Retry-After", "")
            if resp.status_code == 429 or remaining == "0" or retry_after or "rate limit" in resp.text.lower():
                reset_hdr = resp.headers.get("X-RateLimit-Reset", "")
                reset_iso = ""
                if reset_hdr.isdigit():
                    reset_iso = datetime.fromtimestamp(int(reset_hdr), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                raise RateLimitError("GitHub rate limit", reset_at=reset_iso)
        return resp

    def exchange_manifest_code(self, code: str) -> dict[str, Any]:
        """Exchange temporary manifest code for GitHub App credentials."""
        resp = self._request("POST", f"/app-manifests/{code}/conversions")
        if resp.status_code not in (200, 201):
            raise HostError(f"GitHub App manifest conversion failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        return {
            "app_id": str(data.get("id", "")),
            "slug": data.get("slug", ""),
            "client_id": data.get("client_id", ""),
            "client_secret": data.get("client_secret", ""),
            "webhook_secret": data.get("webhook_secret", ""),
            "private_key": data.get("pem", ""),
        }

    def _app_jwt(self) -> str:
        app_id = self.credentials.get("app_id", "")
        pem = self.credentials.get("private_key", "")
        if not app_id or not pem:
            raise AuthError("GitHub App ID and private key are not configured")
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
        tok = jwt.encode(payload, pem, algorithm="RS256")
        return tok if isinstance(tok, str) else tok.decode("ascii")

    def get_installation_token(self, repo_full_name: str = "") -> str:
        """Obtain a short-lived GitHub App installation access token."""
        if self.service_token and self.sign_in_mode in ("B", "C"):
            return self.service_token
        if not self.credentials.get("app_id") or not self.credentials.get("private_key"):
            return self.service_token

        cache_key = repo_full_name or "_default"
        cached = self._install_token_cache.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

        app_jwt = self._app_jwt()
        install_id = self.credentials.get("installation_id")
        if repo_full_name:
            r = self._request("GET", f"/repos/{repo_full_name}/installation", token=app_jwt)
            if r.status_code == 200:
                install_id = r.json().get("id")
            elif r.status_code == 404:
                # Do not fall back to some other installation: its token cannot
                # see this repo either, and the resulting 404 hides the real cause.
                raise AppNotInstalledError(
                    f"The GroundWork Lens GitHub App is not installed on {repo_full_name}. "
                    f"Add this repository to the app's repository access: "
                    f"{app_install_url(self.base_url, str(self.credentials.get('slug') or ''))}"
                )
        if not install_id:
            r = self._request("GET", "/app/installations", token=app_jwt)
            if r.status_code == 200 and r.json():
                install_id = r.json()[0].get("id")
        if not install_id:
            raise AuthError("GitHub App is not installed on any repositories yet")

        tr = self._request("POST", f"/app/installations/{install_id}/access_tokens", token=app_jwt)
        if tr.status_code not in (200, 201):
            raise AuthError(f"Could not create installation token ({tr.status_code})")
        tok = tr.json()["token"]
        self._install_token_cache[cache_key] = (tok, time.time() + 3000)
        return tok

    def _read_token(self, repo_full_name: str = "", user_token: str = "", raise_not_installed: bool = False) -> str:
        if self.sign_in_mode in ("B", "C"):
            return self.service_token
        if self.credentials.get("app_id") and self.credentials.get("private_key"):
            try:
                return self.get_installation_token(repo_full_name)
            except AppNotInstalledError:
                if raise_not_installed:
                    raise
            except AuthError:
                pass
        return user_token or self.service_token

    def authorize_url(self, public_url: str, state: str) -> str:
        client_id = self.credentials.get("client_id", "")
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": f"{public_url.rstrip('/')}/auth/github/callback",
            "state": state,
        }
        if self.sign_in_mode == "A":
            params["scope"] = "read:user user:email"
        return f"{self.base_url}/login/oauth/authorize?{urlencode(params)}"

    def exchange_oauth_code(self, code: str, public_url: str) -> str:
        resp = self.http.post(
            f"{self.base_url}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self.credentials.get("client_id", ""),
                "client_secret": self.credentials.get("client_secret", ""),
                "code": code,
                "redirect_uri": f"{public_url.rstrip('/')}/auth/github/callback",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise AuthError(f"GitHub OAuth token exchange failed ({resp.status_code})")
        data = resp.json()
        if "access_token" not in data:
            raise AuthError(data.get("error_description") or data.get("error") or "Missing access_token")
        return str(data["access_token"])

    def get_user_profile(self, token: str) -> UserProfile:
        resp = self._request("GET", "/user", token=token)
        if resp.status_code != 200:
            raise AuthError(f"Failed to read GitHub profile ({resp.status_code})")
        u = resp.json()
        emails: list[str] = []
        if u.get("email"):
            emails.append(str(u["email"]))
        er = self._request("GET", "/user/emails", token=token)
        if er.status_code == 200 and isinstance(er.json(), list):
            for e in er.json():
                if e.get("verified") and e.get("email") and e["email"] not in emails:
                    emails.append(str(e["email"]))
        return UserProfile(
            login=str(u.get("login") or ""),
            name=str(u.get("name") or u.get("login") or ""),
            verified_emails=emails,
            avatar_url=str(u.get("avatar_url") or ""),
        )

    def _to_repo_info(self, raw: dict[str, Any]) -> RepoInfo:
        perms = raw.get("permissions") or {}
        return RepoInfo(
            full_name=str(raw["full_name"]),
            name=str(raw["name"]),
            default_branch=str(raw.get("default_branch") or "main"),
            web_url=str(raw.get("html_url") or f"{self.base_url}/{raw['full_name']}"),
            clone_url=str(raw.get("clone_url") or f"{self.base_url}/{raw['full_name']}.git"),
            archived=bool(raw.get("archived", False)),
            empty=int(raw.get("size", 1)) == 0,
            private=bool(raw.get("private", False)),
            can_push=bool(perms.get("push", False) or perms.get("admin", False)),
        )

    def list_repos(self, user_token: str = "") -> list[RepoInfo]:
        if self.sign_in_mode == "A" and user_token:
            resp = self._request("GET", "/user/repos?per_page=100&sort=updated", token=user_token)
        elif self.credentials.get("app_id") and self.credentials.get("private_key"):
            tok = self.get_installation_token()
            resp = self._request("GET", "/installation/repositories?per_page=100", token=tok)
            if resp.status_code == 200:
                return [self._to_repo_info(r) for r in resp.json().get("repositories", [])]
            return []
        else:
            resp = self._request("GET", "/user/repos?per_page=100&sort=updated", token=self.service_token)
        if resp.status_code != 200:
            raise HostError(f"Failed to list GitHub repositories ({resp.status_code})")
        return [self._to_repo_info(r) for r in resp.json()]

    def get_repo_info(self, repo_full_name: str, user_token: str = "") -> RepoInfo:
        not_installed = ""
        try:
            tok = self._read_token(repo_full_name, user_token, raise_not_installed=True)
        except AppNotInstalledError as exc:
            not_installed = str(exc)
            tok = user_token or self.service_token
        resp = self._request("GET", f"/repos/{repo_full_name}", token=tok)
        if resp.status_code == 404:
            raise HostError(not_installed or f"Repository {repo_full_name} not found or inaccessible")
        if resp.status_code != 200:
            raise HostError(f"Failed to read {repo_full_name} ({resp.status_code})")
        info = self._to_repo_info(resp.json())
        # Attempt to read collaborators with maintain/admin permissions for N1 page
        try:
            cr = self._request("GET", f"/repos/{repo_full_name}/collaborators?permission=maintain&per_page=10", token=tok)
            if cr.status_code == 200 and isinstance(cr.json(), list):
                info.maintainers = [str(c["login"]) for c in cr.json() if c.get("login")]
        except HostError:
            pass
        return info

    def check_repo_access(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        if self.sign_in_mode == "C":
            return True
        if self.sign_in_mode == "A" and user_token:
            r = self._request("GET", f"/repos/{repo_full_name}", token=user_token)
            return r.status_code == 200
        if self.sign_in_mode == "B" and user_login:
            r = self._request("GET", f"/repos/{repo_full_name}/collaborators/{user_login}", token=self.service_token)
            if r.status_code == 204:
                return True
            info = self.get_repo_info(repo_full_name)
            return not info.private
        return False

    def user_can_push(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        if self.sign_in_mode == "A" and user_token:
            r = self._request("GET", f"/repos/{repo_full_name}", token=user_token)
            if r.status_code == 200:
                perms = r.json().get("permissions") or {}
                return bool(perms.get("push") or perms.get("admin") or perms.get("maintain"))
        if self.sign_in_mode == "B" and user_login and self.service_token:
            r = self._request("GET", f"/repos/{repo_full_name}/collaborators/{user_login}/permission", token=self.service_token)
            if r.status_code == 200:
                return r.json().get("permission") in ("admin", "maintain", "write")
        return False

    def list_open_prs(self, repo_full_name: str, user_token: str = "") -> list[PullRequestInfo]:
        tok = self._read_token(repo_full_name, user_token)
        resp = self._request("GET", f"/repos/{repo_full_name}/pulls?state=open&per_page=50", token=tok)
        if resp.status_code != 200:
            return []
        prs: list[PullRequestInfo] = []
        for raw in resp.json():
            num = int(raw["number"])
            fr = self._request("GET", f"/repos/{repo_full_name}/pulls/{num}/files?per_page=100", token=tok)
            files = [str(f["filename"]) for f in fr.json()] if fr.status_code == 200 else []
            prs.append(
                PullRequestInfo(
                    number=num,
                    title=str(raw.get("title") or f"PR #{num}"),
                    head_ref=str(raw["head"]["ref"]),
                    head_sha=str(raw["head"]["sha"]),
                    base_ref=str(raw["base"]["ref"]),
                    web_url=str(raw.get("html_url") or ""),
                    changed_files=files,
                )
            )
        return prs

    def get_clone_url_with_auth(self, repo_full_name: str, user_token: str = "", write: bool = False) -> str:
        tok = user_token if (write and user_token) else self._read_token(repo_full_name, user_token)
        clean_base = self.base_url.replace("https://", "").replace("http://", "")
        scheme = "http" if self.base_url.startswith("http://") else "https"
        if tok:
            return f"{scheme}://x-access-token:{tok}@{clean_base}/{repo_full_name}.git"
        return f"{scheme}://{clean_base}/{repo_full_name}.git"

    def get_web_file_url(self, repo_full_name: str, ref: str, rel_path: str) -> str:
        return f"{self.base_url}/{repo_full_name}/blob/{ref}/{rel_path.lstrip('/')}"

    def open_pr(
        self,
        repo_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        user_token: str = "",
    ) -> PullRequestInfo:
        tok = user_token or self._read_token(repo_full_name)
        resp = self._request(
            "POST",
            f"/repos/{repo_full_name}/pulls",
            token=tok,
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        )
        if resp.status_code not in (200, 201):
            raise HostError(f"Could not open pull request ({resp.status_code}): {resp.text}")
        raw = resp.json()
        return PullRequestInfo(
            number=int(raw["number"]),
            title=str(raw.get("title") or title),
            head_ref=head_branch,
            head_sha=str(raw.get("head", {}).get("sha") or ""),
            base_ref=base_branch,
            web_url=str(raw.get("html_url") or ""),
        )

    def comment_on_pr(self, repo_full_name: str, pr_number: int, body: str, user_token: str = "") -> str:
        tok = user_token or self._read_token(repo_full_name)
        resp = self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{pr_number}/comments",
            token=tok,
            json={"body": body},
        )
        if resp.status_code not in (200, 201):
            raise HostError(f"Could not post PR comment ({resp.status_code})")
        return str(resp.json().get("html_url") or "")

    def request_reviewers(self, repo_full_name: str, pr_number: int, logins: list[str], user_token: str = "") -> None:
        if self.sign_in_mode != "A" or not logins:
            return
        tok = user_token or self._read_token(repo_full_name)
        self._request(
            "POST",
            f"/repos/{repo_full_name}/pulls/{pr_number}/requested_reviewers",
            token=tok,
            json={"reviewers": logins},
        )

    def verify_webhook(self, headers: dict[str, str], payload: bytes, secret: str) -> bool:
        if not secret:
            return False
        sig_hdr = ""
        for k, v in headers.items():
            if k.lower() == "x-hub-signature-256":
                sig_hdr = v
                break
        if not sig_hdr.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hdr, expected)

    def health_check(self, public_url: str, sample_repo: str = "") -> HealthCheckResult:
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []
        ok = True

        # 1. Credential check
        try:
            tok = self._read_token(sample_repo)
            if tok:
                if sample_repo:
                    r = self._request("GET", f"/repos/{sample_repo}", token=tok)
                    passed = r.status_code == 200
                else:
                    r = self._request("GET", "/rate_limit", token=tok)
                    passed = r.status_code == 200
                checks.append(
                    {
                        "name": "GitHub credentials",
                        "ok": passed,
                        "detail": "Token accepted" if passed else f"HTTP {r.status_code}",
                    }
                )
                if not passed:
                    ok = False
            else:
                checks.append({"name": "GitHub credentials", "ok": False, "detail": "No token or app configured"})
                ok = False
        except Exception as exc:
            checks.append({"name": "GitHub credentials", "ok": False, "detail": str(exc)})
            ok = False

        # 2. Service token expiry (< 14 days warning, Section 10.5)
        if self.service_token_expires_at:
            try:
                exp_dt = datetime.fromisoformat(self.service_token_expires_at.replace("Z", "+00:00"))
                days_left = (exp_dt - datetime.now(timezone.utc)).days
                if days_left <= 14:
                    warnings.append(f"GitHub service token expires in {max(0, days_left)} day(s)")
            except ValueError:
                pass

        # 3. Callback URL reminder
        expected_cb = f"{public_url.rstrip('/')}/auth/github/callback"
        checks.append({"name": "Callback URL", "ok": True, "detail": expected_cb})

        return HealthCheckResult(ok=ok, checks=checks, warnings=warnings)
