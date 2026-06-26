#!/usr/bin/env python3
"""
BQE Core Archive Builder — local server
Run: python3 server.py
Then open http://127.0.0.1:8080 (opens automatically)
"""
from __future__ import annotations

import base64, hashlib, json, re, secrets, sys, time, urllib.error, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

PORT = 8080
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
AUTH_ENDPOINT = "https://api-identity.bqecore.com/idp/connect/authorize"
TOKEN_ENDPOINT = "https://api-identity.bqecore.com/idp/connect/token"
SCOPES = "read:core"

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
TOKENS_FILE = ROOT / "tokens.json"

# BQE contractType numeric values — keys stored as both int and str because the API
# sometimes returns them as strings. Unrecognised values are treated as "fixed".
CONTRACT_TYPE_MAP: dict = {0: "hourly", "0": "hourly",  # T&M / time-and-materials
                           1: "fixed",  "1": "fixed",   # fixed fee
                           2: "hourly", "2": "hourly",  # hourly (NTE variant)
                           3: "fixed",  "3": "fixed",   # pursuit/RFQ phases
                           4: "fixed",  "4": "fixed"}   # internal overhead
DEFAULT_YEAR = 2023   # fallback for projects missing startDate
PPI_FIRST, PPI_LAST = 2010, 2025

# ── in-memory state ───────────────────────────────────────────────────────────
_pending: dict = {}   # state -> {code_verifier, client_id}
_tokens:  dict = {}   # access_token, refresh_token, endpoint, …


def _load_tokens() -> None:
    global _tokens
    if TOKENS_FILE.exists():
        try:
            _tokens = json.loads(TOKENS_FILE.read_text())
        except Exception:
            _tokens = {}


def _save_tokens(data: dict) -> None:
    global _tokens
    _tokens = data
    TOKENS_FILE.write_text(json.dumps(data, indent=2))


def _clear_tokens() -> None:
    global _tokens
    _tokens = {}
    if TOKENS_FILE.exists():
        TOKENS_FILE.unlink()


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_config(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _exchange_code(code: str, code_verifier: str, client_id: str) -> dict:
    """Exchange an authorization code for tokens."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _refresh_access_token() -> bool:
    """Use the stored refresh token to get a new access token."""
    refresh_token = _tokens.get("refresh_token")
    client_id = _load_config().get("client_id")
    if not refresh_token or not client_id:
        return False
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        if "endpoint" not in data and "endpoint" in _tokens:
            data["endpoint"] = _tokens["endpoint"]
        _save_tokens(data)
        return True
    except Exception:
        return False


# ── data fetch helpers ────────────────────────────────────────────────────────

_unknown_contract_types: set = set()

def _contract_type(ct) -> str:
    if ct is None:
        return "fixed"
    result = CONTRACT_TYPE_MAP.get(ct)
    if result:
        return result
    # Try int conversion in case BQE sends a numeric string we haven't seen
    try:
        result = CONTRACT_TYPE_MAP.get(int(ct))
        if result:
            return result
    except (TypeError, ValueError):
        pass
    if ct not in _unknown_contract_types:
        _unknown_contract_types.add(ct)
        print(f"[BQE] Unknown contractType value: {ct!r} (type {type(ct).__name__}) — treating as fixed",
              flush=True)
    return "fixed"


def _start_year(date_str) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except Exception:
        return None


def _clamp_year(yr) -> int:
    if yr is None:
        return DEFAULT_YEAR
    return max(PPI_FIRST, min(yr, PPI_LAST))


_NUM_FROM_NAME = re.compile(r'^#?(\d[\d.]*)')

def _project_num(root: dict) -> str:
    """Extract the project number from BQE data.

    Priority:
      1. Leading number from the code field (handles data errors where someone
         pasted the full name into the code field, e.g. '2002.1 Tufts House')
      2. Code field as-is if it contains no leading digit but looks like a short
         token (no spaces) — e.g. 'CC-Office'
      3. Leading number from the project name — e.g. '#2014.1 - Ashley Park'
      4. First 8 chars of UUID
    """
    code = (root.get("code") or "").strip().lstrip("#").strip()
    if code:
        m = _NUM_FROM_NAME.match(code)
        if m:
            return m.group(1)
        if " " not in code:        # short non-numeric token — use as-is
            return code
    m = _NUM_FROM_NAME.match((root.get("name") or "").strip())
    return m.group(1) if m else root["id"][:8]


def _bqe_get(path: str, params: dict) -> list:
    """Fetch one page from BQE API, with one retry on 401."""
    endpoint = _tokens.get("endpoint", "").rstrip("/")
    url = f"{endpoint}/{path}?" + urllib.parse.urlencode(params)

    def _req():
        r = urllib.request.Request(url)
        r.add_header("Authorization", f"Bearer {_tokens['access_token']}")
        r.add_header("Accept", "application/json")
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if data.strip() else []

    try:
        return _req()
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and _refresh_access_token():
            return _req()
        if exc.code == 429:
            # Respect Retry-After if present, otherwise wait 60s
            retry_after = (exc.headers or {}).get("Retry-After", "60")
            wait = float(retry_after) if str(retry_after).isdigit() else 60.0
            time.sleep(wait)
            return _req()
        raise


def _bqe_fetch_all(path: str, extra_params: dict | None = None,
                   on_page: callable | None = None,
                   page_size: int = 1000,
                   max_pages: int | None = None) -> list:
    """Paginate through a BQE endpoint, returning all items across all pages.

    BQE page param is [pageNumber],[pageSize] where pageNumber increments by 1.
    Max pageSize: 1000 without expand, 100 with expand=*.
    """
    page_num = 0
    all_items: list = []
    while True:
        if max_pages is not None and page_num >= max_pages:
            break
        params = {"page": f"{page_num},{page_size}"}
        if extra_params:
            params.update(extra_params)
        items = _bqe_get(path, params)
        if not isinstance(items, list) or not items:
            break
        all_items.extend(items)
        if on_page:
            on_page(len(all_items))
        page_num += 1
        time.sleep(0.25)   # 4 req/s → well under BQE's per-minute rate limit
    return all_items


def _join_data(projects: list, time_entries: list,
               expense_entries: list, invoices: list) -> list:
    """Aggregate T&E + invoices into a project→phase tree for the archive."""

    # Aggregate time by projectId
    time_agg: dict = {}
    for e in time_entries:
        pid = e.get("projectId")
        if not pid:
            continue
        agg = time_agg.setdefault(pid, {"cost": 0.0, "spent": 0.0})
        agg["cost"]  += (e.get("costRate") or 0) * (e.get("actualHours") or 0)
        agg["spent"] += (e.get("billRate") or 0)  * (e.get("clientHours") or 0)

    # Aggregate expenses by projectId
    exp_agg: dict = {}
    for e in expense_entries:
        pid = e.get("projectId")
        if not pid:
            continue
        agg = exp_agg.setdefault(pid, {"cost": 0.0, "spent": 0.0})
        agg["cost"]  += (e.get("costRate") or 0) * (e.get("units") or 0)
        agg["spent"] += e.get("chargeAmount") or 0

    # Aggregate contract billed by projectId from invoice line items
    inv_agg: dict = {}
    for inv in invoices:
        if inv.get("isVoid"):
            continue
        for detail in (inv.get("invoiceDetails") or []):
            pid = detail.get("projectId")
            if not pid:
                continue
            inv_agg[pid] = inv_agg.get(pid, 0.0) + (detail.get("amount") or 0)

    by_id = {p["id"]: p for p in projects}

    # Index phases (level 1) and subphases (level ≥ 2) by parentId.
    # Deduplicate by ID — BQE can return the same record on multiple pages.
    phases_by_parent: dict = {}
    subs_by_parent: dict = {}
    seen_phase_ids: set = set()
    for p in projects:
        pid_val = p.get("id")
        if pid_val in seen_phase_ids:
            continue
        seen_phase_ids.add(pid_val)
        lvl = p.get("level") or 0
        pid = p.get("parentId")
        if lvl == 1 and pid:
            phases_by_parent.setdefault(pid, []).append(p)
        elif lvl >= 2 and pid:
            subs_by_parent.setdefault(pid, []).append(p)

    def agg_ids(ids):
        cost = spent = billed = 0.0
        for i in ids:
            t = time_agg.get(i, {})
            cost   += t.get("cost", 0)
            spent  += t.get("spent", 0)
            x = exp_agg.get(i, {})
            cost   += x.get("cost", 0)
            spent  += x.get("spent", 0)
            billed += inv_agg.get(i, 0)
        return cost, spent, billed

    seen_root_ids: set = set()
    result = []
    for root in projects:
        lvl = root.get("level")
        pid = root.get("parentId")
        # Root project: no parent, and level is 0 or unset (older BQE data may omit level)
        if pid is not None:
            continue
        if lvl is not None and lvl != 0:
            continue
        if root["id"] in seen_root_ids:
            continue
        seen_root_ids.add(root["id"])
        root_id = root["id"]
        root_yr = _clamp_year(_start_year(root.get("startDate")))
        root_phases = phases_by_parent.get(root_id, [])

        phase_list = []
        seen_phase_keys: set = set()
        for ph in root_phases:
            # Deduplicate phases within a project by (name, contractAmount).
            # BQE sometimes stores genuinely duplicate phase records with different UUIDs.
            ph_key = (ph["name"], round(ph.get("contractAmount") or 0, 2))
            if ph_key in seen_phase_keys:
                continue
            seen_phase_keys.add(ph_key)

            ph_id = ph["id"]
            sub_ids = [s["id"] for s in subs_by_parent.get(ph_id, [])]
            all_ids = [ph_id] + sub_ids

            cost, spent, billed = agg_ids(all_ids)

            # If phase has subphases, contract = max(phase contract, sum of sub contracts)
            sub_contract = sum((by_id.get(sid, {}).get("contractAmount") or 0) for sid in sub_ids)
            ph_contract = ph.get("contractAmount") or 0
            contract = max(ph_contract, sub_contract) if sub_ids else ph_contract

            yr = _clamp_year(_start_year(ph.get("startDate")) or root_yr)

            phase_list.append({
                "phase":    ph["name"],
                "bill":     _contract_type(ph.get("contractType")),
                "yr":       yr,
                "contract": round(contract, 2),
                "billed":   round(billed, 2),
                "spent":    round(spent, 2),
                "cost":     round(cost, 2),
            })

        root_cost, root_spent, root_billed = agg_ids([root_id])

        if not phase_list:
            # No-phase project: synthesize a single phase from the root row
            phase_list.append({
                "phase":    root["name"],
                "bill":     _contract_type(root.get("contractType")),
                "yr":       root_yr,
                "contract": round(root.get("contractAmount") or 0, 2),
                "billed":   round(root_billed, 2),
                "spent":    round(root_spent, 2),
                "cost":     round(root_cost, 2),
            })
        elif root_cost > 0 or root_spent > 0 or root_billed > 0:
            # Time or billings posted directly to the root project (not a phase)
            phase_list.append({
                "phase":    root["name"] + " (project-level)",
                "bill":     _contract_type(root.get("contractType")),
                "yr":       root_yr,
                "contract": 0,
                "billed":   round(root_billed, 2),
                "spent":    round(root_spent, 2),
                "cost":     round(root_cost, 2),
            })

        result.append({
            "num":    _project_num(root),
            "name":   root["name"],
            "phases": phase_list,
        })

    return result


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    # ── helpers ───────────────────────────────────────────────────────────────

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
        elif path.startswith("/api/"):
            self._handle_api(path[5:], params)
        else:
            self._send_json({"error": "not found"}, 404)

    # ── route handlers ────────────────────────────────────────────────────────

    def _handle_root(self):
        ui = ROOT / "ui.html"
        if ui.exists():
            self._send_html(ui.read_bytes())
        else:
            self._send_html("<h2>ui.html not found next to server.py</h2>", 500)

    def _handle_status(self):
        cfg = _load_config()
        self._send_json({
            "connected": bool(_tokens.get("access_token")),
            "client_id": cfg.get("client_id", ""),
            "endpoint":  _tokens.get("endpoint", ""),
        })

    def _handle_connect(self, params: dict):
        client_id = params.get("client_id", "").strip()
        if not client_id:
            self._send_json({"error": "client_id is required"}, 400)
            return
        _save_config({"client_id": client_id})
        code_verifier, code_challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        _pending[state] = {
            "code_verifier": code_verifier,
            "client_id": client_id,
            "popup": params.get("popup", "0"),
        }
        auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(
            {
                "client_id":             client_id,
                "response_type":         "code",
                "scope":                 SCOPES,
                "redirect_uri":          REDIRECT_URI,
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
            desc = params.get("error_description", error)
            self._send_html(_error_page(f"BQE returned an error: {desc}"))
            return
        code  = params.get("code", "")
        state = params.get("state", "")
        if state not in _pending:
            self._send_html(_error_page(
                "State mismatch — the callback didn't match a pending login. "
                "Try connecting again."
            ))
            return
        pend = _pending.pop(state)
        try:
            tok = _exchange_code(code, pend["code_verifier"], pend["client_id"])
            _save_tokens(tok)
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
        _clear_tokens()
        self._redirect("/")

    def _handle_fetch(self, params: dict):
        """Stream all four BQE datasets and join them; report progress via SSE."""
        if not _tokens.get("access_token"):
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
                msg = f"event: {evt}\ndata: {json.dumps(data)}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass

        try:
            emit("progress", {"phase": "projects", "msg": "Fetching projects…", "pct": 5})
            projects = _bqe_fetch_all(
                "project",
                {"fields": "id,code,name,parentId,rootProjectId,level,"
                           "contractAmount,contractType,startDate"},
                max_pages=max_pages,
            )
            emit("progress", {"phase": "projects",
                               "msg": f"{len(projects):,} projects", "pct": 20})

            emit("progress", {"phase": "time", "msg": "Fetching time entries…", "pct": 22})
            time_entries = _bqe_fetch_all(
                "timeentry",
                {"fields": "projectId,costRate,actualHours,billRate,clientHours"},
                on_page=lambda n: emit("progress", {
                    "phase": "time", "msg": f"Time entries: {n:,}…", "pct": 22}),
                max_pages=max_pages,
            )
            emit("progress", {"phase": "time",
                               "msg": f"{len(time_entries):,} time entries", "pct": 60})

            emit("progress", {"phase": "expense", "msg": "Fetching expense entries…", "pct": 62})
            expense_entries = _bqe_fetch_all(
                "expenseentry",
                {"fields": "projectId,costRate,units,chargeAmount"},
                max_pages=max_pages,
            )
            emit("progress", {"phase": "expense",
                               "msg": f"{len(expense_entries):,} expense entries", "pct": 75})

            emit("progress", {"phase": "invoices", "msg": "Fetching invoices…", "pct": 77})
            invoices = _bqe_fetch_all("invoice", {"expand": "invoiceDetails"},
                                      page_size=100, max_pages=max_pages)
            emit("progress", {"phase": "invoices",
                               "msg": f"{len(invoices):,} invoices", "pct": 90})

            emit("progress", {"phase": "join", "msg": "Joining data…", "pct": 93})
            result = _join_data(projects, time_entries, expense_entries, invoices)
            emit("progress", {"phase": "join",
                               "msg": f"{len(result):,} root projects joined", "pct": 99})

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

    def _handle_api(self, subpath: str, params: dict):
        """Proxy GET requests to the BQE API."""
        if not _tokens.get("access_token"):
            self._send_json({"error": "not connected"}, 401)
            return
        endpoint = _tokens.get("endpoint", "").rstrip("/")
        if not endpoint:
            self._send_json({"error": "no API endpoint in token response"}, 500)
            return

        url = f"{endpoint}/{subpath}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        def _do_request():
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {_tokens['access_token']}")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                return resp.read(), resp.status

        try:
            data, status = _do_request()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and _refresh_access_token():
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


# ── helper pages ─────────────────────────────────────────────────────────────

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


# ── error page ────────────────────────────────────────────────────────────────

def _error_page(message: str, detail: str = "", hint: str = "") -> str:
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
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


# ── server ────────────────────────────────────────────────────────────────────

class _Server(ThreadingMixIn, HTTPServer):
    """Multi-threaded server so SSE fetch doesn't block UI requests."""
    daemon_threads = True


def main():
    _load_tokens()
    server = _Server(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"BQE Archive Builder → {url}")
    print("Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
