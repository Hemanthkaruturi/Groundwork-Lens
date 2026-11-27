"""Security, image metadata stripping, markdown sanitization, and WCAG AA contrast tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lens.app import create_app
from lens.images import ImageValidationError, MAX_IMAGE_BYTES, validate_and_strip_image
from lens.markdown_safe import render_markdown_safe
from lens.store import LensStore
from lens.wsgi import TestClient
from tests.helpers import (
    make_gif_with_comment,
    make_jpeg_with_exif,
    make_png_with_text_chunk,
    make_webp_with_exif,
)


def _srgb_channel(c: int) -> float:
    x = c / 255.0
    return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4


def _rel_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _rel_luminance(fg_hex)
    l2 = _rel_luminance(bg_hex)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


class TestSecurityAndImages(unittest.TestCase):
    def test_secrets_encrypted_at_rest_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = LensStore(Path(td))
            secret_tok = "glpat-super-secret-value-987654321"
            store.upsert_host(
                kind="gitlab",
                name="GitLab",
                base_url="https://gitlab.com",
                api_url="https://gitlab.com/api/v4",
                credentials={"client_secret": "oauth-secret-xyz"},
                service_token=secret_tok,
                webhook_secret="wh-secret-abc",
            )
            store.close()
            raw_db = (Path(td) / "lens.db").read_bytes()
            self.assertNotIn(secret_tok.encode(), raw_db)
            self.assertNotIn(b"oauth-secret-xyz", raw_db)
            self.assertNotIn(b"wh-secret-abc", raw_db)

    def test_csrf_rejection_and_security_headers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = create_app(Path(td) / "data", start_worker=False)
            client = TestClient(app)
            status, hdrs, _ = client.get("/login")
            self.assertEqual(status, 200)
            self.assertIn("default-src 'self'", hdrs["Content-Security-Policy"])
            self.assertEqual(hdrs["X-Frame-Options"], "DENY")
            self.assertEqual(hdrs["X-Content-Type-Options"], "nosniff")

            # POST without CSRF token must be rejected
            st_bad, _, _ = client.post("/setup/unlock", form={"setup_code": "WRONG"})
            self.assertEqual(st_bad, 403)
            app.ctx.close()

    def test_markdown_xss_and_external_links_sanitized(self) -> None:
        dirty = '# Heading\n\n<script>alert(1)</script>\n\n[Click](javascript:alert(1)) and [GitHub](https://github.com/org/repo)\n'
        html_out, toc = render_markdown_safe(dirty)
        self.assertNotIn("<script>", html_out)
        self.assertNotIn('href="javascript:', html_out)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html_out)
        self.assertEqual(toc[0]["text"], "Heading")

    def test_image_validation_and_metadata_stripping(self) -> None:
        # PNG tEXt GPS stripped
        png_clean, ext, _ = validate_and_strip_image(make_png_with_text_chunk(b"GPS=37.77"), "pic.png")
        self.assertEqual(ext, ".png")
        self.assertNotIn(b"GPS=37.77", png_clean)

        # JPEG EXIF + COM stripped
        jpg_clean, ext_j, _ = validate_and_strip_image(make_jpeg_with_exif(b"Exif\x00\x00GPS_SECRET"), "pic.jpg")
        self.assertEqual(ext_j, ".jpg")
        self.assertNotIn(b"GPS_SECRET", jpg_clean)
        self.assertNotIn(b"SECRET_CAMERA_SERIAL", jpg_clean)

        # GIF Comment stripped
        gif_clean, ext_g, _ = validate_and_strip_image(make_gif_with_comment(b"HIDDEN_COMMENT"), "pic.gif")
        self.assertEqual(ext_g, ".gif")
        self.assertNotIn(b"HIDDEN_COMMENT", gif_clean)

        # WebP EXIF chunk stripped
        webp_clean, ext_w, _ = validate_and_strip_image(make_webp_with_exif(b"GPS_LAT_LON"), "pic.webp")
        self.assertEqual(ext_w, ".webp")
        self.assertNotIn(b"GPS_LAT_LON", webp_clean)

        # Fake PNG extension rejected by magic-byte inspection
        with self.assertRaises(ImageValidationError):
            validate_and_strip_image(b"#!/bin/sh\nrm -rf /\n", "fake.png")

        # > 5 MB rejected
        with self.assertRaises(ImageValidationError):
            validate_and_strip_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 10), "huge.png")

    def test_wcag_aa_contrast_pairs(self) -> None:
        light_pairs = [
            ("#0A2260", "#FFFFFF"),
            ("#0A2260", "#F3F6FB"),
            ("#4A5570", "#FFFFFF"),
            ("#4A5570", "#F3F6FB"),
            ("#FFFFFF", "#0B5BE0"),
            ("#0B7A50", "#FFFFFF"),
            ("#92400E", "#FEF3C7"),
            ("#B42318", "#FEE4E2"),
        ]
        dark_pairs = [
            ("#E6ECF7", "#0D1B3E"),
            ("#E6ECF7", "#050B1A"),
            ("#9AA8C7", "#0D1B3E"),
            ("#050B1A", "#2B90FF"),
            ("#2EE6A8", "#0D1B3E"),
            ("#FCD34D", "#3A2A05"),
            ("#FCA5A5", "#3B0D0D"),
        ]
        for fg, bg in light_pairs + dark_pairs:
            ratio = contrast_ratio(fg, bg)
            self.assertGreaterEqual(ratio, 4.5, f"Pair {fg} on {bg} contrast {ratio:.2f} < 4.5")
