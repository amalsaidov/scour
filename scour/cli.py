"""scour CLI — deterministic OSS bug-candidate finder. LLM is OFF by default (0 tokens)."""
import argparse
import re
import sys
import time

from . import db, gh, analyze, issues as issuesmod, config as cfgmod

STOPLIST = set()  # repos already contributed to / banned (populate from your campaign)


def _targets_from(args):
    """Resolve target repo names from --from-file and/or --config."""
    names = []
    fp = getattr(args, "from_file", None)
    if fp:
        from pathlib import Path
        p = Path(fp)
        if p.is_file():
            names += [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                      if l.strip() and not l.strip().startswith("#") and "/" in l]
    cfgpath = getattr(args, "config", None)
    if cfgpath is not None or not names:
        cfg, used = cfgmod.load(cfgpath)
        if used:
            from pathlib import Path
            names += cfgmod.load_targets(cfg, base_dir=str(Path(used).parent))
    # de-dupe preserve order
    seen, out = set(), []
    for r in names:
        if r not in seen and "/" in r:
            seen.add(r); out.append(r)
    return out


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
    repos = list(getattr(args, "repos", None) or []) or _targets_from(args)
    if not repos:
        print("no repos given. pass names, --from-file FILE, or --config scour.toml")
        return
    for full in repos:
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
    lang, min_stars, pushed_after, limit = args.lang, args.min_stars, args.pushed_after, args.limit
    if getattr(args, "config", None) is not None:
        cfg, used = cfgmod.load(args.config)
        if used:
            disc = cfg["discover"]
            langs = disc.get("langs") or [lang]
            lang = langs[0]
            min_stars, pushed_after, limit = disc["min_stars"], disc["pushed_after"], disc["limit"]
    repos = gh.discover_repos(lang, min_stars, pushed_after, limit)
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
    """Full deterministic pipeline on given repos (or --from-file / --config)."""
    cmd_analyze(args)
    cmd_dedup(args)
    cmd_report(argparse.Namespace(limit=args.limit))


def cmd_grow(args):
    """Grow the target list: discover popular repos across languages and append new ones.
    Run it repeatedly to keep the list expanding (metadata lives in SQLite)."""
    from pathlib import Path
    con = db.connect()
    target_file = Path(args.file)
    existing = set()
    if target_file.is_file():
        existing = {l.strip() for l in target_file.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#")}
    langs = args.langs or ["java", "rust", "go", "python", "typescript", "kotlin", "c", "cpp"]
    added = []
    for lang in langs:
        for full, stars, pushed in gh.discover_repos(lang, args.min_stars, args.pushed_after, args.per_lang):
            if full in existing or full in STOPLIST:
                continue
            existing.add(full)
            added.append(full)
            db.upsert_repo(con, full, lang=lang, stars=stars, pushed_at=pushed)
    if added:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        with target_file.open("a", encoding="utf-8") as f:
            for r in added:
                f.write(r + "\n")
        # keep the file sorted & unique
        lines = sorted(set(l.strip() for l in target_file.read_text(encoding="utf-8").splitlines() if l.strip()))
        target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"grew target list by {len(added)} repos → {target_file} now has {len(existing)} total")


def cmd_issues(args):
    """PREFILTER: collect open, UNCLAIMED issues (tasks) from target repos, rank, report.
    The LLM only works the shortlist this produces. Deterministic, 0 tokens."""
    con = db.connect()
    repos = list(getattr(args, "repos", None) or []) or _targets_from(args)
    if not repos:
        # fall back to DB targets
        repos = [r["full_name"] for r in con.execute("SELECT full_name FROM repos").fetchall()]
    if args.max_repos:
        import random
        random.shuffle(repos)
        repos = repos[: args.max_repos]
    import time as _t
    t0 = _t.time()
    total = 0
    for i, repo in enumerate(repos, 1):
        if repo in STOPLIST:
            continue
        found = issuesmod.unclaimed_issues(repo, per_repo=args.per_repo)
        for iss in found:
            if db.add_issue(con, iss):
                total += 1
        if i % 20 == 0:
            print(f"  ...scanned {i}/{len(repos)} repos, {total} new free tasks", flush=True)
    print(f"collected {total} new free (unclaimed) tasks from {len(repos)} repos in {_t.time()-t0:.0f}s")
    if args.verify:
        # drop tasks that already have an OPEN referencing PR — check only the top shortlist (cheap).
        claimed = 0
        for r in db.top_issues(con, args.verify):
            if issuesmod.has_referencing_open_pr(r["repo"], r["number"]):
                con.execute("UPDATE task_issues SET status='picked' WHERE repo=? AND number=?",
                            (r["repo"], r["number"]))
                claimed += 1
        con.commit()
        if claimed:
            print(f"verify: dropped {claimed} tasks that already have an open PR")
    _report_issues(con, args.limit)


def _report_issues(con, limit):
    rows = db.top_issues(con, limit)
    if not rows:
        print("no free tasks yet — run `scour issues --config scour.toml`")
        return
    print(f"\nTOP {len(rows)} FREE (unclaimed) TASKS — pick, solve, PR:")
    print(f"{'SCORE':>5}  {'REPO':<30} {'#':>6}  TITLE  [labels]")
    print("-" * 110)
    for r in rows:
        labs = f"  [{r['labels']}]" if r["labels"] else ""
        print(f"{r['score']:>5}  {r['repo']:<30} {r['number']:>6}  {r['title'][:70]}{labs}")
        print(f"       {r['url']}")


def cmd_tasks(args):
    """Just print the current ranked free-task shortlist from the DB."""
    con = db.connect()
    _report_issues(con, args.limit)


def cmd_seed(args):
    """Register target repos in the DB (no analysis), from --from-file / --config."""
    con = db.connect()
    repos = list(getattr(args, "repos", None) or []) or _targets_from(args)
    if not repos:
        print("no targets. pass names, --from-file FILE, or --config scour.toml")
        return
    for full in repos:
        owner = full.split("/")[0]
        db.upsert_repo(con, full)
    print(f"seeded {len(repos)} target repos into the DB (use `scour analyze --config ...` to analyze)")


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
    a.add_argument("repos", nargs="*")
    a.add_argument("--from-file", help="file with one owner/repo per line")
    a.add_argument("--config", nargs="?", const="", help="scour.toml with targets/filters")
    a.add_argument("--check-policy", action="store_true", help="skip repos whose AI policy forbids contributions")
    a.set_defaults(func=cmd_analyze)

    dd = sub.add_parser("dedup", help="check findings against existing issues/PRs")
    dd.set_defaults(func=cmd_dedup)

    r = sub.add_parser("report", help="print ranked clean candidates")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_report)

    rn = sub.add_parser("run", help="analyze + dedup + report in one go")
    rn.add_argument("repos", nargs="*")
    rn.add_argument("--from-file", help="file with one owner/repo per line")
    rn.add_argument("--config", nargs="?", const="", help="scour.toml with targets/filters")
    rn.add_argument("--check-policy", action="store_true")
    rn.add_argument("--limit", type=int, default=20)
    rn.set_defaults(func=cmd_run)

    g = sub.add_parser("grow", help="discover popular repos across languages and append to the target list")
    g.add_argument("--file", default="targets/our-targets.txt")
    g.add_argument("--langs", nargs="*", default=None)
    g.add_argument("--min-stars", type=int, default=500)
    g.add_argument("--pushed-after", default="2025-06-01")
    g.add_argument("--per-lang", type=int, default=30)
    g.set_defaults(func=cmd_grow)

    iss = sub.add_parser("issues", help="PREFILTER: collect open unclaimed tasks from targets, ranked")
    iss.add_argument("repos", nargs="*")
    iss.add_argument("--from-file", help="file with one owner/repo per line")
    iss.add_argument("--config", nargs="?", const="", help="scour.toml with targets")
    iss.add_argument("--max-repos", type=int, default=60, help="scan at most N repos this run")
    iss.add_argument("--per-repo", type=int, default=30)
    iss.add_argument("--verify", type=int, default=40, nargs="?", const=40,
                     help="check the top N tasks for an existing open PR and drop claimed ones (0=off)")
    iss.add_argument("--limit", type=int, default=25, help="shortlist size to print")
    iss.set_defaults(func=cmd_issues)

    tk = sub.add_parser("tasks", help="print the current ranked free-task shortlist")
    tk.add_argument("--limit", type=int, default=25)
    tk.set_defaults(func=cmd_tasks)

    sd = sub.add_parser("seed", help="register target repos in the DB (no analysis)")
    sd.add_argument("repos", nargs="*")
    sd.add_argument("--from-file", help="file with one owner/repo per line")
    sd.add_argument("--config", nargs="?", const="", help="scour.toml with targets")
    sd.set_defaults(func=cmd_seed)

    s = sub.add_parser("stats", help="show DB state + token usage")
    s.set_defaults(func=cmd_stats)

    sc = sub.add_parser("scan", help="discover + analyze + dedup + report in one shot")
    sc.add_argument("--lang", default="rust")
    sc.add_argument("--min-stars", type=int, default=300)
    sc.add_argument("--pushed-after", default="2025-06-01")
    sc.add_argument("--limit", type=int, default=15)
    sc.add_argument("--check-policy", action="store_true")
    sc.add_argument("--config", nargs="?", const="", help="scour.toml with discover filters")
    sc.set_defaults(func=cmd_scan)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
