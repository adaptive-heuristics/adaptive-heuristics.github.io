"""Online Appendix website: build, check and publish from the manuscript sources.

  python tools/oa.py status            what changed in the manuscript since the last publish
  python tools/oa.py build             rebuild docs/ from the current manuscript commit (checks must pass)
  python tools/oa.py preview           serve docs/ on http://localhost:8000
  python tools/oa.py check             run the anonymity and integrity checks on docs/ and the repo
  python tools/oa.py publish           pull the manuscript, build, check, show changes, commit and push
  python tools/oa.py commit -m MSG     commit tool or design changes with the anonymous identity (UTC)

Options: --pull (build: fast-forward the manuscript clone first), --force-compile, --yes (publish without prompt).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oalib import config  # noqa: E402
from oalib.util import BuildError, info  # noqa: E402


def cmd_build(cfg, args) -> int:
    from oalib.build import run_build
    from oalib.checks import run_checks
    from oalib.manifest import compute, diff, load_state, describe, save_pending
    from oalib.render import render
    res = run_build(cfg, pull=args.pull, force_compile=args.force_compile)
    render(cfg, res.model, res.stage)
    report = run_checks(cfg, res.stage, res.model, scope="stage")
    report.print()
    if not report.ok:
        info("checks failed: docs/ was NOT updated")
        return 1
    new = compute(res.model, res.stage)
    old = load_state(cfg)
    d = diff(old, new)
    print(describe(d))
    if cfg.docs.exists():
        shutil.rmtree(cfg.docs)
    shutil.copytree(res.stage, cfg.docs)
    save_pending(cfg, new, res.snap.sha, d)
    info(f"docs/ updated from manuscript commit {res.snap.sha[:10]} ({res.snap.date})")
    return 0


def cmd_check(cfg, args) -> int:
    import json
    from oalib.checks import run_checks
    model = json.loads((cfg.docs / "data" / "exhibits.json").read_text(encoding="utf-8"))
    report = run_checks(cfg, cfg.docs, model, scope="repo")
    report.print()
    return 0 if report.ok else 1


def cmd_preview(cfg, args) -> int:
    import http.server
    import functools

    class NoCache(http.server.SimpleHTTPRequestHandler):
        # the preview changes on every build; make the browser re-check instead of reusing old pages
        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

    handler = functools.partial(NoCache, directory=str(cfg.docs))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    info(f"serving {cfg.docs} on http://localhost:{args.port}/ (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(cfg, args) -> int:
    from oalib.publish import status
    print(status(cfg))
    return 0


def cmd_commit(cfg, args) -> int:
    from oalib.publish import commit
    commit(cfg, args.message, assume_yes=args.yes)
    return 0


def cmd_hook(cfg, args) -> int:
    from oalib.publish import hook_pre_commit, hook_pre_push
    return hook_pre_commit(cfg) if args.which == "pre-commit" else hook_pre_push(cfg)


def cmd_publish(cfg, args) -> int:
    from oalib.publish import publish
    args.pull = True
    rc = cmd_build(cfg, args)
    if rc:
        return rc
    return publish(cfg, assume_yes=args.yes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--pull", action="store_true")
    b.add_argument("--force-compile", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("preview")
    p.add_argument("--port", type=int, default=8000)
    sub.add_parser("status")
    pb = sub.add_parser("publish")
    pb.add_argument("--force-compile", action="store_true")
    pb.add_argument("--yes", action="store_true")
    cm = sub.add_parser("commit")
    cm.add_argument("-m", "--message", required=True)
    cm.add_argument("--yes", action="store_true")
    hk = sub.add_parser("hook")
    hk.add_argument("which", choices=["pre-commit", "pre-push"])
    args = ap.parse_args()
    try:
        cfg = config.load()
        return {"build": cmd_build, "check": cmd_check, "preview": cmd_preview, "status": cmd_status,
                "publish": cmd_publish, "commit": cmd_commit, "hook": cmd_hook}[args.cmd](cfg, args)
    except BuildError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
