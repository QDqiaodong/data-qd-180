"""Canonical business records, plans, conflict evidence and review identities.

The existing :mod:`spectrum.model` / :mod:`spectrum.interference` code remains the
single source of truth for the protection-circle relation.  This module only adds
strict workbench-facing normalization so that semantically equal inputs have one
serialized form.
"""
import hashlib
import json
from datetime import timezone
from fractions import Fraction

from . import model, service
from .errors import WorkbenchError

FIELDS = (
    "id",
    "station_id",
    "receiver_id",
    "center_hz",
    "bandwidth_hz",
    "start",
    "end",
)
FIELD_SET = frozenset(FIELDS)
EVIDENCE_VERSION = 1


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def text_id(value, label):
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchError("invalid_input", f"{label} must be a nonempty string")
    # Deliberately keep the original spelling: no trimming and no case folding.
    return value


def strict_integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise WorkbenchError(
            "invalid_input", f"{label} must be an integer >= {minimum}"
        )
    return value


def normalize_time(value, label):
    try:
        parsed = model.stamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkbenchError(
            "invalid_input", f"{label} must be an ISO-8601 time with an offset"
        ) from exc
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_transmission(row, label="transmission"):
    """Return the seven-field business value of an emission record."""
    if not isinstance(row, dict):
        raise WorkbenchError("invalid_input", f"{label} must be an object")
    keys = set(row)
    missing = sorted(FIELD_SET - keys)
    extra = sorted(keys - FIELD_SET)
    if missing:
        raise WorkbenchError(
            "invalid_input", f"{label} is missing field(s): {', '.join(missing)}"
        )
    if extra:
        raise WorkbenchError(
            "invalid_input", f"{label} has unknown field(s): {', '.join(extra)}"
        )
    return {
        "id": text_id(row["id"], f"{label}.id"),
        "station_id": text_id(row["station_id"], f"{label}.station_id"),
        "receiver_id": text_id(row["receiver_id"], f"{label}.receiver_id"),
        "center_hz": strict_integer(row["center_hz"], f"{label}.center_hz", 1),
        "bandwidth_hz": strict_integer(row["bandwidth_hz"], f"{label}.bandwidth_hz", 1),
        "start": normalize_time(row["start"], f"{label}.start"),
        "end": normalize_time(row["end"], f"{label}.end"),
    }


def canonical_plan(stations, transmissions):
    """A stable array order; array order is never part of business identity."""
    return {
        "stations": [dict(row) for row in sorted(stations, key=lambda r: r["id"])],
        "transmissions": [
            dict(row) for row in sorted(transmissions, key=lambda r: r["id"])
        ],
    }


def plan_records(plan):
    return {row["id"]: row for row in plan["transmissions"]}


def compute_report(plan):
    """Run the untouched original model.  Raises ValueError on invalid plans."""
    return service.run(
        {
            "stations": plan["stations"],
            "transmissions": plan["transmissions"],
        }
    )


def normalize_source_plan(data):
    """Validate an init source against the original contract with strict fields."""
    if not isinstance(data, dict):
        raise WorkbenchError("invalid_input", "source root must be an object")
    if "stations" not in data or "transmissions" not in data:
        raise WorkbenchError(
            "invalid_input", "source must contain stations and transmissions"
        )
    stations = data["stations"]
    raw_transmissions = data["transmissions"]
    if not isinstance(stations, list) or not isinstance(raw_transmissions, list):
        raise WorkbenchError(
            "invalid_input", "stations and transmissions must be arrays"
        )
    transmissions = [
        normalize_transmission(row, f"transmissions[{index}]")
        for index, row in enumerate(raw_transmissions)
    ]
    try:
        plan = canonical_plan(stations, transmissions)
        compute_report(plan)
    except WorkbenchError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise WorkbenchError("invalid_input", str(exc)) from exc
    return plan


def apply_operations(plan, upserts, remove_ids):
    """Apply one complete proposal to a plan map; used on base and on current."""
    records = plan_records(plan)
    for row in upserts:
        records[row["id"]] = dict(row)
    for identifier in remove_ids:
        records.pop(identifier)  # premise checks guarantee presence
    return canonical_plan(plan["stations"], records.values())


def canonical_evidence_row(row):
    """All original conflict-report fields, in comparable business notation."""
    start = model.stamp(row["overlap_start"]).isoformat()
    end = model.stamp(row["overlap_end"]).isoformat()
    return {
        "victim_transmission_id": row["victim_transmission_id"],
        "interferer_transmission_id": row["interferer_transmission_id"],
        "receiver_id": row["receiver_id"],
        "interferer_station_id": row["interferer_station_id"],
        "overlap_start": start,
        "overlap_end": end,
        "overlap_low_hz": str(Fraction(row["overlap_low_hz"])),
        "overlap_high_hz": str(Fraction(row["overlap_high_hz"])),
        "overlap_bandwidth_hz": str(Fraction(row["overlap_bandwidth_hz"])),
        "separation_m": format(float(row["separation_m"]), ".3f"),
    }


def evidence_by_receiver(report):
    """Group complete rows by receiver, in a canonical order."""
    grouped = {}
    for row in report["conflicts"]:
        canonical = canonical_evidence_row(row)
        grouped.setdefault(row["receiver_id"], []).append((canonical, row))
    result = {}
    for receiver, entries in grouped.items():
        entries.sort(key=lambda item: canonical_json(item[0]))
        result[receiver] = {
            "canonical": [item[0] for item in entries],
            "rows": [item[1] for item in entries],
        }
    return result


def make_review_id(ledger_id, proposal_id, receiver_id, evidence_rows):
    """Bind this local ledger, proposal, receiver and the complete evidence set.

    Deliberately excludes revision numbers, other receivers and unrelated plan
    fields so an unchanged evidence set remains reusable across revisions.
    Rows are normalized here as well, so row order, time offsets and equivalent
    numeric spellings can never split one evidence identity.
    """
    canonical = sorted(
        (canonical_evidence_row(row) for row in evidence_rows),
        key=canonical_json,
    )
    payload = {
        "version": EVIDENCE_VERSION,
        "ledger_id": ledger_id,
        "proposal_id": proposal_id,
        "receiver_id": receiver_id,
        "evidence": canonical,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "review-" + digest


def required_reviews(
    ledger_id, proposal_id, current_report, candidate_report, approved_pairs
):
    """Receivers whose full candidate evidence is nonempty and differs now."""
    current = evidence_by_receiver(current_report)
    candidate = evidence_by_receiver(candidate_report)
    reviews = []
    for receiver in sorted(candidate):
        candidate_rows = candidate[receiver]["canonical"]
        if not candidate_rows:
            continue
        current_rows = current.get(receiver, {}).get("canonical", [])
        if candidate_rows == current_rows:
            continue
        review_id = make_review_id(
            ledger_id, proposal_id, receiver, candidate[receiver]["rows"]
        )
        reviews.append(
            {
                "receiver_id": receiver,
                "review_id": review_id,
                "evidence": candidate[receiver]["rows"],
                "approved": (receiver, review_id) in approved_pairs,
            }
        )
    return reviews
