"""Configuration for scour. A scour.toml makes the tool fully configurable:
each user points it at the repos / languages / filters they want to research."""
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

DEFAULTS = {
    "targets": {
        # either an explicit list, or a file with one `owner/repo` per line
        "list": [],
        "file": "",
    },
    "discover": {
        "langs": ["rust", "go"],
        "min_stars": 300,
        "pushed_after": "2025-06-01",
        "limit": 20,
    },
    "analysis": {
        "check_policy": True,
    },
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config(explicit=None):
    """Return the config path to use, or None. Looks at: explicit arg,
    $SCOUR_CONFIG, ./scour.toml, ~/scour/scour.toml."""
    for cand in (explicit, os.environ.get("SCOUR_CONFIG"),
                 "scour.toml", str(Path.home() / "scour" / "scour.toml")):
        if cand and Path(cand).is_file():
            return cand
    return None


def load(explicit=None):
    """Load config merged over defaults. Missing/absent config → defaults."""
    path = find_config(explicit)
    if not path or tomllib is None:
        return DEFAULTS, path
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return DEFAULTS, path
    return _deep_merge(DEFAULTS, data), path


def load_targets(cfg, base_dir="."):
    """Resolve the list of `owner/repo` targets from config (inline list + file)."""
    out = list(cfg["targets"].get("list") or [])
    fpath = cfg["targets"].get("file") or ""
    if fpath:
        p = Path(fpath)
        if not p.is_absolute():
            p = Path(base_dir) / p
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "/" in line:
                    out.append(line)
    # de-dupe, preserve order
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq
