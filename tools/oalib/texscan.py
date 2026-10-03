"""Low-level LaTeX scanning helpers that keep character offsets intact (so errors map to file:line)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .util import BuildError

_URL_CMD = re.compile(r"\\(?:url|href)\s*\{")


def blank_comments(text: str) -> str:
    """Replace every unescaped `%...` comment with spaces (newlines kept), leaving \\url arguments alone."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            m = _URL_CMD.match(text, i)
            if m:  # skip the URL argument verbatim
                j = m.end()
                depth = 1
                while j < n and depth:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                i = j
                continue
            i += 2  # escaped character (\%, \\, \{ ...)
            continue
        if c == "%":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\n":
        i += 1
    return i


def read_group(text: str, i: int, *, what: str = "argument") -> tuple[str, int]:
    """Read a balanced {...} group starting at text[i] (after optional whitespace). Returns (inner, end)."""
    i = skip_ws(text, i)
    if i >= len(text) or text[i] != "{":
        raise BuildError(f"expected '{{' for {what} at offset {i}: {text[i:i + 40]!r}")
    depth, j, n = 0, i, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    raise BuildError(f"unbalanced braces in {what} starting at offset {i}")


def read_opt(text: str, i: int) -> tuple[str | None, int]:
    """Read an optional [...] argument at text[i] (after whitespace); brackets inside braces are ignored."""
    j = skip_ws(text, i)
    if j >= len(text) or text[j] != "[":
        return None, i
    depth, k, n = 0, j + 1, len(text)
    while k < n:
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth == 0:
            return text[j + 1:k], k + 1
        k += 1
    raise BuildError(f"unterminated optional argument at offset {j}")


def find_env(text: str, name: str, start: int = 0) -> tuple[int, int, int, int] | None:
    """Find the next \\begin{name} ... \\end{name} (nesting-aware).

    Returns (begin_start, content_start, content_end, end_end) or None.
    """
    pat = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
    m = pat.search(text, start)
    while m and m.group(1) != "begin":
        m = pat.search(text, m.end())
    if not m:
        return None
    depth = 0
    begin_start, content_start = m.start(), m.end()
    for mm in pat.finditer(text, m.start()):
        depth += 1 if mm.group(1) == "begin" else -1
        if depth == 0:
            return begin_start, content_start, mm.start(), mm.end()
    raise BuildError(f"\\begin{{{name}}} at offset {begin_start} is never closed")


@dataclass
class Command:
    name: str
    start: int
    end: int
    opts: list[str | None]
    args: list[str]


def parse_command(text: str, i: int, n_args: int, n_opts: int = 0, *, name: str = "") -> Command:
    """Parse `\\name[opt]...{arg}...` whose backslash sits at text[i]."""
    m = re.compile(r"\\([A-Za-z@]+\*?)").match(text, i)
    if not m:
        raise BuildError(f"no command at offset {i}")
    j = m.end()
    opts: list[str | None] = []
    for _ in range(n_opts):
        o, j = read_opt(text, j)
        opts.append(o)
    args = []
    for _ in range(n_args):
        a, j = read_group(text, j, what=f"\\{name or m.group(1)}")
        args.append(a)
    return Command(m.group(1), i, j, opts, args)


def find_commands(text: str, name: str, start: int = 0, end: int | None = None) -> list[int]:
    """Offsets of every `\\name` (not a longer control word) in text[start:end]."""
    pat = re.compile(r"\\" + re.escape(name) + r"(?![A-Za-z@])")
    return [m.start() for m in pat.finditer(text, start, len(text) if end is None else end)]


def strip_outer_braces(s: str) -> str:
    s = s.strip()
    while s.startswith("{") and s.endswith("}"):
        try:
            inner, end = read_group(s, 0)
        except BuildError:
            break
        if end != len(s):
            break
        s = inner.strip()
    return s
