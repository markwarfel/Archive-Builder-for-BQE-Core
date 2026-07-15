from __future__ import annotations
import re

# BQE contractType numeric codes. Both int and str keys because the API is inconsistent.
CONTRACT_TYPE_MAP: dict = {
    0: "hourly", "0": "hourly",  # T&M / time-and-materials
    1: "fixed",  "1": "fixed",   # fixed fee
    2: "hourly", "2": "hourly",  # hourly NTE variant
    3: "fixed",  "3": "fixed",   # pursuit / RFQ phases
    4: "fixed",  "4": "fixed",   # internal overhead
}

DEFAULT_YEAR = 2023
PPI_FIRST, PPI_LAST = 2010, 2025

_unknown_contract_types: set = set()


def contract_type(ct) -> str:
    if ct is None:
        return "fixed"
    result = CONTRACT_TYPE_MAP.get(ct)
    if result:
        return result
    try:
        result = CONTRACT_TYPE_MAP.get(int(ct))
        if result:
            return result
    except (TypeError, ValueError):
        pass
    if ct not in _unknown_contract_types:
        _unknown_contract_types.add(ct)
        print(f"[BQE] Unknown contractType {ct!r} ({type(ct).__name__}) — treating as fixed", flush=True)
    return "fixed"


def start_year(date_str) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except Exception:
        return None


def clamp_year(yr) -> int:
    if yr is None:
        return DEFAULT_YEAR
    return max(PPI_FIRST, min(yr, PPI_LAST))


_NUM_FROM_NAME = re.compile(r'^#?(\d[\d.]*)')


def project_num(root: dict) -> str:
    """Extract a display project number from BQE project data.

    Priority: leading number from code field → code field as-is (if short token)
    → leading number from project name → first 8 chars of UUID.
    """
    code = (root.get("code") or "").strip().lstrip("#").strip()
    if code:
        m = _NUM_FROM_NAME.match(code)
        if m:
            return m.group(1)
        if " " not in code:
            return code
    m = _NUM_FROM_NAME.match((root.get("name") or "").strip())
    return m.group(1) if m else root["id"][:8]


def join_data(projects: list, time_entries: list,
              expense_entries: list, invoices: list) -> list:
    """Aggregate T&E + invoices into a project → phase tree for the archive.

    Confirmed formulas (match BQE Core UI exactly):
      Budget Spent = (actualHours × billRate, ALL time entries including non-billable)
                   + (chargeAmount, non-extra expenses only)
      Cost         = (actualHours × costRate, all time) + (expense costRate × units, ALL expenses)
      Contract Billed = sum of (detail.amount - extraServiceAmount - extraExpenseAmount) per project (non-void)
    """

    # Time aggregation by projectId.
    # Budget Spent includes ALL time entries (billable and non-billable) at billRate × actualHours.
    # BQE Core confirmed: non-billable time still consumes budget.
    time_agg: dict = {}
    for e in time_entries:
        pid = e.get("projectId")
        if not pid:
            continue
        agg = time_agg.setdefault(pid, {"cost": 0.0, "spent": 0.0, "role_hours": {}})
        actual = e.get("actualHours") or 0
        agg["cost"]  += (e.get("costRate") or 0) * actual
        agg["spent"] += (e.get("billRate") or 0) * actual
        key = e.get("classification") or ("$" + str(int(e.get("billRate") or 0)) + "/hr")
        agg["role_hours"][key] = agg["role_hours"].get(key, 0.0) + actual

    # Expense aggregation by projectId.
    # Cost includes ALL expenses (reimbursable pass-throughs are real firm costs).
    # Budget Spent excludes extra=True expenses — those are Billed Extra, already
    # captured in Contract Billed and not part of the fee budget.
    # Spent uses chargeAmount; Cost uses costRate × units.
    exp_agg: dict = {}
    for e in expense_entries:
        pid = e.get("projectId")
        if not pid:
            continue
        agg = exp_agg.setdefault(pid, {"cost": 0.0, "spent": 0.0})
        agg["cost"] += (e.get("costRate") or 0) * (e.get("units") or 0)
        if not e.get("extra"):
            agg["spent"] += (e.get("chargeAmount") or 0)

    # Invoice aggregation from line-item details.
    # Each detail's `amount` field is the gross total including reimbursable pass-throughs,
    # so we sum serviceAmount + expenseAmount instead — those fields exclude extraService/
    # extraExpense amounts ("Billed Extra" in BQE UI) and taxes.
    inv_agg: dict = {}
    for inv in invoices:
        if inv.get("isVoid"):
            continue
        for detail in (inv.get("invoiceDetails") or []):
            pid = detail.get("projectId")
            if not pid:
                continue
            billed = ((detail.get("amount") or 0) -
                      (detail.get("extraServiceAmount") or 0) -
                      (detail.get("extraExpenseAmount") or 0))
            inv_agg[pid] = inv_agg.get(pid, 0.0) + billed

    by_id = {p["id"]: p for p in projects}

    # Index phases (level 1) and subphases (level ≥ 2) under their parent.
    # Deduplicate by ID — BQE can return the same record across pages.
    phases_by_parent: dict = {}
    subs_by_parent:   dict = {}
    seen_ids: set = set()
    for p in projects:
        if p.get("id") in seen_ids:
            continue
        seen_ids.add(p["id"])
        lvl = p.get("level") or 0
        pid = p.get("parentId")
        if lvl == 1 and pid:
            phases_by_parent.setdefault(pid, []).append(p)
        elif lvl >= 2 and pid:
            subs_by_parent.setdefault(pid, []).append(p)

    def _all_descendants(parent_id: str) -> list:
        """Recursively collect all descendant project IDs at any nesting depth."""
        ids = []
        for child in subs_by_parent.get(parent_id, []):
            ids.append(child["id"])
            ids.extend(_all_descendants(child["id"]))
        return ids

    def _agg(ids):
        cost = spent = billed = hours = 0.0
        merged: dict = {}
        for i in ids:
            t = time_agg.get(i, {})
            cost  += t.get("cost", 0)
            spent += t.get("spent", 0)
            for k, h in t.get("role_hours", {}).items():
                merged[k] = merged.get(k, 0.0) + h
                hours += h
            x = exp_agg.get(i, {})
            cost  += x.get("cost", 0)
            spent += x.get("spent", 0)
            billed += inv_agg.get(i, 0)
        role_buckets = sorted(merged.items(), key=lambda kh: -kh[1])
        return cost, spent, billed, hours, role_buckets

    result = []
    seen_root_ids: set = set()
    for root in projects:
        lvl = root.get("level")
        pid = root.get("parentId")
        if pid is not None:
            continue
        if lvl is not None and lvl != 0:
            continue
        if root["id"] in seen_root_ids:
            continue
        seen_root_ids.add(root["id"])

        root_id  = root["id"]
        root_yr  = clamp_year(start_year(root.get("startDate")))
        root_phases = phases_by_parent.get(root_id, [])

        phase_list = []
        seen_phase_keys: set = set()
        for ph in root_phases:
            # Deduplicate phases by (name, contractAmount) — BQE sometimes stores
            # genuinely duplicate phase records with different UUIDs.
            ph_key = (ph["name"], round(ph.get("contractAmount") or 0, 2))
            if ph_key in seen_phase_keys:
                continue
            seen_phase_keys.add(ph_key)

            ph_id   = ph["id"]
            all_ids = [ph_id] + _all_descendants(ph_id)

            cost, spent, billed, hours, role_buckets = _agg(all_ids)

            # Contract = max(phase contract, sum of direct sub-contracts) when subphases exist
            direct_subs = [s["id"] for s in subs_by_parent.get(ph_id, [])]
            sub_contract = sum((by_id.get(s, {}).get("contractAmount") or 0) for s in direct_subs)
            ph_contract  = ph.get("contractAmount") or 0
            contract = max(ph_contract, sub_contract) if direct_subs else ph_contract

            yr = clamp_year(start_year(ph.get("startDate")) or root_yr)

            phase_list.append({
                "phase":        ph["name"],
                "bill":         contract_type(ph.get("contractType")),
                "yr":           yr,
                "contract":     round(contract, 2),
                "billed":       round(billed, 2),
                "spent":        round(spent, 2),
                "cost":         round(cost, 2),
                "hours":        round(hours, 2),
                "role_buckets": [[k, round(h, 2)] for k, h in role_buckets],
            })

        root_cost, root_spent, root_billed, root_hours, root_role_buckets = _agg([root_id])

        if not phase_list:
            phase_list.append({
                "phase":        root["name"],
                "bill":         contract_type(root.get("contractType")),
                "yr":           root_yr,
                "contract":     round(root.get("contractAmount") or 0, 2),
                "billed":       round(root_billed, 2),
                "spent":        round(root_spent, 2),
                "cost":         round(root_cost, 2),
                "hours":        round(root_hours, 2),
                "role_buckets": [[k, round(h, 2)] for k, h in root_role_buckets],
            })
        elif root_cost > 0 or root_spent > 0 or root_billed > 0:
            # Time or billings posted directly to the root project (not a phase)
            phase_list.append({
                "phase":        root["name"] + " (project-level)",
                "bill":         contract_type(root.get("contractType")),
                "yr":           root_yr,
                "contract":     0,
                "billed":       round(root_billed, 2),
                "spent":        round(root_spent, 2),
                "cost":         round(root_cost, 2),
                "hours":        round(root_hours, 2),
                "role_buckets": [[k, round(h, 2)] for k, h in root_role_buckets],
            })

        result.append({
            "num":    project_num(root),
            "name":   root["name"],
            "phases": phase_list,
        })

    return result
