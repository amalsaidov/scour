"""Prefilter: collect OPEN, UNCLAIMED issues (tasks) from target repos, with metadata.
Deterministic (GitHub API). The LLM only works the ranked shortlist this produces."""
import json
import subprocess

# labels that mark a task worth picking up (case-insensitive substrings)
GOOD_LABELS = {
    "bug": 5, "good first issue": 4, "good-first-issue": 4, "help wanted": 3,
    "help-wanted": 3, "easy": 3, "e-easy": 3, "d-easy": 3, "low-hanging-fruit": 4,
    "regression": 5, "crash": 5, "defect": 5, "confirmed": 3, "up-for-grabs": 4,
}


def _gh(path, params=None):
    cmd = ["gh", "api", "-X", "GET", path]
    for k, v in (params or {}).items():
        cmd += ["-f", f"{k}={v}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _score(issue):
    """Higher = more promising & more likely free. Deterministic."""
    s = 0
    labels = [l.get("name", "").lower() for l in issue.get("labels", [])]
    for lab in labels:
        for key, w in GOOD_LABELS.items():
            if key in lab:
                s += w
    reactions = (issue.get("reactions") or {}).get("+1", 0)
    s += min(reactions, 5)
    # fewer comments → less contested / less already-discussed-to-death
    s += max(0, 3 - min(issue.get("comments", 0), 3))
    return s


def unclaimed_issues(repo, per_repo=30):
    """Return open, UNassigned issues (not PRs) for a repo, scored. []
    on error. 'Unclaimed' = no assignee (cheap, deterministic signal)."""
    data = _gh(f"repos/{repo}/issues", {"state": "open", "per_page": str(per_repo), "sort": "updated"})
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if it.get("pull_request"):
            continue  # it's a PR, not an issue
        if it.get("assignee") or it.get("assignees"):
            continue  # someone is already on it
        out.append({
            "repo": repo,
            "number": it.get("number"),
            "title": (it.get("title") or "").strip(),
            "url": it.get("html_url", ""),
            "labels": ",".join(l.get("name", "") for l in it.get("labels", [])),
            "comments": it.get("comments", 0),
            "reactions": (it.get("reactions") or {}).get("+1", 0),
            "created_at": it.get("created_at", ""),
            "score": _score(it),
        })
    return out
