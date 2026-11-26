"""SQLite persistence for scour. Remembers repos, commits and findings so work is never repeated."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / "scour" / "scour.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    full_name   TEXT PRIMARY KEY,
    lang        TEXT,
    stars       INTEGER,
    pushed_at   TEXT,
    ai_policy   TEXT DEFAULT 'unknown',   -- unknown | ok | forbids (AI contributions)
    last_commit TEXT,
    analyzed_at REAL
);
CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT,
    commit_sha  TEXT,
    file        TEXT,
    line        INTEGER,
    rule        TEXT,
    category    TEXT,          -- correctness | suspicious | vet | other
    severity    INTEGER,       -- higher = more likely a real bug
    message     TEXT,
    dedup       TEXT DEFAULT 'pending',   -- pending | clean | duplicate
    dedup_ref   TEXT,
    llm_verdict TEXT DEFAULT '',          -- '' | real | not_a_bug | unsure
    created_at  REAL,
    UNIQUE(repo, file, line, rule)
);
CREATE INDEX IF NOT EXISTS idx_find_repo ON findings(repo);
CREATE INDEX IF NOT EXISTS idx_find_rank ON findings(dedup, severity);
CREATE TABLE IF NOT EXISTS task_issues (
    repo        TEXT,
    number      INTEGER,
    title       TEXT,
    url         TEXT,
    labels      TEXT,
    comments    INTEGER,
    reactions   INTEGER,
    created_at  TEXT,
    score       INTEGER,
    status      TEXT DEFAULT 'free',   -- free | picked | done
    seen_at     REAL,
    UNIQUE(repo, number)
);
CREATE INDEX IF NOT EXISTS idx_task_rank ON task_issues(status, score);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_repo(con, full_name, lang=None, stars=None, pushed_at=None, ai_policy=None):
    con.execute(
        """INSERT INTO repos(full_name,lang,stars,pushed_at,ai_policy)
           VALUES(?,?,?,?,COALESCE(?,'unknown'))
           ON CONFLICT(full_name) DO UPDATE SET
             lang=COALESCE(excluded.lang,repos.lang),
             stars=COALESCE(excluded.stars,repos.stars),
             pushed_at=COALESCE(excluded.pushed_at,repos.pushed_at),
             ai_policy=CASE WHEN excluded.ai_policy='unknown' THEN repos.ai_policy ELSE excluded.ai_policy END""",
        (full_name, lang, stars, pushed_at, ai_policy),
    )
    con.commit()


def mark_analyzed(con, full_name, commit_sha):
    con.execute(
        "UPDATE repos SET last_commit=?, analyzed_at=? WHERE full_name=?",
        (commit_sha, time.time(), full_name),
    )
    con.commit()


def already_analyzed(con, full_name, commit_sha):
    row = con.execute(
        "SELECT 1 FROM repos WHERE full_name=? AND last_commit=?", (full_name, commit_sha)
    ).fetchone()
    return row is not None


def add_finding(con, repo, commit_sha, file, line, rule, category, severity, message):
    try:
        con.execute(
            """INSERT INTO findings(repo,commit_sha,file,line,rule,category,severity,message,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (repo, commit_sha, file, line, rule, category, severity, message, time.time()),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # already seen this exact finding


def set_dedup(con, finding_id, status, ref=""):
    con.execute("UPDATE findings SET dedup=?, dedup_ref=? WHERE id=?", (status, ref, finding_id))
    con.commit()


def set_llm(con, finding_id, verdict):
    con.execute("UPDATE findings SET llm_verdict=? WHERE id=?", (verdict, finding_id))
    con.commit()


def pending_dedup(con):
    return con.execute("SELECT * FROM findings WHERE dedup='pending'").fetchall()


def candidates(con, limit=20):
    return con.execute(
        """SELECT * FROM findings WHERE dedup='clean'
           ORDER BY severity DESC, id ASC LIMIT ?""",
        (limit,),
    ).fetchall()


def add_issue(con, iss):
    try:
        con.execute(
            """INSERT INTO task_issues(repo,number,title,url,labels,comments,reactions,created_at,score,seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(repo,number) DO UPDATE SET
                 title=excluded.title, labels=excluded.labels, comments=excluded.comments,
                 reactions=excluded.reactions, score=excluded.score, seen_at=excluded.seen_at""",
            (iss["repo"], iss["number"], iss["title"], iss["url"], iss["labels"],
             iss["comments"], iss["reactions"], iss["created_at"], iss["score"], time.time()),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def top_issues(con, limit=25):
    return con.execute(
        "SELECT * FROM task_issues WHERE status='free' ORDER BY score DESC, reactions DESC, number ASC LIMIT ?",
        (limit,),
    ).fetchall()


def stats(con):
    g = lambda q: con.execute(q).fetchone()[0]
    return {
        "repos": g("SELECT COUNT(*) FROM repos"),
        "repos_analyzed": g("SELECT COUNT(*) FROM repos WHERE analyzed_at IS NOT NULL"),
        "findings": g("SELECT COUNT(*) FROM findings"),
        "clean": g("SELECT COUNT(*) FROM findings WHERE dedup='clean'"),
        "duplicate": g("SELECT COUNT(*) FROM findings WHERE dedup='duplicate'"),
        "pending": g("SELECT COUNT(*) FROM findings WHERE dedup='pending'"),
        "llm_real": g("SELECT COUNT(*) FROM findings WHERE llm_verdict='real'"),
    }
