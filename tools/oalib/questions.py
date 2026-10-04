"""Questionnaire items that a figure reports, found through the appendix's \\respbox{Figure~\\ref{..}, Panel~(x)}.

Each \\respbox sits at the end of a question: either a top-level \\item of a questionnaire enumerate
(background and follow-up surveys) or a quote environment (the AI interviewer's multiple-choice questions).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .latexaux import Aux
from .texdoc import Doc
from .texscan import find_env, read_group, read_opt, skip_ws
from .util import BuildError

BLANK = "\u0001BLANK\u0001"


@dataclass
class Question:
    key: str                         # anchor id, e.g. "bg_age" or "mcq-2"
    label: str | None                # q:bg_age (None for the interviewer's questions)
    source: str                      # "Background survey", "Follow-up survey", "AI interviewer"
    number: str                      # "2" for questionnaire items; "2" for the 2nd multiple-choice question
    tex: str                         # LaTeX of the question without \label and \respbox
    targets: list[tuple[str, str | None]] = field(default_factory=list)   # (figure label, panel letter)
    offset: int = 0


def _top_items(blank: str, c0: int, c1: int) -> list[int]:
    """Offsets of the top-level \\item commands inside an enumerate body blank[c0:c1]."""
    out, depth = [], 0
    for m in re.finditer(r"\\(begin|end)\s*\{(itemize|enumerate|description)\}|\\item(?![A-Za-z@])", blank[c0:c1]):
        if m.group(1) == "begin":
            depth += 1
        elif m.group(1) == "end":
            depth -= 1
        elif depth == 0:
            out.append(c0 + m.start())
    return out


def _source_name(title: str) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    return t[:1].upper() + t[1:].lower() if t else t


def parse_questions(doc: Doc, aux: Aux) -> list[Question]:
    blank = doc.blank
    begin = blank.find("\\begin{document}")
    # uses only: the macro's own definition (\newcommand{\respbox}[1]{...}) is not followed by an argument
    respboxes = [m for m in re.finditer(r"\\respbox(?![A-Za-z@])", blank)
                 if m.start() > begin and blank[skip_ws(blank, m.end()):][:1] == "{"]
    # enclosing quote environments and questionnaire enumerates
    quotes, enums = [], []
    pos = begin
    while (env := find_env(blank, "quote", pos)):
        quotes.append(env)
        pos = env[3]
    pos = begin
    while (env := find_env(blank, "enumerate", pos)):
        enums.append(env)
        pos = env[3]
    tops = {s.id: s for s in doc.sections if s.level == 1}
    questions: list[Question] = []
    mcq_count: dict[str, int] = {}
    seen: dict[tuple[int, int], Question] = {}
    for m in respboxes:
        content, rb_end = read_group(blank, m.end(), what="\\respbox")
        targets = []
        for rm in re.finditer(r"\\ref\s*\{([^}]+)\}([^\\]*)", content):
            pm = re.search(r"Panel[~\s]*\(([a-z])\)", rm.group(2))
            targets.append((rm.group(1).strip(), pm.group(1) if pm else None))
        if not targets:
            raise BuildError(f"\\respbox without a figure reference at {doc.loc(m.start())}")
        sec = next((s for s in reversed(doc.sections) if s.level == 1 and s.offset < m.start()), None)
        source = _source_name(sec.title) if sec else ""
        quote = next((q for q in quotes if q[1] <= m.start() < q[2]), None)
        if quote:
            span = (quote[1], quote[2])
            if span not in seen:
                mcq_count[source] = mcq_count.get(source, 0) + 1
                n = mcq_count[source]
                tex = blank[quote[1]:quote[2]]
                q = Question(f"mcq-{n}", None, "AI interviewer", str(n), tex, [], quote[0])
                seen[span] = q
                questions.append(q)
            seen[span].targets += targets
            continue
        enum = next((e for e in enums if e[1] <= m.start() < e[2]), None)
        if not enum:
            raise BuildError(f"\\respbox at {doc.loc(m.start())} is neither in a quote nor in a questionnaire list")
        items = _top_items(blank, enum[1], enum[2])
        starts = [i for i in items if i < m.start()]
        if not starts:
            raise BuildError(f"\\respbox at {doc.loc(m.start())} precedes the first \\item")
        a = starts[-1]
        later = [i for i in items if i > a]
        b = later[0] if later else enum[2]
        span = (a, b)
        if span not in seen:
            tex = blank[a + len("\\item"):b]
            lm = re.match(r"\s*\\label\s*\{([^}]+)\}", tex)
            label = lm.group(1).strip() if lm else None
            number = aux.labels[label].number if label and label in aux.labels else ""
            key = label.split(":", 1)[-1] if label else f"item-{a}"
            q = Question(key, label, source, number, tex, [], a)
            seen[span] = q
            questions.append(q)
        seen[span].targets += targets
    # strip \label and \respbox from the question text
    for q in questions:
        t = re.sub(r"\\label\s*\{[^}]*\}", "", q.tex)
        out, i = [], 0
        for m in re.finditer(r"\\respbox(?![A-Za-z@])", t):
            out.append(t[i:m.start()])
            _, i = read_group(t, m.end())
        out.append(t[i:])
        q.tex = "".join(out).strip()
    return questions


# ---- rendering ---------------------------------------------------------------------------------

_LIST = re.compile(r"\\begin\s*\{(itemize|enumerate)\}")


def _split_items(body: str) -> list[str]:
    out, depth, cur_start = [], 0, None
    for m in re.finditer(r"\\(begin|end)\s*\{(itemize|enumerate|description)\}|\\item(?![A-Za-z@])", body):
        if m.group(1) == "begin":
            depth += 1
        elif m.group(1) == "end":
            depth -= 1
        elif depth == 0:
            if cur_start is not None:
                out.append(body[cur_start:m.start()])
            cur_start = m.end()
    if cur_start is not None:
        out.append(body[cur_start:])
    return [x.strip() for x in out]


def render(tex: str, mk) -> tuple[str, str]:
    """Question LaTeX -> (html, plain text). `mk(where)` returns an Inline converter."""
    tex = re.sub(r"\\underline\s*\{\s*\\hspace\*?\s*\{[^}]*\}\s*\}", BLANK, tex)
    tex = re.sub(r"\\(setstretch|setlength)\s*\{[^}]*\}(\s*\{[^}]*\})?", "", tex)
    html, text = [], []
    pos = 0
    while True:
        m = _LIST.search(tex, pos)
        seg = tex[pos:m.start() if m else len(tex)]
        if seg.strip():
            r = mk("question text").block(seg)
            html.append(r.html)
            text.append(r.text)
        if not m:
            break
        env = find_env(tex, m.group(1), m.start())
        opts, j = read_opt(tex, env[1])
        body = tex[j:env[2]]
        kind = "radio" if opts and "bigcirc" in opts else "check" if opts and "square" in opts else "bullet"
        lis = []
        for it in _split_items(body):
            sub_html, sub_text = render(it, mk) if _LIST.search(it) else (None, None)
            if sub_html is None:
                r = mk("question option").inline(it)
                lis.append(f"<li>{r.html}</li>")
                text.append(r.text)
            else:
                lis.append(f"<li>{sub_html}</li>")
                text.append(sub_text)
        tag = "ol" if m.group(1) == "enumerate" else "ul"
        html.append(f'<{tag} class="q-opts q-{kind}">' + "".join(lis) + f"</{tag}>")
        pos = env[3]
    out = "\n".join(html).replace(BLANK, '<span class="q-blank" aria-label="text field"></span>')
    return out, " ".join(text).replace(BLANK, "____")
