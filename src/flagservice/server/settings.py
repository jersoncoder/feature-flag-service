"""Server settings, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MIN_ADMIN_KEY_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    database_path: Path
    admin_key: str

    def __post_init__(self) -> None:
        if len(self.admin_key) < MIN_ADMIN_KEY_LENGTH:
            raise ValueError(
                f"the admin key must be at least {MIN_ADMIN_KEY_LENGTH} characters; "
                'generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """``FLAGS_ADMIN_KEY`` (required) and ``FLAGS_DATABASE`` (default ``flags.db``)."""
        env = os.environ if environ is None else environ
        admin_key = env.get("FLAGS_ADMIN_KEY")
        if not admin_key:
            raise ValueError("FLAGS_ADMIN_KEY is not set")
        return cls(
            database_path=Path(env.get("FLAGS_DATABASE", "flags.db")),
            admin_key=admin_key,
        )
