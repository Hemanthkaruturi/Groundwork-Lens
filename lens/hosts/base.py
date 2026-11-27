"""Host adapter base interfaces and shared dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class HostError(RuntimeError):
    """Base error for host adapter operations."""


class RateLimitError(HostError):
    """Raised when GitHub or GitLab returns HTTP 403/429 rate-limit response."""

    def __init__(self, message: str, reset_at: str = "") -> None:
        super().__init__(message)
        self.reset_at = reset_at


class PushRejectedError(HostError):
    """Raised when a push is rejected (e.g. protected branch or permission denied)."""


class AuthError(HostError):
    """Raised when authentication or token verification fails."""


@dataclass
class RepoInfo:
    full_name: str
    name: str
    default_branch: str
    web_url: str
    clone_url: str
    archived: bool = False
    empty: bool = False
    private: bool = True
    can_push: bool = False
    maintainers: list[str] = field(default_factory=list)


@dataclass
class PullRequestInfo:
    number: int
    title: str
    head_ref: str
    head_sha: str
    base_ref: str
    web_url: str
    changed_files: list[str] = field(default_factory=list)


@dataclass
class UserProfile:
    login: str
    name: str
    verified_emails: list[str] = field(default_factory=list)
    avatar_url: str = ""


@dataclass
class HealthCheckResult:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class HostAdapter(ABC):
    """Abstract interface implemented by GitHub, GitLab, and LocalGit adapters."""

    kind: str = "unknown"

    @abstractmethod
    def list_repos(self, user_token: str = "") -> list[RepoInfo]:
        """List repositories visible to the current user or service token."""

    @abstractmethod
    def get_repo_info(self, repo_full_name: str, user_token: str = "") -> RepoInfo:
        """Fetch repository metadata including default branch and archived/empty state."""

    @abstractmethod
    def check_repo_access(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        """Verify whether the user can currently read the repository."""

    @abstractmethod
    def user_can_push(self, repo_full_name: str, user_login: str, user_token: str = "") -> bool:
        """Return True if the user can push directly to the repository."""

    @abstractmethod
    def list_open_prs(self, repo_full_name: str, user_token: str = "") -> list[PullRequestInfo]:
        """List open pull/merge requests with head branch, SHA, and changed files."""

    @abstractmethod
    def get_clone_url_with_auth(self, repo_full_name: str, user_token: str = "", write: bool = False) -> str:
        """Return an authenticated HTTPS/file URL suitable for git clone/fetch/push."""

    @abstractmethod
    def get_web_file_url(self, repo_full_name: str, ref: str, rel_path: str) -> str:
        """Return a browser URL to view a file on the host."""

    @abstractmethod
    def open_pr(
        self,
        repo_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        user_token: str = "",
    ) -> PullRequestInfo:
        """Open a pull request or merge request."""

    @abstractmethod
    def comment_on_pr(self, repo_full_name: str, pr_number: int, body: str, user_token: str = "") -> str:
        """Post a comment on an open pull/merge request and return its URL."""

    @abstractmethod
    def request_reviewers(self, repo_full_name: str, pr_number: int, logins: list[str], user_token: str = "") -> None:
        """Request review from host usernames if supported."""

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], payload: bytes, secret: str) -> bool:
        """Verify webhook signature or token."""

    @abstractmethod
    def health_check(self, public_url: str, sample_repo: str = "") -> HealthCheckResult:
        """Run diagnostic checks on host credentials, scopes, expiry, and callback URL."""
