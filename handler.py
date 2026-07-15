from __future__ import annotations
import csv, io, json, secrets, urllib.error, urllib.parse, urllib.request
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
        elif path == "/debug":
            self._handle_debug(params)
        elif path == "/debug/time":
            self._handle_debug_time(params)
        elif path.startswith("/download/"):
            self._handle_download_csv(path[10:])
        elif path.startswith("/api/"):
            self._handle_api(path[5:], params)
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

        max_pages = int(params["pages"]) if params.get("pages", "").isdigit() else None

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
                max_pages=max_pages,
            )
            emit("progress", {"phase": "projects", "msg": f"{len(projects):,} projects", "pct": 20})

            emit("progress", {"phase": "time", "msg": "Fetching time entries…", "pct": 22})
            time_entries = bqe.fetch_all(
                "timeentry",
                {"fields": "projectId,billable,extra,costRate,actualHours,billRate,classification,resource"},
                on_page=lambda n: emit("progress", {"phase": "time", "msg": f"Time entries: {n:,}…", "pct": 22}),
                max_pages=max_pages,
            )
            emit("progress", {"phase": "time", "msg": f"{len(time_entries):,} time entries", "pct": 60})

            emit("progress", {"phase": "expense", "msg": "Fetching expense entries…", "pct": 62})
            expense_entries = bqe.fetch_all(
                "expenseentry",
                {"fields": "projectId,billable,extra,costRate,units,chargeAmount"},
                max_pages=max_pages,
            )
            emit("progress", {"phase": "expense", "msg": f"{len(expense_entries):,} expense entries", "pct": 75})

            emit("progress", {"phase": "invoices", "msg": "Fetching invoices…", "pct": 77})
            invoices = bqe.fetch_all("invoice", {"expand": "invoiceDetails"}, page_size=100, max_pages=max_pages)
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
                    "projects":       len(projects),
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

    def _handle_debug(self, params: dict):
        """Return a project's full phase tree with IDs — used to get phase IDs for Postman queries."""
        root_id = params.get("project", "")
        if not root_id:
            self._send_json({"error": "?project=<root-project-id> required"}, 400)
            return
        if not auth.tokens.get("access_token"):
            self._send_json({"error": "not connected"}, 401)
            return
        try:
            projects = bqe.fetch_all("project", {
                "fields": "id,name,level,parentId,contractAmount,startDate"
            })
            by_id = {p["id"]: p for p in projects}
            if root_id not in by_id:
                self._send_json({"error": f"{root_id!r} not found in {len(projects)} projects"}, 404)
                return
            children_of: dict = {}
            for p in projects:
                pid = p.get("parentId")
                if pid:
                    children_of.setdefault(pid, []).append(p)

            def subtree(node_id: str) -> dict:
                p = by_id[node_id]
                return {
                    "id":             node_id,
                    "name":           p.get("name"),
                    "level":          p.get("level"),
                    "contractAmount": p.get("contractAmount"),
                    "children":       [subtree(c["id"]) for c in children_of.get(node_id, [])],
                }

            self._send_json(subtree(root_id))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_debug_time(self, params: dict):
        """Fetch all time entries for a phase and return computed totals for comparison against Core.

        GET /debug/time?project=<phase-id>
        Returns: entry count, budget_spent (clientHours×billRate, billable only), cost (actualHours×costRate).
        """
        phase_id = params.get("project", "")
        if not phase_id:
            self._send_json({"error": "?project=<phase-id> required"}, 400)
            return
        if not auth.tokens.get("access_token"):
            self._send_json({"error": "not connected"}, 401)
            return
        try:
            entries = bqe.fetch_all(
                "timeentry",
                {
                    "where":  f"projectId='{phase_id}'",
                    "fields": "projectId,billable,costRate,actualHours,billRate,classification,resource",
                },
            )
            cost = raw = 0.0
            billable_count = 0
            role_hours: dict = {}
            for e in entries:
                actual = e.get("actualHours") or 0
                rate   = e.get("billRate") or 0
                cost  += (e.get("costRate") or 0) * actual
                if e.get("billable"):
                    billable_count += 1
                    raw += actual * rate
                key = e.get("classification") or f"${int(rate)}/hr"
                role_hours[key] = role_hours.get(key, 0.0) + actual

            expenses = bqe.fetch_all(
                "expenseentry",
                {
                    "where":  f"projectId='{phase_id}'",
                    "fields": "id,billable,extra,costRate,units,chargeAmount",
                },
            )
            exp_cost = exp_charge_billable = exp_charge_all = 0.0
            for e in expenses:
                exp_cost += (e.get("costRate") or 0) * (e.get("units") or 0)
                charge = e.get("chargeAmount") or 0
                exp_charge_all += charge
                if e.get("billable") and not e.get("extra"):
                    exp_charge_billable += charge

            role_summary = sorted(role_hours.items(), key=lambda x: -x[1])

            self._send_json({
                "projectId":  phase_id,
                "time": {
                    "totalEntries":    len(entries),
                    "billableEntries": billable_count,
                    "raw":             round(raw, 2),
                    "cost":            round(cost, 2),
                    "by_role":         [[k, round(h, 2)] for k, h in role_summary],
                },
                "expenses": {
                    "totalEntries":         len(expenses),
                    "cost":                 round(exp_cost, 2),
                    "charge_billable_nonextra": round(exp_charge_billable, 2),
                    "charge_all":           round(exp_charge_all, 2),
                },
                "combined_raw_plus_exp_charge": round(raw + exp_charge_billable, 2),
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_download_csv(self, name: str):
        """Serve a raw-data CSV from the most recent fetch."""
        key_map = {
            "projects.csv":     "projects",
            "time-entries.csv": "time_entries",
            "expenses.csv":     "expense_entries",
            "invoices.csv":     "invoices",
        }
        key = key_map.get(name)
        if not key:
            self._send_json({"error": f"unknown dataset: {name}"}, 404)
            return
        if not _raw_cache:
            self._send_json({"error": "no data — run a fetch first"}, 404)
            return

        rows = list(_raw_cache.get(key, []))

        if key == "invoices":
            flat = []
            for inv in rows:
                details = inv.get("invoiceDetails") or []
                base = {k: v for k, v in inv.items() if k != "invoiceDetails"}
                if not details:
                    flat.append(base)
                else:
                    for d in details:
                        flat.append({"invoiceId": base.get("id"), **base, **d})
            rows = flat

        # Enrich time entries, expenses, and invoice details with human-readable names.
        # Prepend project_name / phase_name so they appear as the first columns.
        if key in ("time_entries", "expense_entries", "invoices"):
            by_id = {p["id"]: p for p in _raw_cache.get("projects", [])}
            enriched = []
            for row in rows:
                pid = row.get("projectId")
                proj = by_id.get(pid) if pid else None
                if proj:
                    parent = by_id.get(proj.get("parentId") or "")
                    if parent:
                        project_name = parent["name"]
                        phase_name   = proj["name"]
                    else:
                        project_name = proj["name"]
                        phase_name   = ""
                else:
                    project_name = phase_name = ""
                enriched.append({"project_name": project_name, "phase_name": phase_name, **row})
            rows = enriched

        if not rows:
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            return

        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()),
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        body = ("﻿" + out.getvalue()).encode("utf-8")  # BOM for Excel

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_api(self, subpath: str, params: dict):
        """Proxy GET requests to the BQE API; exposes /api/<path> for ad-hoc inspection."""
        if not auth.tokens.get("access_token"):
            self._send_json({"error": "not connected"}, 401)
            return
        endpoint = auth.tokens.get("endpoint", "").rstrip("/")
        if not endpoint:
            self._send_json({"error": "no API endpoint in token response"}, 500)
            return

        url = f"{endpoint}/{subpath}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        def _do_request():
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {auth.tokens['access_token']}")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                return resp.read(), resp.status

        try:
            data, status = _do_request()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and auth.refresh_access_token():
                try:
                    data, status = _do_request()
                except urllib.error.HTTPError as exc2:
                    self._send_json({"error": f"API error {exc2.code}"}, exc2.code)
                    return
            else:
                self._send_json({"error": f"API error {exc.code}"}, exc.code)
                return
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)
            return

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
