from __future__ import annotations
import csv, io, json, secrets, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler

import auth
import bqe
import join

# Populated after a successful fetch; persists until the next fetch.
_raw_cache: dict = {}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    # ── response helpers ──────────────────────────────────────────────────────

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status: int = 200) -> None:
        body = html.encode("utf-8") if isinstance(html, str) else html
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    # ── routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/":
            self._handle_root()
        elif path == "/archive-template.html":
            self._serve_file("archive-template.html")
        elif path == "/archive-template.css":
            self._serve_file("archive-template.css", "text/css; charset=utf-8")
        elif path == "/README.md":
            self._serve_file("README.md", "text/plain; charset=utf-8")
        elif path == "/status":
            self._handle_status()
        elif path == "/connect":
            self._handle_connect(params)
        elif path == "/callback":
            self._handle_callback(params)
        elif path == "/disconnect":
            self._handle_disconnect()
        elif path == "/fetch":
            self._handle_fetch(params)
        elif path.startswith("/download/"):
            self._handle_download_csv(path[10:])
        else:
            self._send_json({"error": "not found"}, 404)

    # ── route handlers ────────────────────────────────────────────────────────

    def _handle_root(self):
        f = auth.ROOT / "index.html"
        if f.exists():
            self._send_html(f.read_bytes())
        else:
            self._send_html("<h2>index.html not found next to server.py</h2>", 500)

    def _serve_file(self, filename: str, content_type: str = "text/html; charset=utf-8"):
        f = auth.ROOT / filename
        if not f.exists():
            self._send_json({"error": f"{filename} not found"}, 404)
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self):
        cfg = auth.load_config()
        self._send_json({
            "connected": bool(auth.tokens.get("access_token")),
            "client_id": cfg.get("client_id", ""),
            "endpoint":  auth.tokens.get("endpoint", ""),
        })

    def _handle_connect(self, params: dict):
        client_id = params.get("client_id", "").strip()
        if not client_id:
            self._send_json({"error": "client_id is required"}, 400)
            return
        auth.save_config({"client_id": client_id})
        code_verifier, code_challenge = auth.pkce_pair()
        state = secrets.token_urlsafe(16)
        auth.pending[state] = {
            "code_verifier": code_verifier,
            "client_id":     client_id,
            "popup":         params.get("popup", "0"),
        }
        auth_url = auth.AUTH_ENDPOINT + "?" + urllib.parse.urlencode(
            {
                "client_id":             client_id,
                "response_type":         "code",
                "scope":                 auth.SCOPES,
                "redirect_uri":          auth.REDIRECT_URI,
                "state":                 state,
                "code_challenge":        code_challenge,
                "code_challenge_method": "S256",
            },
            quote_via=urllib.parse.quote,  # %20 not + — BQE requires this
        )
        self._redirect(auth_url)

    def _handle_callback(self, params: dict):
        error = params.get("error", "")
        if error:
            self._send_html(_error_page(f"BQE returned an error: {params.get('error_description', error)}"))
            return
        code  = params.get("code", "")
        state = params.get("state", "")
        if state not in auth.pending:
            self._send_html(_error_page(
                "State mismatch — the callback didn't match a pending login. "
                "Try connecting again."
            ))
            return
        pend = auth.pending.pop(state)
        try:
            tok = auth.exchange_code(code, pend["code_verifier"], pend["client_id"])
            auth.save_tokens(tok)
            if pend.get("popup") == "1":
                self._send_html(_popup_done_page())
            else:
                self._redirect("/?connected=1")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            self._send_html(_error_page(
                f"Token exchange failed (HTTP {exc.code}).",
                detail=body,
                hint=(
                    "This often means BQE doesn't support public-client (PKCE-only) "
                    "token exchange for your app type. Try registering a "
                    "<b>Regular Web App</b> instead of Native App and add your "
                    "Client Secret to the connect form."
                ) if exc.code in (400, 401) else None,
            ))
        except Exception as exc:
            self._send_html(_error_page(f"Unexpected error: {exc}"))

    def _handle_disconnect(self):
        auth.clear_tokens()
        self._redirect("/")

    def _handle_fetch(self, params: dict):
        """Stream all four BQE datasets, join them, and report progress via SSE."""
        if not auth.tokens.get("access_token"):
            self._send_json({"error": "not connected"}, 401)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(evt: str, data: dict) -> None:
            try:
                self.wfile.write(f"event: {evt}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass

        try:
            emit("progress", {"phase": "projects", "msg": "Fetching projects…", "pct": 5})
            projects = bqe.fetch_all(
                "project",
                {"fields": "id,code,name,parentId,rootProjectId,level,"
                           "contractAmount,contractType,startDate"},
            )
            emit("progress", {"phase": "projects", "msg": f"{len(projects):,} projects", "pct": 20})

            emit("progress", {"phase": "time", "msg": "Fetching time entries…", "pct": 22})
            time_entries = bqe.fetch_all(
                "timeentry",
                {"fields": "projectId,billable,extra,costRate,actualHours,billRate,classification,resource"},
                on_page=lambda n: emit("progress", {"phase": "time", "msg": f"Time entries: {n:,}…", "pct": 22}),
            )
            emit("progress", {"phase": "time", "msg": f"{len(time_entries):,} time entries", "pct": 60})

            emit("progress", {"phase": "expense", "msg": "Fetching expense entries…", "pct": 62})
            expense_entries = bqe.fetch_all(
                "expenseentry",
                {"fields": "projectId,billable,extra,costRate,units,chargeAmount"},
            )
            emit("progress", {"phase": "expense", "msg": f"{len(expense_entries):,} expense entries", "pct": 75})

            emit("progress", {"phase": "invoices", "msg": "Fetching invoices…", "pct": 77})
            invoices = bqe.fetch_all("invoice", {"expand": "invoiceDetails"}, page_size=100)
            emit("progress", {"phase": "invoices", "msg": f"{len(invoices):,} invoices", "pct": 90})

            emit("progress", {"phase": "join", "msg": "Joining data…", "pct": 93})
            result = join.join_data(projects, time_entries, expense_entries, invoices)
            emit("progress", {"phase": "join", "msg": f"{len(result):,} root projects joined", "pct": 99})

            global _raw_cache
            _raw_cache = {
                "projects":       projects,
                "time_entries":   time_entries,
                "expense_entries": expense_entries,
                "invoices":       invoices,
            }

            emit("done", {
                "projects": result,
                "counts": {
                    "timeEntries":    len(time_entries),
                    "expenseEntries": len(expense_entries),
                    "invoices":       len(invoices),
                    "rootProjects":   len(result),
                },
            })

        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                emit("error", {"msg": "Session expired — disconnect and reconnect to BQE Core, then fetch again."})
            else:
                emit("error", {"msg": f"BQE API error {exc.code}: {exc.reason}"})
        except Exception as exc:
            emit("error", {"msg": str(exc)})

    def _handle_download_csv(self, filename: str):
        """Serve a raw-data CSV from the most recent fetch (backs the builder's Export buttons)."""
        filename_to_dataset = {
            "projects.csv":     "projects",
            "time-entries.csv": "time_entries",
            "expenses.csv":     "expense_entries",
            "invoices.csv":     "invoices",
        }
        dataset = filename_to_dataset.get(filename)
        if not dataset:
            self._send_json({"error": f"unknown dataset: {filename}"}, 404)
            return
        if not _raw_cache:
            self._send_json({"error": "no data — run a fetch first"}, 404)
            return

        rows = list(_raw_cache.get(dataset, []))

        # Invoices arrive with nested line items; flatten to one row per detail line
        # so the CSV is a flat table. Invoices with no details still emit one row.
        if dataset == "invoices":
            flattened_rows = []
            for invoice in rows:
                detail_lines = invoice.get("invoiceDetails") or []
                invoice_fields = {k: v for k, v in invoice.items() if k != "invoiceDetails"}
                if not detail_lines:
                    flattened_rows.append(invoice_fields)
                else:
                    for detail in detail_lines:
                        flattened_rows.append({"invoiceId": invoice_fields.get("id"),
                                               **invoice_fields, **detail})
            rows = flattened_rows

        # Enrich T&E and invoice rows with human-readable names so a reader doesn't
        # have to resolve projectId UUIDs. project_name / phase_name lead the columns.
        # A row's project can be a phase (has a parent) or a top-level project.
        if dataset in ("time_entries", "expense_entries", "invoices"):
            projects_by_id = {proj["id"]: proj for proj in _raw_cache.get("projects", [])}
            enriched_rows = []
            for row in rows:
                project_id = row.get("projectId")
                project = projects_by_id.get(project_id) if project_id else None
                if project:
                    parent = projects_by_id.get(project.get("parentId") or "")
                    if parent:
                        project_name = parent["name"]
                        phase_name   = project["name"]
                    else:
                        project_name = project["name"]
                        phase_name   = ""
                else:
                    project_name = phase_name = ""
                enriched_rows.append({"project_name": project_name,
                                      "phase_name": phase_name, **row})
            rows = enriched_rows

        if not rows:
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            return

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0].keys()),
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        body = ("﻿" + csv_buffer.getvalue()).encode("utf-8")  # UTF-8 BOM so Excel detects encoding

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── helper pages ──────────────────────────────────────────────────────────────

def _popup_done_page() -> str:
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>body{font-family:system-ui,sans-serif;text-align:center;padding:60px 20px;color:#5d6b76}</style>
</head><body>
<p>Connected ✓ — this window will close automatically.</p>
<script>
try { window.opener.postMessage("bqe-connected", location.origin); } catch(e) {}
window.close();
</script>
</body></html>"""


def _error_page(message: str, detail: str = "", hint: str = "") -> str:
    hint_html   = f'<div class="hint">{hint}</div>' if hint else ""
    detail_html = f"<pre>{detail}</pre>" if detail else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Error — BQE Archive Builder</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:620px;margin:60px auto;padding:0 20px;color:#1d211d}}
  h2{{color:#c0392b;margin-bottom:8px}}
  .hint{{margin-top:16px;padding:12px 14px;background:#fff9ef;border:1px solid #f0d8a8;
         border-radius:8px;font-size:13px;line-height:1.55}}
  pre{{background:#f3f3f1;border-radius:6px;padding:12px;font-size:12px;
       white-space:pre-wrap;word-break:break-all;overflow:auto}}
  a{{color:#0f7d8c;font-weight:600}}
</style></head>
<body>
<h2>Something went wrong</h2>
<p>{message}</p>
{hint_html}
{detail_html}
<p><a href="/">← Back to the tool</a></p>
</body></html>"""
