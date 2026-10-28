"""Entity tags for flags (optimistic concurrency) and published configs (caching)."""

from __future__ import annotations

import hashlib
import re

# entity-tag = [ "W/" ] DQUOTE *etagc DQUOTE, in a comma-separated list (RFC 9110).
_TAG = re.compile(r'\s*(W/)?"([\x21\x23-\x7e\x80-\xff]*)"\s*(?:,|$)')


class MalformedHeader(ValueError):
    pass


def flag_etag(flag_id: int, version: int) -> str:
    """A strong ETag for one flag version.

    It includes the flag's row id, which is never reused, so a flag deleted and
    created again with the same key does not accept ETags from its predecessor.
    """
    return f'"{flag_id}-{version}"'


def content_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def parse_tags(header: str) -> list[tuple[bool, str]]:
    """Parse an If-Match or If-None-Match value into ``(weak, tag)`` pairs.

    ``*`` is returned as ``[(False, "*")]``.
    """
    if header.strip() == "*":
        return [(False, "*")]
    tags: list[tuple[bool, str]] = []
    position = 0
    while position < len(header):
        match = _TAG.match(header, position)
        if match is None or match.end() == position:
            raise MalformedHeader(f"cannot parse entity tags in {header!r}")
        tags.append((match.group(1) is not None, f'"{match.group(2)}"'))
        position = match.end()
    if not tags:
        raise MalformedHeader("empty entity tag list")
    return tags


def strong_match(tags: list[tuple[bool, str]], current: str) -> bool:
    """If-Match comparison: weak tags never match."""
    return any(not weak and tag == current for weak, tag in tags)


def weak_match(tags: list[tuple[bool, str]], current: str) -> bool:
    """If-None-Match comparison: the W/ prefix is ignored, ``*`` matches anything."""
    return any(tag in ("*", current) for _, tag in tags)
