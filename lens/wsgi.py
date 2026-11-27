"""Lightweight PEP-3333 WSGI micro-framework with CSRF, CSP, multipart, and TestClient."""

from __future__ import annotations

import hmac
import hashlib
import io
import json
import mimetypes
import re
import secrets
from dataclasses import dataclass, field
from http import cookies
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
from socketserver import ThreadingMixIn

from jinja2 import Environment, FileSystemLoader, select_autoescape


@dataclass
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Request:
    method: str
    path: str
    query_string: str
    headers: dict[str, str]
    cookies: dict[str, str]
    query: dict[str, list[str]]
    body: bytes
    form: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, list[UploadedFile]] = field(default_factory=dict)
    scheme: str = "http"
    host: str = "localhost:8080"
    params: dict[str, str] = field(default_factory=dict)
    session: dict[str, Any] | None = None
    csrf_token: str = ""

    def arg(self, key: str, default: str = "") -> str:
        vals = self.query.get(key)
        return vals[0] if vals else default

    def form_val(self, key: str, default: str = "") -> str:
        vals = self.form.get(key)
        return vals[0] if vals else default

    def form_list(self, key: str) -> list[str]:
        return list(self.form.get(key, []))

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}"


class Response:
    """HTTP response container."""

    def __init__(
        self,
        body: bytes | str = b"",
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.headers: dict[str, str] = dict(headers or {})
        self._cookies: list[str] = []

    def set_cookie(
        self,
        name: str,
        value: str,
        max_age: int | None = None,
        httponly: bool = True,
        samesite: str = "Lax",
        secure: bool = False,
        path: str = "/",
    ) -> None:
        c = cookies.SimpleCookie()
        c[name] = value
        c[name]["path"] = path
        if httponly:
            c[name]["httponly"] = True
        if samesite:
            c[name]["samesite"] = samesite
        if secure:
            c[name]["secure"] = True
        if max_age is not None:
            c[name]["max-age"] = str(max_age)
        self._cookies.append(c[name].OutputString())

    def delete_cookie(self, name: str, path: str = "/") -> None:
        self.set_cookie(name, "", max_age=0, path=path)

    def header_list(self) -> list[tuple[str, str]]:
        hdrs = [("Content-Type", self.content_type), ("Content-Length", str(len(self.body)))]
        for k, v in self.headers.items():
            hdrs.append((k, v))
        for c in self._cookies:
            hdrs.append(("Set-Cookie", c))
        return hdrs


_STATUS_TEXT = {
    200: "200 OK",
    201: "201 Created",
    204: "204 No Content",
    302: "302 Found",
    303: "303 See Other",
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    413: "413 Payload Too Large",
    429: "429 Too Many Requests",
    500: "500 Internal Server Error",
}

MAX_UPLOAD_TOTAL = 30 * 1024 * 1024  # 30 MB total request ceiling


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, list[str]], dict[str, list[UploadedFile]]]:
    form: dict[str, list[str]] = {}
    files: dict[str, list[UploadedFile]] = {}
    bm = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.IGNORECASE)
    if not bm:
        return form, files
    boundary = (bm.group(1) or bm.group(2)).encode("latin-1")
    delim = b"--" + boundary
    parts = body.split(delim)
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        sep = b"\r\n\r\n"
        sep_idx = part.find(sep)
        if sep_idx == -1:
            sep = b"\n\n"
            sep_idx = part.find(sep)
            if sep_idx == -1:
                continue
        raw_hdrs = part[:sep_idx].decode("utf-8", errors="replace")
        payload = part[sep_idx + len(sep) :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]

        disp = ""
        ctype = "application/octet-stream"
        for hline in raw_hdrs.splitlines():
            if ":" in hline:
                hk, hv = hline.split(":", 1)
                if hk.strip().lower() == "content-disposition":
                    disp = hv.strip()
                elif hk.strip().lower() == "content-type":
                    ctype = hv.strip()
        nm = re.search(r'name="([^"]+)"', disp)
        if not nm:
            continue
        field_name = nm.group(1)
        fnm = re.search(r'filename="([^"]*)"', disp)
        if fnm is not None:
            fname = fnm.group(1)
            if fname or payload:
                files.setdefault(field_name, []).append(
                    UploadedFile(filename=fname, content_type=ctype, data=payload)
                )
        else:
            form.setdefault(field_name, []).append(payload.decode("utf-8", errors="replace"))
    return form, files


def _compile_route(pattern: str) -> re.Pattern[str]:
    def _repl(m: re.Match[str]) -> str:
        full = m.group(0)
        name = m.group(1)
        if full.startswith("<path:"):
            return f"(?P<{name}>.+)"
        return f"(?P<{name}>[^/]+)"

    escaped = re.sub(r"<(?:path:)?([a-zA-Z_][a-zA-Z0-9_]*)>", _repl, pattern)
    return re.compile(f"^{escaped}$")


class WebApp:
    """Minimal WSGI web application with Jinja2, CSRF, and strict security headers."""

    def __init__(self, template_dir: Path, static_dir: Path, session_secret: str) -> None:
        self.static_dir = Path(static_dir)
        self.session_secret = session_secret
        self.jinja = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self._routes: list[tuple[str, re.Pattern[str], Callable[[Request], Response], bool]] = []
        self._before_request: list[Callable[[Request], Response | None]] = []

    def before_request(self, fn: Callable[[Request], Response | None]) -> Callable[[Request], Response | None]:
        self._before_request.append(fn)
        return fn

    def route(
        self,
        path: str,
        methods: tuple[str, ...] = ("GET",),
        csrf_exempt: bool = False,
    ) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
        regex = _compile_route(path)

        def decorator(fn: Callable[[Request], Response]) -> Callable[[Request], Response]:
            for m in methods:
                self._routes.append((m.upper(), regex, fn, csrf_exempt))
            return fn

        return decorator

    def render(self, template_name: str, status: int = 200, **ctx: Any) -> Response:
        tpl = self.jinja.get_template(template_name)
        return Response(tpl.render(**ctx), status=status, content_type="text/html; charset=utf-8")

    def redirect(self, location: str, status: int = 303) -> Response:
        return Response(b"", status=status, headers={"Location": location})

    def json(self, data: Any, status: int = 200) -> Response:
        return Response(json.dumps(data), status=status, content_type="application/json; charset=utf-8")

    def make_csrf_token(self, session_key: str) -> str:
        nonce = secrets.token_hex(16)
        sig = hmac.new(
            self.session_secret.encode("utf-8"),
            f"{session_key}:{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        return f"{nonce}.{sig}"

    def verify_csrf_token(self, token: str, session_key: str) -> bool:
        if "." not in (token or ""):
            return False
        nonce, sig = token.split(".", 1)
        expected = hmac.new(
            self.session_secret.encode("utf-8"),
            f"{session_key}:{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        return hmac.compare_digest(sig, expected)

    def _apply_security_headers(self, resp: Response) -> None:
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self' https://github.com https://gitlab.com http://localhost:* http://127.0.0.1:*",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")

    def _serve_static(self, rel_path: str) -> Response:
        safe_rel = Path(rel_path.lstrip("/"))
        target = (self.static_dir / safe_rel).resolve()
        if not str(target).startswith(str(self.static_dir.resolve())) or not target.is_file():
            return Response("Not Found", status=404, content_type="text/plain; charset=utf-8")
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".svg":
            mime = "image/svg+xml"
        return Response(
            target.read_bytes(),
            status=200,
            content_type=mime or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def _parse_request(self, environ: dict[str, Any]) -> tuple[Request | None, Response | None]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = unquote(str(environ.get("PATH_INFO") or "/"))
        qs = str(environ.get("QUERY_STRING") or "")
        hdrs: dict[str, str] = {}
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                hdrs[k[5:].replace("_", "-").title()] = str(v)
        if environ.get("CONTENT_TYPE"):
            hdrs["Content-Type"] = str(environ["CONTENT_TYPE"])

        cookie_map: dict[str, str] = {}
        if "Cookie" in hdrs:
            sc = cookies.SimpleCookie()
            try:
                sc.load(hdrs["Cookie"])
                cookie_map = {k: m.value for k, m in sc.items()}
            except cookies.CookieError:
                cookie_map = {}

        cl_str = str(environ.get("CONTENT_LENGTH") or "0").strip()
        content_len = int(cl_str) if cl_str.isdigit() else 0
        if content_len > MAX_UPLOAD_TOTAL:
            return None, Response("Payload Too Large", status=413, content_type="text/plain; charset=utf-8")

        raw_body = b""
        if content_len > 0:
            raw_body = environ["wsgi.input"].read(content_len)
        elif method in ("POST", "PUT", "PATCH") and isinstance(environ.get("wsgi.input"), io.BytesIO):
            raw_body = environ["wsgi.input"].read(MAX_UPLOAD_TOTAL + 1)
            if len(raw_body) > MAX_UPLOAD_TOTAL:
                return None, Response("Payload Too Large", status=413, content_type="text/plain; charset=utf-8")

        ctype = hdrs.get("Content-Type", "")
        form: dict[str, list[str]] = {}
        files: dict[str, list[UploadedFile]] = {}
        if method in ("POST", "PUT", "PATCH") and raw_body:
            if "multipart/form-data" in ctype.lower():
                form, files = _parse_multipart(ctype, raw_body)
            elif "application/x-www-form-urlencoded" in ctype.lower() or not ctype:
                form = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)

        scheme = hdrs.get("X-Forwarded-Proto") or str(environ.get("wsgi.url_scheme") or "http")
        host = hdrs.get("X-Forwarded-Host") or hdrs.get("Host") or str(environ.get("HTTP_HOST") or "localhost:8080")
        req = Request(
            method=method,
            path=path,
            query_string=qs,
            headers=hdrs,
            cookies=cookie_map,
            query=parse_qs(qs, keep_blank_values=True),
            body=raw_body,
            form=form,
            files=files,
            scheme=scheme.split(",")[0].strip(),
            host=host.split(",")[0].strip(),
        )
        return req, None

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        req, err_resp = self._parse_request(environ)
        if err_resp is not None or req is None:
            resp = err_resp or Response("Bad Request", status=400, content_type="text/plain")
            self._apply_security_headers(resp)
            start_response(_STATUS_TEXT.get(resp.status, f"{resp.status} Error"), resp.header_list())
            return [resp.body]

        if req.path.startswith("/static/") and req.method == "GET":
            resp = self._serve_static(req.path[len("/static/") :])
            self._apply_security_headers(resp)
            start_response(_STATUS_TEXT.get(resp.status, f"{resp.status} OK"), resp.header_list())
            return [resp.body]

        handler: Callable[[Request], Response] | None = None
        csrf_exempt = False
        matched_path = False
        for route_method, regex, fn, exempt in self._routes:
            m = regex.match(req.path)
            if m:
                matched_path = True
                if route_method == req.method:
                    req.params = {k: unquote(v) for k, v in m.groupdict().items()}
                    handler = fn
                    csrf_exempt = exempt
                    break

        if handler is None:
            resp = Response(
                "Method Not Allowed" if matched_path else "Not Found",
                status=405 if matched_path else 404,
                content_type="text/plain; charset=utf-8",
            )
            self._apply_security_headers(resp)
            start_response(_STATUS_TEXT[resp.status], resp.header_list())
            return [resp.body]

        for hook in self._before_request:
            hook_resp = hook(req)
            if hook_resp is not None:
                self._apply_security_headers(hook_resp)
                start_response(_STATUS_TEXT.get(hook_resp.status, f"{hook_resp.status} OK"), hook_resp.header_list())
                return [hook_resp.body]

        # CSRF check on mutating requests unless exempt (e.g. webhooks)
        new_csrf_cookie = ""
        csrf_session_key = (req.session or {}).get("id") or req.cookies.get("lens_csrf_sid") or ""
        if not csrf_session_key:
            csrf_session_key = secrets.token_hex(12)
            new_csrf_cookie = csrf_session_key
        req.csrf_token = self.make_csrf_token(csrf_session_key)

        if req.method in ("POST", "PUT", "DELETE", "PATCH") and not csrf_exempt:
            submitted_tok = req.form_val("_csrf") or req.headers.get("X-Csrf-Token", "")
            if not self.verify_csrf_token(submitted_tok, csrf_session_key):
                resp = Response("CSRF verification failed", status=403, content_type="text/plain; charset=utf-8")
                self._apply_security_headers(resp)
                start_response(_STATUS_TEXT[403], resp.header_list())
                return [resp.body]

        resp = handler(req)
        if new_csrf_cookie and "lens_csrf_sid" not in req.cookies:
            resp.set_cookie("lens_csrf_sid", new_csrf_cookie, httponly=True, samesite="Lax")
        self._apply_security_headers(resp)
        start_response(_STATUS_TEXT.get(resp.status, f"{resp.status} OK"), resp.header_list())
        return [resp.body]


class QuietWSGIHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: Any, handler_class: type[WSGIRequestHandler] = QuietWSGIHandler) -> None:
        super().__init__(server_address, handler_class)
        self.set_app(app)


class TestClient:
    """In-process WSGI test client supporting cookies and multipart uploads."""

    def __init__(self, app: WebApp) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}

    def _call(
        self,
        method: str,
        path: str,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        if "?" in path:
            p_info, qs = path.split("?", 1)
        else:
            p_info, qs = path, ""
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": p_info,
            "QUERY_STRING": qs,
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "localhost:8080",
            "CONTENT_LENGTH": str(len(data)),
            "wsgi.input": io.BytesIO(data),
        }
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for k, v in (headers or {}).items():
            if k.lower() == "content-type":
                environ["CONTENT_TYPE"] = v
            else:
                environ["HTTP_" + k.upper().replace("-", "_")] = v

        captured: dict[str, Any] = {}

        def start_response(status: str, resp_headers: list[tuple[str, str]]) -> None:
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = resp_headers

        body = b"".join(self.app(environ, start_response))
        hdr_dict: dict[str, str] = {}
        for hk, hv in captured["headers"]:
            if hk.lower() == "set-cookie":
                sc = cookies.SimpleCookie()
                sc.load(hv)
                for ck, cm in sc.items():
                    if cm["max-age"] == "0":
                        self.cookies.pop(ck, None)
                    else:
                        self.cookies[ck] = cm.value
            else:
                hdr_dict[hk] = hv

        status_code = captured["status"]
        if follow_redirects and status_code in (301, 302, 303, 307, 308) and "Location" in hdr_dict:
            return self._call("GET", hdr_dict["Location"], follow_redirects=True)
        return status_code, hdr_dict, body

    def get(self, path: str, follow_redirects: bool = False, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        return self._call("GET", path, headers=headers, follow_redirects=follow_redirects)

    def post(
        self,
        path: str,
        form: dict[str, str] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        from urllib.parse import urlencode

        req_hdrs = dict(headers or {})
        if raw_body is not None:
            body = raw_body
        elif json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req_hdrs.setdefault("Content-Type", "application/json")
        else:
            body = urlencode(form or {}).encode("utf-8")
            req_hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return self._call("POST", path, data=body, headers=req_hdrs, follow_redirects=follow_redirects)
