from __future__ import annotations
import csv, io, json, secrets, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler

import auth
import bqe
import join

# Everything /fetch pulls from BQE, in fetch order. Full records are requested (no
# `fields` filter) so the raw exports are a complete copy, not just the columns the
# archive needs. The first four feed join.py and are required; the rest are
# export-only and skipped with a warning if the API refuses them.
#   key          — _raw_cache key (and top-level key in the JSON dump)
#   path         — BQE API endpoint
#   expand       — nested child records to include (forces pageSize 100)
#   csv          — export filename under /download/
#   children     — nested lists flattened to one CSV row each, fields prefixed child_prefix
#   project_col  — column holding the projectId used to add project_name/phase_name
#   address      — flatten the default address + phone/email into columns
#   inactive_too — the endpoint hides isActive=false records unless asked; fetch them too
DATASETS = [
    {"key": "projects",        "path": "project",      "label": "projects",
     "csv": "projects.csv",       "required": True},
    {"key": "time_entries",    "path": "timeentry",    "label": "time entries",
     "csv": "time-entries.csv",   "required": True, "project_col": "projectId"},
    {"key": "expense_entries", "path": "expenseentry", "label": "expense entries",
     "csv": "expenses.csv",       "required": True, "project_col": "projectId"},
    {"key": "invoices",        "path": "invoice",      "label": "invoices",
     "csv": "invoices.csv",       "required": True, "expand": "invoiceDetails",
     "children": ["invoiceDetails"], "child_prefix": "detail_", "project_col": "detail_projectId"},
    {"key": "clients",         "path": "client",       "label": "clients",
     "csv": "clients.csv",        "expand": "address", "address": True},
    {"key": "contacts",        "path": "contact",      "label": "contacts",
     "csv": "contacts.csv",       "expand": "address", "address": True},
    {"key": "payments",        "path": "payment",      "label": "payments",
     "csv": "payments.csv",       "expand": "lineItems",
     "children": ["lineItems"], "child_prefix": "line_", "project_col": "line_projectId"},
    {"key": "vendor_bills",    "path": "bill",         "label": "vendor bills",
     "csv": "vendor-bills.csv",   "expand": "lineItems,expenseItems",
     "children": ["lineItems", "expenseItems"], "child_prefix": "line_", "project_col": "line_projectId"},
    {"key": "notes",           "path": "note",         "label": "notes",
     "csv": "notes.csv"},
    {"key": "activities",      "path": "activity",     "label": "activities",
     "csv": "activities.csv",     "inactive_too": True},
    {"key": "expense_items",   "path": "expense",      "label": "expense items",
     "csv": "expense-items.csv",  "inactive_too": True},
]
JSON_FILENAME = "bqe-core-data.json"

# The archive job fetches only the fields join.py reads — about 3x faster than full
# records. (Invoices aren't listed: they need expand=invoiceDetails, which returns
# full records regardless.)
ARCHIVE_FIELDS = {
    "projects":        "id,code,name,parentId,rootProjectId,level,"
                       "contractAmount,contractType,startDate,client",
    "time_entries":    "projectId,billable,extra,costRate,actualHours,billRate,classification,resource",
    "expense_entries": "projectId,billable,extra,costRate,units,chargeAmount",
}

# Fetch jobs. min_token_seconds: login time that must remain to start the job, since
# logins can't be renewed mid-fetch (see access_token_seconds_left in auth.py).
JOBS = {
    "archive": {"duration": "2–4 minutes",  "min_token_seconds": 8 * 60},
    "export":  {"duration": "5–15 minutes", "min_token_seconds": 20 * 60},
}

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
        elif path == "/download/" + JSON_FILENAME:
            self._handle_download_json()
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
        """Run one fetch job and report progress via SSE.

        job=archive — the four datasets join.py needs, restricted to the fields it
                      reads (fast); the done event carries the joined project tree.
        job=export  — every dataset in DATASETS as full records (slow); fills
                      _raw_cache for the /download/ endpoints.
        """
        job = params.get("job", "archive")
        if job not in JOBS:
            self._send_json({"error": f"unknown job: {job}"}, 400)
            return
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

        # Access tokens last an hour and (without offline_access) can't be renewed,
        # so a fetch started near expiry would die partway. Refuse up front.
        seconds_left = auth.access_token_seconds_left()
        if (seconds_left is not None and seconds_left < JOBS[job]["min_token_seconds"]
                and not auth.refresh_access_token()):
            minutes = max(seconds_left, 0) // 60
            emit("error", {"msg": (
                f"Your BQE login expires in {minutes} minute{'s' if minutes != 1 else ''} — "
                f"not enough time for this fetch ({JOBS[job]['duration']}). Click Disconnect, "
                "connect again, then fetch."
                if seconds_left > 0 else
                "Your BQE login has expired. Click Disconnect, connect again, then fetch."
            )})
            return

        try:
            if job == "archive":
                datasets = [d for d in DATASETS if d.get("required")]
                fetched, _ = _fetch_datasets(datasets, emit, fields=ARCHIVE_FIELDS, pct_span=90)

                emit("progress", {"phase": "join", "msg": "Joining data…", "pct": 93})
                result = join.join_data(fetched["projects"], fetched["time_entries"],
                                        fetched["expense_entries"], fetched["invoices"])
                emit("progress", {"phase": "join", "msg": f"{len(result):,} root projects joined", "pct": 99})

                emit("done", {
                    "projects": result,
                    "counts": {
                        "timeEntries":    len(fetched["time_entries"]),
                        "expenseEntries": len(fetched["expense_entries"]),
                        "invoices":       len(fetched["invoices"]),
                        "rootProjects":   len(result),
                    },
                })
            else:
                fetched, skipped = _fetch_datasets(DATASETS, emit, fields={}, pct_span=97)

                global _raw_cache
                _raw_cache = fetched

                emit("done", {
                    "counts":  [[d["label"], len(fetched[d["key"]])] for d in DATASETS],
                    "skipped": skipped,
                })

        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                emit("error", {"msg": "Session expired — disconnect and reconnect to BQE Core, then fetch again."})
            else:
                emit("error", {"msg": f"BQE API error {exc.code}: {exc.reason}"})
        except Exception as exc:
            emit("error", {"msg": str(exc)})

    def _handle_download_json(self):
        """Serve every record from the most recent fetch, unflattened, as one JSON file."""
        if not _raw_cache:
            self._send_json({"error": "no data — run a fetch first"}, 404)
            return
        body = json.dumps(_raw_cache, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{JSON_FILENAME}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_download_csv(self, filename: str):
        """Serve a raw-data CSV from the most recent fetch (backs the builder's Export buttons)."""
        dataset = next((d for d in DATASETS if d["csv"] == filename), None)
        if not dataset:
            self._send_json({"error": f"unknown dataset: {filename}"}, 404)
            return
        if not _raw_cache:
            self._send_json({"error": "no data — run a fetch first"}, 404)
            return

        rows = list(_raw_cache.get(dataset["key"], []))

        # Records with nested child lines (invoice details, payment/bill line items) are
        # flattened to one row per child so the CSV is a flat table. Child fields get a
        # prefix so they can't overwrite same-named parent fields (e.g. amount, id).
        # A record with no children still emits one row.
        if dataset.get("children"):
            prefix = dataset["child_prefix"]
            flattened_rows = []
            for record in rows:
                parent_fields = {k: v for k, v in record.items() if k not in dataset["children"]}
                child_rows = []
                for child_key in dataset["children"]:
                    for child in record.get(child_key) or []:
                        tagged = {f"{prefix}{k}": v for k, v in child.items()}
                        if len(dataset["children"]) > 1:
                            tagged = {f"{prefix}type": child_key, **tagged}
                        child_rows.append(tagged)
                if not child_rows:
                    flattened_rows.append(parent_fields)
                for child in child_rows:
                    flattened_rows.append({**parent_fields, **child})
            rows = flattened_rows

        # Clients and contacts: pull the default address and its phone/email entries up
        # into plain columns. The full nested address list stays in the `address` column.
        if dataset.get("address"):
            rows = [{**row, **_address_columns(row.get("address"))} for row in rows]

        # Enrich rows with human-readable names so a reader doesn't have to resolve
        # projectId UUIDs. project_name / phase_name lead the columns.
        # A row's project can be a phase (has a parent) or a top-level project.
        if dataset.get("project_col"):
            projects_by_id = {proj["id"]: proj for proj in _raw_cache.get("projects", [])}
            enriched_rows = []
            for row in rows:
                project_id = row.get(dataset["project_col"]) or row.get("projectId")
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

        # Union of keys across all rows (first-seen order) so fields missing from
        # the first row still get a column.
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        # Nested values (custom fields, addresses, account splits) are written as JSON
        # rather than Python's repr so they stay machine-readable.
        writer.writerows({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                          for k, v in row.items()} for row in rows)
        body = ("\ufeff" + csv_buffer.getvalue()).encode("utf-8")  # UTF-8 BOM so Excel detects encoding

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _fetch_datasets(datasets: list, emit, fields: dict, pct_span: int) -> tuple:
    """Fetch each dataset in order, emitting progress. Returns (fetched by key, skipped labels).

    `fields` maps a dataset key to a BQE `fields` filter; datasets without one get
    full records. Required datasets re-raise HTTP errors; the rest are skipped.
    """
    fetched: dict = {}
    skipped: list = []
    for index, dataset in enumerate(datasets):
        key, label = dataset["key"], dataset["label"]
        pct = 2 + round(pct_span * index / len(datasets))
        emit("progress", {"phase": key, "msg": f"Fetching {label}…", "pct": pct})
        params = {"expand": dataset["expand"]} if dataset.get("expand") else {}
        # BQE caps pageSize at 100 when expanding child records.
        page_size = 100 if params else 1000
        if key in fields:
            params["fields"] = fields[key]
        on_page = lambda n, label=label, pct=pct: emit(
            "progress", {"phase": key, "msg": f"{label.capitalize()}: {n:,}…", "pct": pct})
        try:
            fetched[key] = bqe.fetch_all(dataset["path"], params,
                                         page_size=page_size, on_page=on_page)
            if dataset.get("inactive_too"):
                seen = {rec.get("id") for rec in fetched[key]}
                inactive = bqe.fetch_all(dataset["path"], {**params, "where": "isActive = false"},
                                         page_size=page_size)
                fetched[key] += [rec for rec in inactive if rec.get("id") not in seen]
        except urllib.error.HTTPError as exc:
            # The archive's four datasets are required; the export-only extras are
            # best-effort (a firm's Core plan or permissions may not expose them).
            if dataset.get("required") or exc.code == 401:
                raise
            fetched[key] = []
            skipped.append(f"{label} (HTTP {exc.code})")
            emit("progress", {"phase": key, "msg": f"Skipped {label} — HTTP {exc.code}", "pct": pct})
            continue
        emit("progress", {"phase": key, "msg": f"{len(fetched[key]):,} {label}", "pct": pct})
    return fetched, skipped


def _address_columns(address) -> dict:
    """Flatten a client/contact's default address (and its phone/email) into columns."""
    addresses = address if isinstance(address, list) else [address] if address else []
    if not addresses:
        return {}
    default = next((a for a in addresses if a.get("isDefault")), addresses[0])
    columns = {f"address_{field}": default.get(field)
               for field in ("street1", "street2", "city", "state", "zip", "country")}
    # Communications are typed entries (Email, Phone, Fax, …); several of one type join with "; ".
    for comm in default.get("communications") or []:
        column = (comm.get("typeName") or "other").strip().lower().replace(" ", "_")
        value  = (comm.get("value") or "").strip()
        if value:
            columns[column] = f"{columns[column]}; {value}" if columns.get(column) else value
    return columns


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
<title>Error — Archive Builder for BQE Core</title>
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
