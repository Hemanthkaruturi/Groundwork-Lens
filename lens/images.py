"""Image validation (magic bytes, size/count limits) and metadata stripping."""

from __future__ import annotations

import io
import struct
import zlib

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 5


class ImageValidationError(ValueError):
    """Raised when an uploaded image fails format, size, or count rules."""


def detect_image_format(raw: bytes) -> tuple[str, str, str]:
    """Detect PNG, JPEG, GIF, or WebP from magic bytes only."""
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", ".png", "image/png"
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return "jpeg", ".jpg", "image/jpeg"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", ".gif", "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", ".webp", "image/webp"
    raise ImageValidationError("Only PNG, JPEG, WebP, and GIF images are accepted (checked by file content).")


def _strip_png(raw: bytes) -> bytes:
    """Keep only critical + safe color PNG chunks, stripping tEXt/iTXt/zTXt/eXIf/tIME."""
    sig = raw[:8]
    pos = 8
    out = bytearray(sig)
    safe_chunks = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM"}
    saw_ihdr = False
    saw_iend = False
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        data_end = pos + 8 + length
        crc_end = data_end + 4
        if crc_end > len(raw):
            raise ImageValidationError("Corrupted PNG image")
        data = raw[pos + 8 : data_end]
        if ctype == b"IHDR":
            saw_ihdr = True
        if ctype == b"IEND":
            saw_iend = True
        if ctype in safe_chunks:
            crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
            out.extend(struct.pack(">I", length))
            out.extend(ctype)
            out.extend(data)
            out.extend(struct.pack(">I", crc))
        pos = crc_end
        if saw_iend:
            break
    if not saw_ihdr or not saw_iend:
        raise ImageValidationError("Incomplete PNG image")
    return bytes(out)


def _strip_jpeg(raw: bytes) -> bytes:
    """Strip APP1-APP15 (EXIF/GPS/XMP/IPTC) and COM markers from JPEG."""
    if not (len(raw) >= 4 and raw[:2] == b"\xff\xd8"):
        raise ImageValidationError("Corrupted JPEG image")
    out = bytearray(b"\xff\xd8")
    pos = 2
    n = len(raw)
    while pos < n:
        if raw[pos] != 0xFF:
            raise ImageValidationError("Corrupted JPEG marker stream")
        while pos < n and raw[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = raw[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            out.extend(b"\xff\xd9")
            break
        if marker == 0x00 or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            out.extend(bytes([0xFF, marker]))
            continue
        if pos + 2 > n:
            raise ImageValidationError("Truncated JPEG segment")
        seg_len = struct.unpack(">H", raw[pos : pos + 2])[0]
        if seg_len < 2 or pos + seg_len > n:
            raise ImageValidationError("Invalid JPEG segment length")
        seg_data = raw[pos : pos + seg_len]
        pos += seg_len
        if marker == 0xDA:  # SOS -> followed by entropy-coded data until EOI
            out.extend(bytes([0xFF, marker]))
            out.extend(seg_data)
            out.extend(raw[pos:])
            break
        # Drop APP1..APP15 (EXIF/GPS/XMP) and COM (0xFE)
        if (0xE1 <= marker <= 0xEF) or marker == 0xFE:
            continue
        out.extend(bytes([0xFF, marker]))
        out.extend(seg_data)
    return bytes(out)


def _strip_gif(raw: bytes) -> bytes:
    """Strip Comment (0xFE) and Application (0xFF, except NETSCAPE2.0) extensions from GIF."""
    if len(raw) < 13:
        raise ImageValidationError("Corrupted GIF image")
    out = bytearray(raw[:13])
    pos = 13
    packed = raw[10]
    if packed & 0x80:
        gct_size = 3 * (1 << ((packed & 0x07) + 1))
        if pos + gct_size > len(raw):
            raise ImageValidationError("Corrupted GIF color table")
        out.extend(raw[pos : pos + gct_size])
        pos += gct_size

    def _skip_subblocks(start: int) -> int:
        p = start
        while p < len(raw):
            sz = raw[p]
            p += 1
            if sz == 0:
                return p
            p += sz
        return p

    while pos < len(raw):
        b = raw[pos]
        if b == 0x3B:  # Trailer
            out.append(0x3B)
            break
        if b == 0x21:  # Extension
            if pos + 2 >= len(raw):
                break
            label = raw[pos + 1]
            if label == 0xFE:  # Comment extension -> strip
                pos = _skip_subblocks(pos + 2)
                continue
            if label == 0xFF:  # Application extension
                blk_sz = raw[pos + 2]
                app_id = raw[pos + 3 : pos + 3 + blk_sz]
                end_pos = _skip_subblocks(pos + 3 + blk_sz)
                if app_id.startswith(b"NETSCAPE2.0"):
                    out.extend(raw[pos:end_pos])
                pos = end_pos
                continue
            # Graphic control or plain text extension -> keep
            if pos + 2 < len(raw):
                blk_sz = raw[pos + 2]
                end_pos = _skip_subblocks(pos + 3 + blk_sz)
                out.extend(raw[pos:end_pos])
                pos = end_pos
                continue
        if b == 0x2C:  # Image descriptor
            if pos + 10 > len(raw):
                raise ImageValidationError("Corrupted GIF frame")
            img_packed = raw[pos + 9]
            lct_size = 0
            if img_packed & 0x80:
                lct_size = 3 * (1 << ((img_packed & 0x07) + 1))
            data_start = pos + 10 + lct_size + 1  # +1 LZW min code size
            end_pos = _skip_subblocks(data_start)
            out.extend(raw[pos:end_pos])
            pos = end_pos
            continue
        break
    return bytes(out)


def _strip_webp(raw: bytes) -> bytes:
    """Strip EXIF and XMP metadata chunks from WebP RIFF container."""
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise ImageValidationError("Corrupted WebP image")
    pos = 12
    chunks: list[tuple[bytes, bytes]] = []
    while pos + 8 <= len(raw):
        fourcc = raw[pos : pos + 4]
        size = struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        data_start = pos + 8
        data_end = data_start + size
        if data_end > len(raw):
            raise ImageValidationError("Truncated WebP chunk")
        data = raw[data_start:data_end]
        pos = data_end + (size & 1)
        if fourcc in (b"EXIF", b"XMP "):
            continue
        if fourcc == b"VP8X" and len(data) >= 10:
            # Clear EXIF (bit 3 = 0x08) and XMP (bit 2 = 0x04) feature flags
            ba = bytearray(data)
            ba[0] &= ~(0x08 | 0x04)
            data = bytes(ba)
        chunks.append((fourcc, data))

    body = bytearray(b"WEBP")
    for fourcc, data in chunks:
        body.extend(fourcc)
        body.extend(struct.pack("<I", len(data)))
        body.extend(data)
        if len(data) & 1:
            body.append(0)
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)


def validate_and_strip_image(raw_bytes: bytes, filename: str = "") -> tuple[bytes, str, str]:
    """Validate an uploaded image and strip metadata (re-encoding via Pillow if available)."""
    if not raw_bytes:
        raise ImageValidationError(f"Empty image file {filename!r}")
    if len(raw_bytes) > MAX_IMAGE_BYTES:
        raise ImageValidationError(f"Image {filename or 'upload'} exceeds the 5 MB limit.")

    fmt, ext, mime = detect_image_format(raw_bytes)

    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(raw_bytes)) as img:
            out_buf = io.BytesIO()
            pil_fmt = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}[fmt]
            if pil_fmt == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out_buf, format=pil_fmt)
            return out_buf.getvalue(), ext, mime
    except ImportError:
        pass

    if fmt == "png":
        return _strip_png(raw_bytes), ext, mime
    if fmt == "jpeg":
        return _strip_jpeg(raw_bytes), ext, mime
    if fmt == "gif":
        return _strip_gif(raw_bytes), ext, mime
    return _strip_webp(raw_bytes), ext, mime
