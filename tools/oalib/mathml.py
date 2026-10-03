"""Batch LaTeX -> MathML through one node process running the vendored Temml (results cached)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .util import BuildError, run, sha256_text, write_json

_TOKEN = re.compile("\u0000M(\\d+)\u0000")


class MathRegistry:
    def __init__(self, node: Path, script: Path, cache_file: Path):
        self.node, self.script, self.cache_file = node, script, cache_file
        self.items: list[tuple[str, bool, str]] = []   # (tex, display, where)
        self.index: dict[tuple[str, bool], int] = {}
        self.cache: dict[str, str] = {}
        if cache_file.exists():
            self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.results: dict[int, str] = {}

    def add(self, tex: str, display: bool, where: str) -> str:
        key = (tex.strip(), display)
        if key not in self.index:
            self.index[key] = len(self.items)
            self.items.append((key[0], display, where))
        return f"\u0000M{self.index[key]}\u0000"

    @staticmethod
    def _ckey(tex: str, display: bool) -> str:
        return sha256_text(("D:" if display else "I:") + tex)

    def render(self) -> None:
        todo = [(i, t, d, w) for i, (t, d, w) in enumerate(self.items) if self._ckey(t, d) not in self.cache]
        if todo:
            payload = json.dumps([{"tex": t, "display": d} for _, t, d, _ in todo])
            res = run([str(self.node), str(self.script)], input_text=payload, timeout=120)
            out = json.loads(res.stdout)
            errors = []
            for (i, t, d, w), r in zip(todo, out):
                if not r.get("ok"):
                    errors.append(f"{w}: ${t}$ -> {r.get('error')}")
                    continue
                self.cache[self._ckey(t, d)] = r["mathml"]
            if errors:
                raise BuildError("math conversion failed:\n  " + "\n  ".join(errors[:20]))
            write_json(self.cache_file, self.cache)
        for i, (t, d, _) in enumerate(self.items):
            self.results[i] = self.cache[self._ckey(t, d)]

    def fill(self, html: str) -> str:
        return _TOKEN.sub(lambda m: self.results[int(m.group(1))], html)
