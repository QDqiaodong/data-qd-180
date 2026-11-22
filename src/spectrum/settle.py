"""Post-activity planned-loss settlement drafting (``radio-settle``).

After an activity, finance drafts channel-performance compensation from the
confirmed fixed plan.  The original directed-conflict rows only state that
some emission hindered some reception task: summing duration x bandwidth
over those rows counts a channel chunk hit by several interferers several
times, while one grand total can neither prove that a responsible operator
can cover a promised compensation nor keep that operator inside its credit
across overlapping promises.  This module therefore measures, for every
commitment, the exact loss region inside the promised time/frequency
rectangle under the declared protection-circle model, groups the loss by
the exact set of liable operators, and drafts a compensation allocation
that is optimal under three lexicographic levels:

1. maximize the total compensated units over the whole report;
2. then lexicographically maximize the per-commitment paid amounts taken
   in ``(priority, commitment id)`` ascending order;
3. then lexicographically maximize the per-commitment per-operator
   payments, commitments in the same order, operators in id order
   (zero payments included).

The settlement is over *planned* loss under the declared model.  It is not
a statement about field measurements or actual propagation, it does not
modify the plan and it does not execute any payment.

All arithmetic is exact integer arithmetic: time in UTC microseconds,
frequency in integer twice-Hz (0.5 Hz) units; one loss unit is
1 microsecond x 0.5 Hz.  Neither measurement nor allocation expands per
microsecond, per half-Hz or per compensation unit: measurement is a
coordinate-compression sweep over the clipped evidence rectangles, and the
allocation is a sequence of deterministic max-flow computations on a graph
whose size depends on the number of loss groups, never on the magnitudes
involved.

Within-group split rule: once the invoice (per commitment, per operator)
is fixed, any split of each operator's commitment payment across that
commitment's loss groups that respects every group's measured units is
equally valid.  This implementation takes, per commitment, the result of
one deterministic Dinic max-flow (groups in report order, operators in id
order, adjacency built in that fixed order); equivalent inputs yield the
identical split.

Public entry points:

* :func:`build_settlement_report` -- the testable pure report builder;
* :func:`solve_settlement` -- the allocation layer on abstract loss
  groups, exposed so tests can check it against independent brute force.
"""
import math
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

from .interference import distance_m
from .model import stamp

# ---------------------------------------------------------------------------
# contract constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
TOOL = "radio-settle"
MODEL = "declared_receiver_protection_circle"
MODE = "planned_loss_settlement"

MAX_STATIONS = 500
MAX_TRANSMISSIONS = 300
MAX_OPERATORS = 32
MAX_COMMITMENTS = 64
MAX_CREDIT_CAPACITY = 10 ** 24
MAX_PRIORITY = 999
MAX_TASK_DURATION = timedelta(days=7)

PLAN_ROOT_KEYS = frozenset(("stations", "transmissions"))
STATION_KEYS = frozenset(("id", "lat", "lon", "protection_radius_m"))
TRANSMISSION_KEYS = frozenset((
    "id", "station_id", "receiver_id", "center_hz", "bandwidth_hz",
    "start", "end",
))
TERMS_ROOT_KEYS = frozenset(("operators", "station_operators", "commitments"))
OPERATOR_KEYS = frozenset(("id", "credit_capacity"))
STATION_OPERATOR_KEYS = frozenset(("station_id", "operator_id"))
COMMITMENT_KEYS = frozenset((
    "id", "transmission_id", "start", "end",
    "low_twice_hz", "high_twice_hz", "priority",
))

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_US = timedelta(microseconds=1)
_FRACTION_RE = re.compile(r"[.,](\d+)")
_EMPTY_SET = frozenset()


# ---------------------------------------------------------------------------
# strict validation helpers
# ---------------------------------------------------------------------------
def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(obj, expected, label):
    keys = set(obj)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing:
        raise ValueError(f"{label} is missing field(s): {', '.join(missing)}")
    if extra:
        raise ValueError(f"{label} has unknown field(s): {', '.join(extra)}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _strict_int(value, label):
    # JSON booleans are ints in Python; the contract rejects them.
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _bounded_int(value, label, low, high):
    number = _strict_int(value, label)
    if not low <= number <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return number


def _coordinate(value, label, bound):
    if type(value) is bool or type(value) not in (int, float) \
            or not math.isfinite(value) or not -bound <= value <= bound:
        raise ValueError(f"invalid coordinate {label}")
    return value


def _plan_time(value, label):
    """Plan times follow the original contract (``spectrum.model.stamp``)."""
    try:
        return stamp(value)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc


def _time(value, label):
    """ISO-8601 with an explicit offset and at most six fractional digits."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 time with an offset")
    match = _FRACTION_RE.search(value)
    if match is not None and len(match.group(1)) > 6:
        raise ValueError(f"{label} allows at most six fractional digits")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{label} must be an ISO-8601 time with an offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} requires an explicit offset")
    return parsed.astimezone(timezone.utc)


def _microseconds(moment):
    return (moment - _EPOCH) // _ONE_US


def _iso_us(microseconds):
    return (_EPOCH + timedelta(microseconds=microseconds)).isoformat()


# ---------------------------------------------------------------------------
# plan document (original structure and limits, strictly re-validated)
# ---------------------------------------------------------------------------
def _parse_plan(data):
    _object(data, "plan root")
    _exact_keys(data, PLAN_ROOT_KEYS, "plan root")
    raw_stations = data["stations"]
    if not isinstance(raw_stations, list) \
            or not 1 <= len(raw_stations) <= MAX_STATIONS:
        raise ValueError(
            f"stations must be an array of 1..{MAX_STATIONS} objects")
    stations = {}
    for index, row in enumerate(raw_stations):
        label = f"stations[{index}]"
        _object(row, label)
        _exact_keys(row, STATION_KEYS, label)
        station_id = _text(row["id"], f"{label}.id")
        if station_id in stations:
            raise ValueError(f"duplicate station id: {station_id}")
        stations[station_id] = {
            "id": station_id,
            "lat": _coordinate(row["lat"], f"{label}.lat", 90),
            "lon": _coordinate(row["lon"], f"{label}.lon", 180),
            "protection_radius_m": _int(
                row["protection_radius_m"],
                f"{label}.protection_radius_m", 1),
        }
    raw_transmissions = data["transmissions"]
    if not isinstance(raw_transmissions, list) \
            or len(raw_transmissions) > MAX_TRANSMISSIONS:
        raise ValueError(
            f"transmissions must be an array of at most "
            f"{MAX_TRANSMISSIONS} objects")
    transmissions = {}
    for index, row in enumerate(raw_transmissions):
        label = f"transmissions[{index}]"
        _object(row, label)
        _exact_keys(row, TRANSMISSION_KEYS, label)
        transmission_id = _text(row["id"], f"{label}.id")
        if transmission_id in transmissions:
            raise ValueError(f"duplicate transmission id: {transmission_id}")
        station_id = _text(row["station_id"], f"{label}.station_id")
        receiver_id = _text(row["receiver_id"], f"{label}.receiver_id")
        if station_id not in stations:
            raise ValueError(
                f"{label}.station_id does not reference a station")
        if receiver_id not in stations:
            raise ValueError(
                f"{label}.receiver_id does not reference a station")
        if station_id == receiver_id:
            raise ValueError(
                f"{label} station_id and receiver_id must differ")
        center = _int(row["center_hz"], f"{label}.center_hz", 1)
        bandwidth = _int(row["bandwidth_hz"], f"{label}.bandwidth_hz", 1)
        if bandwidth > 2 * center:
            raise ValueError(f"{label} occupied band extends below zero")
        start = _plan_time(row["start"], f"{label}.start")
        end = _plan_time(row["end"], f"{label}.end")
        if not timedelta(0) < end - start <= MAX_TASK_DURATION:
            raise ValueError(
                f"{label} duration must be positive and <= 7 days")
        transmissions[transmission_id] = {
            "id": transmission_id,
            "station_id": station_id,
            "receiver_id": receiver_id,
            "center_hz": center,
            "bandwidth_hz": bandwidth,
            "start_dt": start,
            "end_dt": end,
            "start_us": _microseconds(start),
            "end_us": _microseconds(end),
            "low_twice_hz": 2 * center - bandwidth,
            "high_twice_hz": 2 * center + bandwidth,
        }
    return stations, transmissions


# ---------------------------------------------------------------------------
# terms document
# ---------------------------------------------------------------------------
def _parse_operators(raw):
    if not isinstance(raw, list) or len(raw) > MAX_OPERATORS:
        raise ValueError(
            f"operators must be an array of at most {MAX_OPERATORS} objects")
    operators = {}
    for index, row in enumerate(raw):
        label = f"operators[{index}]"
        _object(row, label)
        _exact_keys(row, OPERATOR_KEYS, label)
        operator_id = _text(row["id"], f"{label}.id")
        if operator_id in operators:
            raise ValueError(f"duplicate operator id: {operator_id}")
        capacity = _bounded_int(
            row["credit_capacity"], f"{label}.credit_capacity",
            0, MAX_CREDIT_CAPACITY)
        operators[operator_id] = {
            "id": operator_id,
            "credit_capacity": capacity,
        }
    return operators


def _parse_station_operators(raw, stations, operators):
    if not isinstance(raw, list):
        raise ValueError("station_operators must be an array")
    assignment = {}
    for index, row in enumerate(raw):
        label = f"station_operators[{index}]"
        _object(row, label)
        _exact_keys(row, STATION_OPERATOR_KEYS, label)
        station_id = _text(row["station_id"], f"{label}.station_id")
        if station_id not in stations:
            raise ValueError(
                f"{label}.station_id does not reference a plan station")
        if station_id in assignment:
            raise ValueError(
                f"station {station_id} is assigned more than one operator")
        operator_id = _text(row["operator_id"], f"{label}.operator_id")
        if operator_id not in operators:
            raise ValueError(
                f"{label}.operator_id does not reference an operator")
        assignment[station_id] = operator_id
    missing = sorted(set(stations) - set(assignment))
    if missing:
        raise ValueError(
            "every plan station needs exactly one operator; missing: "
            + ", ".join(missing))
    return assignment


def _parse_commitments(raw, transmissions):
    if not isinstance(raw, list) or len(raw) > MAX_COMMITMENTS:
        raise ValueError(
            f"commitments must be an array of at most "
            f"{MAX_COMMITMENTS} objects")
    commitments = {}
    promised = set()
    for index, row in enumerate(raw):
        label = f"commitments[{index}]"
        _object(row, label)
        _exact_keys(row, COMMITMENT_KEYS, label)
        commitment_id = _text(row["id"], f"{label}.id")
        if commitment_id in commitments:
            raise ValueError(f"duplicate commitment id: {commitment_id}")
        transmission_id = _text(
            row["transmission_id"], f"{label}.transmission_id")
        if transmission_id not in transmissions:
            raise ValueError(
                f"{label}.transmission_id does not reference a transmission")
        if transmission_id in promised:
            raise ValueError(
                f"transmission {transmission_id} already has a commitment")
        promised.add(transmission_id)
        task = transmissions[transmission_id]
        start = _time(row["start"], f"{label}.start")
        end = _time(row["end"], f"{label}.end")
        if not start < end:
            raise ValueError(
                f"{label} time interval must have positive length")
        if start < task["start_dt"] or end > task["end_dt"]:
            raise ValueError(
                f"{label} time interval must lie within its transmission")
        low = _strict_int(row["low_twice_hz"], f"{label}.low_twice_hz")
        high = _strict_int(row["high_twice_hz"], f"{label}.high_twice_hz")
        if not low < high:
            raise ValueError(f"{label} band must have positive width")
        if low < task["low_twice_hz"] or high > task["high_twice_hz"]:
            raise ValueError(
                f"{label} band must lie within the occupied band")
        priority = _bounded_int(
            row["priority"], f"{label}.priority", 0, MAX_PRIORITY)
        commitments[commitment_id] = {
            "id": commitment_id,
            "transmission_id": transmission_id,
            "priority": priority,
            "start_dt": start,
            "end_dt": end,
            "start_us": _microseconds(start),
            "end_us": _microseconds(end),
            "low_twice_hz": low,
            "high_twice_hz": high,
        }
    return commitments


def _parse_terms(data, stations, transmissions):
    _object(data, "terms root")
    _exact_keys(data, TERMS_ROOT_KEYS, "terms root")
    operators = _parse_operators(data["operators"])
    station_operator = _parse_station_operators(
        data["station_operators"], stations, operators)
    commitments = _parse_commitments(data["commitments"], transmissions)
    return operators, station_operator, commitments


# ---------------------------------------------------------------------------
# loss measurement
# ---------------------------------------------------------------------------
def _commitment_evidence(commitment, task, transmissions, stations,
                         station_operator):
    """Directed conflicts with ``task`` as the protected reception, clipped
    to the commitment rectangle in both dimensions (original protection
    circle rule: great-circle distance, radius inclusive)."""
    receiver = stations[task["receiver_id"]]
    rows = []
    for other_id in sorted(transmissions):
        if other_id == task["id"]:
            continue
        other = transmissions[other_id]
        start = max(task["start_us"], other["start_us"],
                    commitment["start_us"])
        end = min(task["end_us"], other["end_us"], commitment["end_us"])
        if start >= end:
            continue
        low = max(task["low_twice_hz"], other["low_twice_hz"],
                  commitment["low_twice_hz"])
        high = min(task["high_twice_hz"], other["high_twice_hz"],
                   commitment["high_twice_hz"])
        if low >= high:
            continue
        separation = distance_m(receiver, stations[other["station_id"]])
        if separation > receiver["protection_radius_m"]:
            continue
        rows.append({
            "interferer_transmission_id": other_id,
            "interferer_station_id": other["station_id"],
            "operator_id": station_operator[other["station_id"]],
            "start_us": start,
            "end_us": end,
            "low_twice_hz": low,
            "high_twice_hz": high,
            "units": (end - start) * (high - low),
        })
    return rows


def _loss_groups(evidence, beneficiary):
    """Group the union of clipped evidence rectangles by the exact payable
    operator set (covering operators minus the beneficiary).

    Coordinate-compression sweep: only rectangle boundaries are visited, so
    the cost depends on the number of rectangles, never on the microsecond
    or half-Hz magnitudes.  Regions covered by no interferer are not loss;
    regions covered only by the beneficiary itself map to the empty set.
    """
    rects = [
        (row["start_us"], row["end_us"], row["low_twice_hz"],
         row["high_twice_hz"], row["operator_id"])
        for row in evidence
    ]
    times = sorted({rect[0] for rect in rects}
                   | {rect[1] for rect in rects})
    groups = {}
    for first, second in zip(times, times[1:]):
        active = [rect for rect in rects
                  if rect[0] <= first and rect[1] >= second]
        if not active:
            continue
        starts = {}
        ends = {}
        boundaries = set()
        for rect in active:
            starts.setdefault(rect[2], []).append(rect[4])
            ends.setdefault(rect[3], []).append(rect[4])
            boundaries.add(rect[2])
            boundaries.add(rect[3])
        ordered = sorted(boundaries)
        counts = {}
        current = set()
        for position, point in enumerate(ordered[:-1]):
            for operator_id in ends.get(point, ()):
                counts[operator_id] -= 1
                if counts[operator_id] == 0:
                    current.discard(operator_id)
            for operator_id in starts.get(point, ()):
                if counts.get(operator_id, 0) == 0:
                    current.add(operator_id)
                counts[operator_id] = counts.get(operator_id, 0) + 1
            if not current:
                continue
            following = ordered[position + 1]
            key = frozenset(
                op for op in current if op != beneficiary)
            area = (second - first) * (following - point)
            groups[key] = groups.get(key, 0) + area
    return groups


# ---------------------------------------------------------------------------
# deterministic max-flow (Dinic) on a reusable residual graph
# ---------------------------------------------------------------------------
class _FlowNetwork:
    """Residual graph with exact integer capacities and deterministic
    Dinic max-flow.

    Logical edge ``i`` owns arcs ``2*i`` (forward residual) and ``2*i+1``
    (backward residual, which equals the current flow because every lower
    bound is zero).  ``fix`` locks an edge at its current flow by zeroing
    both residuals.  Adjacency lists keep insertion order, so identical
    graphs always produce identical flows.
    """

    def __init__(self):
        self._to = []
        self._cap = []
        self._adj = {}
        self._size = 0

    def add_edge(self, frm, to, capacity):
        self._adj.setdefault(frm, []).append(2 * self._size)
        self._adj.setdefault(to, []).append(2 * self._size + 1)
        self._to.append(to)
        self._cap.append(capacity)
        self._to.append(frm)
        self._cap.append(0)
        self._size += 1
        return self._size - 1

    def flow(self, edge):
        return self._cap[2 * edge + 1]

    def set_flow(self, edge, value):
        delta = value - self._cap[2 * edge + 1]
        self._cap[2 * edge + 1] += delta
        self._cap[2 * edge] -= delta

    def fix(self, edge):
        self._cap[2 * edge] = 0
        self._cap[2 * edge + 1] = 0

    def max_flow(self, source, sink, exclude=None):
        """Augment a maximum source->sink flow through the current residual
        graph (optionally ignoring one logical edge); return the amount."""
        skip = ()
        if exclude is not None:
            skip = (2 * exclude, 2 * exclude + 1)
        needed = 4 * (len(self._adj) + 16)
        if needed > sys.getrecursionlimit():
            sys.setrecursionlimit(needed)
        total = 0
        while True:
            level = {source: 0}
            queue = deque((source,))
            while queue:
                node = queue.popleft()
                for arc in self._adj.get(node, ()):
                    if arc in skip:
                        continue
                    target = self._to[arc]
                    if self._cap[arc] > 0 and target not in level:
                        level[target] = level[node] + 1
                        queue.append(target)
            if sink not in level:
                return total
            progress = {node: 0 for node in level}
            bound = sum(self._cap[arc] for arc in self._adj.get(source, ()))
            while True:
                pushed = self._push(source, sink, level, progress, skip,
                                    bound)
                if pushed <= 0:
                    break
                total += pushed

    def _push(self, node, sink, level, progress, skip, offered):
        if node == sink:
            return offered
        arcs = self._adj.get(node, ())
        index = progress[node]
        while index < len(arcs):
            arc = arcs[index]
            if arc not in skip:
                target = self._to[arc]
                if self._cap[arc] > 0 \
                        and level.get(target) == level[node] + 1:
                    pushed = self._push(
                        target, sink, level, progress, skip,
                        offered if offered < self._cap[arc]
                        else self._cap[arc])
                    if pushed > 0:
                        self._cap[arc] -= pushed
                        self._cap[arc ^ 1] += pushed
                        progress[node] = index
                        return pushed
            index += 1
        progress[node] = index
        return 0


# ---------------------------------------------------------------------------
# compensation allocation
# ---------------------------------------------------------------------------
def solve_settlement(commitments, credit_capacities):
    """Three-level lexicographic compensation allocation.

    ``commitments`` is a list of ``{"id", "priority", "groups"}`` where
    ``groups`` is a list of ``(frozenset(operator_id), units)`` pairs with
    positive integer units; the empty operator set cannot be compensated
    but is accepted so group indexes stay aligned with the caller.
    ``credit_capacities`` maps every registered operator id to its credit.

    Returns ``total_units``, ``commitment_paid`` (per commitment id),
    ``operator_paid_by_commitment`` (full matrix, zeros included) and
    ``group_payments`` (per commitment, per group, aligned with the input
    group order).
    """
    order = sorted(commitments, key=lambda row: (row["priority"], row["id"]))
    operator_ids = sorted(credit_capacities)
    paid = {row["id"]: 0 for row in commitments}
    invoice = {
        row["id"]: {operator_id: 0 for operator_id in operator_ids}
        for row in commitments
    }
    splits = {
        row["id"]: [dict() for _ in row["groups"]]
        for row in commitments
    }
    supply = sum(
        units for row in commitments
        for operators, units in row["groups"] if operators)
    if supply > 0:
        _run_levels(order, operator_ids, credit_capacities, supply,
                    paid, invoice)
        for row in order:
            targets = {
                operator_id: amount
                for operator_id, amount in invoice[row["id"]].items()
                if amount > 0
            }
            if targets:
                splits[row["id"]] = _split_commitment(row["groups"], targets)
    return {
        "total_units": sum(paid.values()),
        "commitment_paid": paid,
        "operator_paid_by_commitment": invoice,
        "group_payments": splits,
    }


def _run_levels(order, operator_ids, credit_capacities, supply,
                paid, invoice):
    """Run the three lexicographic levels on one residual network.

    The network is a circulation: source -> commitment meter -> loss group
    -> (commitment, operator) pair meter -> operator -> sink -> source.
    The commitment meter sits on the source side so operator identity is
    preserved all the way to the operator capacity edges.  Level 1
    maximizes the circulation (the total); level 2 then maximizes each
    commitment meter in ``(priority, id)`` order; level 3 maximizes each
    (commitment, operator) meter in commitment order then operator id
    order.  Every settled meter edge is locked at its value before the
    next one is optimized, which is exactly lexicographic maximization
    over the feasible integer flows.
    """
    net = _FlowNetwork()
    source = ("source",)
    sink = ("sink",)
    total_edge = net.add_edge(sink, source, supply)
    p_edge = {}
    y_edge = {}
    pair_node = {}
    for row in order:
        commitment_id = row["id"]
        compensable = [
            (index, operators, units)
            for index, (operators, units) in enumerate(row["groups"])
            if operators
        ]
        if not compensable:
            continue
        meter = ("commitment", commitment_id)
        p_edge[commitment_id] = net.add_edge(source, meter, supply)
        involved = sorted({op for _, operators, _ in compensable
                           for op in operators})
        for operator_id in involved:
            node = ("pair", commitment_id, operator_id)
            pair_node[(commitment_id, operator_id)] = node
            y_edge[(commitment_id, operator_id)] = net.add_edge(
                node, ("operator", operator_id), supply)
        for index, operators, units in compensable:
            group_node = ("group", commitment_id, index)
            net.add_edge(meter, group_node, units)
            for operator_id in sorted(operators):
                net.add_edge(
                    group_node,
                    pair_node[(commitment_id, operator_id)], supply)
    for operator_id in operator_ids:
        net.add_edge(("operator", operator_id), sink,
                     credit_capacities[operator_id])

    total = net.max_flow(source, sink, exclude=total_edge)
    net.set_flow(total_edge, total)
    net.fix(total_edge)
    if total > 0:
        for row in order:
            commitment_id = row["id"]
            edge = p_edge.get(commitment_id)
            if edge is None:
                continue
            delta = net.max_flow(("commitment", commitment_id), source,
                                 exclude=edge)
            value = net.flow(edge) + delta
            net.set_flow(edge, value)
            net.fix(edge)
            paid[commitment_id] = value
        for row in order:
            commitment_id = row["id"]
            for operator_id in operator_ids:
                edge = y_edge.get((commitment_id, operator_id))
                if edge is None:
                    continue
                delta = net.max_flow(
                    ("operator", operator_id),
                    pair_node[(commitment_id, operator_id)],
                    exclude=edge)
                value = net.flow(edge) + delta
                net.set_flow(edge, value)
                net.fix(edge)
                invoice[commitment_id][operator_id] = value
    if sum(paid.values()) != total:
        raise AssertionError("commitment totals do not add up to the total")
    for row in order:
        commitment_id = row["id"]
        if sum(invoice[commitment_id].values()) != paid[commitment_id]:
            raise AssertionError("invoice does not match commitment total")
    for operator_id in operator_ids:
        owed = sum(invoice[row["id"]][operator_id] for row in order)
        if owed > credit_capacities[operator_id]:
            raise AssertionError("operator credit capacity exceeded")


def _split_commitment(groups, targets):
    """Deterministic within-commitment split of the fixed invoice.

    Per commitment the invoice is split across that commitment's loss
    groups by one deterministic Dinic max-flow (groups in caller order,
    operators in id order).  The global solve guarantees feasibility.
    """
    net = _FlowNetwork()
    source = ("source",)
    sink = ("sink",)
    edges = []
    for index, (operators, units) in enumerate(groups):
        node = ("group", index)
        net.add_edge(source, node, units)
        row = {}
        for operator_id in sorted(operators):
            if targets.get(operator_id):
                row[operator_id] = net.add_edge(
                    node, ("operator", operator_id), units)
        edges.append(row)
    for operator_id in sorted(targets):
        net.add_edge(("operator", operator_id), sink, targets[operator_id])
    pushed = net.max_flow(source, sink)
    if pushed != sum(targets.values()):
        raise AssertionError("within-commitment split is infeasible")
    return [
        {operator_id: net.flow(edge)
         for operator_id, edge in row.items() if net.flow(edge) > 0}
        for row in edges
    ]


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------
def _evidence_row(row):
    return {
        "interferer_transmission_id": row["interferer_transmission_id"],
        "interferer_station_id": row["interferer_station_id"],
        "operator_id": row["operator_id"],
        "start": _iso_us(row["start_us"]),
        "end": _iso_us(row["end_us"]),
        "low_twice_hz": row["low_twice_hz"],
        "high_twice_hz": row["high_twice_hz"],
        "units": row["units"],
    }


def build_settlement_report(plan, terms):
    """Validate both documents and draft the settlement report.

    ``plan`` follows the original stations/transmissions structure and
    limits (strictly re-validated, including records that take no part in
    the settlement); ``terms`` follows the strict terms contract.  Raises
    ``ValueError`` on any contract violation.  Zero loss, zero credit, no
    liable operator and no commitments are all valid results.
    """
    stations, transmissions = _parse_plan(plan)
    operators, station_operator, commitments = _parse_terms(
        terms, stations, transmissions)

    ordered = sorted(
        commitments.values(), key=lambda row: (row["priority"], row["id"]))
    measured = []
    solver_commitments = []
    for commitment in ordered:
        task = transmissions[commitment["transmission_id"]]
        beneficiary = station_operator[task["receiver_id"]]
        evidence = _commitment_evidence(
            commitment, task, transmissions, stations, station_operator)
        groups = _loss_groups(evidence, beneficiary)
        keys = sorted(groups, key=lambda key: sorted(key))
        measured.append({
            "commitment": commitment,
            "beneficiary": beneficiary,
            "evidence": evidence,
            "groups": groups,
            "keys": keys,
        })
        solver_commitments.append({
            "id": commitment["id"],
            "priority": commitment["priority"],
            "groups": [(key, groups[key]) for key in keys],
        })

    capacities = {
        operator_id: row["credit_capacity"]
        for operator_id, row in operators.items()
    }
    allocation = solve_settlement(solver_commitments, capacities)

    commitment_rows = []
    receivable = {operator_id: 0 for operator_id in operators}
    total_commitment_units = 0
    total_loss = 0
    total_own = 0
    total_compensable = 0
    total_paid = 0
    for entry in measured:
        commitment = entry["commitment"]
        commitment_id = commitment["id"]
        groups = entry["groups"]
        loss_units = sum(groups.values())
        own_units = groups.get(_EMPTY_SET, 0)
        compensable_units = loss_units - own_units
        paid_units = allocation["commitment_paid"][commitment_id]
        group_rows = []
        for index, key in enumerate(entry["keys"]):
            payments = allocation["group_payments"][commitment_id][index]
            allocated = sum(payments.values())
            if allocated > groups[key]:
                raise AssertionError("group allocation exceeds measurement")
            group_rows.append({
                "operator_ids": sorted(key),
                "units": groups[key],
                "allocated_units": allocated,
                "payments": [
                    {"operator_id": operator_id, "units": amount}
                    for operator_id, amount in sorted(payments.items())
                ],
            })
        commitment_units = (
            (commitment["end_us"] - commitment["start_us"])
            * (commitment["high_twice_hz"] - commitment["low_twice_hz"]))
        evidence_rows = [
            _evidence_row(row) for row in sorted(
                entry["evidence"],
                key=lambda row: (row["interferer_transmission_id"],
                                 row["start_us"], row["low_twice_hz"]))
        ]
        commitment_rows.append({
            "commitment_id": commitment_id,
            "transmission_id": commitment["transmission_id"],
            "priority": commitment["priority"],
            "beneficiary_operator_id": entry["beneficiary"],
            "region": {
                "start": _iso_us(commitment["start_us"]),
                "end": _iso_us(commitment["end_us"]),
                "low_twice_hz": commitment["low_twice_hz"],
                "high_twice_hz": commitment["high_twice_hz"],
            },
            "commitment_units": commitment_units,
            "loss_units": loss_units,
            "own_only_loss_units": own_units,
            "compensable_loss_units": compensable_units,
            "paid_units": paid_units,
            "unpaid_units": compensable_units - paid_units,
            "evidence": evidence_rows,
            "loss_groups": group_rows,
        })
        receivable[entry["beneficiary"]] += paid_units
        total_commitment_units += commitment_units
        total_loss += loss_units
        total_own += own_units
        total_compensable += compensable_units
        total_paid += paid_units

    operator_rows = []
    for operator_id in sorted(operators):
        payments = []
        for entry in measured:
            commitment_id = entry["commitment"]["id"]
            amount = allocation["operator_paid_by_commitment"][
                commitment_id][operator_id]
            if amount > 0:
                payments.append({
                    "commitment_id": commitment_id,
                    "units": amount,
                })
        paid_total = sum(row["units"] for row in payments)
        capacity = operators[operator_id]["credit_capacity"]
        if paid_total > capacity:
            raise AssertionError("operator credit capacity exceeded")
        operator_rows.append({
            "operator_id": operator_id,
            "credit_capacity": capacity,
            "payments": payments,
            "paid_units": paid_total,
            "remaining_credit": capacity - paid_total,
            "receivable_units": receivable[operator_id],
        })

    sum_commitment_paid = sum(row["paid_units"] for row in commitment_rows)
    sum_operator_paid = sum(row["paid_units"] for row in operator_rows)
    if not total_paid == sum_commitment_paid == sum_operator_paid:
        raise AssertionError("settlement totals do not reconcile")
    if total_paid != allocation["total_units"]:
        raise AssertionError("settlement total does not match the solver")

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "mode": MODE,
        "commitments": commitment_rows,
        "operators": operator_rows,
        "totals": {
            "commitment_units": total_commitment_units,
            "loss_units": total_loss,
            "own_only_loss_units": total_own,
            "compensable_loss_units": total_compensable,
            "paid_units": total_paid,
            "unpaid_units": total_compensable - total_paid,
            "sum_commitment_paid_units": sum_commitment_paid,
            "sum_operator_paid_units": sum_operator_paid,
        },
        "summary": {
            "stations": len(stations),
            "transmissions": len(transmissions),
            "operators": len(operators),
            "commitments": len(commitments),
            "commitments_with_loss": sum(
                1 for row in commitment_rows if row["loss_units"] > 0),
            "loss_units": total_loss,
            "compensable_loss_units": total_compensable,
            "paid_units": total_paid,
            "unpaid_units": total_compensable - total_paid,
        },
    }
