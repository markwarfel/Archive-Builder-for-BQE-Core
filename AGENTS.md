# AGENTS.md

Context for AI coding agents working in this repo. For user-facing setup and
usage, see [README.md](README.md).

## What this is

A small, **dependency-free** (Python 3.8+, standard library only) local tool that
pulls data from the BQE Core API and builds a self-contained HTML archive. Keep
it dependency-free — do not add third-party packages.

## Architecture / data flow

- `server.py` — starts the local HTTP server on **127.0.0.1:8080** (the address
  must match the OAuth redirect URI in `auth.py`; don't change one without the other).
- `handler.py` — request routing: OAuth endpoints, `/fetch?job=…` (SSE progress),
  `/download/*.csv` and `/download/bqe-core-data.json`. Two independent fetch jobs:
  `archive` pulls the four required datasets restricted to `ARCHIVE_FIELDS` (fast) and
  returns the `join.py` tree; `export` pulls every dataset in `DATASETS` as full
  records into the in-memory cache the downloads read from.
- `auth.py` — OAuth PKCE flow; stores tokens in `tokens.json`, client id in `config.json`.
- `bqe.py` — paginated BQE API client.
- `join.py` — aggregates time/expense/invoice records into a project → phase tree.
- `index.html` — the builder UI.
- `archive-template.html` / `archive-template.css` — template for the generated
  archive. The builder injects data by string-replacing placeholders
  (`__PROJECTS__`, `__STYLES__`, `__ACCENT__`, `__TITLE__`, `__ASOF__`, …). `__STYLES__` must
  be replaced **before** `__ACCENT__`, because the CSS itself contains `__ACCENT__`.
  Embedded JSON escapes `</` as `<\/` so it can't close the `<script>` block.
- `build_sample.py` — builds `sample-archive.html` from fake data, mirroring the
  builder's substitution in Python. Use it to preview/verify template changes.

## ⚠️ Do not change without re-verifying against BQE Core

The financial formulas in `join.py` were reverse-engineered to match the BQE Core
UI **exactly** (see the `join_data` docstring). Treat them as load-bearing:

- **Budget spent** = all time × billRate (billable *and* non-billable) + non-extra expense chargeAmounts.
- **Cost** = all time × costRate + all expense costRate × units.
- **Contract billed** = per invoice detail, `amount − extraServiceAmount − extraExpenseAmount`, non-void only.

Changing these silently corrupts every archive. If you must touch them, re-verify
against real Core numbers first.

## Conventions

- No third-party dependencies. No build step. No tests framework — verify by running.
- The generated archive must stay **fully self-contained** (all CSS/JS/data inline,
  works offline with no server).
- Don't commit secrets: `tokens.json`, `config.json` are gitignored. Real built
  archives (`*-archive-*.html`) are gitignored because they contain live client data.

## How to verify a change

```bash
python3 -m py_compile server.py handler.py bqe.py auth.py join.py   # Python compiles
python3 build_sample.py                                             # rebuild the sample archive
# open sample-archive.html in a browser to check the template renders
python3 server.py                                                   # smoke-test the full flow
```

The server imports the Python modules once at startup — restart it after any
`.py` change. HTML/CSS (builder and archive template) are read from disk on every
request, so a browser reload picks those up.

For archive-template.html JS changes, syntax-check the embedded script by extracting
it, replacing `__PROJECTS__` with `[]`, and running `node --check`.
