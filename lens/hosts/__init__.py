"""Host adapter factory."""

from __future__ import annotations

from typing import Any

from lens.hosts.base import HostAdapter
from lens.hosts.github import GitHubAdapter
from lens.hosts.gitlab import GitLabAdapter
from lens.hosts.local_git import LocalGitAdapter


def adapter_for_host(host: dict[str, Any]) -> HostAdapter:
    """Instantiate the appropriate HostAdapter from a database host row."""
    kind = host["kind"]
    if kind == "github":
        return GitHubAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host.get("credentials") or {},
            service_token=host.get("service_token") or "",
            service_token_expires_at=host.get("service_token_expires_at") or "",
        )
    if kind == "gitlab":
        return GitLabAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host.get("credentials") or {},
            service_token=host.get("service_token") or "",
            service_token_expires_at=host.get("service_token_expires_at") or "",
        )
    if kind == "local":
        return LocalGitAdapter(root_dir=host["base_url"])
    raise ValueError(f"Unsupported host kind: {kind}")
