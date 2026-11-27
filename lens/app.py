"""GroundWork Lens WSGI application and route handlers."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from engine import ENGINE_VERSION
from lens import LENS_VERSION
from lens.config import LensConfig
from lens.demo import ensure_demo_project
from lens.hosts import adapter_for_host
from lens.hosts.base import HostError
from lens.hosts.github import GitHubAdapter, app_install_url, build_app_manifest, is_publicly_reachable
from lens.hosts.gitlab import GitLabAdapter
from lens.markdown_safe import render_markdown_safe
from lens.snapshot_merge import format_change_actor, merge_project_snapshots, relative_time, user_aliases_set
from lens.store import LensStore
from lens.sync import SyncWorker
from lens.writer import LensWriter, WriteError
from lens.wsgi import Request, Response, WebApp

PKG_DIR = Path(__file__).resolve().parent


class LensContext:
    """Holds application services (store, config, sync worker, writer)."""

    def __init__(self, data_dir: Path, start_worker: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self.store = LensStore(self.data_dir)
        self.config = LensConfig(self.store)
        self.sync_worker = SyncWorker(self.store, self.config)
        self.writer = LensWriter(self.store, self.sync_worker)
        self.setup_code = self.config.ensure_setup_code()
        if start_worker:
            self.sync_worker.start()

    def close(self) -> None:
        self.sync_worker.stop()
        self.store.close()


def _user_key(session: dict[str, Any]) -> str:
    return f"{session['host_id']}:{session['user_login']}"


def _is_named_approver_session(session: dict[str, Any], approvers: list[str], people: list[dict[str, Any]]) -> bool:
    aliases = user_aliases_set(session, people)
    return any(a.strip().lower() in aliases for a in approvers)


def _is_author_session(session: dict[str, Any], author: str, people: list[dict[str, Any]]) -> bool:
    if not author:
        return False
    aliases = user_aliases_set(session, people)
    return author.strip().lower() in aliases


def create_app(data_dir: Path | str | None = None, start_worker: bool = False) -> WebApp:
    """Create and wire the GroundWork Lens WSGI WebApp."""
    resolved_dir = Path(data_dir or os.environ.get("LENS_DATA_DIR") or (Path.cwd() / "data")).resolve()
    ctx = LensContext(resolved_dir, start_worker=start_worker)
    app = WebApp(
        template_dir=PKG_DIR / "templates",
        static_dir=PKG_DIR / "static",
        session_secret=ctx.config.get_session_secret(),
    )
    app.ctx = ctx  # type: ignore[attr-defined]

    def _current_session(req: Request) -> dict[str, Any] | None:
        sid = req.cookies.get("lens_sid", "")
        sess = ctx.store.get_session(sid) if sid else None
        if sess is not None:
            return sess
        # Auto-session in Mode C when configured
        hosts = ctx.store.list_hosts()
        mode_c_host = next((h for h in hosts if h["sign_in_mode"] == "C"), None)
        if mode_c_host and ctx.config.get("setup_complete", False):
            return {
                "id": "mode-c",
                "host_id": mode_c_host["id"],
                "user_login": "mode-c",
                "user_name": "Team Member",
                "verified_emails": [],
                "user_token": "",
                "sign_in_mode": "C",
                "is_admin": True,
                "is_demo": False,
            }
        return None

    def _flash_from_query(req: Request) -> tuple[str, str]:
        return req.arg("err", ""), req.arg("info", "")

    def _redirect_with(url: str, **params: str) -> Response:
        clean = {k: v for k, v in params.items() if v}
        if not clean:
            return app.redirect(url)
        sep = "&" if "?" in url else "?"
        return app.redirect(f"{url}{sep}{urlencode(clean)}")

    def _nav_projects(session: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Projects the signed-in user follows, for the sidebar."""
        if not session:
            return []
        try:
            return [
                {"id": p["id"], "display_name": p["display_name"]}
                for p in ctx.store.list_followed_projects(_user_key(session))
            ]
        except Exception:
            return []

    app.jinja.globals["nav_projects"] = _nav_projects

    @app.before_request
    def _attach_session(req: Request) -> Response | None:
        req.session = _current_session(req)
        return None

    def _require_session(req: Request) -> tuple[dict[str, Any] | None, Response | None]:
        if req.session is None:
            return None, app.redirect("/login")
        return req.session, None

    def _check_project_access(req: Request, proj: dict[str, Any]) -> bool:
        if proj.get("is_demo") or proj.get("is_public"):
            return True
        sess = req.session
        if not sess:
            return False
        if sess.get("sign_in_mode") == "C":
            return True
        host = ctx.store.get_host(proj["host_id"])
        if not host:
            return False
        adapter = adapter_for_host(host)
        try:
            return adapter.check_repo_access(
                proj["repo_full_name"],
                sess["user_login"],
                user_token=sess.get("user_token", ""),
            )
        except HostError:
            return False

    # Health endpoint for container HEALTHCHECK
    @app.route("/healthz", methods=("GET",))
    def healthz(req: Request) -> Response:
        return app.json({"ok": True, "version": LENS_VERSION, "engine_version": ENGINE_VERSION})

    @app.route("/", methods=("GET",))
    def index(req: Request) -> Response:
        if not ctx.config.get("setup_complete", False) and not ctx.store.list_hosts():
            return app.redirect("/setup")
        if req.session is None:
            return app.redirect("/login")
        followed = ctx.store.list_followed_projects(_user_key(req.session))
        if not followed and req.session.get("sign_in_mode") == "C":
            followed = ctx.store.list_projects(include_demo=False)
        if len(followed) == 1:
            return app.redirect(f"/projects/{followed[0]['id']}")
        if len(followed) > 1:
            return app.redirect("/portfolio")
        return app.redirect("/projects/select")

    @app.route("/login", methods=("GET",))
    def login_page(req: Request) -> Response:
        hosts = [h for h in ctx.store.list_hosts() if h["kind"] != "local"]
        err, info = _flash_from_query(req)
        return app.render(
            "login.html",
            hosts=hosts,
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/logout", methods=("GET",))
    def logout(req: Request) -> Response:
        sid = req.cookies.get("lens_sid", "")
        if sid:
            ctx.store.delete_session(sid)
        resp = app.redirect("/login")
        resp.delete_cookie("lens_sid")
        return resp

    # Demo mode
    @app.route("/demo", methods=("GET",))
    def demo_entry(req: Request) -> Response:
        pid = ensure_demo_project(ctx.store, ctx.sync_worker)
        sid = secrets.token_urlsafe(24)
        ctx.store.create_session(
            session_id=sid,
            host_id=0,
            user_login="jordanreyes",
            user_name="Jordan Reyes",
            verified_emails=["jordan@brightwater.example"],
            sign_in_mode="A",
            is_admin=True,
            is_demo=True,
        )
        ctx.store.follow_project("0:jordanreyes", pid)
        resp = app.redirect(f"/projects/{pid}")
        resp.set_cookie("lens_sid", sid, httponly=True, samesite="Lax")
        return resp

    @app.route("/demo/switch", methods=("GET", "POST"))
    def demo_switch(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        if req.method == "GET":
            return app.render("demo_switch.html", session=sess, csrf_token=req.csrf_token)
        persona = req.form_val("persona", "leader")
        pid = ensure_demo_project(ctx.store, ctx.sync_worker)
        sid = secrets.token_urlsafe(24)
        if persona == "developer":
            ctx.store.create_session(
                session_id=sid,
                host_id=0,
                user_login="mayachen",
                user_name="Maya Chen",
                verified_emails=["maya@brightwater.example"],
                sign_in_mode="A",
                is_admin=True,
                is_demo=True,
            )
            ctx.store.follow_project("0:mayachen", pid)
        else:
            ctx.store.create_session(
                session_id=sid,
                host_id=0,
                user_login="jordanreyes",
                user_name="Jordan Reyes",
                verified_emails=["jordan@brightwater.example"],
                sign_in_mode="A",
                is_admin=True,
                is_demo=True,
            )
            ctx.store.follow_project("0:jordanreyes", pid)
        resp = app.redirect(f"/projects/{pid}")
        resp.set_cookie("lens_sid", sid, httponly=True, samesite="Lax")
        return resp

    # Setup Wizard
    def _is_setup_unlocked(req: Request) -> bool:
        if req.session and req.session.get("is_admin"):
            return True
        if req.cookies.get("lens_setup_ok") == "1":
            return True
        return bool(ctx.config.get("setup_complete", False))

    @app.route("/setup", methods=("GET",))
    def setup_page(req: Request) -> Response:
        err, info = _flash_from_query(req)
        unlocked = _is_setup_unlocked(req)
        public_url = str(ctx.config.get("public_url") or req.base_url)
        gh_base = "https://github.com"
        manifest = build_app_manifest(public_url, app_name="GroundWork Lens", webhook_active=False)
        hosts_vm = []
        for h in ctx.store.list_hosts():
            if h["kind"] == "local":
                continue
            row = dict(h)
            slug = str((h.get("credentials") or {}).get("slug") or "")
            row["install_url"] = app_install_url(h["base_url"], slug) if h["kind"] == "github" and slug else ""
            row.pop("credentials", None)
            row.pop("service_token", None)
            hosts_vm.append(row)
        return app.render(
            "setup.html",
            session=req.session,
            setup_unlocked=unlocked,
            public_url=public_url,
            public_url_env=ctx.config.is_env_overridden("public_url"),
            public_url_reachable=is_publicly_reachable(public_url),
            github_base_url=gh_base,
            github_manifest_json=json.dumps(manifest),
            hosts=hosts_vm,
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/setup/unlock", methods=("POST",))
    def setup_unlock(req: Request) -> Response:
        code = req.form_val("setup_code")
        if not ctx.config.verify_setup_code(code):
            return _redirect_with("/setup", err="Invalid setup code. Check container logs and try again.")
        resp = app.redirect("/setup")
        resp.set_cookie("lens_setup_ok", "1", httponly=True, samesite="Lax")
        return resp

    @app.route("/setup/public-url", methods=("POST",))
    def setup_public_url(req: Request) -> Response:
        if not _is_setup_unlocked(req):
            return app.redirect("/setup")
        url = req.form_val("public_url").rstrip("/")
        if url:
            ctx.config.set("public_url", url)
        return _redirect_with("/setup", info="Public address saved.")

    @app.route("/setup/host", methods=("POST",))
    def setup_host(req: Request) -> Response:
        if not _is_setup_unlocked(req):
            return app.redirect("/setup")
        kind = req.form_val("kind")
        name = req.form_val("name") or kind.title()
        base_url = (req.form_val("base_url") or ("https://github.com" if kind == "github" else "https://gitlab.com")).rstrip("/")
        if kind == "github":
            default_api = "https://api.github.com" if base_url == "https://github.com" else f"{base_url}/api/v3"
        else:
            default_api = f"{base_url}/api/v4"
        api_url = (req.form_val("api_url") or default_api).rstrip("/")
        mode = req.form_val("sign_in_mode", "A")
        creds = {
            "app_id": req.form_val("app_id"),
            "client_id": req.form_val("client_id"),
            "client_secret": req.form_val("client_secret"),
            "private_key": req.form_val("private_key"),
        }
        ctx.store.upsert_host(
            kind=kind,
            name=name,
            base_url=base_url,
            api_url=api_url,
            sign_in_mode=mode,
            credentials={k: v for k, v in creds.items() if v},
            service_token=req.form_val("service_token"),
        )
        return _redirect_with("/setup", info=f"{name} configuration saved.")

    @app.route("/setup/github/manifest-callback", methods=("GET",))
    def setup_github_manifest_cb(req: Request) -> Response:
        if not _is_setup_unlocked(req):
            return app.redirect("/setup")
        code = req.arg("code")
        if not code:
            return _redirect_with("/setup", err="Missing GitHub manifest code.")
        try:
            adapter = GitHubAdapter()
            creds = adapter.exchange_manifest_code(code)
            ctx.store.upsert_host(
                kind="github",
                name="GitHub",
                base_url="https://github.com",
                api_url="https://api.github.com",
                sign_in_mode="A",
                credentials=creds,
                webhook_secret=creds.get("webhook_secret", ""),
            )
            install = app_install_url("https://github.com", str(creds.get("slug") or ""))
            return _redirect_with(
                "/setup",
                info=(
                    "GitHub App created. Next, install it on your repositories (for a workspace, "
                    f"include every sub-repo): {install} — then run Test connection."
                ),
            )
        except Exception as exc:
            return _redirect_with("/setup", err=f"GitHub App creation failed: {exc}")

    @app.route("/setup/check", methods=("POST",))
    def setup_check(req: Request) -> Response:
        if not _is_setup_unlocked(req):
            return app.redirect("/setup")
        host_id = int(req.form_val("host_id") or "0")
        host = ctx.store.get_host(host_id)
        if not host:
            return _redirect_with("/setup", err="Host not found.")
        adapter = adapter_for_host(host)
        pub = str(ctx.config.get("public_url") or req.base_url)
        hc = adapter.health_check(pub)
        if hc.ok:
            return _redirect_with("/setup", info=f"{host['name']} connection check passed.")
        details = "; ".join(f"{c['name']}: {c['detail']}" for c in hc.checks if not c["ok"])
        return _redirect_with("/setup", err=f"{host['name']} check failed — {details}")

    @app.route("/setup/complete", methods=("POST",))
    def setup_complete(req: Request) -> Response:
        if not _is_setup_unlocked(req):
            return app.redirect("/setup")
        ctx.config.mark_setup_complete()
        if req.session:
            return app.redirect("/projects/select")
        hosts = ctx.store.list_hosts()
        if any(h["sign_in_mode"] == "C" for h in hosts):
            return app.redirect("/projects/select")
        return app.redirect("/login")

    # Auth flows
    @app.route("/auth/github/start", methods=("GET",))
    def auth_gh_start(req: Request) -> Response:
        host_id = int(req.arg("host_id") or "0")
        host = ctx.store.get_host(host_id) or ctx.store.get_host_by_kind("github")
        if not host:
            return _redirect_with("/login", err="GitHub host is not configured.")
        adapter = GitHubAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host["credentials"],
        )
        state = f"{host['id']}:{secrets.token_urlsafe(16)}"
        pub = str(ctx.config.get("public_url") or req.base_url)
        resp = app.redirect(adapter.authorize_url(pub, state))
        resp.set_cookie("lens_oauth_state", state, httponly=True, samesite="Lax", max_age=600)
        return resp

    @app.route("/auth/github/callback", methods=("GET",))
    def auth_gh_cb(req: Request) -> Response:
        code = req.arg("code")
        state = req.arg("state")
        if not code or not state or state != req.cookies.get("lens_oauth_state"):
            return _redirect_with("/login", err="Invalid OAuth state. Please try signing in again.")
        host_id = int(state.split(":", 1)[0])
        host = ctx.store.get_host(host_id)
        if not host:
            return _redirect_with("/login", err="Host missing.")
        adapter = GitHubAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host["credentials"],
        )
        try:
            pub = str(ctx.config.get("public_url") or req.base_url)
            tok = adapter.exchange_oauth_code(code, pub)
            prof = adapter.get_user_profile(tok)
            sid = secrets.token_urlsafe(24)
            stored_tok = tok if host["sign_in_mode"] == "A" else ""
            ctx.store.create_session(
                session_id=sid,
                host_id=host_id,
                user_login=prof.login,
                user_name=prof.name,
                verified_emails=prof.verified_emails,
                user_token=stored_tok,
                sign_in_mode=host["sign_in_mode"],
                is_admin=True,
            )
            resp = app.redirect("/")
            resp.set_cookie("lens_sid", sid, httponly=True, samesite="Lax")
            resp.delete_cookie("lens_oauth_state")
            return resp
        except Exception as exc:
            return _redirect_with("/login", err=f"GitHub sign-in failed: {exc}")

    @app.route("/auth/gitlab/start", methods=("GET",))
    def auth_gl_start(req: Request) -> Response:
        host_id = int(req.arg("host_id") or "0")
        host = ctx.store.get_host(host_id) or ctx.store.get_host_by_kind("gitlab")
        if not host:
            return _redirect_with("/login", err="GitLab host is not configured.")
        adapter = GitLabAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host["credentials"],
        )
        state = f"{host['id']}:{secrets.token_urlsafe(16)}"
        pub = str(ctx.config.get("public_url") or req.base_url)
        resp = app.redirect(adapter.authorize_url(pub, state))
        resp.set_cookie("lens_oauth_state", state, httponly=True, samesite="Lax", max_age=600)
        return resp

    @app.route("/auth/gitlab/callback", methods=("GET",))
    def auth_gl_cb(req: Request) -> Response:
        code = req.arg("code")
        state = req.arg("state")
        if not code or not state or state != req.cookies.get("lens_oauth_state"):
            return _redirect_with("/login", err="Invalid OAuth state. Please try signing in again.")
        host_id = int(state.split(":", 1)[0])
        host = ctx.store.get_host(host_id)
        if not host:
            return _redirect_with("/login", err="Host missing.")
        adapter = GitLabAdapter(
            base_url=host["base_url"],
            api_url=host["api_url"],
            sign_in_mode=host["sign_in_mode"],
            credentials=host["credentials"],
        )
        try:
            pub = str(ctx.config.get("public_url") or req.base_url)
            tok = adapter.exchange_oauth_code(code, pub)
            prof = adapter.get_user_profile(tok, is_oauth=True)
            sid = secrets.token_urlsafe(24)
            stored_tok = tok if host["sign_in_mode"] == "A" else ""
            ctx.store.create_session(
                session_id=sid,
                host_id=host_id,
                user_login=prof.login,
                user_name=prof.name,
                verified_emails=prof.verified_emails,
                user_token=stored_tok,
                sign_in_mode=host["sign_in_mode"],
                is_admin=True,
            )
            resp = app.redirect("/")
            resp.set_cookie("lens_sid", sid, httponly=True, samesite="Lax")
            resp.delete_cookie("lens_oauth_state")
            return resp
        except Exception as exc:
            return _redirect_with("/login", err=f"GitLab sign-in failed: {exc}")

    @app.route("/auth/mode-b-token", methods=("POST",))
    def auth_mode_b_token(req: Request) -> Response:
        """Verify identity via one-time profile token and IMMEDIATELY discard the token (Section 10.2)."""
        host_id = int(req.form_val("host_id") or "0")
        raw_tok = req.form_val("token").strip()
        host = ctx.store.get_host(host_id)
        if not host or not raw_tok:
            return _redirect_with("/login", err="Please enter your profile-only token.")
        try:
            if host["kind"] == "gitlab":
                gl = GitLabAdapter(base_url=host["base_url"], api_url=host["api_url"], sign_in_mode="B")
                prof = gl.get_user_profile(raw_tok, is_oauth=False)
            else:
                gh = GitHubAdapter(base_url=host["base_url"], api_url=host["api_url"], sign_in_mode="B")
                prof = gh.get_user_profile(raw_tok)
            sid = secrets.token_urlsafe(24)
            # Token is discarded: user_token=""
            ctx.store.create_session(
                session_id=sid,
                host_id=host_id,
                user_login=prof.login,
                user_name=prof.name,
                verified_emails=prof.verified_emails,
                user_token="",
                sign_in_mode="B",
                is_admin=False,
            )
            resp = app.redirect("/")
            resp.set_cookie("lens_sid", sid, httponly=True, samesite="Lax")
            return resp
        except Exception as exc:
            return _redirect_with("/login", err=f"Token verification failed: {exc}")

    # Project selection & Portfolio
    @app.route("/projects/select", methods=("GET", "POST"))
    def projects_select(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        if req.method == "POST":
            selected = set(req.form_list("repo_keys"))
            existing = {f"{p['host_id']}:{p['repo_full_name']}": p["id"] for p in ctx.store.list_projects()}
            for key in selected:
                if ":" not in key:
                    continue
                hid_s, full_name = key.split(":", 1)
                hid = int(hid_s)
                host = ctx.store.get_host(hid)
                if not host:
                    continue
                adapter = adapter_for_host(host)
                try:
                    info = adapter.get_repo_info(full_name, user_token=sess.get("user_token", ""))
                    pid = ctx.store.upsert_project(
                        host_id=hid,
                        repo_full_name=info.full_name,
                        display_name=info.name,
                        default_branch=info.default_branch,
                        archived=info.archived,
                    )
                except Exception:
                    pid = existing.get(key) or ctx.store.upsert_project(hid, full_name, full_name.split("/")[-1])
                ctx.store.follow_project(_user_key(sess), pid)
                ctx.sync_worker.sync_project(pid, user_token=sess.get("user_token", ""))
            for key, pid in existing.items():
                if key not in selected:
                    ctx.store.unfollow_project(_user_key(sess), pid)
            return app.redirect("/")

        followed_ids = {p["id"] for p in ctx.store.list_followed_projects(_user_key(sess))}
        all_projs = ctx.store.list_projects(include_demo=bool(sess.get("is_demo")))
        proj_by_key = {f"{p['host_id']}:{p['repo_full_name']}": p for p in all_projs}
        repos_vm: list[dict[str, Any]] = []
        for host in ctx.store.list_hosts():
            if host["kind"] == "local" and not sess.get("is_demo"):
                continue
            adapter = adapter_for_host(host)
            try:
                for r in adapter.list_repos(user_token=sess.get("user_token", "")):
                    key = f"{host['id']}:{r.full_name}"
                    p = proj_by_key.get(key)
                    snap = ctx.store.get_latest_snapshot(p["id"]) if p else None
                    gw_stage = ((snap or {}).get("data") or {}).get("groundwork", {}).get("stage", "not-started")
                    repos_vm.append(
                        {
                            "host_id": host["id"],
                            "full_name": r.full_name,
                            "name": r.name,
                            "followed": bool(p and p["id"] in followed_ids),
                            "is_workspace": bool(p and p["is_workspace"]),
                            # None = Lens has not read this repo yet, so it cannot know.
                            "has_groundwork": (
                                None
                                if snap is None
                                else gw_stage != "not-started" or bool(snap["data"].get("items"))
                            ),
                        }
                    )
            except Exception:
                continue
        err, info = _flash_from_query(req)
        return app.render(
            "select_projects.html",
            session=sess,
            repos=repos_vm,
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/projects/public", methods=("POST",))
    def follow_public_repo(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        raw = req.form_val("repo").strip()
        if not raw:
            return _redirect_with("/projects/select", err="Enter owner/repo or a public URL.")
        kind = "gitlab" if "gitlab" in raw.lower() else "github"
        full_name = raw.rstrip("/")
        if full_name.startswith(("http://", "https://")):
            full_name = "/".join(full_name.split("/")[3:])
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        host = ctx.store.get_host_by_kind(kind)
        if not host:
            hid = ctx.store.upsert_host(
                kind=kind,
                name=f"Public {kind.title()}",
                base_url=f"https://{kind}.com",
                api_url=f"https://api.{kind}.com" if kind == "github" else "https://gitlab.com/api/v4",
                sign_in_mode="C",
            )
        else:
            hid = host["id"]
        pid = ctx.store.upsert_project(
            host_id=hid,
            repo_full_name=full_name,
            display_name=full_name.split("/")[-1],
            is_public=True,
        )
        ctx.store.follow_project(_user_key(sess), pid)
        ctx.sync_worker.sync_project(pid, force=True)
        return app.redirect(f"/projects/{pid}")

    @app.route("/portfolio", methods=("GET",))
    def portfolio(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        followed = ctx.store.list_followed_projects(_user_key(sess))
        if not followed:
            followed = ctx.store.list_projects(include_demo=bool(sess.get("is_demo")))
        cards: list[dict[str, Any]] = []
        for proj in followed:
            if not _check_project_access(req, proj):
                continue
            main_snap = ctx.store.get_latest_snapshot(proj["id"])
            pr_snaps = ctx.store.get_open_pr_snapshots(proj["id"])
            sbs = ctx.store.list_send_backs(proj["id"])
            vm = merge_project_snapshots(proj, main_snap, pr_snaps, send_backs=sbs, user=sess)
            cards.append(
                {
                    "project": proj,
                    "counts": vm["counts"],
                    "last_activity_rel": relative_time(proj.get("last_sync_at") or ""),
                }
            )
        cards.sort(key=lambda c: c["counts"]["waiting"], reverse=True)
        return app.render("portfolio.html", session=sess, cards=cards, csrf_token=req.csrf_token)

    def _load_project_view(req: Request, pid: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, Response | None]:
        sess, redir = _require_session(req)
        if redir:
            return None, None, redir
        proj = ctx.store.get_project(pid)
        if not proj or not _check_project_access(req, proj):
            return None, None, Response("Project not found or access denied", status=404, content_type="text/plain")
        main_snap = ctx.store.get_latest_snapshot(pid)
        if main_snap is None and proj.get("removed_groundwork_at"):
            main_snap = ctx.store.get_last_groundwork_snapshot(pid)
        pr_snaps = ctx.store.get_open_pr_snapshots(pid)
        sbs = ctx.store.list_send_backs(pid)
        only_mine = req.arg("mine") == "1"
        vm = merge_project_snapshots(proj, main_snap, pr_snaps, send_backs=sbs, user=sess, only_mine=only_mine)
        return proj, vm, None

    @app.route("/projects/<pid>", methods=("GET",))
    def project_home(req: Request) -> Response:
        pid = int(req.params["pid"])
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")

        gw_stage = vm["groundwork"].get("stage", "not-started")
        # Only conclude "not set up" once Lens has actually read the repo. A
        # failed or pending sync leaves no snapshot, which looks the same.
        read_ok = proj.get("last_sync_status") in ("ok", "archived")
        if read_ok and gw_stage == "not-started" and not vm["all_items"] and not proj.get("removed_groundwork_at"):
            return app.redirect(f"/projects/{pid}/not-set-up")

        err, info = _flash_from_query(req)
        return app.render(
            "project_home.html",
            session=req.session,
            project=proj,
            active_project=proj,
            active_project_can_request=(gw_stage != "not-started"),
            view=vm,
            only_mine=(req.arg("mine") == "1"),
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/projects/<pid>/not-set-up", methods=("GET",))
    def project_not_set_up(req: Request) -> Response:
        pid = int(req.params["pid"])
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")
        host = ctx.store.get_host(proj["host_id"])
        maintainers: list[str] = []
        if host:
            try:
                info = adapter_for_host(host).get_repo_info(proj["repo_full_name"], user_token=(req.session or {}).get("user_token", ""))
                maintainers = info.maintainers
            except Exception:
                pass
        return app.render(
            "not_set_up.html",
            session=req.session,
            project=proj,
            setup_pr=vm["setup_pr"],
            maintainers=maintainers,
            public_url=str(ctx.config.get("public_url") or req.base_url),
            csrf_token=req.csrf_token,
        )

    @app.route("/projects/<pid>/refresh", methods=("POST",))
    def project_refresh(req: Request) -> Response:
        pid = int(req.params["pid"])
        sess, redir = _require_session(req)
        if redir:
            return redir
        ok, msg = ctx.sync_worker.trigger_manual_refresh(pid, user_token=sess.get("user_token", "") if sess else "")
        if ok:
            return _redirect_with(f"/projects/{pid}", info="Project refreshed.")
        return _redirect_with(f"/projects/{pid}", err=msg)

    @app.route("/projects/<pid>/item/<path:item_id>", methods=("GET",))
    def item_detail(req: Request) -> Response:
        pid = int(req.params["pid"])
        item_id = req.params["item_id"]
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")
        item = vm["all_items"].get(item_id)
        if not item:
            return Response("Item not found", status=404, content_type="text/plain")
        host = ctx.store.get_host(proj["host_id"])
        adapter = adapter_for_host(host) if host else None
        for d in item.get("documents") or []:
            if adapter:
                repo_name = proj["repo_full_name"] if item["repo"] == proj["display_name"] or not proj["is_workspace"] else (
                    f"{proj['repo_full_name'].rsplit('/', 1)[0]}/{item['repo']}" if "/" in proj["repo_full_name"] else item["repo"]
                )
                d["host_url"] = adapter.get_web_file_url(repo_name, item.get("source_ref") or "main", d["path"])
        timeline = [
            dict(ev, actor_display=format_change_actor(ev) if ev.get("via") else ev.get("who", ""))
            for ev in vm["events"]
            if ev.get("item") in (item_id, item.get("rfc"), item.get("req_id"))
        ]
        err, info = _flash_from_query(req)
        return app.render(
            "item_detail.html",
            session=req.session,
            project=proj,
            active_project=proj,
            active_project_can_request=True,
            item=item,
            item_timeline=timeline,
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/projects/<pid>/bug/<path:bug_id>", methods=("GET",))
    def bug_detail(req: Request) -> Response:
        pid = int(req.params["pid"])
        bug_id = req.params["bug_id"]
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")
        bug = vm["all_items"].get(bug_id)
        if not bug:
            return Response("Bug not found", status=404, content_type="text/plain")
        timeline = [
            dict(ev, actor_display=format_change_actor(ev) if ev.get("via") else ev.get("who", ""))
            for ev in vm["events"]
            if ev.get("item") == bug_id
        ]
        return app.render(
            "bug_detail.html",
            session=req.session,
            project=proj,
            active_project=proj,
            active_project_can_request=True,
            bug=bug,
            item_timeline=timeline,
            csrf_token=req.csrf_token,
        )

    @app.route("/projects/<pid>/doc", methods=("GET",))
    def doc_view(req: Request) -> Response:
        pid = int(req.params["pid"])
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")
        sess = req.session
        assert sess is not None
        rel_path = req.arg("path")
        repo_override = req.arg("repo")
        target_ref = req.arg("ref") or proj["default_branch"]
        pr_num = int(req.arg("pr") or "0")
        if not rel_path:
            return Response("Missing document path", status=400, content_type="text/plain")

        try:
            doc = ctx.writer.read_document(
                pid,
                rel_path,
                ref_name=target_ref,
                repo_override=repo_override,
                user_token=sess.get("user_token", ""),
            )
        except Exception as exc:
            return _redirect_with(f"/projects/{pid}", err=f"Could not read document: {exc}")

        rendered_html, toc = render_markdown_safe(doc.get("body", ""), highlight_markers=True)
        people = vm["people"]
        approvers = doc.get("approvers") or []
        author = str(doc.get("author") or "")
        is_author = _is_author_session(sess, author, people)
        is_named_app = _is_named_approver_session(sess, approvers, people)

        if sess.get("sign_in_mode") == "C":
            can_edit = False
            can_approve_role = False
        else:
            can_edit = is_author or is_named_app
            if approvers:
                can_approve_role = is_named_app
            else:
                host = ctx.store.get_host(proj["host_id"])
                adapter = adapter_for_host(host) if host else None
                can_approve_role = bool(
                    adapter and adapter.user_can_push(doc["repo_full_name"], sess["user_login"], user_token=sess.get("user_token", ""))
                )

        meta = doc.get("meta") or {}
        facts = [(k.replace("_", " ").title(), ", ".join(map(str, v)) if isinstance(v, list) else str(v)) for k, v in meta.items() if v not in ("", [], None)]

        err, info = _flash_from_query(req)
        return app.render(
            "document.html",
            session=sess,
            project=proj,
            active_project=proj,
            active_project_can_request=True,
            doc=doc,
            rendered_html=rendered_html,
            toc=toc,
            facts=facts,
            user_can_edit=can_edit,
            user_can_approve=can_approve_role,
            repo_override=repo_override,
            target_ref=target_ref,
            pr_number=pr_num,
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/projects/<pid>/doc/edit", methods=("POST",))
    def doc_edit(req: Request) -> Response:
        pid = int(req.params["pid"])
        sess, redir = _require_session(req)
        if redir:
            return redir
        assert sess is not None
        rel_path = req.form_val("path")
        repo_override = req.form_val("repo")
        target_ref = req.form_val("ref")
        pr_num = int(req.form_val("pr") or "0")
        expect_hash = req.form_val("expect_hash")
        new_body = req.form_val("body")
        change_note = req.form_val("change_note")
        also_approve = req.form_val("action") == "save_and_approve"

        doc_url = f"/projects/{pid}/doc?{urlencode({'path': rel_path, 'repo': repo_override, 'ref': target_ref, 'pr': str(pr_num)})}"
        try:
            res = ctx.writer.edit_document(
                project_id=pid,
                user=sess,
                rel_path=rel_path,
                target_ref=target_ref,
                new_body=new_body,
                expect_hash=expect_hash,
                change_note=change_note,
                also_approve=also_approve,
                repo_override=repo_override,
                pr_number=pr_num,
            )
            fb = res["push"].get("fallback_pr")
            if fb:
                return _redirect_with(doc_url, info=f"Direct push was refused; opened PR #{fb['number']} instead.")
            return _redirect_with(doc_url, info="Saved and approved." if also_approve else "Document saved.")
        except Exception as exc:
            return _redirect_with(doc_url, err=str(exc))

    @app.route("/projects/<pid>/doc/approve", methods=("POST",))
    def doc_approve(req: Request) -> Response:
        pid = int(req.params["pid"])
        sess, redir = _require_session(req)
        if redir:
            return redir
        assert sess is not None
        rel_path = req.form_val("path")
        repo_override = req.form_val("repo")
        target_ref = req.form_val("ref")
        pr_num = int(req.form_val("pr") or "0")
        expect_hash = req.form_val("expect_hash")
        change_note = req.form_val("change_note")
        doc_url = f"/projects/{pid}/doc?{urlencode({'path': rel_path, 'repo': repo_override, 'ref': target_ref, 'pr': str(pr_num)})}"

        try:
            res = ctx.writer.approve_document(
                project_id=pid,
                user=sess,
                rel_path=rel_path,
                target_ref=target_ref,
                expect_hash=expect_hash,
                change_note=change_note,
                repo_override=repo_override,
                pr_number=pr_num,
            )
            fb = res["push"].get("fallback_pr")
            if fb:
                return _redirect_with(doc_url, info=f"Approved, waiting for PR #{fb['number']} to merge (direct push refused).")
            return _redirect_with(doc_url, info="Document approved.")
        except Exception as exc:
            return _redirect_with(doc_url, err=str(exc))

    @app.route("/projects/<pid>/doc/send-back", methods=("POST",))
    def doc_send_back(req: Request) -> Response:
        pid = int(req.params["pid"])
        sess, redir = _require_session(req)
        if redir:
            return redir
        assert sess is not None
        rel_path = req.form_val("path")
        repo_override = req.form_val("repo")
        target_ref = req.form_val("ref")
        pr_num = int(req.form_val("pr") or "0")
        note = req.form_val("note")
        doc_url = f"/projects/{pid}/doc?{urlencode({'path': rel_path, 'repo': repo_override, 'ref': target_ref, 'pr': str(pr_num)})}"
        try:
            ctx.writer.send_back_document(
                project_id=pid,
                user=sess,
                rel_path=rel_path,
                target_ref=target_ref,
                pr_number=pr_num,
                note=note,
                repo_override=repo_override,
            )
            return _redirect_with(doc_url, info="Sent back to author.")
        except Exception as exc:
            return _redirect_with(doc_url, err=str(exc))

    # Requests: Ask for a feature / Report a bug
    @app.route("/projects/<pid>/request/<kind>", methods=("GET", "POST"))
    def request_form(req: Request) -> Response:
        kind = req.params["kind"]
        if kind not in ("feature", "bug"):
            return Response("Not Found", status=404, content_type="text/plain")
        pid = int(req.params["pid"])
        proj, vm, err_resp = _load_project_view(req, pid)
        if err_resp or not proj or not vm:
            return err_resp or app.redirect("/")
        sess = req.session
        assert sess is not None

        gw_stage = vm["groundwork"].get("stage", "not-started")
        if gw_stage == "not-started":
            return app.render(
                "request_form.html",
                session=sess,
                project=proj,
                kind=kind,
                gw_disabled=True,
                fields={},
                product_areas=[],
                csrf_token=req.csrf_token,
            )

        # Product areas
        areas: list[str] = []
        if proj.get("is_workspace"):
            areas.extend(vm["snapshot"].get("repos") or [])
            areas.extend(["Several", "Not sure"])
        else:
            areas.append(proj["display_name"])

        # Help examples from an existing approved RFC
        ex_summary = ""
        ex_done = ""
        for it in vm["all_items"].values():
            if it.get("summary") and it.get("done_means"):
                ex_summary = it["summary"]
                ex_done = it["done_means"]
                break

        # Check push access to inform read-only users before typing (Section 9.3, M4)
        host = ctx.store.get_host(proj["host_id"])
        adapter = adapter_for_host(host) if host else None
        read_only_note = ""
        if adapter and sess.get("sign_in_mode") != "C":
            if not adapter.user_can_push(proj["repo_full_name"], sess["user_login"], user_token=sess.get("user_token", "")):
                read_only_note = "You do not have direct push access; Lens will commit your request under its own identity with your name recorded."

        if req.method == "GET":
            err, info = _flash_from_query(req)
            return app.render(
                "request_form.html",
                session=sess,
                project=proj,
                kind=kind,
                gw_disabled=False,
                preview=False,
                fields={},
                people=vm["people"],
                product_areas=areas,
                example_summary=ex_summary,
                example_done=ex_done,
                read_only_note=read_only_note,
                allow_direct_submit=True,
                csrf_token=req.csrf_token,
                flash_error=err,
                flash_info=info,
            )

        fields = {
            "title": req.form_val("title").strip(),
            "product_area": req.form_val("product_area") or (areas[0] if areas else "Not sure"),
            "requested_by": req.form_val("requested_by").strip() or sess["user_name"],
        }
        if kind == "feature":
            fields["need"] = req.form_val("need").strip()
            fields["done_looks_like"] = req.form_val("done_looks_like").strip()
            fields["why_now"] = req.form_val("why_now").strip()
        else:
            fields["expected"] = req.form_val("expected").strip()
            fields["actual"] = req.form_val("actual").strip()
            fields["where_when"] = req.form_val("where_when").strip()
            fields["severity"] = req.form_val("severity", "medium").strip()
            fields["links"] = req.form_val("links").strip()

        raw_imgs = [(f.filename, f.data) for f in req.files.get("images", []) if f.data]
        action_mode = req.form_val("action_mode") or req.form_val("step", "submit")

        if action_mode == "preview" and not raw_imgs:
            return app.render(
                "request_form.html",
                session=sess,
                project=proj,
                kind=kind,
                gw_disabled=False,
                preview=True,
                fields=fields,
                people=vm["people"],
                product_areas=areas,
                read_only_note=read_only_note,
                csrf_token=req.csrf_token,
            )

        try:
            res = ctx.writer.submit_request(pid, sess, kind, fields, raw_images=raw_imgs)
            req_id = res["request"]["id"]
            fb = res["push"].get("fallback_pr")
            msg = f"Saved request {req_id}."
            if fb:
                msg += f" Opened PR #{fb['number']} because direct push was refused."
            return _redirect_with(f"/projects/{pid}", info=msg)
        except Exception as exc:
            return app.render(
                "request_form.html",
                session=sess,
                project=proj,
                kind=kind,
                gw_disabled=False,
                preview=False,
                fields=fields,
                people=vm["people"],
                product_areas=areas,
                example_summary=ex_summary,
                example_done=ex_done,
                read_only_note=read_only_note,
                allow_direct_submit=True,
                csrf_token=req.csrf_token,
                flash_error=str(exc),
            )

    @app.route("/projects/<pid>/request/<req_id>/answer", methods=("POST",))
    def request_answer(req: Request) -> Response:
        pid = int(req.params["pid"])
        req_id = req.params["req_id"]
        sess, redir = _require_session(req)
        if redir:
            return redir
        assert sess is not None
        answers: dict[str, str] = {}
        for k, vals in req.form.items():
            if k.startswith("answer_custom_") and vals and vals[0].strip():
                qid = k[len("answer_custom_") :]
                answers[qid] = vals[0].strip()
            elif k.startswith("answer_") and not k.startswith("answer_custom_") and vals and vals[0].strip():
                qid = k[len("answer_") :]
                answers.setdefault(qid, vals[0].strip())

        repo_override = req.form_val("repo_override")
        item_id = req.form_val("item_id")
        if not item_id:
            snap = ctx.store.get_latest_snapshot(pid)
            for it in ((snap or {}).get("data") or {}).get("items") or []:
                if it.get("req_id") == req_id or it.get("id") == req_id:
                    item_id = it["id"]
                    break
            item_id = item_id or req_id
        try:
            ctx.writer.submit_answers(pid, sess, req_id, answers, repo_override=repo_override)
            return _redirect_with(f"/projects/{pid}/item/{item_id}", info="Answers saved.")
        except Exception as exc:
            return _redirect_with(f"/projects/{pid}/item/{item_id}", err=str(exc))

    # Admin Health
    @app.route("/admin/health", methods=("GET",))
    def admin_health(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        pub = str(ctx.config.get("public_url") or req.base_url)
        hosts = ctx.store.list_hosts()
        projects = ctx.store.list_projects(include_demo=True)
        host_results = []
        for h in hosts:
            sample_repo = next((p["repo_full_name"] for p in projects if p["host_id"] == h["id"]), "")
            hc = adapter_for_host(h).health_check(pub, sample_repo=sample_repo)
            host_results.append({"host": h, "health": hc})
        webhooks = ctx.store.list_webhook_deliveries(20)
        disk_mb = round(ctx.store.disk_usage_bytes() / (1024 * 1024), 2)
        diag = {
            "lens_version": LENS_VERSION,
            "engine_version": ENGINE_VERSION,
            "hosts": [{"kind": r["host"]["kind"], "mode": r["host"]["sign_in_mode"], "ok": r["health"].ok, "warnings": r["health"].warnings} for r in host_results],
            "projects": [{"name": p["display_name"], "status": p["last_sync_status"], "error": p["last_sync_error"]} for p in projects],
        }
        err, info = _flash_from_query(req)
        return app.render(
            "admin_health.html",
            session=sess,
            lens_version=LENS_VERSION,
            engine_version=ENGINE_VERSION,
            disk_mb=disk_mb,
            host_results=host_results,
            projects=projects,
            webhooks=webhooks,
            diagnostics_json=json.dumps(diag, indent=2),
            csrf_token=req.csrf_token,
            flash_error=err,
            flash_info=info,
        )

    @app.route("/admin/project-settings", methods=("POST",))
    def admin_project_settings(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return redir
        pid = int(req.form_val("project_id") or "0")
        idle_days = int(req.form_val("idle_days") or "14")
        ctx.store.set_project_idle_days(pid, idle_days)
        return _redirect_with("/admin/health", info="Project settings saved.")

    # Live markdown preview API
    @app.route("/api/preview-markdown", methods=("POST",))
    def api_preview_markdown(req: Request) -> Response:
        sess, redir = _require_session(req)
        if redir:
            return app.json({"html": ""}, status=401)
        md_text = req.form_val("markdown")
        rendered, _ = render_markdown_safe(md_text, highlight_markers=True)
        return app.json({"html": rendered})

    # Webhooks (CSRF-exempt; verified by HMAC / token)
    @app.route("/webhooks/github", methods=("POST",), csrf_exempt=True)
    def webhook_github(req: Request) -> Response:
        host = ctx.store.get_host_by_kind("github")
        secret = (host or {}).get("webhook_secret", "")
        if host and secret:
            adapter = adapter_for_host(host)
            if not adapter.verify_webhook(req.headers, req.body, secret):
                ctx.store.record_webhook_delivery("github", req.headers.get("X-Github-Event", "unknown"), "", "rejected", "Bad signature")
                return app.json({"ok": False, "error": "Invalid signature"}, status=403)
        payload = json.loads(req.body.decode("utf-8") or "{}") if req.body else {}
        repo_full = str((payload.get("repository") or {}).get("full_name") or "")
        ev_type = req.headers.get("X-Github-Event", "push")
        ctx.store.record_webhook_delivery("github", ev_type, repo_full, "accepted")
        for p in ctx.store.list_projects(include_demo=True):
            if p["repo_full_name"] == repo_full:
                ctx.sync_worker.sync_project(p["id"], force=True)
        return app.json({"ok": True})

    @app.route("/webhooks/gitlab", methods=("POST",), csrf_exempt=True)
    def webhook_gitlab(req: Request) -> Response:
        host = ctx.store.get_host_by_kind("gitlab")
        secret = (host or {}).get("webhook_secret", "")
        if host and secret:
            adapter = adapter_for_host(host)
            if not adapter.verify_webhook(req.headers, req.body, secret):
                ctx.store.record_webhook_delivery("gitlab", req.headers.get("X-Gitlab-Event", "unknown"), "", "rejected", "Bad token")
                return app.json({"ok": False, "error": "Invalid token"}, status=403)
        payload = json.loads(req.body.decode("utf-8") or "{}") if req.body else {}
        repo_full = str((payload.get("project") or {}).get("path_with_namespace") or "")
        ev_type = req.headers.get("X-Gitlab-Event", "Push Hook")
        ctx.store.record_webhook_delivery("gitlab", ev_type, repo_full, "accepted")
        for p in ctx.store.list_projects(include_demo=True):
            if p["repo_full_name"] == repo_full:
                ctx.sync_worker.sync_project(p["id"], force=True)
        return app.json({"ok": True})

    return app
