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

# Fallback rate-table year for phases with no start date, used only to clamp the
# inflation year. This is distinct from the archive template's DEFAULT_YEAR, which
# is the baseline "today's dollars" display year — the two serve different purposes.
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
    for entry in time_entries:
        project_id = entry.get("projectId")
        if not project_id:
            continue
        totals = time_agg.setdefault(project_id, {"cost": 0.0, "spent": 0.0, "role_hours": {}})
        actual_hours = entry.get("actualHours") or 0
        totals["cost"]  += (entry.get("costRate") or 0) * actual_hours
        totals["spent"] += (entry.get("billRate") or 0) * actual_hours
        # Bucket hours by staff role (classification); fall back to bill rate when unclassified.
        role = entry.get("classification") or ("$" + str(int(entry.get("billRate") or 0)) + "/hr")
        totals["role_hours"][role] = totals["role_hours"].get(role, 0.0) + actual_hours

    # Expense aggregation by projectId.
    # Cost includes ALL expenses (reimbursable pass-throughs are real firm costs).
    # Budget Spent excludes extra=True expenses — those are Billed Extra, already
    # captured in Contract Billed and not part of the fee budget.
    # Spent uses chargeAmount; Cost uses costRate × units.
    exp_agg: dict = {}
    for entry in expense_entries:
        project_id = entry.get("projectId")
        if not project_id:
            continue
        totals = exp_agg.setdefault(project_id, {"cost": 0.0, "spent": 0.0})
        totals["cost"] += (entry.get("costRate") or 0) * (entry.get("units") or 0)
        if not entry.get("extra"):
            totals["spent"] += (entry.get("chargeAmount") or 0)

    # Invoice aggregation from line-item details.
    # Each detail's `amount` field is the gross total including reimbursable pass-throughs,
    # so we sum serviceAmount + expenseAmount instead — those fields exclude extraService/
    # extraExpense amounts ("Billed Extra" in BQE UI) and taxes.
    inv_agg: dict = {}
    for invoice in invoices:
        if invoice.get("isVoid"):
            continue
        for detail in (invoice.get("invoiceDetails") or []):
            project_id = detail.get("projectId")
            if not project_id:
                continue
            billed = ((detail.get("amount") or 0) -
                      (detail.get("extraServiceAmount") or 0) -
                      (detail.get("extraExpenseAmount") or 0))
            inv_agg[project_id] = inv_agg.get(project_id, 0.0) + billed

    by_id = {proj["id"]: proj for proj in projects}

    # Index phases (level 1) and subphases (level ≥ 2) under their parent.
    # Deduplicate by ID — BQE can return the same record across pages.
    phases_by_parent: dict = {}
    subs_by_parent:   dict = {}
    seen_ids: set = set()
    for project in projects:
        if project.get("id") in seen_ids:
            continue
        seen_ids.add(project["id"])
        level     = project.get("level") or 0
        parent_id = project.get("parentId")
        if level == 1 and parent_id:
            phases_by_parent.setdefault(parent_id, []).append(project)
        elif level >= 2 and parent_id:
            subs_by_parent.setdefault(parent_id, []).append(project)

    def _all_descendants(parent_id: str) -> list:
        """Recursively collect all descendant project IDs at any nesting depth."""
        ids = []
        for child in subs_by_parent.get(parent_id, []):
            ids.append(child["id"])
            ids.extend(_all_descendants(child["id"]))
        return ids

    def aggregate_ids(project_ids):
        """Sum cost/spent/billed/hours and merge role-hour buckets across a set of project IDs."""
        cost = spent = billed = hours = 0.0
        role_hours_merged: dict = {}
        for project_id in project_ids:
            time_totals = time_agg.get(project_id, {})
            cost  += time_totals.get("cost", 0)
            spent += time_totals.get("spent", 0)
            for role, role_hrs in time_totals.get("role_hours", {}).items():
                role_hours_merged[role] = role_hours_merged.get(role, 0.0) + role_hrs
                hours += role_hrs
            expense_totals = exp_agg.get(project_id, {})
            cost  += expense_totals.get("cost", 0)
            spent += expense_totals.get("spent", 0)
            billed += inv_agg.get(project_id, 0)
        role_buckets = sorted(role_hours_merged.items(), key=lambda rb: -rb[1])
        return cost, spent, billed, hours, role_buckets

    result = []
    seen_root_ids: set = set()
    for root in projects:
        level     = root.get("level")
        parent_id = root.get("parentId")
        if parent_id is not None:
            continue
        if level is not None and level != 0:
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

            cost, spent, billed, hours, role_buckets = aggregate_ids(all_ids)

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
                "role_buckets": [[role, round(role_hrs, 2)] for role, role_hrs in role_buckets],
            })

        root_cost, root_spent, root_billed, root_hours, root_role_buckets = aggregate_ids([root_id])

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
                "role_buckets": [[role, round(role_hrs, 2)] for role, role_hrs in root_role_buckets],
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
                "role_buckets": [[role, round(role_hrs, 2)] for role, role_hrs in root_role_buckets],
            })

        result.append({
            "num":    project_num(root),
            "name":   root["name"],
            "phases": phase_list,
        })

    return result
