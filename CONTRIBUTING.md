# Contributing to GroundWork Lens

Thank you for contributing to GroundWork Lens!

## Principles

1. **No LLM, anywhere.** All views come deterministically from GroundWork files and git history.
2. **One parser.** Never parse GroundWork markdown or front matter in `lens/`. All parsing lives in `engine/` (Python standard library only).
3. **Zero external network calls.** Serve all assets locally; only talk to configured GitHub or GitLab hosts.
4. **Phone first.** Test every view at 360 px width with keyboard navigation and 44 px minimum tap targets.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m lens
```

## Opening a bug report

Please include the JSON output from `/admin/health` (Copy diagnostics) in your issue.
