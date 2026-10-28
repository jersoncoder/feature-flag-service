"""Semantic Versioning 2.0.0 parsing and precedence.

Only what targeting needs: parse a version string and compare two versions by
precedence (section 11 of https://semver.org). Build metadata is parsed so that
valid strings are accepted, and then ignored, as the specification requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# The regular expression suggested by semver.org, restricted to ASCII digits.
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)

# A prerelease identifier as a sort key: numeric identifiers sort before
# alphanumeric ones and compare as integers; alphanumeric ones compare in ASCII order.
_Identifier = tuple[int, int] | tuple[int, str]


@dataclass(frozen=True, eq=False)
class SemVer:
    """A parsed version. Comparison follows SemVer precedence, so build metadata is ignored."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def _key(self) -> tuple[int, int, int, int, tuple[_Identifier, ...]]:
        # A release (no prerelease) has higher precedence than any of its prereleases.
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        identifiers: list[_Identifier] = [
            (0, int(part)) if part.isdigit() else (1, part) for part in self.prerelease
        ]
        return (self.major, self.minor, self.patch, 0, tuple(identifiers))

    def __lt__(self, other: SemVer) -> bool:
        return self._key() < other._key()

    def __le__(self, other: SemVer) -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: SemVer) -> bool:
        return self._key() > other._key()

    def __ge__(self, other: SemVer) -> bool:
        return self._key() >= other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text


@lru_cache(maxsize=4096)
def parse_semver(text: str) -> SemVer | None:
    """Return the parsed version, or None when ``text`` is not a valid SemVer 2.0.0 string."""
    match = _SEMVER.fullmatch(text)
    if match is None:
        return None
    major, minor, patch, prerelease, build = match.groups()
    return SemVer(
        major=int(major),
        minor=int(minor),
        patch=int(patch),
        prerelease=tuple(prerelease.split(".")) if prerelease else (),
        build=tuple(build.split(".")) if build else (),
    )
