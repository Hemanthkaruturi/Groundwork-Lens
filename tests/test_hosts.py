"""Unit tests for GitHub and GitLab host adapters using mocked HTTP responses."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from lens.hosts.base import HostError, RateLimitError
from lens.hosts.github import AppNotInstalledError, GitHubAdapter, build_app_manifest, is_publicly_reachable
from lens.hosts.gitlab import GitLabAdapter


def _fake_resp(status: int, json_data: object = None, headers: dict[str, str] | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text or (json.dumps(json_data) if json_data is not None else "")
    r.json.return_value = json_data
    return r


def _generate_test_rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


class TestHostAdapters(unittest.TestCase):
    def test_github_manifest_oauth_installation_prs_rate_limit_and_webhook(self) -> None:
        manifest = build_app_manifest("https://lens.example.com", app_name="Acme GroundWork Lens", webhook_active=True)
        self.assertEqual(manifest["redirect_url"], "https://lens.example.com/setup/github/manifest-callback")
        self.assertEqual(manifest["default_permissions"]["contents"], "write")
        self.assertTrue(manifest["hook_attributes"]["active"])

        sess = MagicMock()
        pem = _generate_test_rsa_pem()
        sess.request.side_effect = [
            # exchange_manifest_code
            _fake_resp(201, {"id": 99, "slug": "acme-lens", "client_id": "cid", "client_secret": "csec", "webhook_secret": "wsec", "pem": pem}),
            # get_user_profile (/user, /user/emails)
            _fake_resp(200, {"login": "amitsingh", "name": "Amit Singh", "email": ""}),
            _fake_resp(200, [{"email": "amit@acme.com", "verified": True}]),
            # list_open_prs -> get_installation_token (/repos/.../installation, /app/installations/.../access_tokens), pulls, files
            _fake_resp(200, {"id": 500}),
            _fake_resp(201, {"token": "ghs_install_tok"}),
            _fake_resp(200, [{"number": 42, "title": "Add RFC", "head": {"ref": "rfc-42", "sha": "abc123"}, "base": {"ref": "main"}, "html_url": "https://github.com/org/repo/pull/42"}]),
            _fake_resp(200, [{"filename": "DECISIONS/RFC-0042.md"}]),
            # rate limit 403
            _fake_resp(403, {"message": "API rate limit exceeded"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"}),
        ]

        gh = GitHubAdapter(session=sess)
        creds = gh.exchange_manifest_code("temp-code")
        self.assertEqual(creds["app_id"], "99")
        gh.credentials = creds

        prof = gh.get_user_profile("user-tok")
        self.assertEqual(prof.login, "amitsingh")
        self.assertEqual(prof.verified_emails, ["amit@acme.com"])

        prs = gh.list_open_prs("org/repo")
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0].number, 42)
        self.assertEqual(prs[0].changed_files, ["DECISIONS/RFC-0042.md"])

        with self.assertRaises(RateLimitError):
            gh.get_repo_info("org/repo")

        payload = b'{"repository":{"full_name":"org/repo"}}'
        sig = "sha256=" + hmac.new(b"wsec", payload, hashlib.sha256).hexdigest()
        self.assertTrue(gh.verify_webhook({"X-Hub-Signature-256": sig}, payload, "wsec"))
        self.assertFalse(gh.verify_webhook({"X-Hub-Signature-256": "sha256=bad"}, payload, "wsec"))

    def test_github_manifest_for_local_address_omits_webhook(self) -> None:
        # GitHub rejects manifests whose hook URL is localhost, even when inactive.
        manifest = build_app_manifest("http://localhost:8080", webhook_active=False)
        self.assertNotIn("hook_attributes", manifest)
        self.assertEqual(manifest["default_events"], [])
        self.assertEqual(manifest["callback_urls"], ["http://localhost:8080/auth/github/callback"])
        self.assertFalse(manifest["public"])

        public = build_app_manifest("https://lens.example.com", public=True)
        self.assertTrue(public["public"])
        self.assertEqual(public["hook_attributes"], {"url": "https://lens.example.com/webhooks/github", "active": False})
        self.assertEqual(len(public["callback_urls"]), len(set(public["callback_urls"])))

    def test_is_publicly_reachable(self) -> None:
        for url in ("http://localhost:8080", "http://127.0.0.1:8080", "http://10.0.0.5", "http://192.168.1.20:8080",
                    "http://lens.local", "http://lens", "http://[::1]:8080"):
            self.assertFalse(is_publicly_reachable(url), url)
        for url in ("https://lens.example.com", "https://8.8.8.8"):
            self.assertTrue(is_publicly_reachable(url), url)

    def test_github_repo_without_app_installation_explains_how_to_fix(self) -> None:
        sess = MagicMock()
        sess.request.side_effect = [
            _fake_resp(404, {"message": "Not Found"}),  # /repos/org/sub/installation
            _fake_resp(404, {"message": "Not Found"}),  # /repos/org/sub (no token)
        ]
        gh = GitHubAdapter(
            session=sess,
            credentials={"app_id": "1", "private_key": _generate_test_rsa_pem(), "slug": "acme-lens"},
        )
        with self.assertRaises(HostError) as ctx:
            gh.get_repo_info("org/sub")
        msg = str(ctx.exception)
        self.assertIn("not installed on org/sub", msg)
        self.assertIn("https://github.com/apps/acme-lens/installations/new", msg)
        # Must not fall back to another installation's token.
        called = [c.args[1] if len(c.args) > 1 else c.kwargs.get("url", "") for c in sess.request.call_args_list]
        self.assertFalse(any("/app/installations" in str(u) for u in called))

        sess.request.side_effect = [_fake_resp(404, {"message": "Not Found"})]
        with self.assertRaises(AppNotInstalledError):
            gh.get_installation_token("org/other")

    def test_gitlab_oauth_mrs_rate_limit_expiry_warning_and_webhook(self) -> None:
        sess = MagicMock()
        exp_soon = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        sess.post.return_value = _fake_resp(200, {"access_token": "gl-oauth-tok"})
        sess.request.side_effect = [
            _fake_resp(200, {"username": "amitsingh", "name": "Amit Singh", "email": "amit@acme.com"}),
            _fake_resp(200, [{"email": "amit@acme.com", "confirmed_at": "2026-01-01T00:00:00Z"}]),
            _fake_resp(200, [{"iid": 7, "title": "Update spec", "source_branch": "spec-7", "target_branch": "main", "sha": "def456", "web_url": "https://gitlab.com/g/p/-/merge_requests/7"}]),
            _fake_resp(200, {"changes": [{"new_path": "specs/007/spec.md"}]}),
            _fake_resp(200, {"scopes": ["read_api", "read_repository", "api"], "expires_at": exp_soon}),
            _fake_resp(429, {"message": "Too many requests"}, headers={"Retry-After": "60"}),
        ]

        gl = GitLabAdapter(
            credentials={"client_id": "gl-cid", "client_secret": "gl-csec"},
            service_token="glpat-service-123",
            session=sess,
        )
        tok = gl.exchange_oauth_code("code-1", "https://lens.example.com")
        self.assertEqual(tok, "gl-oauth-tok")
        prof = gl.get_user_profile(tok, is_oauth=True)
        self.assertEqual(prof.login, "amitsingh")

        mrs = gl.list_open_prs("g/p")
        self.assertEqual(mrs[0].number, 7)
        self.assertEqual(mrs[0].changed_files, ["specs/007/spec.md"])

        hc = gl.health_check("https://lens.example.com")
        self.assertTrue(hc.ok)
        self.assertTrue(any("expires in" in w for w in hc.warnings))

        with self.assertRaises(RateLimitError):
            gl.get_repo_info("g/p")

        self.assertTrue(gl.verify_webhook({"X-Gitlab-Token": "sec123"}, b"{}", "sec123"))
        self.assertFalse(gl.verify_webhook({"X-Gitlab-Token": "wrong"}, b"{}", "sec123"))
