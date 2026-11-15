"""Display recorded auditor costs without treating missing usage as a free check."""


def deepseek_cost_label(cost, audit_status):
    if cost is not None and float(cost) > 0:
        return f"${float(cost):.4f}"
    status = (audit_status or "").upper()
    if status in ("NOT_RUN", "SKIPPED"):
        return "not run"
    if status == "ERROR":
        return "check failed; price unavailable"
    if status in ("APPROVED", "REVISION_NEEDED"):
        return "checked; price unavailable"
    return "status unknown"
