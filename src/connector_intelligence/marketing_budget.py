"""Deterministic extraction and calculation for campaign-budget workbooks."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .rental_quote import _locations_for_span, _read_locations


_AMOUNT = re.compile(
    r"^\s*(?P<currency>INR|USD|EUR|GBP|₹|\$|€|£)?\s*"
    r"(?P<negative>\()?\s*(?P<number>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*\)?\s*$",
    re.I,
)
_CURRENCY = re.compile(r"\b(INR|USD|EUR|GBP)\b|[₹$€£]", re.I)
_TOTAL_NAMES = {"total", "all campaigns", "grand total", "subtotal", "overall total"}


def is_marketing_budget_question(question: str) -> bool:
    """Recognize the first deterministic campaign expenditure question family."""
    value = question.casefold()
    if re.search(r"\b(?:rental|quotation|invoice|payment|phone|whatsapp|gmail)\b", value):
        return False
    context = bool(re.search(r"\b(?:marketing|campaigns?|advertising)\b", value))
    expenditure_request = bool(re.search(
        r"\b(?:budget|expenditure|spend(?:ing)?|planned|actual|overspent|underspent|variance)\b",
        value,
    ))
    return expenditure_request and (
        context or "planned expenditure" in value or "actual expenditure" in value
        or "planned budget" in value or "actual budget" in value
    )


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.casefold())


def _column_index(headers: list[str], alternatives: tuple[str, ...]) -> int | None:
    normalized = [_normalized_header(value) for value in headers]
    for index, value in enumerate(normalized):
        if any(alternative in value for alternative in alternatives):
            return index
    return None


def _cell_spans(line: str, line_start: int) -> list[tuple[str, int, int]]:
    cells = []
    cursor = 0
    for raw in line.split("|"):
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        cells.append((raw.strip(), line_start + cursor + left, line_start + cursor + right))
        cursor += len(raw) + 1
    return cells


def _parse_amount(raw: str, default_currency: str | None) -> tuple[Decimal, str | None] | None:
    match = _AMOUNT.fullmatch(raw)
    if not match:
        return None
    try:
        amount = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    currency = match.group("currency") or default_currency
    if match.group("negative"):
        amount = -abs(amount)
    return amount, currency.upper() if currency else None


def _currency_label(value: str | None) -> str:
    return {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}.get(value or "", value or "")


def format_amount(amount: Decimal, currency: str | None) -> str:
    """Format a calculated amount without losing fractional currency precision."""
    quantized = amount.quantize(Decimal("0.01"))
    number = f"{quantized:,.2f}".rstrip("0").rstrip(".")
    label = _currency_label(currency)
    return f"{label} {number}" if label else number


def _line_location(locations: list[dict[str, Any]], start: int, end: int) -> dict[str, Any] | None:
    for location in _locations_for_span(start, end, locations):
        if type(location.get("row")) is int and isinstance(location.get("worksheet"), str):
            return location
    return None


def extract_campaign_budget(content: str, metadata_json: Any = "{}") -> dict[str, Any]:
    """Extract campaign rows from worksheet text using its labeled column headers."""
    locations = _read_locations(metadata_json, len(content))
    campaigns: list[dict[str, Any]] = []
    sheet_name: str | None = None
    header: list[str] | None = None
    columns: dict[str, int | None] = {}
    default_currency: str | None = None
    offset = 0

    for raw_line in content.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start, line_end = offset, offset + len(line)
        offset += len(raw_line)
        worksheet_match = re.match(r"\s*Worksheet\s*:\s*(.+?)\s*$", line, re.I)
        if worksheet_match:
            sheet_name = worksheet_match.group(1).strip()
            header = None
            continue
        if not line.strip() or "|" not in line:
            continue

        cells = _cell_spans(line, line_start)
        values = [cell[0] for cell in cells]
        campaign_column = _column_index(values, ("campaign",))
        planned_column = _column_index(values, ("plannedexpenditure", "plannedspend", "plannedbudget"))
        actual_column = _column_index(values, ("actualexpenditure", "actualspend", "actualbudget"))
        if campaign_column is not None and planned_column is not None and actual_column is not None:
            header = values
            columns = {
                "campaign": campaign_column,
                "channel": _column_index(values, ("channel",)),
                "planned": planned_column,
                "actual": actual_column,
                "performance": _column_index(values, ("performance", "result", "engagement")),
            }
            header_currency = _CURRENCY.search(" ".join(values))
            default_currency = header_currency.group(0).upper() if header_currency else None
            continue
        if header is None or sheet_name is None:
            continue

        name_index = columns["campaign"]
        if name_index is None or name_index >= len(cells):
            continue
        name = cells[name_index][0]
        if not name or name.casefold().strip(" :") in _TOTAL_NAMES:
            continue
        # A campaign row must belong to a data row locator on the same sheet.
        location = _line_location(locations, line_start, line_end)
        if not location or location.get("worksheet") != sheet_name:
            continue
        if len(cells) < len(header):
            cells.extend([("", line_end, line_end)] * (len(header) - len(cells)))

        campaign: dict[str, Any] = {
            "campaign": name,
            "channel": cells[columns["channel"]][0] if columns["channel"] is not None else None,
            "planned_expenditure": None,
            "actual_expenditure": None,
            "performance": cells[columns["performance"]][0] if columns["performance"] is not None else None,
            "variance": None,
            "status": "supported",
            "evidence": line.strip(),
            "start": line_start,
            "end": line_end,
            "source_locations": [location],
        }
        for key, column_key in (("planned_expenditure", "planned"), ("actual_expenditure", "actual")):
            index = columns[column_key]
            if index is None or index >= len(cells):
                continue
            raw_value = cells[index][0]
            if not raw_value:
                continue
            parsed = _parse_amount(raw_value, default_currency)
            if parsed is None:
                campaign[key] = {"value": raw_value, "status": "invalid"}
            else:
                amount, currency = parsed
                campaign[key] = {
                    "value": format_amount(amount, currency), "source_value": raw_value, "amount": amount,
                    "currency": currency, "status": "extracted",
                }
        planned = campaign["planned_expenditure"]
        actual = campaign["actual_expenditure"]
        if isinstance(planned, dict) and isinstance(actual, dict) and \
                planned.get("status") == actual.get("status") == "extracted":
            if planned.get("currency") == actual.get("currency"):
                variance = actual["amount"] - planned["amount"]
                campaign["variance"] = {
                    "value": format_amount(variance, actual.get("currency")),
                    "amount": variance, "currency": actual.get("currency"),
                    "status": "calculated",
                }
                campaign["spending_status"] = (
                    "over_budget" if variance > 0 else "under_budget" if variance < 0 else "on_budget"
                )
            else:
                campaign["variance"] = {"value": None, "status": "unsupported",
                                         "note": "Planned and actual currencies differ."}
                campaign["spending_status"] = "not_comparable"
        else:
            campaign["variance"] = {"value": None, "status": "missing",
                                     "note": "Both planned and actual values are required."}
            campaign["spending_status"] = "insufficient_data"
        campaigns.append(campaign)

    return {"campaigns": campaigns, "source_locations_available": bool(locations)}


def requested_budget_view(question: str) -> str:
    """Choose the deterministic view requested by the question."""
    value = question.casefold()
    if re.search(r"\b(?:exceed|exceeded|over\s*budget|overspent|above\s+budget)\b", value):
        return "over_budget"
    if re.search(r"\b(?:below\s+budget|under\s*budget|underspent|spending\s+less)\b", value):
        return "under_budget"
    if re.search(r"\bcompare\b|\bvariance\b", value) or re.search(r"\bplanned\b.*\bactual\b|\bactual\b.*\bplanned\b", value):
        return "compare"
    if re.search(r"\btotal\b.*\b(?:planned|budget)\b|\b(?:planned|budget)\b.*\btotal\b", value):
        return "total_planned"
    if re.search(r"\btotal\b.*\bactual\b|\bactual\b.*\btotal\b", value):
        return "total_actual"
    if re.search(r"\bactual\b", value):
        return "actual"
    if re.search(r"\bplanned\b", value):
        return "planned"
    return "details"


def _total(campaigns: list[dict[str, Any]], key: str) -> dict[str, Any]:
    entries = [campaign[key] for campaign in campaigns
               if isinstance(campaign.get(key), dict) and campaign[key].get("status") == "extracted"]
    if not entries:
        return {"value": None, "status": "insufficient_evidence", "row_count": 0,
                "source_locations": []}
    currencies = {entry.get("currency") for entry in entries}
    if len(currencies) != 1:
        return {"value": None, "status": "unsupported", "row_count": len(entries),
                "source_locations": [], "note": "Values use different or unspecified currencies."}
    amount = sum((entry["amount"] for entry in entries), Decimal("0"))
    supporting = [campaign for campaign in campaigns
                  if isinstance(campaign.get(key), dict) and campaign[key].get("status") == "extracted"]
    locations = []
    for campaign in supporting:
        for location in campaign["source_locations"]:
            if location not in locations:
                locations.append(location)
    return {
        "value": format_amount(amount, next(iter(currencies))), "amount": amount,
        "currency": next(iter(currencies)),
        "status": "calculated" if len(entries) == len(campaigns) else "partially_calculated",
        "row_count": len(entries),
        "campaign_count": len(campaigns),
        "source_locations": locations,
        "evidence": [campaign["evidence"] for campaign in supporting],
    }


def summarize_campaign_budget(campaigns: list[dict[str, Any]], view: str) -> tuple[str, str, dict[str, Any]]:
    """Create a deterministic summary, calculations, and evidence status."""
    planned_total = _total(campaigns, "planned_expenditure")
    actual_total = _total(campaigns, "actual_expenditure")
    variance_total = None
    paired = [campaign for campaign in campaigns
              if isinstance(campaign.get("planned_expenditure"), dict)
              and isinstance(campaign.get("actual_expenditure"), dict)
              and campaign["planned_expenditure"].get("status") == "extracted"
              and campaign["actual_expenditure"].get("status") == "extracted"
              and campaign["planned_expenditure"].get("currency") == campaign["actual_expenditure"].get("currency")]
    paired_currencies = {campaign["planned_expenditure"].get("currency") for campaign in paired}
    if paired and len(paired_currencies) == 1:
        currency = next(iter(paired_currencies))
        variance_amount = sum((campaign["actual_expenditure"]["amount"] -
                               campaign["planned_expenditure"]["amount"] for campaign in paired), Decimal("0"))
        variance_locations = []
        for campaign in paired:
            for location in campaign["source_locations"]:
                if location not in variance_locations:
                    variance_locations.append(location)
        variance_total = {
            "value": format_amount(variance_amount, currency),
            "status": "calculated" if len(paired) == len(campaigns) else "partially_calculated",
            "source_locations": variance_locations,
            "row_count": len(paired),
        }

    has_comparison = any(item.get("spending_status") in {"over_budget", "under_budget", "on_budget"}
                         for item in campaigns)
    status = ("supported" if campaigns and all(
        isinstance(item.get("planned_expenditure"), dict)
        and isinstance(item.get("actual_expenditure"), dict)
        and item["planned_expenditure"].get("status") == item["actual_expenditure"].get("status") == "extracted"
        for item in campaigns
    ) else "partially_supported" if campaigns else "insufficient_evidence")
    if view == "over_budget":
        matches = [item for item in campaigns if item.get("spending_status") == "over_budget"]
        summary = ("Above-budget campaigns: " + (", ".join(item["campaign"] for item in matches)
                   if matches else "none identified from comparable planned and actual values."))
        status = "supported" if has_comparison and all(item.get("spending_status") != "insufficient_data" for item in campaigns) else "partially_supported"
    elif view == "under_budget":
        matches = [item for item in campaigns if item.get("spending_status") == "under_budget"]
        summary = ("Below-budget campaigns: " + (", ".join(item["campaign"] for item in matches)
                   if matches else "none identified from comparable planned and actual values."))
        status = "supported" if has_comparison and all(item.get("spending_status") != "insufficient_data" for item in campaigns) else "partially_supported"
    elif view == "total_planned":
        qualifier = "calculated" if planned_total.get("status") == "calculated" else "partially calculated"
        summary = (f"Total planned expenditure: {planned_total['value']} ({qualifier} from {planned_total['row_count']} of {planned_total.get('campaign_count', 0)} campaign rows)."
                   if planned_total["value"] is not None else "Total planned expenditure could not be calculated from supported campaign rows.")
        status = "supported" if planned_total["status"] == "calculated" else "partially_supported" if planned_total["status"] == "partially_calculated" else "insufficient_evidence"
    elif view == "total_actual":
        qualifier = "calculated" if actual_total.get("status") == "calculated" else "partially calculated"
        summary = (f"Total actual expenditure: {actual_total['value']} ({qualifier} from {actual_total['row_count']} of {actual_total.get('campaign_count', 0)} campaign rows)."
                   if actual_total["value"] is not None else "Total actual expenditure could not be calculated from supported campaign rows.")
        status = "supported" if actual_total["status"] == "calculated" else "partially_supported" if actual_total["status"] == "partially_calculated" else "insufficient_evidence"
    elif view == "planned":
        summary = "; ".join(
            f"{item['campaign']}: planned {item['planned_expenditure']['value']}"
            for item in campaigns if isinstance(item.get("planned_expenditure"), dict)
            and item["planned_expenditure"].get("status") == "extracted"
        ) or "No planned expenditure values were verified."
        if planned_total["value"] is not None:
            summary += f". Total planned expenditure: {planned_total['value']} (calculated)."
    elif view == "actual":
        summary = "; ".join(
            f"{item['campaign']}: actual {item['actual_expenditure']['value']}"
            for item in campaigns if isinstance(item.get("actual_expenditure"), dict)
            and item["actual_expenditure"].get("status") == "extracted"
        ) or "No actual expenditure values were verified."
        if actual_total["value"] is not None:
            summary += f". Total actual expenditure: {actual_total['value']} (calculated)."
    else:
        above = [item["campaign"] for item in campaigns if item.get("spending_status") == "over_budget"]
        below = [item["campaign"] for item in campaigns if item.get("spending_status") == "under_budget"]
        summary = (
            f"{len(campaigns)} campaign rows were extracted. Planned total: {planned_total['value']} (calculated); "
            f"actual total: {actual_total['value']} (calculated). "
            f"Variance (actual minus planned): {variance_total['value'] if variance_total else 'unavailable'}. "
            f"Above budget: {', '.join(above) if above else 'none'}. "
            f"Below budget: {', '.join(below) if below else 'none'}."
        )
    calculations = {"planned_total": planned_total, "actual_total": actual_total,
                    "variance_total": variance_total}
    return summary, status, calculations
