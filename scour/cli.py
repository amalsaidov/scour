"""scour CLI — deterministic OSS bug-candidate finder. LLM is OFF by default (0 tokens)."""
import argparse
import re
import sys
import time

from . import db, gh, analyze

STOPLIST = set()  # repos already contributed to / banned (populate from your campaign)


def _keywords(f):
    """Deterministic dedup keywords from a finding (file stem + rule tail)."""
    stem = re.sub(r".*/", "", f["file"]).replace(".rs", "").replace(".go", "")
    rule = f["rule"].split("::")[-1].replace("vet:", "")
    return [stem, rule]


def cmd_discover(args):
    con = db.connect()
    repos = gh.discover_repos(args.lang, args.min_stars, args.pushed_after, args.limit)
    added = 0
    for full, stars, pushed in repos:
        if full in STOPLIST:
            continue
        db.upsert_repo(con, full, lang=args.lang, stars=stars, pushed_at=pushed)
        added += 1
    print(f"discovered {added} {args.lang} repos (stars>={args.min_stars}, pushed>={args.pushed_after})")


def cmd_analyze(args):
    con = db.connect()
    t0 = time.time()
    total_find = 0
    for full in args.repos:
        if full in STOPLIST:
            print(f"skip {full}: stoplist")
            continue
        head = gh.head_commit(full)
        if head and db.already_analyzed(con, full, head):
            print(f"skip {full}: already analyzed @ {head[:8]} (incremental)")
            continue
        if args.check_policy:
            pol = gh.ai_policy(full)
            db.upsert_repo(con, full, ai_policy=pol)
            if pol == "forbids":
                print(f"skip {full}: AI-contribution policy forbids")
                db.mark_analyzed(con, full, head or "")
                continue
        print(f"analyzing {full} ...", flush=True)
        sha, findings, lang, err, tmp = analyze.analyze_repo(full)
        try:
            if err:
                print(f"  {full}: {err}")
            else:
                kept = 0
                for f in findings:
                    if db.add_finding(con, full, sha, f["file"], f["line"], f["rule"],
                                      f["category"], f["severity"], f["message"]):
                        kept += 1
                total_find += kept
                print(f"  {full} [{lang}] @ {sha[:8]}: {len(findings)} raw / {kept} new high-signal findings")
            db.upsert_repo(con, full, lang=lang)
            db.mark_analyzed(con, full, sha or (head or ""))
        finally:
            analyze.cleanup(tmp)
    print(f"\nanalyze done: {total_find} new findings in {time.time()-t0:.1f}s")


def cmd_dedup(args):
    con = db.connect()
    pend = db.pending_dedup(con)
    dup = clean = 0
    for f in pend:
        if f["repo"] in STOPLIST:
            db.set_dedup(con, f["id"], "duplicate", "stoplist")
            dup += 1
            continue
        is_dup, ref = gh.is_duplicate(f["repo"], _keywords(f))
        if is_dup:
            db.set_dedup(con, f["id"], "duplicate", ref)
            dup += 1
        else:
            db.set_dedup(con, f["id"], "clean", "")
            clean += 1
    print(f"dedup: {clean} clean, {dup} duplicates (of {len(pend)} pending)")


def cmd_report(args):
    con = db.connect()
    rows = db.candidates(con, args.limit)
    if not rows:
        print("no clean candidates yet. run: analyze then dedup")
        return
    print(f"{'SEV':>3}  {'CATEGORY':<12} {'REPO':<32} LOCATION  —  RULE")
    print("-" * 100)
    for r in rows:
        loc = f"{r['file']}:{r['line']}"
        print(f"{r['severity']:>3}  {r['category']:<12} {r['repo']:<32} {loc}")
        print(f"       {r['rule']}  —  {r['message'][:110]}")


def cmd_stats(args):
    con = db.connect()
    s = db.stats(con)
    print("scour state:")
    for k, v in s.items():
        print(f"  {k:<16} {v}")
    if not s["llm_real"]:
        print(f"  {'llm_tokens':<16} 0 (LLM stage not run)")


def cmd_scan(args):
    """One shot: discover repos then analyze+dedup+report the top N."""
    con = db.connect()
    repos = gh.discover_repos(args.lang, args.min_stars, args.pushed_after, args.limit)
    names = [full for full, _s, _p in repos if full not in STOPLIST]
    for full, stars, pushed in repos:
        db.upsert_repo(con, full, lang=args.lang, stars=stars, pushed_at=pushed)
    if not names:
        print("no repos discovered for those filters")
        return
    print(f"scanning {len(names)} {args.lang} repos ...")
    ns = argparse.Namespace(repos=names, check_policy=args.check_policy, limit=args.limit)
    cmd_analyze(ns)
    cmd_dedup(ns)
    cmd_report(argparse.Namespace(limit=args.limit))


def cmd_run(args):
    """Full deterministic pipeline on given repos (discover optional)."""
    cmd_analyze(args)
    cmd_dedup(args)
    cmd_report(argparse.Namespace(limit=args.limit))


def main(argv=None):
    p = argparse.ArgumentParser(prog="scour", description="Deterministic OSS bug-candidate finder (LLM optional).")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find active popular repos via GitHub API")
    d.add_argument("--lang", default="rust")
    d.add_argument("--min-stars", type=int, default=300)
    d.add_argument("--pushed-after", default="2025-06-01")
    d.add_argument("--limit", type=int, default=20)
    d.set_defaults(func=cmd_discover)

    a = sub.add_parser("analyze", help="clone + static-analyze repos (incremental)")
    a.add_argument("repos", nargs="+")
    a.add_argument("--check-policy", action="store_true", help="skip repos whose AI policy forbids contributions")
    a.set_defaults(func=cmd_analyze)

    dd = sub.add_parser("dedup", help="check findings against existing issues/PRs")
    dd.set_defaults(func=cmd_dedup)

    r = sub.add_parser("report", help="print ranked clean candidates")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_report)

    rn = sub.add_parser("run", help="analyze + dedup + report in one go")
    rn.add_argument("repos", nargs="+")
    rn.add_argument("--check-policy", action="store_true")
    rn.add_argument("--limit", type=int, default=20)
    rn.set_defaults(func=cmd_run)

    s = sub.add_parser("stats", help="show DB state + token usage")
    s.set_defaults(func=cmd_stats)

    sc = sub.add_parser("scan", help="discover + analyze + dedup + report in one shot")
    sc.add_argument("--lang", default="rust")
    sc.add_argument("--min-stars", type=int, default=300)
    sc.add_argument("--pushed-after", default="2025-06-01")
    sc.add_argument("--limit", type=int, default=15)
    sc.add_argument("--check-policy", action="store_true")
    sc.set_defaults(func=cmd_scan)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
