# scour

**Deterministic, token‑minimal finder of bug‑candidates in open‑source repositories.**

scour replaces expensive "AI scouting" with ordinary code. It discovers active repos, runs
high‑precision static analysis, removes anything already reported, remembers everything in SQLite,
and prints a ranked shortlist of candidates **with evidence** — all with **zero LLM tokens**.
An LLM is used (if at all) only on the final shortlist, where it's cheap and worthwhile.

> Philosophy: do the search, the filtering and the first‑pass verification in **deterministic code**.
> Spend an LLM only on the handful of candidates that survive. Minimize tokens; maximize signal.

---

## Why

Scanning open source with an LLM in the loop is slow and burns tokens on repos that turn out clean.
Most of that work — finding repos, running analyzers, checking for duplicates, remembering results —
is a solved, deterministic problem. scour does exactly that part in plain Python + existing tools,
so the only thing (optionally) left for an LLM is confirming a short, pre‑qualified list.

## How it works

```
 discover ──▶ analyze ──▶ dedup ──▶ store ──▶ rank/report ──▶ (optional) LLM triage
 GitHub API   clippy /     issues    SQLite    severity         only the shortlist,
 by lang,     go vet       + PRs     memory    + evidence       minimal context
 stars,       (correctness
 freshness    only)
```

1. **Discover** — GitHub API: active, popular repos filtered by language, stars and recent push.
2. **Analyze** — clone shallow, detect language (incl. nested modules/workspaces), run **high‑signal**
   static analysis:
   - **Rust** → `cargo clippy`, restricted to the **correctness** and **suspicious** lint groups
     (real defects, not style).
   - **Go** → `go vet -json` diagnostics.
   - **Incremental**: a repo already analyzed at its current HEAD commit is skipped.
   - `--check-policy` skips repos whose `AI_POLICY` / `AGENTS` / `CONTRIBUTING` forbid AI contributions.
3. **Dedup** — for each finding, search the repo's **open and closed** issues *and* PRs. Matches are dropped.
4. **Store** — everything in SQLite (`~/scour/scour.db`): repos, commits, findings, dedup status.
   Work is never repeated.
5. **Rank / report** — clean candidates ordered by severity, each with `file:line`, the rule, and the message.
6. **LLM (optional, OFF by default)** — only the shortlist, with minimal context, is handed to an agent
   to confirm. Token usage stays **0** unless you explicitly enable this step.

## Install

No Python dependencies. scour drives tools you already have:

| Need            | Used for                                   |
|-----------------|--------------------------------------------|
| `python3` ≥ 3.9 | the tool itself                            |
| `gh` (authed)   | repo discovery + duplicate search          |
| `git`           | shallow clones                             |
| `cargo`+`clippy`| Rust analysis (`rustup component add clippy`) |
| `go`            | Go analysis (`go vet` is built in)         |

```bash
git clone https://github.com/amalsaidov/scour
cd scour
pip install -e .          # optional: installs the `scour` command
```

## Usage

```bash
# 1) find active, popular repos
scour discover --lang rust --min-stars 300 --limit 20

# 2) analyze + dedup + report in one go (incremental; skips AI‑forbidding repos)
scour run owner/repo1 owner/repo2 --check-policy

# individual stages
scour analyze owner/repo --check-policy
scour dedup
scour report --limit 20

# state + token spend
scour stats
```

(Without `pip install`, run `python3 -m scour.cli <command>` from the repo root.)

## Output

`report` prints ranked, de‑duplicated candidates:

```
SEV  CATEGORY     REPO                             LOCATION  —  RULE
  9  correctness  owner/repo                       src/foo.rs:42
       clippy::eq_op  —  equal expressions on both sides of `==`
```

Everything is also in SQLite for querying / incremental re‑runs:
`repos`, `findings` (with `dedup` and optional `llm_verdict`).

## Honest limitations

- scour surfaces **candidates with evidence**, not guaranteed maintainer‑wanted bugs. Clippy‑correctness
  and go‑vet findings are genuine defects far more often than style lints, but a human (or the optional LLM
  step) still confirms.
- On **well‑maintained, popular** repos these high‑precision rules often find **nothing** — that's expected
  and correct (near‑zero false positives). The value comes from scanning **many** repos cheaply and letting
  the rare hits rise to the top.
- It does **not** auto‑open issues or PRs. That stays a deliberate, reviewed step.

## Roadmap

- Broader deterministic signal: `semgrep` rulesets, running the repo's own test suite to catch failures,
  property/fuzz testing for numeric libraries.
- Volume mode: scheduled overnight scans across hundreds of repos with incremental dedup.
- Pluggable language analyzers.

## License

MIT — see [LICENSE](LICENSE).
