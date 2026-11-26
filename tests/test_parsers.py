"""Unit tests for the pure analyzer parsers (no cargo/go needed)."""
import json
import unittest

from bugscout.analyze import parse_clippy, parse_go_vet


def _clippy_line(lint, msg, file, line, level="warning"):
    return json.dumps({
        "reason": "compiler-message",
        "message": {
            "level": level,
            "code": {"code": f"clippy::{lint}"},
            "message": msg,
            "spans": [{"is_primary": True, "file_name": file, "line_start": line}],
        },
    })


class TestClippyParser(unittest.TestCase):
    def test_keeps_correctness_lint(self):
        out = _clippy_line("eq_op", "equal expressions on both sides of `==`", "src/a.rs", 42)
        f = parse_clippy(out)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["category"], "correctness")
        self.assertEqual(f[0]["severity"], 9)
        self.assertEqual(f[0]["file"], "src/a.rs")
        self.assertEqual(f[0]["line"], 42)

    def test_keeps_suspicious_lint_lower_severity(self):
        out = _clippy_line("empty_loop", "empty `loop {}`", "src/b.rs", 7)
        f = parse_clippy(out)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["category"], "suspicious")
        self.assertEqual(f[0]["severity"], 6)

    def test_drops_style_lint(self):
        # a style lint not in the correctness/suspicious allowlists must be dropped
        out = _clippy_line("needless_return", "unneeded `return`", "src/c.rs", 3)
        self.assertEqual(parse_clippy(out), [])

    def test_ignores_non_messages_and_garbage(self):
        lines = "\n".join([
            json.dumps({"reason": "compiler-artifact"}),
            "not json at all",
            _clippy_line("eq_op", "x", "f.rs", 1),
        ])
        self.assertEqual(len(parse_clippy(lines)), 1)


class TestGoVetParser(unittest.TestCase):
    def test_parses_diagnostics(self):
        buf = json.dumps({
            "github.com/x/y": {
                "printf": [
                    {"posn": "/tmp/repo/main.go:10:2", "message": "Printf format %d has arg s of wrong type string"}
                ]
            }
        })
        f = parse_go_vet(buf)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["rule"], "vet:printf")
        self.assertEqual(f[0]["category"], "vet")
        self.assertEqual(f[0]["line"], 10)
        self.assertTrue(f[0]["file"].endswith("main.go"))

    def test_handles_concatenated_objects(self):
        a = json.dumps({"pkg/a": {"copylocks": [{"posn": "a.go:1:1", "message": "m1"}]}})
        b = json.dumps({"pkg/b": {"lostcancel": [{"posn": "b.go:2:1", "message": "m2"}]}})
        f = parse_go_vet(a + b)
        self.assertEqual(len(f), 2)

    def test_empty(self):
        self.assertEqual(parse_go_vet(""), [])


if __name__ == "__main__":
    unittest.main()
