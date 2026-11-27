"""At-rest Fernet encryption for tokens and app secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class KeyManager:
    """Manages the master encryption key stored in <data_dir>/master.key or LENS_MASTER_KEY."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.key_file = self.data_dir / "master.key"
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        env_key = os.environ.get("LENS_MASTER_KEY", "").strip()
        if env_key:
            return env_key.encode("utf-8")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_file.is_file():
            raw = self.key_file.read_bytes().strip()
            if raw:
                return raw
        key = Fernet.generate_key()
        fd = os.open(str(self.key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key + b"\n")
        finally:
            os.close(fd)
        return key

    def encrypt_str(self, plaintext: str) -> str:
        """Encrypt a string into a UTF-8 Fernet token."""
        if plaintext == "":
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt_str(self, token: str) -> str:
        """Decrypt a Fernet token back to plaintext string."""
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt stored secret with current master key") from exc

    def encrypt_json(self, obj: Any) -> str:
        """Encrypt a JSON-serializable object."""
        return self.encrypt_str(json.dumps(obj, separators=(",", ":")))

    def decrypt_json(self, token: str, default: Any = None) -> Any:
        """Decrypt a JSON payload."""
        if not token:
            return default
        return json.loads(self.decrypt_str(token))
