# Contributing to bugscout

Thanks for your interest! bugscout aims to stay small, deterministic and dependency‑free.

## Principles

- **Deterministic first.** Discovery, analysis, dedup and storage are plain code. Keep it that way.
  An LLM, if used at all, is only the optional final triage of an already short, pre‑qualified list.
- **High precision over recall.** Prefer rules that almost never produce false positives
  (e.g. clippy *correctness*, `go vet`). A quiet run on clean code is a correct run.
- **No required runtime Python dependencies.** The tool drives existing CLIs (`gh`, `git`, `cargo`, `go`).

## Development

```bash
git clone https://github.com/amalsaidov/bugscout
cd bugscout
pip install -e .
python -m unittest discover -s tests -v
```

The analyzer parsers (`parse_clippy`, `parse_go_vet` in `bugscout/analyze.py`) are pure functions
and fully unit‑tested — add fixtures there when you extend them.

## Adding a language / analyzer

1. Add a `parse_<tool>(output) -> list[finding]` pure function and unit tests.
2. Add an `analyze_<lang>(path)` runner that invokes the tool and calls the parser.
3. Wire detection into `detect_lang` and dispatch in `analyze_repo`.
4. Keep severities honest: correctness/behavioral bugs high, heuristics lower.

## Pull requests

- Keep changes focused and covered by tests where practical.
- Run the test suite before submitting.
- Describe the behavior change and why.
