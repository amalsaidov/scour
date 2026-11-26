"""Deterministic analysis: clone, detect language, run static analyzers, parse high-signal findings. No LLM."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

# clippy "correctness" group (deny-by-default): these flag genuine bugs, not style.
CORRECTNESS_LINTS = {
    "absurd_extreme_comparisons", "almost_swapped", "approx_constant", "async_yields_async",
    "bad_bit_mask", "cast_slice_different_sizes", "char_lit_as_u8", "cmp_nan",
    "deprecated_semver", "derive_ord_xor_partial_ord", "derived_hash_with_manual_eq",
    "eq_op", "erasing_op", "ifs_same_cond", "ineffective_bit_mask", "infinite_iter",
    "inherent_to_string_shadow_display", "invalid_null_ptr_usage", "invalid_regex",
    "invalid_utf8_in_unchecked", "iter_next_loop", "iterator_step_by_zero",
    "let_underscore_lock", "match_str_case_mismatch", "mem_replace_with_uninit",
    "min_max", "mistyped_literal_suffixes", "modulo_one", "mut_from_ref",
    "never_loop", "non_octal_unix_permissions", "nonsensical_open_options",
    "not_unsafe_ptr_arg_deref", "out_of_bounds_indexing", "overly_complex_bool_expr",
    "panicking_unwrap", "possible_missing_comma", "read_line_without_trim",
    "recursive_format_impl", "redundant_comparisons", "reversed_empty_ranges",
    "self_assignment", "serde_api_misuse", "size_of_in_element_count",
    "suspicious_splitn", "transmute_null_to_fn", "transmuting_null", "undropped_manually_drops",
    "uninit_assumed_init", "uninit_vec", "unit_cmp", "unit_return_expecting_ord",
    "unsound_collection_transmute", "unused_io_amount", "useless_attribute",
    "vec_resize_to_zero", "while_immutable_condition", "wrong_transmute",
    "zst_offset", "invalid_atomic_ordering", "cast_ref_to_mut", "clone_double_ref",
}
SUSPICIOUS_LINTS = {
    "suspicious_arithmetic_impl", "suspicious_assignment_formatting", "suspicious_else_formatting",
    "suspicious_map", "suspicious_op_assign_impl", "suspicious_unary_op_formatting",
    "empty_loop", "float_equality_without_abs", "double_comparisons", "drop_copy", "forget_copy",
}


def run(cmd, cwd=None, timeout=900):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def shallow_clone(full_name, dest):
    url = f"https://github.com/{full_name}.git"
    r = run(["git", "clone", "--depth", "1", url, dest], timeout=300)
    return r is not None and r.returncode == 0


def detect_lang(path):
    p = Path(path)
    if (p / "Cargo.toml").exists():
        return "rust"
    if (p / "go.mod").exists():
        return "go"
    # search a couple of levels down for workspace members / nested modules
    for depth_glob in ("*/Cargo.toml", "*/*/Cargo.toml"):
        if any(p.glob(depth_glob)):
            return "rust"
    for depth_glob in ("*/go.mod", "*/*/go.mod"):
        if any(p.glob(depth_glob)):
            return "go"
    return None


def _module_dir(path, marker):
    """Return the dir containing the given marker file (root or nearest nested)."""
    p = Path(path)
    if (p / marker).exists():
        return str(p)
    for g in (f"*/{marker}", f"*/*/{marker}"):
        for m in p.glob(g):
            return str(m.parent)
    return str(p)


def parse_clippy(stdout):
    """Parse `cargo clippy --message-format=json` output into high-signal findings.
    Pure function (no subprocess) so it is unit-testable."""
    findings = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        code = (msg.get("code") or {}).get("code") or ""
        level = msg.get("level") or ""
        if level not in ("warning", "error"):
            continue
        lint = code.split("::")[-1] if code.startswith("clippy::") else code
        if lint in CORRECTNESS_LINTS:
            cat, sev = "correctness", 9
        elif lint in SUSPICIOUS_LINTS:
            cat, sev = "suspicious", 6
        else:
            continue  # drop style/pedantic/unknown — minimize false positives
        spans = msg.get("spans") or []
        prim = next((s for s in spans if s.get("is_primary")), spans[0] if spans else None)
        if not prim:
            continue
        findings.append({
            "file": prim.get("file_name", ""),
            "line": prim.get("line_start", 0),
            "rule": code or lint,
            "category": cat,
            "severity": sev,
            "message": (msg.get("message") or "").strip(),
        })
    return findings


def analyze_rust(path):
    """Run clippy in `path` and parse. Returns high-signal findings."""
    r = run(["cargo", "clippy", "--message-format=json", "--quiet",
             "--", "-W", "clippy::correctness", "-W", "clippy::suspicious"],
            cwd=path, timeout=1200)
    if r is None:
        return []
    return parse_clippy(r.stdout)


def parse_go_vet(buf):
    """Parse `go vet -json` output (concatenated JSON objects) into findings.
    Pure function (no subprocess) so it is unit-testable."""
    findings = []
    dec = json.JSONDecoder()
    i = 0
    s = buf.strip()
    while i < len(s):
        s2 = s[i:].lstrip()
        if not s2.startswith("{"):
            break
        try:
            obj, off = dec.raw_decode(s2)
        except json.JSONDecodeError:
            break
        i = len(s) - len(s2) + off
        for pkg, analyzers in obj.items():
            if not isinstance(analyzers, dict):
                continue
            for analyzer, diags in analyzers.items():
                if not isinstance(diags, list):
                    continue
                for d in diags:
                    posn = d.get("posn", "")
                    file, _, rest = posn.partition(":")
                    line = 0
                    if rest:
                        try:
                            line = int(rest.split(":")[0])
                        except ValueError:
                            pass
                    findings.append({
                        "file": file,
                        "line": line,
                        "rule": f"vet:{analyzer}",
                        "category": "vet",
                        "severity": 7,
                        "message": d.get("message", "").strip(),
                    })
    return findings


def analyze_go(path):
    """Run `go vet -json` in `path` and parse. Returns findings."""
    r = run(["go", "vet", "-json", "./..."], cwd=path, timeout=900)
    if r is None:
        return []
    return parse_go_vet(r.stderr)


def analyze_repo(full_name):
    """Clone + analyze. Always returns (commit_sha, findings, lang, error, tmp)."""
    tmp = tempfile.mkdtemp(prefix="bugscout-")
    dest = os.path.join(tmp, "repo")
    if not shallow_clone(full_name, dest):
        return None, [], None, "clone-failed", tmp
    sha = ""
    r = run(["git", "rev-parse", "HEAD"], cwd=dest)
    if r and r.returncode == 0:
        sha = r.stdout.strip()
    lang = detect_lang(dest)
    if lang == "rust":
        findings = analyze_rust(_module_dir(dest, "Cargo.toml"))
    elif lang == "go":
        findings = analyze_go(_module_dir(dest, "go.mod"))
    else:
        return sha, [], lang, "unsupported-lang", tmp
    return sha, findings, lang, None, tmp


def cleanup(tmp):
    subprocess.run(["rm", "-rf", tmp], timeout=60)
