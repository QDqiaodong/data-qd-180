"""Cross-process plan-change workbench built on the unchanged conflict model.

State lives only in one local ledger.  Each command holds the ledger's exclusive
lock for its whole read-modify-write, so commands of two processes take effect
one at a time.
"""
from .errors import WorkbenchError
from .ledger import LedgerFile, create_new, empty_ledger
from .records import (
    apply_operations,
    canonical_json,
    compute_report,
    normalize_source_plan,
    normalize_transmission,
    plan_records,
    required_reviews,
    strict_integer,
    text_id,
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _revision(data, number):
    if not 0 <= number < len(data["revisions"]):
        raise WorkbenchError("revision_not_found", f"revision {number} does not exist")
    return data["revisions"][number]


def _proposal(data, proposal_id):
    proposal = data["proposals"].get(proposal_id)
    if proposal is None:
        raise WorkbenchError(
            "proposal_not_found", f"proposal does not exist: {proposal_id}"
        )
    return proposal


def _normalize_upserts(raw):
    if not isinstance(raw, list):
        raise WorkbenchError("invalid_input", "upserts must be an array")
    normalized = []
    seen = set()
    for index, row in enumerate(raw):
        record = normalize_transmission(row, f"upserts[{index}]")
        if record["id"] in seen:
            raise WorkbenchError(
                "invalid_input", f"duplicate upsert id: {record['id']}"
            )
        seen.add(record["id"])
        normalized.append(record)
    return normalized


def _normalize_remove_ids(raw):
    if not isinstance(raw, list):
        raise WorkbenchError("invalid_input", "remove_ids must be an array")
    result = []
    seen = set()
    for index, value in enumerate(raw):
        identifier = text_id(value, f"remove_ids[{index}]")
        if identifier in seen:
            raise WorkbenchError(
                "invalid_input", f"duplicate remove id: {identifier}"
            )
        seen.add(identifier)
        result.append(identifier)
    return result


def _proposal_content(base_revision, upserts, remove_ids):
    return {
        "base_revision": base_revision,
        "upserts": sorted(upserts, key=lambda row: row["id"]),
        "remove_ids": sorted(remove_ids),
    }


def _premises_for(plan, upserts, remove_ids):
    records = plan_records(plan)
    premises = {}
    for identifier in sorted({row["id"] for row in upserts} | set(remove_ids)):
        old = records.get(identifier)
        premises[identifier] = dict(old) if old is not None else None
    return premises


def _premise_failures(premises, current_plan):
    current = plan_records(current_plan)
    failures = []
    for identifier in sorted(premises):
        expected = premises[identifier]
        actual = current.get(identifier)
        if canonical_json(expected) != canonical_json(actual):
            failures.append(
                {
                    "transmission_id": identifier,
                    "expected": expected,
                    "current": actual,
                }
            )
    return failures


def _candidate_or_error(proposal, current_plan):
    """Apply the proposal to a plan, returning (plan, report) or raise codes."""
    try:
        candidate = apply_operations(
            current_plan, proposal["upserts"], proposal["remove_ids"]
        )
        report = compute_report(candidate)
    except WorkbenchError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise WorkbenchError("invalid_candidate", str(exc)) from exc
    return candidate, report


def _review_state(data, proposal):
    """Recompute one proposal against the currently committed plan.

    Returns a dict with status, candidate/report/reviews/failures, always based on
    the current revision only.
    """
    current = data["revisions"][-1]
    failures = _premise_failures(proposal["premises"], current["plan"])
    state = {
        "current_revision": current["revision"],
        "write_conflicts": failures,
        "candidate_report": None,
        "candidate_plan": None,
        "reviews": [],
        "status": None,
        "validation_error": None,
    }
    if failures:
        state["status"] = "write_conflict"
        return state
    try:
        candidate, candidate_report = _candidate_or_error(proposal, current["plan"])
    except WorkbenchError as exc:
        if exc.code != "invalid_candidate":
            raise
        state["status"] = "invalid_candidate"
        state["validation_error"] = exc.message
        return state
    state["candidate_plan"] = candidate
    state["candidate_report"] = candidate_report
    saved = data["approvals"].get(proposal["proposal_id"], {})
    approved_pairs = set()
    for receiver, identities in saved.items():
        for review_id in identities:
            approved_pairs.add((receiver, review_id))
    reviews = required_reviews(
        data["ledger_id"],
        proposal["proposal_id"],
        current["report"],
        candidate_report,
        approved_pairs,
    )
    state["reviews"] = reviews
    state["status"] = (
        "mergeable" if all(review["approved"] for review in reviews)
        else "approval_required"
    )
    return state


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def init_ledger(ledger_path, source):
    plan = normalize_source_plan(source)
    report = compute_report(plan)
    data = empty_ledger(plan["stations"])
    data["revisions"].append(
        {
            "revision": 0,
            "plan": plan,
            "report": report,
            "proposal_id": None,
        }
    )
    create_new(ledger_path, data)
    return {
        "command": "init",
        "ledger_id": data["ledger_id"],
        "revision": 0,
        "current_revision": 0,
        "summary": report["summary"],
    }


def propose(ledger_path, change):
    if not isinstance(change, dict):
        raise WorkbenchError("invalid_input", "change root must be an object")
    proposal_id = text_id(change.get("proposal_id"), "proposal_id")
    if "base_revision" not in change:
        raise WorkbenchError("invalid_input", "base_revision is required")
    base_revision = strict_integer(change["base_revision"], "base_revision", 0)
    if "upserts" not in change or "remove_ids" not in change:
        raise WorkbenchError(
            "invalid_input", "upserts and remove_ids are both required"
        )
    upserts = _normalize_upserts(change["upserts"])
    remove_ids = _normalize_remove_ids(change["remove_ids"])
    upsert_ids = {row["id"] for row in upserts}
    overlap = sorted(upsert_ids & set(remove_ids))
    if overlap:
        raise WorkbenchError(
            "invalid_input",
            "ids may not appear in both upserts and remove_ids: " + ", ".join(overlap),
        )
    if not upserts and not remove_ids:
        raise WorkbenchError(
            "invalid_input", "a proposal must change at least one transmission"
        )
    content = _proposal_content(base_revision, upserts, remove_ids)

    with LedgerFile(ledger_path) as holder:
        data = holder.data
        existing = data["proposals"].get(proposal_id)
        if existing is not None:
            if canonical_json(existing["content"]) == canonical_json(content):
                return {
                    "command": "propose",
                    "ledger_id": data["ledger_id"],
                    "proposal_id": proposal_id,
                    "base_revision": existing["base_revision"],
                    "created": False,
                    "upsert_ids": sorted(upsert_ids),
                    "remove_ids": sorted(remove_ids),
                }
            raise WorkbenchError(
                "proposal_conflict",
                f"proposal id is already used with different content: {proposal_id}",
            )
        base = _revision(data, base_revision)
        records = plan_records(base["plan"])
        for identifier in remove_ids:
            if identifier not in records:
                raise WorkbenchError(
                    "invalid_input",
                    f"remove id does not exist at base revision: {identifier}",
                )
        for row in upserts:
            old = records.get(row["id"])
            if old is not None and canonical_json(old) == canonical_json(row):
                raise WorkbenchError(
                    "invalid_input",
                    f"upsert is identical to the base record: {row['id']}",
                )
        try:
            candidate = apply_operations(base["plan"], upserts, remove_ids)
            compute_report(candidate)
        except WorkbenchError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise WorkbenchError("invalid_candidate", str(exc)) from exc

        proposal = {
            "proposal_id": proposal_id,
            "base_revision": base_revision,
            "upserts": upserts,
            "remove_ids": remove_ids,
            "content": content,
            "premises": _premises_for(base["plan"], upserts, remove_ids),
            "submitted": None,
        }
        data["proposals"][proposal_id] = proposal
        holder.commit(data)
        return {
            "command": "propose",
            "ledger_id": data["ledger_id"],
            "proposal_id": proposal_id,
            "base_revision": base_revision,
            "created": True,
            "upsert_ids": sorted(upsert_ids),
            "remove_ids": sorted(remove_ids),
        }


def preview(ledger_path, proposal_id):
    proposal_id = text_id(proposal_id, "proposal_id")
    with LedgerFile(ledger_path) as holder:
        data = holder.data
        proposal = _proposal(data, proposal_id)
        if proposal["submitted"] is not None:
            submitted = proposal["submitted"]
            frozen = submitted["result"]
            return {
                "command": "preview",
                "ledger_id": data["ledger_id"],
                "proposal_id": proposal_id,
                "base_revision": proposal["base_revision"],
                "current_revision": data["revisions"][-1]["revision"],
                "can_merge": True,
                "status": "already_applied",
                "applied_revision": submitted["revision"],
                "write_conflicts": [],
                "validation_error": None,
                "candidate_report": frozen["report"],
                "reviews": [],
            }
        state = _review_state(data, proposal)
        result = {
            "command": "preview",
            "ledger_id": data["ledger_id"],
            "proposal_id": proposal_id,
            "base_revision": proposal["base_revision"],
            "current_revision": state["current_revision"],
            "can_merge": state["status"] == "mergeable",
            "status": state["status"],
            "write_conflicts": state["write_conflicts"],
            "validation_error": state["validation_error"],
            "candidate_report": state["candidate_report"],
            "reviews": state["reviews"],
        }
        return result


def _apply_result(proposal):
    """The frozen first-success result; replay never re-checks or re-runs."""
    return dict(proposal["submitted"]["result"])


def approve(ledger_path, proposal_id, receiver_id, review_id):
    proposal_id = text_id(proposal_id, "proposal_id")
    receiver_id = text_id(receiver_id, "receiver_id")
    review_id = text_id(review_id, "review_id")
    with LedgerFile(ledger_path) as holder:
        data = holder.data
        proposal = _proposal(data, proposal_id)
        if proposal["submitted"] is not None:
            raise WorkbenchError(
                "proposal_already_applied",
                f"proposal has already been committed at revision "
                f"{proposal['submitted']['revision']}",
            )
        state = _review_state(data, proposal)
        if state["status"] == "write_conflict":
            raise WorkbenchError(
                "write_conflict",
                "proposal no longer matches the current plan",
                conflicts=state["write_conflicts"],
            )
        if state["status"] == "invalid_candidate":
            raise WorkbenchError(
                "invalid_candidate", state["validation_error"] or "invalid candidate"
            )
        review = next(
            (item for item in state["reviews"] if item["receiver_id"] == receiver_id),
            None,
        )
        if review is None:
            raise WorkbenchError(
                "approval_not_required",
                f"receiver {receiver_id} has no review item at the current revision",
            )
        if review["review_id"] != review_id:
            raise WorkbenchError(
                "review_mismatch",
                f"review id does not match the current evidence for {receiver_id}",
                expected=review["review_id"],
                received=review_id,
            )
        saved = data["approvals"].setdefault(proposal_id, {})
        identities = saved.setdefault(receiver_id, [])
        if review_id not in identities:
            identities.append(review_id)
            identities.sort()
            holder.commit(data)
        return {
            "command": "approve",
            "ledger_id": data["ledger_id"],
            "proposal_id": proposal_id,
            "receiver_id": receiver_id,
            "review_id": review_id,
            "revision": state["current_revision"],
            "approved": True,
            "evidence": review["evidence"],
        }


def apply_proposal(ledger_path, proposal_id):
    proposal_id = text_id(proposal_id, "proposal_id")
    with LedgerFile(ledger_path) as holder:
        data = holder.data
        proposal = _proposal(data, proposal_id)
        if proposal["submitted"] is not None:
            # First success is fixed; return it verbatim without re-checking,
            # even if unrelated revisions have advanced the current plan since.
            return _apply_result(proposal)
        state = _review_state(data, proposal)
        if state["status"] == "write_conflict":
            raise WorkbenchError(
                "write_conflict",
                "proposal no longer matches the current plan",
                conflicts=state["write_conflicts"],
            )
        if state["status"] == "invalid_candidate":
            raise WorkbenchError(
                "invalid_candidate", state["validation_error"] or "invalid candidate"
            )
        missing = [review for review in state["reviews"] if not review["approved"]]
        if missing:
            raise WorkbenchError(
                "approval_required",
                "current evidence still needs receiver approval",
                missing=[
                    {
                        "receiver_id": item["receiver_id"],
                        "review_id": item["review_id"],
                    }
                    for item in missing
                ],
            )
        revision = len(data["revisions"])
        data["revisions"].append(
            {
                "revision": revision,
                "plan": state["candidate_plan"],
                "report": state["candidate_report"],
                "proposal_id": proposal_id,
            }
        )
        result = {
            "command": "apply",
            "ledger_id": data["ledger_id"],
            "proposal_id": proposal_id,
            "base_revision": proposal["base_revision"],
            "revision": revision,
            "report": state["candidate_report"],
            "reviews": [
                {
                    "receiver_id": item["receiver_id"],
                    "review_id": item["review_id"],
                }
                for item in state["reviews"]
            ],
        }
        proposal["submitted"] = {"revision": revision, "result": result}
        holder.commit(data)
        return result


def show(ledger_path, revision=None):
    with LedgerFile(ledger_path) as holder:
        data = holder.data
        current = data["revisions"][-1]["revision"]
        if revision is None:
            number = current
        else:
            if type(revision) is not int:
                raise WorkbenchError("invalid_input", "revision must be an integer")
            number = revision
        entry = _revision(data, number)
        return {
            "command": "show",
            "ledger_id": data["ledger_id"],
            "revision": entry["revision"],
            "current_revision": current,
            "is_current": entry["revision"] == current,
            "source_proposal_id": entry["proposal_id"],
            "plan": entry["plan"],
            "report": entry["report"],
        }
