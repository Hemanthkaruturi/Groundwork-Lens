"""Shared git and image test builders."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import zlib
from pathlib import Path

from engine.groundwork_common import body_hash, split_front_matter


def git_run(cwd: Path, args: list[str], commit_date: str = "2026-11-20T12:00:00Z") -> str:
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = commit_date
    env["GIT_COMMITTER_DATE"] = commit_date
    env["GIT_TERMINAL_PROMPT"] = "0"
    res = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Hemanth Karuturi",
            "-c",
            "user.email=karuturihemanth@gmail.com",
            *args,
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_run(path, ["init", "-b", "main"])
    git_run(path, ["config", "receive.denyCurrentBranch", "updateInstead"])
    return path


def commit_all(path: Path, message: str, date: str = "2026-11-20T12:00:00Z") -> str:
    git_run(path, ["add", "-A"], commit_date=date)
    git_run(path, ["commit", "-m", message], commit_date=date)
    return git_run(path, ["rev-parse", "HEAD"], commit_date=date)


def create_minimal_gw_repo(
    path: Path,
    name: str = "sample-repo",
    extra_people: str = "",
) -> Path:
    init_git_repo(path)
    (path / ".groundwork").mkdir(parents=True, exist_ok=True)
    (path / ".groundwork" / "config.json").write_text(json.dumps({"name": name, "kind": "repo"}), encoding="utf-8")
    (path / ".groundwork" / "approvals.json").write_text("{}", encoding="utf-8")
    (path / ".groundwork" / "ledger.jsonl").write_text(
        f'{{"at":"2026-11-10T09:00:00Z","type":"created","item":"{name}","who":"Hemanth Karuturi"}}\n',
        encoding="utf-8",
    )
    people_tbl = (
        f"# {name}\n\n## Who works on what\n\n"
        "| Name | Role | Contact |\n| --- | --- | --- |\n"
        "| Hemanth Karuturi | Owner | karuturihemanth@gmail.com, Hemanthkaruturi |\n"
        "| Amit Singh | Reviewer | amit@example.com, amitsingh |\n"
    )
    if extra_people:
        people_tbl += extra_people
    (path / "PROJECT.md").write_text(people_tbl, encoding="utf-8")
    (path / "ARCHITECTURE.md").write_text("# Architecture\nService.\n", encoding="utf-8")
    (path / "CONSTITUTION.md").write_text("# Constitution\nSimple rules.\n", encoding="utf-8")
    return path


def doc_hash(path: Path) -> str:
    _, body = split_front_matter(path.read_text(encoding="utf-8"))
    return body_hash(body)


def make_png_with_text_chunk(secret_text: bytes = b"GPS=37.77,-122.41") -> bytes:
    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    text_chunk = b"Comment\x00" + secret_text
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", text_chunk)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def make_jpeg_with_exif(exif_payload: bytes = b"Exif\x00\x00GPS_SECRET") -> bytes:
    app1 = struct.pack(">H", len(exif_payload) + 2) + exif_payload
    com_txt = b"SECRET_CAMERA_SERIAL"
    com = struct.pack(">H", len(com_txt) + 2) + com_txt
    sof0 = struct.pack(">H", 11) + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    sos = struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe1" + app1
        + b"\xff\xfe" + com
        + b"\xff\xc0" + sof0
        + b"\xff\xda" + sos
        + b"\x00\x01\x02"
        + b"\xff\xd9"
    )


def make_gif_with_comment(comment: bytes = b"HIDDEN_COMMENT") -> bytes:
    header = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x00, 0, 0)
    com_ext = b"\x21\xfe" + bytes([len(comment)]) + comment + b"\x00"
    img = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x44\x01\x00"
    return header + com_ext + img + b"\x3b"


def make_webp_with_exif(exif: bytes = b"GPS_LAT_LON") -> bytes:
    vp8_data = b"\x00" * 10
    exif_pad = exif + (b"\x00" if len(exif) % 2 else b"")
    body = (
        b"WEBP"
        + b"VP8 " + struct.pack("<I", len(vp8_data)) + vp8_data
        + b"EXIF" + struct.pack("<I", len(exif)) + exif_pad
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body
