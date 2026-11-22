"""Privacy-reviewed reception statistics bulletins (``radio-bulletin``).

The activity organizer publishes, edition by edition, integer-microsecond
statistics of a *frozen* transmission plan aggregated onto a venue (row) by
channel-group (column) matrix.  Some cells carry confidential reception
demand: even when the detailed plan itself is never released, combining a
new aggregate with older bulletins can let the public reconstruct a
protected cell.  This module decides which requested statistics may be
published without shrinking any protected cell's public uncertainty below
a required gap.

Public-knowledge model (the only model used by the review).  Given the
standing integer interval bound of every caliber (a single cell, a whole
row, a whole column, or the whole-table total) and every exact value already
published, the public can infer precisely the set of non-negative integer
matrices consistent with all of them.  A protected cell with required gap
``gap_us`` stays hidden only when that feasible set contains two matrices
whose value in the cell differs by at least ``gap_us``; the two witnesses
may differ between protected cells.  Nothing from the private plan (task
presence, non-zero positions, original records) is added to this model.

Admission is itself public.  A request is granted only if, for *every*
integer value the requested caliber could still take under the current
public facts, publishing that hypothetical value would leave every
protected cell its full gap.  If even one possible value is unsafe the
whole caliber is withheld; the value of the real plan makes no exception.
A refusal publishes no new fact; an acceptance appends the real value and
later admissions see the enlarged fact set.

Exact, magnitude-independent decision.  The matrix constraints are
non-negative integers with row/column/total sums and interval bounds: the
constraint matrix is the transportation (bipartite b-matching) incidence
matrix, which is totally unimodular, so every linear-programming vertex is
integral and integer feasibility is decided exactly by a lower-bound
circulation (a deterministic Dinic max-flow on a graph with one edge per
cell, row, column and the total).  The feasible values of any caliber form
one integer interval; its two endpoints are found with a binary search over
forced-bound feasibility whose number of flow runs is O(log U) for the
bound U, never O(U).  Because the feasible-value interval is contiguous,
the "every possible value is safe" admission test reduces to the two
endpoints: there is no enumeration of microseconds, possible aggregate
values or feasible matrices.

Public entry points:

* :func:`build_bulletin` -- the testable pure builder taking the parsed
  PLAN and RULES documents;
* :class:`Circulation` -- the lower-bound flow engine, exposed so tests can
  cross-check it against an independent brute-force oracle.
"""
import math
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

from .io import text
from .model import stamp

# ---------------------------------------------------------------------------
# contract constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
TOOL = "radio-bulletin"
MODEL = "frozen_plan_reception_statistics"
MODE = "privacy_reviewed_bulletin"

MAX_VENUES = 8
MAX_GROUPS = 8
MAX_PROTECTED = 12
MAX_REQUESTS = 32
MAX_WINDOW_US = 7 * 24 * 60 * 60 * 1_000_000
# Default public upper bound for every caliber, including the whole table.
DEFAULT_BOUND = 181_440_000_000_000
# 300 transmissions x the longest possible positive window intersection.
PLAN_CAP = 300 * MAX_WINDOW_US

PLAN_ROOT_KEYS = frozenset(("stations", "transmissions"))
STATION_KEYS = frozenset(("id", "lat", "lon", "protection_radius_m"))
TRANSMISSION_KEYS = frozenset((
    "id", "station_id", "receiver_id", "center_hz", "bandwidth_hz",
    "start", "end",
))
RULES_ROOT_KEYS = frozenset((
    "edition_id", "window", "venues", "channel_groups", "receiver_venues",
    "bounds", "protected", "prior", "requests",
))
WINDOW_KEYS = frozenset(("start", "end"))
GROUP_KEYS = frozenset(("id", "low_hz", "high_hz"))
MAPPING_KEYS = frozenset(("receiver_id", "venue_id"))
BOUND_KEYS = frozenset(("caliber", "low", "high"))
PRIOR_KEYS = frozenset(("edition_id", "revision", "publications"))
PROTECTED_KEYS = frozenset(("cell", "gap_us"))
REQUEST_KEYS = frozenset(("caliber",))
CELL_KEYS = frozenset(("venue_id", "group_id"))
SCOPES = frozenset(("cell", "row", "column", "total"))
CALIBER_SCOPE_KEYS = {
    "cell": CELL_KEYS,
    "row": frozenset(("venue_id",)),
    "column": frozenset(("group_id",)),
    "total": frozenset(),
}
STATUSES = frozenset(("published", "granted", "withheld"))

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_US = timedelta(microseconds=1)
_FRACTION_RE = re.compile(r"[.,](\d+)")


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


def _strict_int(value, label):
    # JSON booleans are ints in Python; the contract rejects them.
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _int(value, label, minimum=0):
    number = _strict_int(value, label)
    if number < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return number


def _bounded(value, label, low, high):
    number = _strict_int(value, label)
    if not low <= number <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return number


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


def _plan_time(value, label):
    """Plan times follow the original contract (``spectrum.model.stamp``)."""
    try:
        return stamp(value)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc


# ---------------------------------------------------------------------------
# PLAN document: original structure and limits, strictly re-validated
# ---------------------------------------------------------------------------
def _parse_plan(data):
    _object(data, "plan root")
    _exact_keys(data, PLAN_ROOT_KEYS, "plan root")
    raw_stations = data["stations"]
    if not isinstance(raw_stations, list) or not 1 <= len(raw_stations) <= 500:
        raise ValueError("stations must be an array of 1..500 objects")
    stations = {}
    for index, row in enumerate(raw_stations):
        label = f"stations[{index}]"
        _object(row, label)
        _exact_keys(row, STATION_KEYS, label)
        station_id = text(row["id"], f"{label}.id")
        if station_id in stations:
            raise ValueError(f"duplicate station id: {station_id}")
        for key, bound in (("lat", 90), ("lon", 180)):
            value = row[key]
            if type(value) is bool or type(value) not in (int, float) \
                    or not math.isfinite(value) \
                    or not -bound <= value <= bound:
                raise ValueError(f"invalid coordinate {key}")
        _bounded(row["protection_radius_m"],
                 f"{label}.protection_radius_m", 1, 10 ** 12)
        stations[station_id] = row
    raw_transmissions = data["transmissions"]
    if not isinstance(raw_transmissions, list) or len(raw_transmissions) > 300:
        raise ValueError("transmissions must be an array of at most 300 objects")
    transmissions = {}
    for index, row in enumerate(raw_transmissions):
        label = f"transmissions[{index}]"
        _object(row, label)
        _exact_keys(row, TRANSMISSION_KEYS, label)
        transmission_id = text(row["id"], f"{label}.id")
        if transmission_id in transmissions:
            raise ValueError(f"duplicate transmission id: {transmission_id}")
        station_id = text(row["station_id"], f"{label}.station_id")
        receiver_id = text(row["receiver_id"], f"{label}.receiver_id")
        if station_id not in stations:
            raise ValueError(f"{label}.station_id does not reference a station")
        if receiver_id not in stations:
            raise ValueError(f"{label}.receiver_id does not reference a station")
        if station_id == receiver_id:
            raise ValueError(f"{label} station_id and receiver_id must differ")
        center = _int(row["center_hz"], f"{label}.center_hz", 1)
        bandwidth = _int(row["bandwidth_hz"], f"{label}.bandwidth_hz", 1)
        if bandwidth > 2 * center:
            raise ValueError(f"{label} occupied band extends below zero")
        start = _plan_time(row["start"], f"{label}.start")
        end = _plan_time(row["end"], f"{label}.end")
        if not timedelta(0) < end - start <= timedelta(days=7):
            raise ValueError(f"{label} duration must be positive and <= 7 days")
        transmissions[transmission_id] = {
            "id": transmission_id,
            "receiver_id": receiver_id,
            "center_hz": center,
            "start_us": _microseconds(start),
            "end_us": _microseconds(end),
        }
    return stations, transmissions


# ---------------------------------------------------------------------------
# RULES document
# ---------------------------------------------------------------------------
def _caliber(obj, label, venue_ids, group_ids):
    """Parse and cross-check a caliber reference; return its canonical key."""
    _object(obj, label)
    scope = obj.get("scope")
    if scope not in SCOPES:
        raise ValueError(f"{label}.scope must be one of cell, row, column, total")
    expected = CALIBER_SCOPE_KEYS[scope]
    _exact_keys(obj, frozenset(("scope",)) | expected, label)
    if scope == "cell":
        venue_id = text(obj["venue_id"], f"{label}.venue_id")
        group_id = text(obj["group_id"], f"{label}.group_id")
        if venue_id not in venue_ids:
            raise ValueError(f"{label}.venue_id does not reference a venue")
        if group_id not in group_ids:
            raise ValueError(f"{label}.group_id does not reference a channel group")
        return ("cell", venue_id, group_id)
    if scope == "row":
        venue_id = text(obj["venue_id"], f"{label}.venue_id")
        if venue_id not in venue_ids:
            raise ValueError(f"{label}.venue_id does not reference a venue")
        return ("row", venue_id)
    if scope == "column":
        group_id = text(obj["group_id"], f"{label}.group_id")
        if group_id not in group_ids:
            raise ValueError(f"{label}.group_id does not reference a channel group")
        return ("column", group_id)
    return ("total",)


def _parse_rules(data, stations, transmissions):
    _object(data, "rules root")
    _exact_keys(data, RULES_ROOT_KEYS, "rules root")
    edition_id = text(data["edition_id"], "edition_id")

    # --- statistical window: half-open, positive length, at most seven days
    window = _object(data["window"], "window")
    _exact_keys(window, WINDOW_KEYS, "window")
    window_start = _time(window["start"], "window.start")
    window_end = _time(window["end"], "window.end")
    if not window_start < window_end:
        raise ValueError("window must have positive length")
    window_start_us = _microseconds(window_start)
    window_end_us = _microseconds(window_end)
    if not 0 < window_end_us - window_start_us <= MAX_WINDOW_US:
        raise ValueError("window length must be positive and <= 7 days")

    # --- venue rows (1..8 distinct ids)
    raw_venues = data["venues"]
    if not isinstance(raw_venues, list) or not 1 <= len(raw_venues) <= MAX_VENUES:
        raise ValueError(f"venues must be an array of 1..{MAX_VENUES} ids")
    venue_ids = []
    venue_set = set()
    for index, value in enumerate(raw_venues):
        venue_id = text(value, f"venues[{index}]")
        if venue_id in venue_set:
            raise ValueError(f"duplicate venue id: {venue_id}")
        venue_set.add(venue_id)
        venue_ids.append(venue_id)

    # --- channel-group columns (1..8 distinct ids, non-overlapping half-open
    # positive-integer-Hz intervals)
    raw_groups = data["channel_groups"]
    if not isinstance(raw_groups, list) or not 1 <= len(raw_groups) <= MAX_GROUPS:
        raise ValueError(f"channel_groups must be an array of 1..{MAX_GROUPS} objects")
    groups = {}
    for index, row in enumerate(raw_groups):
        label = f"channel_groups[{index}]"
        _object(row, label)
        _exact_keys(row, GROUP_KEYS, label)
        group_id = text(row["id"], f"{label}.id")
        if group_id in groups:
            raise ValueError(f"duplicate channel group id: {group_id}")
        low = _int(row["low_hz"], f"{label}.low_hz", 1)
        high = _int(row["high_hz"], f"{label}.high_hz", 1)
        if not low < high:
            raise ValueError(f"{label} half-open interval must have positive width")
        groups[group_id] = (low, high)
    ordered_groups = sorted(groups, key=lambda group_id: groups[group_id][0])
    for earlier, later in zip(ordered_groups, ordered_groups[1:]):
        if groups[earlier][1] > groups[later][0]:
            raise ValueError(
                "channel group intervals must not overlap: "
                f"{earlier} and {later}")

    # --- receiver station -> venue mapping
    raw_mapping = data["receiver_venues"]
    if not isinstance(raw_mapping, list):
        raise ValueError("receiver_venues must be an array")
    receiver_venue = {}
    for index, row in enumerate(raw_mapping):
        label = f"receiver_venues[{index}]"
        _object(row, label)
        _exact_keys(row, MAPPING_KEYS, label)
        receiver_id = text(row["receiver_id"], f"{label}.receiver_id")
        if receiver_id not in stations:
            raise ValueError(f"{label}.receiver_id does not reference a plan station")
        if receiver_id in receiver_venue:
            raise ValueError(f"receiver {receiver_id} is mapped more than once")
        venue_id = text(row["venue_id"], f"{label}.venue_id")
        if venue_id not in venue_set:
            raise ValueError(f"{label}.venue_id does not reference a venue")
        receiver_venue[receiver_id] = venue_id

    # --- per-caliber public integer closed intervals (at most one each)
    raw_bounds = data["bounds"]
    if not isinstance(raw_bounds, list):
        raise ValueError("bounds must be an array")
    bounds = {}
    for index, row in enumerate(raw_bounds):
        label = f"bounds[{index}]"
        _object(row, label)
        _exact_keys(row, BOUND_KEYS, label)
        key = _caliber(row["caliber"], f"{label}.caliber", venue_set, groups)
        if key in bounds:
            raise ValueError(f"duplicate public bound for one caliber: {key}")
        low = _int(row["low"], f"{label}.low", 0)
        high = _int(row["high"], f"{label}.high", 0)
        if low > high:
            raise ValueError(f"{label} requires low <= high")
        if high > DEFAULT_BOUND:
            raise ValueError(
                f"{label}.high must be <= {DEFAULT_BOUND}")
        bounds[key] = (low, high)

    # --- protected cells (1..12 distinct, positive integer gap)
    raw_protected = data["protected"]
    if not isinstance(raw_protected, list) \
            or not 1 <= len(raw_protected) <= MAX_PROTECTED:
        raise ValueError(
            f"protected must be an array of 1..{MAX_PROTECTED} objects")
    protected = {}
    for index, row in enumerate(raw_protected):
        label = f"protected[{index}]"
        _object(row, label)
        _exact_keys(row, PROTECTED_KEYS, label)
        _object(row["cell"], f"{label}.cell")
        _exact_keys(row["cell"], CELL_KEYS, f"{label}.cell")
        key = _caliber(
            {"scope": "cell", **row["cell"]}, f"{label}.cell",
            venue_set, groups)
        if key in protected:
            raise ValueError(f"duplicate protected cell: {key[1]}/{key[2]}")
        gap = _int(row["gap_us"], f"{label}.gap_us", 1)
        protected[key] = gap

    # --- prior bulletin of the same frozen edition
    prior = _object(data["prior"], "prior")
    _exact_keys(prior, PRIOR_KEYS, "prior")
    prior_edition = text(prior["edition_id"], "prior.edition_id")
    if prior_edition != edition_id:
        raise ValueError(
            f"prior.edition_id {prior_edition!r} does not match edition_id "
            f"{edition_id!r}; records are never merged across editions")
    prior_revision = _int(prior["revision"], "prior.revision", 0)
    raw_pubs = prior["publications"]
    if not isinstance(raw_pubs, list):
        raise ValueError("prior.publications must be an array")
    prior_publications = []
    seen_pub = set()
    for index, row in enumerate(raw_pubs):
        label = f"prior.publications[{index}]"
        _object(row, label)
        expected = frozenset(("caliber", "value"))
        _exact_keys(row, expected, label)
        key = _caliber(row["caliber"], f"{label}.caliber", venue_set, groups)
        if key in seen_pub:
            raise ValueError(f"duplicate prior publication for caliber: {key}")
        seen_pub.add(key)
        value = _int(row["value"], f"{label}.value", 0)
        if value > DEFAULT_BOUND:
            raise ValueError(f"{label}.value must be <= {DEFAULT_BOUND}")
        prior_publications.append((key, value))

    # --- requests (1..32 distinct calibers, explicit processing order)
    raw_requests = data["requests"]
    if not isinstance(raw_requests, list) \
            or not 1 <= len(raw_requests) <= MAX_REQUESTS:
        raise ValueError(f"requests must be an array of 1..{MAX_REQUESTS} objects")
    requests = []
    seen_request = set()
    for index, row in enumerate(raw_requests):
        label = f"requests[{index}]"
        _object(row, label)
        _exact_keys(row, REQUEST_KEYS, label)
        key = _caliber(row["caliber"], f"{label}.caliber", venue_set, groups)
        if key in seen_request:
            raise ValueError(f"duplicate request caliber: {key}")
        seen_request.add(key)
        requests.append(key)

    return {
        "edition_id": edition_id,
        "window_start_us": window_start_us,
        "window_end_us": window_end_us,
        "venue_ids": venue_ids,
        "groups": groups,
        "ordered_groups": ordered_groups,
        "receiver_venue": receiver_venue,
        "bounds": bounds,
        "protected": protected,
        "prior_revision": prior_revision,
        "prior_publications": prior_publications,
        "requests": requests,
        "stations": stations,
        "transmissions": transmissions,
    }


# ---------------------------------------------------------------------------
# measurement: aggregate the frozen plan onto the venue x group matrix
# ---------------------------------------------------------------------------
def measure_matrix(rules):
    """Return the real non-negative integer matrix and the true caliber map.

    Every transmission whose half-open time span has positive-length
    intersection with the window contributes, once, the integer microsecond
    length of that intersection to the unique cell selected by its receiving
    station's venue and the channel group that contains its center frequency.
    Overlapping tasks are counted separately (no de-duplication), bandwidth is
    never a multiplier and the transmitting station's ownership is ignored.
    A missing receiver mapping or a center frequency outside every group
    rejects the whole input.
    """
    venue_index = {venue_id: i for i, venue_id in enumerate(rules["venue_ids"])}
    group_index = {
        group_id: j for j, group_id in enumerate(rules["ordered_groups"])}
    rows = len(venue_index)
    cols = len(group_index)
    matrix = [[0 for _ in range(cols)] for _ in range(rows)]
    window_start = rules["window_start_us"]
    window_end = rules["window_end_us"]
    groups = rules["groups"]
    ordered_groups = rules["ordered_groups"]
    for task in rules["transmissions"].values():
        start = max(task["start_us"], window_start)
        end = min(task["end_us"], window_end)
        if start >= end:
            continue  # no positive-length intersection with the window
        receiver = task["receiver_id"]
        if receiver not in rules["receiver_venue"]:
            raise ValueError(
                f"transmission {task['id']} receiver {receiver} has no venue mapping")
        venue_id = rules["receiver_venue"][receiver]
        center = task["center_hz"]
        match = None
        for group_id in ordered_groups:
            low, high = groups[group_id]
            if low <= center < high:  # half-open frequency interval
                match = group_id
                break
        if match is None:
            raise ValueError(
                f"transmission {task['id']} center_hz {center} is outside "
                "every channel group")
        matrix[venue_index[venue_id]][group_index[match]] += end - start

    totals = {("total",): sum(sum(row) for row in matrix)}
    for i, venue_id in enumerate(rules["venue_ids"]):
        totals[("row", venue_id)] = sum(matrix[i])
    for j, group_id in enumerate(ordered_groups):
        totals[("column", group_id)] = sum(
            matrix[i][j] for i in range(rows))
    for i, venue_id in enumerate(rules["venue_ids"]):
        for j, group_id in enumerate(ordered_groups):
            totals[("cell", venue_id, group_id)] = matrix[i][j]
    return matrix, totals


# ---------------------------------------------------------------------------
# lower-bound circulation over the transportation network
# ---------------------------------------------------------------------------
class Circulation:
    """Deterministic Dinic max-flow on a lower-bound circulation.

    The network is ``source -> row meter -> column meter -> sink`` plus a
    ``sink -> source`` total meter; every edge carries an integer lower and
    upper bound.  Feasibility is the standard lower-bound transformation
    with a super source/sink.  Node and edge insertion order is fixed, so
    identical inputs produce identical runs; only exact integer arithmetic
    is used and the work depends on the graph size, never on the magnitudes.
    """

    def __init__(self):
        self._to = []
        self._cap = []
        self._low = []
        self._adj = {}

    def add_edge(self, frm, to, low, high):
        if low > high:
            return None
        edge = len(self._low)
        self._low.append(low)
        self._adj.setdefault(frm, []).append(2 * edge)
        self._adj.setdefault(to, []).append(2 * edge + 1)
        self._to.extend((to, frm))
        self._cap.extend((high - low, 0))
        return edge

    def max_flow(self, source, sink):
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
                    target = self._to[arc]
                    if self._cap[arc] > 0 and target not in level:
                        level[target] = level[node] + 1
                        queue.append(target)
            if sink not in level:
                return total
            progress = {node: 0 for node in level}
            bound = sum(self._cap[arc]
                        for arc in self._adj.get(source, ()))
            while True:
                pushed = self._push(source, sink, level, progress, bound)
                if pushed <= 0:
                    break
                total += pushed

    def _push(self, node, sink, level, progress, offered):
        if node == sink:
            return offered
        arcs = self._adj.get(node, ())
        index = progress[node]
        while index < len(arcs):
            arc = arcs[index]
            target = self._to[arc]
            if self._cap[arc] > 0 \
                    and level.get(target) == level[node] + 1:
                pushed = self._push(
                    target, sink, level, progress,
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


class Feasibility:
    """One fixed set of caliber bounds and exact-value publications."""

    def __init__(self, rows, cols, edge_bounds):
        self.rows = rows
        self.cols = cols
        # edge_bounds maps every caliber key to an integer [low, high] pair.
        self.bounds = edge_bounds
        self._edges = {}
        self._feasible = self._build()

    @property
    def feasible(self):
        return self._feasible

    def _build(self):
        net = Circulation()
        source = ("source",)
        sink = ("sink",)
        super_source = ("super_source",)
        super_sink = ("super_sink",)
        demand = {}

        def add(frm, to, key):
            low, high = self.bounds[key]
            edge = net.add_edge(frm, to, low, high)
            if edge is None:
                return False
            self._edges[key] = edge
            demand[frm] = demand.get(frm, 0) - low
            demand[to] = demand.get(to, 0) + low
            return True

        for i in range(self.rows):
            row_key = ("row", i)
            if not add(source, ("row", i), row_key):
                return False
        for j in range(self.cols):
            col_key = ("col", j)
            if not add(("col", j), sink, col_key):
                return False
        for i in range(self.rows):
            for j in range(self.cols):
                if not add(("row", i), ("col", j), ("cell", i, j)):
                    return False
        if not add(sink, source, ("total",)):
            return False

        mark = len(net._low)
        required = 0
        for node, balance in demand.items():
            if balance > 0:
                net.add_edge(super_source, node, 0, balance)
                required += balance
            elif balance < 0:
                net.add_edge(node, super_sink, 0, -balance)
        net.add_edge(super_sink, super_source, 0, DEFAULT_BOUND)
        if net.max_flow(super_source, super_sink) != required:
            return False
        # Detach every super-node arc so later queries see only the
        # circulation itself.
        for edge in range(mark, len(net._low)):
            net._cap[2 * edge] = 0
            net._cap[2 * edge + 1] = 0
        self._net = net
        return True

    def value_of(self, key):
        """A feasible value of one caliber (the anchor flow on its edge)."""
        edge = self._edges[key]
        return self._net._low[edge] + self._net._cap[2 * edge + 1]


class PublicRegion:
    """The public's feasible-matrix region under bounds and publications.

    Caliber keys are normalized to positional form ``("cell", i, j)``,
    ``("row", i)``, ``("col", j)`` and ``("total",)`` so the flow engine is
    independent of raw ids.
    """

    def __init__(self, rows, cols, standing_bounds, publications=()):
        self.rows = rows
        self.cols = cols
        bounds = {key: pair for key, pair in standing_bounds.items()}
        for key, value in publications:
            low, high = bounds[key]
            if not low <= value <= high:
                self._base = None
                self.bounds = bounds
                return
            bounds[key] = (value, value)
        self.bounds = bounds
        self._base = Feasibility(rows, cols, bounds)

    @property
    def feasible(self):
        return self._base is not None and self._base.feasible

    def _with_pins(self, pins):
        """A copy with extra (key, value) exact pins intersected in."""
        bounds = {key: pair for key, pair in self.bounds.items()}
        for key, value in pins:
            low, high = bounds[key]
            if not low <= value <= high:
                return None
            bounds[key] = (value, value)
        probe = Feasibility(self.rows, self.cols, bounds)
        return probe if probe.feasible else None

    def caliber_range(self, key, extra_pins=()):
        """Minimum and maximum feasible integer value of ``key``.

        O(log U) forced-bound feasibility runs using a feasible anchor read
        from the base circulation; the feasible-value set is the contiguous
        integer interval between the returned endpoints.
        """
        pins = list(extra_pins)
        base = self._with_pins(pins)
        if base is None:
            return None
        low0, high0 = self.bounds[key]
        anchor = base.value_of(key)

        def feasible_at(value):
            return self._with_pins(pins + [(key, value)]) is not None

        left, right = low0, anchor
        while left < right:
            middle = (left + right) // 2
            if feasible_at(middle):
                right = middle
            else:
                left = middle + 1
        minimum = left
        left, right = anchor, high0
        while left < right:
            middle = (left + right + 1) // 2
            if feasible_at(middle):
                left = middle
            else:
                right = middle - 1
        return minimum, left

    def protected_safe(self, protected, extra_pins=()):
        """Every protected cell still spans at least its required gap."""
        for key, gap in protected.items():
            span = self.caliber_range(key, extra_pins)
            if span is None or span[1] - span[0] < gap:
                return False
        return True

    def admit(self, key, protected):
        """Admission test for requesting caliber ``key``.

        Grant only when publishing *every* still-possible integer value of
        ``key`` preserves every protected gap.  Because the possible values
        form one integer interval, testing its two endpoints is exhaustive.
        """
        projection = self.caliber_range(key)
        if projection is None:
            return False
        low, high = projection
        for value in (low, high):
            if not self.protected_safe(protected, [(key, value)]):
                return False
        return True


# ---------------------------------------------------------------------------
# caliber key normalization between raw ids and matrix positions
# ---------------------------------------------------------------------------
def _normalize_key(key, venue_index, group_index):
    kind = key[0]
    if kind == "cell":
        return ("cell", venue_index[key[1]], group_index[key[2]])
    if kind == "row":
        return ("row", venue_index[key[1]])
    if kind == "column":
        return ("col", group_index[key[1]])
    return ("total",)


def _standing_bounds(rules, venue_index, group_index):
    """Every caliber gets an explicit bound; declarations tighten default."""
    rows = len(venue_index)
    cols = len(group_index)
    bounds = {}
    for i in range(rows):
        bounds[("row", i)] = (0, DEFAULT_BOUND)
    for j in range(cols):
        bounds[("col", j)] = (0, DEFAULT_BOUND)
    for i in range(rows):
        for j in range(cols):
            bounds[("cell", i, j)] = (0, DEFAULT_BOUND)
    bounds[("total",)] = (0, DEFAULT_BOUND)
    for raw_key, (low, high) in rules["bounds"].items():
        bounds[_normalize_key(raw_key, venue_index, group_index)] = (low, high)
    return bounds


def _caliber_reference(key, venue_ids, ordered_groups):
    kind = key[0]
    if kind == "cell":
        return {
            "scope": "cell",
            "venue_id": venue_ids[key[1]],
            "group_id": ordered_groups[key[2]],
        }
    if kind == "row":
        return {"scope": "row", "venue_id": venue_ids[key[1]]}
    if kind == "col":
        return {"scope": "column", "group_id": ordered_groups[key[1]]}
    return {"scope": "total"}


# ---------------------------------------------------------------------------
# bulletin assembly
# ---------------------------------------------------------------------------
def build_bulletin(plan, rules):
    """Validate both documents and produce the privacy-reviewed bulletin.

    Raises ``ValueError`` on any contract violation, history that conflicts
    with the frozen plan, or a prior bulletin that has already shrunk a
    protected cell below its required gap.  Withholding a requested caliber
    is a normal business result and yields exit code 0 at the CLI layer.
    """
    stations, transmissions = _parse_plan(plan)
    rules = _parse_rules(rules, stations, transmissions)
    _, true_values = measure_matrix(rules)

    venue_index = {venue_id: i for i, venue_id in enumerate(rules["venue_ids"])}
    group_index = {
        group_id: j for j, group_id in enumerate(rules["ordered_groups"])}
    rows = len(venue_index)
    cols = len(group_index)
    standing = _standing_bounds(rules, venue_index, group_index)

    def truth(raw_key):
        return true_values[raw_key]

    # Every declared public bound must contain the real caliber value.
    for raw_key, (low, high) in rules["bounds"].items():
        value = truth(raw_key)
        if not low <= value <= high:
            # Diagnostics name the caliber and interval, never the private
            # measured value.
            raise ValueError(
                f"declared public bound for {raw_key} excludes the real "
                "frozen-plan statistic")
    # Every real value must respect the universal upper bound.
    for raw_key, value in true_values.items():
        if value > DEFAULT_BOUND:
            raise ValueError(
                f"real statistic for {raw_key} exceeds the universal bound")

    # Normalize the historical exact values and verify them against the plan.
    history = []
    for raw_key, value in rules["prior_publications"]:
        if value != truth(raw_key):
            # The published number is public but the real one is private, so
            # the diagnostic names the disagreement without echoing either.
            raise ValueError(
                f"prior publication for {raw_key} disagrees with the frozen "
                "plan statistics")
        history.append((_normalize_key(raw_key, venue_index, group_index), value))

    protected = {
        _normalize_key(key, venue_index, group_index): gap
        for key, gap in rules["protected"].items()
    }

    # The historical facts alone must still leave every required gap.
    region = PublicRegion(rows, cols, standing, history)
    if not region.feasible:
        raise ValueError("prior publications are jointly infeasible")
    if not region.protected_safe(protected):
        raise ValueError(
            "prior bulletin has already disclosed a protected cell below its "
            "required gap; history cannot be deleted or restarted")

    # Process requests in their explicit order against the growing fact set.
    publications = list(history)
    published_keys = {key for key, _ in publications}
    request_rows = []
    granted = withheld = already = 0
    for raw_key in rules["requests"]:
        key = _normalize_key(raw_key, venue_index, group_index)
        if key in published_keys:
            status = "published"
            already += 1
        elif region.admit(key, protected):
            value = truth(raw_key)
            publications.append((key, value))
            published_keys.add(key)
            region = PublicRegion(rows, cols, standing, publications)
            if not region.feasible:
                raise AssertionError("accepted publication made region infeasible")
            status = "granted"
            granted += 1
        else:
            status = "withheld"
            withheld += 1
        request_rows.append({
            "caliber": _caliber_reference(
                key, rules["venue_ids"], rules["ordered_groups"]),
            "status": status,
        })

    publication_rows = [
        {
            "caliber": _caliber_reference(
                key, rules["venue_ids"], rules["ordered_groups"]),
            "value": value,
        }
        for key, value in publications
    ]
    revision = rules["prior_revision"] + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "mode": MODE,
        "edition_id": rules["edition_id"],
        "revision": revision,
        "window": {
            "start": _iso(rules["window_start_us"]),
            "end": _iso(rules["window_end_us"]),
        },
        "requests": request_rows,
        "publications": publication_rows,
        "prior": {
            "edition_id": rules["edition_id"],
            "revision": revision,
            "publications": publication_rows,
        },
        "summary": {
            "venues": rows,
            "channel_groups": cols,
            "requests": len(request_rows),
            "already_published": already,
            "granted": granted,
            "withheld": withheld,
            "cumulative_publications": len(publication_rows),
        },
    }


def _iso(microseconds):
    return (_EPOCH + timedelta(microseconds=microseconds)).isoformat()
