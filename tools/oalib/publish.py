"""Commit with the anonymous identity (UTC, day precision) and push as the anonymous account."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .checks import Banned, Report, REPO_ALLOW, TEXT_EXT, _vendor_pins, _vendored, check_repo
from .config import Config
from .manifest import commit_message, load_state, mark_published, state_dir
from .util import BuildError, info, run


def _git(cfg: Config, *args: str, env: dict | None = None, check: bool = True):
    return run(["git", "-C", str(cfg.root), *args], env=env, check=check, timeout=300)


def identity_env(cfg: Config) -> dict:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+0000")
    return {"GIT_AUTHOR_NAME": cfg.identity_name, "GIT_AUTHOR_EMAIL": cfg.identity_email,
            "GIT_COMMITTER_NAME": cfg.identity_name, "GIT_COMMITTER_EMAIL": cfg.identity_email,
            "GIT_AUTHOR_DATE": day, "GIT_COMMITTER_DATE": day, "TZ": "UTC"}


def _token(cfg: Config) -> str:
    res = run([str(cfg.gh), "auth", "token", "--hostname", "github.com", "--user", cfg.github_user], check=False)
    tok = res.stdout.strip()
    if res.returncode != 0 or not tok:
        raise BuildError(f"gh has no token for {cfg.github_user}; run `gh auth login` and sign in as that account")
    return tok


def verify_account(cfg: Config) -> str:
    tok = _token(cfg)
    res = run([str(cfg.gh), "api", "user", "--jq", "[.login,.id]|@tsv"], env={"GH_TOKEN": tok}, check=False)
    login, _, uid = res.stdout.strip().partition("\t")
    if login != cfg.github_user or uid != str(cfg.github_user_id):
        raise BuildError(f"the token belongs to {login!r} ({uid}), not {cfg.github_user} ({cfg.github_user_id})")
    return tok


def status(cfg: Config) -> str:
    out = []
    pub = load_state(cfg)
    head = run(["git", "-C", str(cfg.source), "rev-parse", "HEAD"], check=False).stdout.strip()
    if pub and pub.get("source_sha"):
        src = pub["source_sha"]
        n = run(["git", "-C", str(cfg.source), "rev-list", "--count", f"{src}..HEAD"], check=False).stdout.strip()
        files = run(["git", "-C", str(cfg.source), "diff", "--name-only", src, "HEAD", "--", "*.tex", "*.bib",
                     "tables", "figures"], check=False).stdout.split()
        out.append(f"manuscript: {n or '?'} commit(s) since the last publish ({src[:10]} -> {head[:10]})")
        out.append("  changed sources: " + (", ".join(files[:30]) + (" ..." if len(files) > 30 else "")
                                          if files else "none in .tex, .bib, tables/ or figures/"))
        out.append("  run `python tools/oa.py build --pull` to see which exhibits change")
    else:
        out.append(f"manuscript: HEAD {head[:10]}; nothing published yet")
    pend = state_dir(cfg) / "pending.json"
    if pend.exists():
        d = json.loads(pend.read_text(encoding="utf-8"))
        out.append(f"last build: from manuscript {d['source_sha'][:10]}")
    dirty = _git(cfg, "status", "--porcelain", check=False).stdout.strip()
    out.append("site repo: " + ("uncommitted changes" if dirty else "clean"))
    ahead = _git(cfg, "rev-list", "--count", "origin/main..main", check=False)
    if ahead.returncode == 0:
        out.append(f"  {ahead.stdout.strip()} commit(s) not pushed")
    try:
        verify_account(cfg)
        out.append(f"push account: {cfg.github_user} is available through gh")
    except BuildError as e:
        out.append(f"push account: {e}")
    return "\n".join(out)


def commit(cfg: Config, message: str, *, assume_yes: bool = False) -> bool:
    _git(cfg, "add", "-A")
    staged = _git(cfg, "diff", "--cached", "--name-only").stdout.split()
    if not staged:
        info("nothing to commit")
        return False
    print("\n" + message + "\n")
    if not assume_yes:
        ans = input(f"Commit {len(staged)} file(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _git(cfg, "reset", "-q")
            info("cancelled; nothing committed")
            raise SystemExit(1)
    _git(cfg, "commit", "-q", "-m", message, env=identity_env(cfg))
    return True


def push(cfg: Config) -> None:
    tok = verify_account(cfg)
    remote = _git(cfg, "remote", "get-url", "origin", check=False).stdout.strip()
    expect = f"https://github.com/{cfg.repo}.git"
    if remote != expect:
        raise BuildError(f"origin is {remote!r}; expected {expect}")
    rep = Report()
    check_repo(cfg, rep, Banned(cfg.banned_terms))
    if not rep.ok:
        rep.print()
        raise BuildError("repository checks failed; nothing was pushed")
    helper = f"!f() {{ echo username={cfg.github_user}; echo \"password=$OA_PUSH_TOKEN\"; }}; f"
    res = run(["git", "-C", str(cfg.root), "-c", "credential.helper=", "-c", f"credential.helper={helper}",
               "push", "-u", "origin", "main"],
              env={"OA_PUSH_TOKEN": tok, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
              check=False, timeout=600)
    if res.returncode != 0:
        raise BuildError("git push failed:\n" + res.stderr[-1500:])


def publish(cfg: Config, *, assume_yes: bool = False) -> int:
    pend_p = state_dir(cfg) / "pending.json"
    if not pend_p.exists():
        raise BuildError("nothing built yet; run `oa.py build` first")
    pend = json.loads(pend_p.read_text(encoding="utf-8"))
    verify_account(cfg)
    commit(cfg, commit_message(pend["diff"], pend["manifest"]["source_date"]), assume_yes=assume_yes)
    push(cfg)
    mark_published(cfg)
    info(f"pushed to https://github.com/{cfg.repo}; {cfg.base_url} updates within a minute or two")
    return 0


# ---- git hooks (installed through core.hooksPath = tools/hooks) ----------------------------------

def hook_pre_commit(cfg: Config) -> int:
    problems = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        ident = run(["git", "-C", str(cfg.root), "var", var], check=False).stdout.strip()
        if f"{cfg.identity_name} <{cfg.identity_email}>" not in ident:
            problems.append(f"{var} is {ident.rsplit(' ', 2)[0]!r}, not the anonymous identity")
        if not ident.endswith("+0000"):
            problems.append(f"{var} has time zone {ident.rsplit(' ', 1)[-1]}; commit through "
                            "`python tools/oa.py commit -m ...` (it sets UTC dates)")
    banned = Banned(cfg.banned_terms)
    staged = _git(cfg, "diff", "--cached", "--name-only", "--diff-filter=ACMR").stdout.splitlines()
    pins, rep = _vendor_pins(cfg), Report()
    for p in staged:
        if not REPO_ALLOW.match(p):
            problems.append(f"{p} is outside the allowed paths")
        for t in banned.find(p):
            problems.append(f"file name {p} contains {t!r}")
        f = cfg.root / p
        rel = p[len("docs/"):] if p.startswith("docs/") else p
        if f.exists() and _vendored(cfg, rel, f, rep, pins):
            continue  # third-party file, verified byte-identical to its pinned upstream hash
        if f.suffix.lower() in TEXT_EXT and f.exists() and f.stat().st_size < 5_000_000:
            for t in banned.find(f.read_text(encoding="utf-8", errors="replace")):
                problems.append(f"{p} contains {t!r}")
    problems += rep.errors
    if problems:
        print("pre-commit: commit refused (anonymity):\n  " + "\n  ".join(problems))
        return 1
    return 0


def hook_pre_push(cfg: Config) -> int:
    import json as _json
    from .checks import run_checks
    model = _json.loads((cfg.docs / "data" / "exhibits.json").read_text(encoding="utf-8"))
    rep = run_checks(cfg, cfg.docs, model, scope="repo")
    if not rep.ok:
        rep.print()
        print("pre-push: push refused; fix the failures above")
        return 1
    return 0
