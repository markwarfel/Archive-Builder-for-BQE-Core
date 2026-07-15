#!/usr/bin/env python3
"""
Build a sample archive from fake data — no BQE connection required.

Run:  python3 build_sample.py
Then open the generated sample-archive.html in a browser.

This demonstrates the output of the tool (search, inflation adjustment, computed
columns, hours-by-role, horizontal scroll) using invented projects, so you can
preview or screenshot it without touching real client data. The data below is
entirely fictional.
"""
import json
from pathlib import Path

import join

ROOT = Path(__file__).parent

# ── Fake source data, shaped exactly like the BQE API responses ───────────────
# Roles double as the "classification" field that drives the hours-by-role column.
PRINCIPAL, PM, DESIGNER, DRAFTER = (
    "Principal", "Project Manager", "Landscape Designer", "Drafter",
)

_projects = []
_time = []
_expenses = []
_invoices = []


def _project(pid, name, level, parent, contract, ctype, year):
    _projects.append({
        "id": pid, "name": name, "level": level, "parentId": parent,
        "contractAmount": contract, "contractType": ctype,
        "startDate": f"{year}-03-15T00:00:00",
    })


def _time_entry(pid, hours, bill_rate, cost_rate, role, billable=True):
    _time.append({
        "projectId": pid, "billable": billable, "extra": False,
        "actualHours": hours, "billRate": bill_rate, "costRate": cost_rate,
        "classification": role,
    })


def _expense(pid, cost_rate, units, charge, extra=False):
    _expenses.append({
        "projectId": pid, "billable": True, "extra": extra,
        "costRate": cost_rate, "units": units, "chargeAmount": charge,
    })


def _invoice(pid, amount, extra_service=0, extra_expense=0):
    _invoices.append({
        "isVoid": False,
        "invoiceDetails": [{
            "projectId": pid, "amount": amount,
            "extraServiceAmount": extra_service, "extraExpenseAmount": extra_expense,
        }],
    })


# Project 1 — a healthy, profitable multi-phase project spanning several years.
_project("p1", "2101 Riverside Park Restoration", 0, None, 240000, 1, 2021)
_project("p1-sd", "Schematic Design", 1, "p1", 48000, 1, 2021)
_project("p1-dd", "Design Development", 1, "p1", 72000, 1, 2022)
_project("p1-cd", "Construction Documents", 1, "p1", 120000, 1, 2023)
for phase, principal_h, pm_h, des_h, draft_h in [
    ("p1-sd", 24, 60, 120, 40),
    ("p1-dd", 30, 90, 220, 110),
    ("p1-cd", 40, 160, 300, 260),
]:
    _time_entry(phase, principal_h, 235, 150, PRINCIPAL)
    _time_entry(phase, pm_h, 165, 104, PM)
    _time_entry(phase, des_h, 125, 78, DESIGNER)
    _time_entry(phase, draft_h, 95, 60, DRAFTER)
_expense("p1-cd", 40, 3, 180)
_invoice("p1-sd", 48000)
_invoice("p1-dd", 72000)
_invoice("p1-cd", 108000)  # partially billed

# Project 2 — an older residence; the CA phase ran over budget (a loss).
_project("p2", "1906 Hillcrest Residence", 0, None, 60000, 1, 2019)
_project("p2-sd", "Schematic Design", 1, "p2", 40000, 1, 2019)
_project("p2-ca", "Construction Administration", 1, "p2", 20000, 2, 2020)
_time_entry("p2-sd", 20, 220, 140, PRINCIPAL)
_time_entry("p2-sd", 80, 120, 75, DESIGNER)
_time_entry("p2-ca", 60, 220, 140, PRINCIPAL)   # over-serviced → loss
_time_entry("p2-ca", 140, 120, 75, DESIGNER)
_time_entry("p2-ca", 10, 90, 58, DRAFTER, billable=False)
_expense("p2-ca", 55, 4, 260)
_invoice("p2-sd", 40000)
_invoice("p2-ca", 20000)

# Project 3 — an hourly project with no fixed contract (shows "Hourly — open to view").
_project("p3", "2308 Civic Plaza Concept Study", 0, None, 0, 0, 2023)
_project("p3-study", "Feasibility Study", 1, "p3", 0, 0, 2023)
_time_entry("p3-study", 18, 235, 150, PRINCIPAL)
_time_entry("p3-study", 46, 165, 104, PM)
_time_entry("p3-study", 30, 125, 78, DESIGNER)
_invoice("p3-study", 14200)

# ── Build the archive HTML (mirrors index.html's build step) ──────────────────
FIRM = "Meridian Design Studio"
TITLE = "Project Performance Archive"
ACCENT = "#2d6a5c"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _hex_soft(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(c * 0.12 + 255 * 0.88)
    return f"rgb({mix(r)},{mix(g)},{mix(b)})"


def main():
    projects = join.join_data(_projects, _time, _expenses, _invoices)

    template = (ROOT / "archive-template.html").read_text()
    css = (ROOT / "archive-template.css").read_text()

    brand_lockup = (f'<div class="masthead"><div class="brand">'
                    f'<div class="wordmark">{_esc(FIRM)}</div></div></div>')
    # Escape "</" so project data can't prematurely close the archive's <script>.
    safe_json = json.dumps(projects).replace("</", "<\\/")

    # Inject styles first so the __ACCENT__ tokens inside the CSS get replaced too.
    html = template.replace("__STYLES__", css)
    for token, value in [
        ("__DOCTITLE__",    _esc(f"{FIRM} — {TITLE}")),
        ("__ACCENT__",      ACCENT),
        ("__ACCENT_SOFT__", _hex_soft(ACCENT)),
        ("__BRAND_LOCKUP__", brand_lockup),
        ("__TITLE__",       _esc(TITLE)),
        ("__PROJECTS__",    safe_json),
    ]:
        html = html.replace(token, value)

    out = ROOT / "sample-archive.html"
    out.write_text(html)
    print(f"Wrote {out} ({len(html):,} bytes) — open it in a browser.")


if __name__ == "__main__":
    main()
