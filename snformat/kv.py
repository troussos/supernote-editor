"""Ordered <KEY:VALUE> metadata text handling.

Metadata blocks are UTF-8 text of the form `<KEY1:VALUE1><KEY2:VALUE2>...`.
Keys may repeat (e.g. KEYWORD_ entries in the footer). Order and duplicates
are significant for faithful round-trips, so metadata is modelled as an
ordered list of (key, value) string pairs rather than a dict.
"""
from __future__ import annotations

import re

_KV_RE = re.compile(r"<([^:<>]+):(.*?)>")


class MetadataError(ValueError):
    pass


class Meta:
    """Ordered multimap of metadata parameters."""

    __slots__ = ("pairs",)

    def __init__(self, pairs=None):
        self.pairs: list[list[str]] = [list(p) for p in (pairs or [])]

    @classmethod
    def parse(cls, payload: bytes, *, what: str = "metadata") -> "Meta":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as e:
            raise MetadataError(f"{what}: not valid UTF-8: {e}") from None
        pairs = [[m[1], m[2]] for m in _KV_RE.finditer(text)]
        residue = _KV_RE.sub("", text)
        if residue.strip():
            # Bytes outside <..:..> pairs would be silently lost on rewrite;
            # refuse rather than risk dropping meaning we don't understand.
            raise MetadataError(f"{what}: unrecognized content outside <KEY:VALUE> pairs: {residue!r}")
        return cls(pairs)

    def serialize(self) -> bytes:
        out = []
        for k, v in self.pairs:
            if ":" in k or "<" in k or ">" in k or "<" in v or ">" in v:
                raise MetadataError(f"illegal characters in pair ({k!r}, {v!r})")
            out.append(f"<{k}:{v}>")
        return "".join(out).encode("utf-8")

    # -- read helpers --
    def get(self, key: str, default: str | None = None) -> str | None:
        for k, v in self.pairs:
            if k == key:
                return v
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except ValueError:
            return default

    def get_all(self, key: str) -> list[str]:
        return [v for k, v in self.pairs if k == key]

    def keys(self) -> list[str]:
        return [k for k, _ in self.pairs]

    def has(self, key: str) -> bool:
        return any(k == key for k, _ in self.pairs)

    # -- write helpers --
    def set(self, key: str, value) -> None:
        """Replace the first occurrence of key (append if missing)."""
        value = str(value)
        for p in self.pairs:
            if p[0] == key:
                p[1] = value
                return
        self.pairs.append([key, value])

    def set_if_present(self, key: str, value) -> None:
        for p in self.pairs:
            if p[0] == key:
                p[1] = str(value)
                return

    def remove_all(self, key: str) -> None:
        self.pairs = [p for p in self.pairs if p[0] != key]

    def copy(self) -> "Meta":
        return Meta(self.pairs)

    def __repr__(self):
        return f"Meta({self.pairs!r})"
