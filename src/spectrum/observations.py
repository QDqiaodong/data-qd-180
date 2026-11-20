"""Post-event execution review for observed radio transmissions.

The original conflict report (``spectrum.service.run``) answers what *would*
happen under the scheduled times and channels.  Field devices instead log
execution ticks against per-reboot clock *epochs*.  Each epoch's offset to UTC
is one unknown integer microsecond value, only known to lie inside an
error interval, and ``relative_syncs`` add difference constraints between
epochs.  Every constraint in the document bounds one single *global* vector of
epoch offsets; offsets are never re-picked per record or per question.

The review is exact over integer microseconds, using only the standard
library:

* feasibility and every reachable offset / cross-epoch time difference are
  solved as a system of difference constraints with an all-pairs
  Floyd-Warshall style relaxation (a negative cycle means
  ``inconsistent_clocks``);
* each observed directed pair is classified ``certain`` / ``possible`` /
  ``absent`` over *all* feasible vectors, reusing the untouched declared
  protection-circle model in :mod:`spectrum.interference`;
* group extrema use a branch-and-bound search over offset *cut values*, never
  an expansion per microsecond, so a domain spanning billions of microseconds
  costs the same as a domain of width one.

Public, documented entry points (see docs/observations.md):

* :func:`review_executions` -- the testable pure review function;
* :func:`build_review_report` -- plan report plus review, as emitted by the
  ``radio-coordinate --observations`` CLI path.
"""
from datetime import datetime, timedelta, timezone

from . import service
from .interference import distance_m
from .model import stamp

# ---------------------------------------------------------------------------
# contract constants
# ---------------------------------------------------------------------------
ROOT_KEYS = frozenset(("clock_epochs", "relative_syncs", "executions", "groups"))
EPOCH_KEYS = frozenset(
    ("id", "utc_origin", "offset_min_us", "offset_max_us"))
SYNC_KEYS = frozenset(
    ("id", "left_epoch_id", "right_epoch_id", "min_us", "max_us"))
EXECUTION_KEYS = frozenset((
    "transmission_id", "epoch_id", "start_tick_us", "end_tick_us",
    "center_hz", "bandwidth_hz"))
PAIR_KEYS = frozenset(("victim_transmission_id", "interferer_transmission_id"))
GROUP_KEYS = frozenset(("id", "pairs"))

MAX_EPOCHS = 8
MAX_SYNCS = 32
MAX_EXECUTIONS = 300
MAX_GROUPS = 8
MAX_GROUP_PAIRS = 12
MAX_TICK_US = 604_800_000_000
EPOCH_OFFSET_BOUND = 3_600_000_000
SYNC_BOUND = 7_200_000_000

# Difference-constraint infinity (always strictly above any finite path:
# the longest simple path has at most n-1 edges, each <= 7.2e9).
_INF = 10 ** 30

MODEL = "declared_receiver_protection_circle"
TOOL = "radio-coordinate"
SCHEMA_VERSION = 1


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


def _bounded_int(value, label, low, high):
    number = _strict_int(value, label)
    if not low <= number <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return number


def _unique_rows(raw, label, max_count, min_count=0):
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be an array")
    if not min_count <= len(raw) <= max_count:
        raise ValueError(f"{label} must contain {min_count}..{max_count} entries")
    seen = set()
    for index, row in enumerate(raw):
        _object(row, f"{label}[{index}]")
        identifier = _text(row.get("id"), f"{label}[{index}].id")
        if identifier in seen:
            raise ValueError(f"duplicate {label} id: {identifier}")
        seen.add(identifier)
    return raw


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _parse_epochs(raw):
    _unique_rows(raw, "clock_epochs", MAX_EPOCHS, min_count=1)
    epochs = {}
    for index, row in enumerate(raw):
        label = f"clock_epochs[{index}]"
        _exact_keys(row, EPOCH_KEYS, label)
        epoch_id = _text(row["id"], f"{label}.id")
        try:
            origin = stamp(row["utc_origin"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{label}.utc_origin must be an ISO-8601 time with an offset"
            ) from exc
        low = _bounded_int(
            row["offset_min_us"], f"{label}.offset_min_us",
            -EPOCH_OFFSET_BOUND, EPOCH_OFFSET_BOUND)
        high = _bounded_int(
            row["offset_max_us"], f"{label}.offset_max_us",
            -EPOCH_OFFSET_BOUND, EPOCH_OFFSET_BOUND)
        if low > high:
            raise ValueError(f"{label} offset lower bound must not exceed upper bound")
        epochs[epoch_id] = {
            "id": epoch_id,
            "utc_origin_dt": origin,
            "utc_origin": origin.isoformat(),
            "origin_us": _epoch_origin_us(origin),
            "offset_min_us": low,
            "offset_max_us": high,
        }
    return epochs


def _epoch_origin_us(origin_dt):
    """Microseconds counted on the absolute UTC axis (arbitrary fixed origin).

    The value only participates in differences together with ticks, so the
    fixed reference below is irrelevant; integer microseconds are preserved
    exactly without floating point.
    """
    ref = datetime(1, 1, 1, tzinfo=timezone.utc)
    delta = origin_dt.astimezone(timezone.utc) - ref
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 \
        + delta.microseconds


def _parse_syncs(raw, epochs):
    _unique_rows(raw, "relative_syncs", MAX_SYNCS)
    syncs = []
    for index, row in enumerate(raw):
        label = f"relative_syncs[{index}]"
        _exact_keys(row, SYNC_KEYS, label)
        sync_id = _text(row["id"], f"{label}.id")
        left = _text(row["left_epoch_id"], f"{label}.left_epoch_id")
        right = _text(row["right_epoch_id"], f"{label}.right_epoch_id")
        if left not in epochs:
            raise ValueError(f"{label}.left_epoch_id does not reference an epoch")
        if right not in epochs:
            raise ValueError(f"{label}.right_epoch_id does not reference an epoch")
        if left == right:
            raise ValueError(f"{label} must reference two different epochs")
        low = _bounded_int(row["min_us"], f"{label}.min_us",
                           -SYNC_BOUND, SYNC_BOUND)
        high = _bounded_int(row["max_us"], f"{label}.max_us",
                            -SYNC_BOUND, SYNC_BOUND)
        if low > high:
            raise ValueError(f"{label} min_us must not exceed max_us")
        syncs.append({"id": sync_id, "left": left, "right": right,
                      "min_us": low, "max_us": high})
    return syncs


def _parse_executions(raw, epochs, plan_transmissions):
    if not isinstance(raw, list) or len(raw) > MAX_EXECUTIONS:
        raise ValueError(f"executions must be an array of <= {MAX_EXECUTIONS} entries")
    executions = {}
    for index, row in enumerate(raw):
        label = f"executions[{index}]"
        _exact_keys(row, EXECUTION_KEYS, label)
        transmission_id = _text(
            row["transmission_id"], f"{label}.transmission_id")
        if transmission_id not in plan_transmissions:
            raise ValueError(
                f"{label}.transmission_id does not reference a plan transmission")
        if transmission_id in executions:
            raise ValueError(
                f"duplicate execution for transmission: {transmission_id}")
        epoch_id = _text(row["epoch_id"], f"{label}.epoch_id")
        if epoch_id not in epochs:
            raise ValueError(f"{label}.epoch_id does not reference an epoch")
        start_tick = _bounded_int(
            row["start_tick_us"], f"{label}.start_tick_us", 0, MAX_TICK_US)
        end_tick = _bounded_int(
            row["end_tick_us"], f"{label}.end_tick_us", 0, MAX_TICK_US)
        if not start_tick < end_tick:
            raise ValueError(f"{label} start_tick_us must be smaller than end_tick_us")
        center = _strict_int(row["center_hz"], f"{label}.center_hz")
        bandwidth = _strict_int(row["bandwidth_hz"], f"{label}.bandwidth_hz")
        if center < 1 or bandwidth < 1:
            raise ValueError(f"{label} center_hz and bandwidth_hz must be positive")
        if bandwidth > 2 * center:
            raise ValueError(f"{label} occupied band extends below zero")
        epoch = epochs[epoch_id]
        # Both endpoints, under either declared extreme of the epoch's own
        # offset range, must be representable as Python datetimes.
        for tick, tick_label in ((start_tick, "start_tick_us"),
                                 (end_tick, "end_tick_us")):
            for bound in (epoch["offset_min_us"], epoch["offset_max_us"]):
                _representable(epoch, tick, bound, f"{label}.{tick_label}")
        plan_row = plan_transmissions[transmission_id]
        executions[transmission_id] = {
            "transmission_id": transmission_id,
            "epoch_id": epoch_id,
            "start_tick_us": start_tick,
            "end_tick_us": end_tick,
            "center_hz": center,
            "bandwidth_hz": bandwidth,
            "low_twice_hz": 2 * center - bandwidth,
            "high_twice_hz": 2 * center + bandwidth,
            "station_id": plan_row["station_id"],
            "receiver_id": plan_row["receiver_id"],
        }
    return executions


def _representable(epoch, tick, offset, label):
    absolute_us = epoch["origin_us"] + tick + offset
    try:
        _utc_from_axis_us(absolute_us)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            f"{label} converts to a datetime outside the representable range"
        ) from exc


def _parse_groups(raw, executions):
    _unique_rows(raw, "groups", MAX_GROUPS)
    groups = []
    for index, row in enumerate(raw):
        label = f"groups[{index}]"
        _exact_keys(row, GROUP_KEYS, label)
        group_id = _text(row["id"], f"{label}.id")
        pairs_raw = row["pairs"]
        if not isinstance(pairs_raw, list) or not 1 <= len(pairs_raw) <= MAX_GROUP_PAIRS:
            raise ValueError(
                f"{label}.pairs must be an array of 1..{MAX_GROUP_PAIRS} entries")
        pairs = []
        seen_pairs = set()
        for pos, pair_row in enumerate(pairs_raw):
            pair_label = f"{label}.pairs[{pos}]"
            _object(pair_row, pair_label)
            _exact_keys(pair_row, PAIR_KEYS, pair_label)
            victim = _text(pair_row["victim_transmission_id"],
                           f"{pair_label}.victim_transmission_id")
            interferer = _text(pair_row["interferer_transmission_id"],
                               f"{pair_label}.interferer_transmission_id")
            if victim == interferer:
                raise ValueError(f"{pair_label} must reference two different transmissions")
            if victim not in executions:
                raise ValueError(
                    f"{pair_label}.victim_transmission_id has no execution observation")
            if interferer not in executions:
                raise ValueError(
                    f"{pair_label}.interferer_transmission_id has no execution observation")
            key = (victim, interferer)
            if key in seen_pairs:
                raise ValueError(f"{label} contains a duplicate pair: {key}")
            seen_pairs.add(key)
            pairs.append(key)
        groups.append({"id": group_id, "pairs": pairs})
    return groups


def parse_observations(data, plan_transmissions):
    """Validate the whole observation document against the strict contract.

    Raises ``ValueError`` for every malformed document, dangling reference,
    boolean-integer or count-limit violation.  Returns parsed epochs, syncs,
    executions and groups.
    """
    _object(data, "observations root")
    _exact_keys(data, ROOT_KEYS, "observations root")
    epochs = _parse_epochs(data["clock_epochs"])
    syncs = _parse_syncs(data["relative_syncs"], epochs)
    executions = _parse_executions(
        data["executions"], epochs, plan_transmissions)
    groups = _parse_groups(data["groups"], executions)
    return epochs, syncs, executions, groups


# ---------------------------------------------------------------------------
# difference-constraint system
# ---------------------------------------------------------------------------
def build_constraint_matrix(epochs, syncs):
    """Return the all-pairs upper-bound matrix d with d[i][j] = max(xj-xi).

    Epoch domains and relative-sync evidence are the same global system.
    A sentinel node 0 anchors every epoch domain; epoch indices start at 1.
    """
    epoch_ids = sorted(epochs)
    index = {epoch_id: pos + 1 for pos, epoch_id in enumerate(epoch_ids)}
    size = len(epoch_ids) + 1
    matrix = [[_INF] * size for _ in range(size)]
    for pos in range(size):
        matrix[pos][pos] = 0
    for epoch_id, pos in index.items():
        epoch = epochs[epoch_id]
        # x[pos] <= high and -x[pos] <= -low, both relative to the sentinel.
        matrix[0][pos] = min(matrix[0][pos], epoch["offset_max_us"])
        matrix[pos][0] = min(matrix[pos][0], -epoch["offset_min_us"])
    for sync in syncs:
        left = index[sync["left"]]
        right = index[sync["right"]]
        # x[right] - x[left] in [min_us, max_us].
        matrix[left][right] = min(matrix[left][right], sync["max_us"])
        matrix[right][left] = min(matrix[right][left], -sync["min_us"])
    return epoch_ids, index, _relax(matrix)


def _relax(matrix):
    """Floyd-Warshall closure; return None when a negative cycle exists."""
    size = len(matrix)
    for k in range(size):
        row_k = matrix[k]
        for i in range(size):
            via = matrix[i][k]
            if via == _INF:
                continue
            row_i = matrix[i]
            for j in range(size):
                candidate = via + row_k[j]
                if candidate < row_i[j]:
                    row_i[j] = candidate
    for pos in range(size):
        if matrix[pos][pos] < 0:
            return None
    return matrix


def _fix_variable(matrix, pos, value):
    """Clone the closed matrix and additionally pin x[pos] == value."""
    size = len(matrix)
    cloned = [row[:] for row in matrix]
    cloned[0][pos] = min(cloned[0][pos], value)
    cloned[pos][0] = min(cloned[pos][0], -value)
    return _relax(cloned)


def _pin_feasible(matrix, pos, value):
    """Cheap feasibility test for pinning x[pos] == value, no matrix rewrite.

    The input matrix is already a closed difference-constraint system, so
    adding the two edges 0 -> pos (weight ``value``) and pos -> 0 (weight
    ``-value``) is feasible exactly when they create no negative cycle;
    every affected path routes through exactly one of those edges.
    """
    if not -matrix[pos][0] <= value <= matrix[0][pos]:
        return False
    for j in range(len(matrix)):
        if value + matrix[pos][j] + matrix[j][0] < 0:
            return False
        if -value + matrix[0][j] + matrix[j][pos] < 0:
            return False
    return True


# ---------------------------------------------------------------------------
# time conversion and pair geometry
# ---------------------------------------------------------------------------
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def _utc_from_axis_us(axis_us):
    return _EPOCH + timedelta(microseconds=axis_us)


def _utc_iso(axis_us):
    return _utc_from_axis_us(axis_us).isoformat()


def _pair_static_ok(victim, interferer, stations):
    """Frequency overlap and protection-circle relation are offset-invariant."""
    band_low = max(victim["low_twice_hz"], interferer["low_twice_hz"])
    band_high = min(victim["high_twice_hz"], interferer["high_twice_hz"])
    if band_low >= band_high:
        return False
    receiver = stations[victim["receiver_id"]]
    separation = distance_m(receiver, stations[interferer["station_id"]])
    return separation <= receiver["protection_radius_m"]


def _timing_cuts(predicates, pos, matrix, low, high):
    """All offset cut values for one branching variable.

    A predicate is ``x_v - x_u <= c``.  Fixing the current variable only
    changes truth values at boundary constants (plus the adjacent integer,
    because inequalities are strict on the real overlap test); every open
    interval between consecutive cuts keeps every predicate truth fixed.
    ``low_i = -matrix[i][0]`` and ``high_i = matrix[0][i]`` are the variable's
    currently reachable bounds inside the partial assignment.
    """
    cuts = {low, high}

    def add(value):
        if low <= value <= high:
            cuts.add(value)

    for predicate in predicates:
        u = predicate["a"]
        v = predicate["b"]
        constant = predicate["constant"]
        low_v = -matrix[v][0]
        high_v = matrix[0][v]
        low_u = -matrix[u][0]
        high_u = matrix[0][u]
        if pos == u:
            # Branching x_u: true needs x_u >= low_v - c; false needs
            # x_u <= high_v - c - 1.
            add(low_v - constant)
            add(low_v - constant - 1)
            add(high_v - constant)
            add(high_v - constant + 1)
        if pos == v:
            # Branching x_v: true needs x_v <= high_u + c; false needs
            # x_v >= low_u + c + 1.
            add(high_u + constant)
            add(high_u + constant + 1)
            add(low_u + constant)
            add(low_u + constant - 1)
    return sorted(cuts)


def _build_predicates(victim, interferer, epochs, index):
    """Half-open overlap of [vs, ve) and [is, ie) as two difference tests.

    Absolute endpoint time = epoch origin_us + local tick + x[epoch].  Let
    ``a`` be the victim epoch node and ``b`` the interferer epoch node.
    Positive-length overlap requires both strict inequalities:

    * ``start_v < end_i``  ->  ``x_a - x_b <= (end_i base) - (start_v base) - 1``
    * ``start_i < end_v``  ->  ``x_b - x_a <= (end_v base) - (start_i base) - 1``
    """
    epoch_v = epochs[victim["epoch_id"]]
    epoch_i = epochs[interferer["epoch_id"]]
    a = index[epoch_v["id"]]
    b = index[epoch_i["id"]]
    base_v = epoch_v["origin_us"]
    base_i = epoch_i["origin_us"]
    c1 = (base_i + interferer["end_tick_us"]) \
        - (base_v + victim["start_tick_us"]) - 1
    c2 = (base_v + victim["end_tick_us"]) \
        - (base_i + interferer["start_tick_us"]) - 1
    return [
        {"a": b, "b": a, "constant": c1},  # x_a - x_b <= c1
        {"a": a, "b": b, "constant": c2},  # x_b - x_a <= c2
    ]


def _predicate_holds(matrix, predicate):
    # x_b - x_a <= constant holds for every feasible vector iff the tightest
    # reachable upper bound on the difference does not exceed the constant.
    return matrix[predicate["a"]][predicate["b"]] <= predicate["constant"]


def _classify_pair(victim, interferer, matrix, static_ok, epochs, index):
    if not static_ok:
        return "absent"
    predicates = _build_predicates(victim, interferer, epochs, index)
    holds_all = all(_predicate_holds(matrix, predicate)
                    for predicate in predicates)
    if holds_all:
        return "certain"
    # A predicate is impossible for every vector iff its strict negation
    # always holds, i.e. min(x_b - x_a) > constant.
    impossible_any = any(
        -matrix[predicate["b"]][predicate["a"]] > predicate["constant"]
        for predicate in predicates)
    if impossible_any:
        return "absent"
    return "possible"


# ---------------------------------------------------------------------------
# group extrema: exhaustive branch-and-bound over offset cut values
# ---------------------------------------------------------------------------
def _group_extremum(group, pairs_by_key, epochs, index, matrix, want_max):
    """Return (extreme_count, lexicographically smallest offset vector).

    The search branches one epoch at a time (epoch IDs sorted), trying only
    cut values: the variable's current reachable bounds plus every timing
    boundary touching it.  Between two consecutive cuts all involved
    predicates have fixed truth values, so no feasible firing pattern can be
    missed while the explored tree stays independent of the microsecond width
    of the offset domains.
    """
    size = len(matrix)
    pair_specs = []
    predicates = []
    for key in group["pairs"]:
        spec = pairs_by_key[key]
        local = _build_predicates(
            spec["victim"], spec["interferer"], epochs, index)
        spec = dict(spec)
        spec["predicates"] = local
        pair_specs.append(spec)
        predicates.extend(local)

    best = {"count": None, "vector": None}
    # Prefix feasibility memo: the same partial vector can be reached through
    # different cut branches; the system state depends only on the prefix.
    memo = set()

    def evaluate(vector_by_pos):
        count = 0
        for spec in pair_specs:
            if all(vector_by_pos[predicate["b"]] - vector_by_pos[predicate["a"]]
                   <= predicate["constant"] for predicate in spec["predicates"]):
                count += 1
        return count

    def consider(count, vector_by_pos):
        vector = tuple(vector_by_pos[pos] for pos in range(1, size))
        current = best["count"]
        if current is None:
            better = True
        elif want_max:
            better = count > current or (count == current and vector < best["vector"])
        else:
            better = count < current or (count == current and vector < best["vector"])
        if better:
            best["count"] = count
            best["vector"] = vector

    def search(pos, local_matrix, fixed):
        if pos == size:
            consider(evaluate(fixed), fixed)
            return
        low = -local_matrix[pos][0]
        high = local_matrix[0][pos]
        # Prune with the best count already proved attainable at a leaf.
        if best["count"] is not None:
            remaining = len(pair_specs)
            if want_max and best["count"] == remaining:
                return
            if not want_max and best["count"] == 0:
                return
        cuts = _timing_cuts(predicates, pos, local_matrix, low, high)
        prefix = tuple(fixed[item] for item in range(1, pos))
        for value in cuts:
            key = prefix + (value,)
            if key in memo:
                continue
            # Fast reject without an O(n^3) closure on hopeless branches.
            if not _pin_feasible(local_matrix, pos, value):
                memo.add(key)
                continue
            pinned = _fix_variable(local_matrix, pos, value)
            if pinned is None:  # pragma: no cover - the fast test is exact
                memo.add(key)
                continue
            memo.add(key)
            fixed[pos] = value
            search(pos + 1, pinned, fixed)
            del fixed[pos]

    search(1, matrix, {})
    if best["count"] is None:  # pragma: no cover - feasibility was proved earlier
        raise ValueError("group extremum search found no feasible vector")
    return best["count"], best["vector"]


# ---------------------------------------------------------------------------
# witness assembly
# ---------------------------------------------------------------------------
def _witness_plan(plan, executions, epochs, epoch_ids, vector):
    stations = [dict(row) for row in sorted(plan["stations"], key=lambda r: r["id"])]
    transmissions = []
    for transmission_id in sorted(executions):
        observed = executions[transmission_id]
        plan_row = next(
            row for row in plan["transmissions"] if row["id"] == transmission_id)
        epoch = epochs[observed["epoch_id"]]
        offset = vector[epoch_ids.index(observed["epoch_id"])]
        start_us = epoch["origin_us"] + observed["start_tick_us"] + offset
        end_us = epoch["origin_us"] + observed["end_tick_us"] + offset
        transmissions.append({
            "id": transmission_id,
            "station_id": plan_row["station_id"],
            "receiver_id": plan_row["receiver_id"],
            "center_hz": observed["center_hz"],
            "bandwidth_hz": observed["bandwidth_hz"],
            "start": _utc_iso(start_us),
            "end": _utc_iso(end_us),
        })
    return {"stations": stations, "transmissions": transmissions}


def _firing_pairs(pair_keys, pairs_by_key, epochs, index, epoch_ids, vector):
    vector_by_pos = {pos + 1: value for pos, value in enumerate(vector)}
    firing = []
    for victim_id, interferer_id in pair_keys:
        spec = pairs_by_key[(victim_id, interferer_id)]
        predicates = _build_predicates(
            spec["victim"], spec["interferer"], epochs, index)
        if all(vector_by_pos[predicate["b"]] - vector_by_pos[predicate["a"]]
               <= predicate["constant"] for predicate in predicates):
            firing.append({
                "victim_transmission_id": victim_id,
                "interferer_transmission_id": interferer_id,
            })
    return firing


def _witness_payload(pair_keys, pairs_by_key, count, vector, plan,
                     executions, epochs, index, epoch_ids):
    return {
        "conflict_count": count,
        "epoch_offsets_us": [
            {"epoch_id": epoch_id, "offset_us": vector[pos]}
            for pos, epoch_id in enumerate(epoch_ids)
        ],
        "occurring_pairs": _firing_pairs(
            pair_keys, pairs_by_key, epochs, index, epoch_ids, vector),
        "observed_plan": _witness_plan(
            plan, executions, epochs, epoch_ids, vector),
    }


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------
def review_executions(plan, observations):
    """Pure, testable execution-review function.

    ``plan`` is a validated original plan document (stations / transmissions);
    ``observations`` follows the strict observation contract.  Returns a
    result dict whose ``status`` is either ``inconsistent_clocks`` or
    ``reviewed``.  Format/contract violations raise ``ValueError`` so callers
    can keep the CLI's exit-code-2, no-report semantics.
    """
    plan_stations = {row["id"]: row for row in plan["stations"]}
    plan_transmissions = {row["id"]: row for row in plan["transmissions"]}
    epochs, syncs, executions, groups = parse_observations(
        observations, plan_transmissions)

    epoch_ids, index, matrix = build_constraint_matrix(epochs, syncs)
    if matrix is None:
        return {
            "status": "inconsistent_clocks",
            "status_detail": (
                "no integer epoch offset vector satisfies every clock epoch "
                "range and relative sync simultaneously; no clock evidence "
                "was discarded"
            ),
            "clock_review": None,
            "execution_review": None,
            "group_review": None,
        }

    clock_review = []
    for epoch_id in epoch_ids:
        pos = index[epoch_id]
        low = -matrix[pos][0]
        high = matrix[0][pos]
        epoch = epochs[epoch_id]
        origin_iso = epoch["utc_origin_dt"].isoformat()
        clock_review.append({
            "epoch_id": epoch_id,
            "utc_origin": origin_iso,
            "joint_offset_min_us": low,
            "joint_offset_max_us": high,
            "utc_conversion": (
                "true_utc = utc_origin + local_tick_microseconds + "
                "offset_microseconds; min/max below are jointly reachable "
                "under the single global offset vector, not independent "
                "per-epoch intervals"
            ),
            "zero_tick_min_utc": _utc_iso(epoch["origin_us"] + low),
            "zero_tick_max_utc": _utc_iso(epoch["origin_us"] + high),
        })

    # One review row per directed pair of distinct plan transmissions, in the
    # same ordering as the original model.
    ordered_ids = sorted(plan_transmissions)
    pairs_by_key = {}
    execution_rows = []
    for victim_id in ordered_ids:
        for interferer_id in ordered_ids:
            if victim_id == interferer_id:
                continue
            victim = executions.get(victim_id)
            interferer = executions.get(interferer_id)
            row = {
                "victim_transmission_id": victim_id,
                "interferer_transmission_id": interferer_id,
            }
            if victim is None or interferer is None:
                row["status"] = "unobserved"
                row["detail"] = (
                    "at least one transmission lacks an execution observation; "
                    "planned times and frequencies are never substituted"
                )
            else:
                static_ok = _pair_static_ok(victim, interferer, plan_stations)
                row["status"] = _classify_pair(
                    victim, interferer, matrix, static_ok, epochs, index)
                row["detail"] = (
                    "classified over every feasible global offset vector; "
                    "touching time endpoints and disjoint bands never conflict"
                )
                pairs_by_key[(victim_id, interferer_id)] = {
                    "victim": victim,
                    "interferer": interferer,
                    "static_ok": static_ok,
                }
            execution_rows.append(row)

    group_review = []
    for group in groups:
        live_pairs = [key for key in group["pairs"]
                      if pairs_by_key[key]["static_ok"]]
        live_group = {"id": group["id"], "pairs": live_pairs}
        min_count, min_vector = _group_extremum(
            live_group, pairs_by_key, epochs, index, matrix, want_max=False)
        max_count, max_vector = _group_extremum(
            live_group, pairs_by_key, epochs, index, matrix, want_max=True)
        group_review.append({
            "group_id": group["id"],
            "min_conflicts": min_count,
            "max_conflicts": max_count,
            "min_witness": _witness_payload(
                live_pairs, pairs_by_key, min_count, min_vector, plan,
                executions, epochs, index, epoch_ids),
            "max_witness": _witness_payload(
                live_pairs, pairs_by_key, max_count, max_vector, plan,
                executions, epochs, index, epoch_ids),
        })

    unobserved_ids = sorted(set(plan_transmissions) - set(executions))
    return {
        "status": "reviewed",
        "status_detail": (
            "execution conflicts classified jointly over all integer offset "
            "vectors satisfying the complete clock evidence"
        ),
        "clock_review": clock_review,
        "execution_review": {
            "rows": execution_rows,
            "unobserved_transmission_ids": unobserved_ids,
        },
        "group_review": group_review,
    }


def build_review_report(plan, observations):
    """Run the untouched original service and attach the execution review.

    The original report is embedded verbatim under ``plan_report``; observed
    findings never delete or rewrite its planned conflicts.
    """
    plan_report = service.run(plan)
    review = review_executions(plan, observations)
    reported = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "mode": "execution_review",
        "plan_report": plan_report,
        "review": review,
        "summary": {
            "status": review["status"],
            "transmissions": plan_report["summary"]["transmissions"],
            "observed_transmissions": (
                None if review["status"] == "inconsistent_clocks"
                else plan_report["summary"]["transmissions"]
                - len(review["execution_review"]["unobserved_transmission_ids"])
            ),
            "planned_directed_conflicts":
                plan_report["summary"]["directed_conflicts"],
        },
    }
    return reported
