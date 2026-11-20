"""Offline channel arrangement for pending reception requests.

The module turns a set of optional/required reception requests plus immutable
fixed transmissions into one globally optimal *suggestion*: a channel choice per
selected request and a per-receiver tuning schedule.  It never creates
proposals, records confirmations or mutates a ledger.

Radio evidence reuses the untouched declared protection-circle model in
:mod:`spectrum.interference` / :mod:`spectrum.service`.  This module adds:

* strict validation of the arrangement input contract (``ValueError`` on any
  invalid input, which the CLI maps to exit code 2);
* receiver feasibility: one device per receiver, half-open occupation, a
  declared directed switching table applied only to actually adjacent tasks;
* an exhaustive lexicographic optimizer (utility, then switch count, then the
  assignment vector) with branch-and-bound pruning -- no external solver and
  no greedy shortcut.
"""
from datetime import timedelta

from . import service
from .interference import distance_m
from .io import integer
from .model import stamp

SCHEMA_VERSION = 1
TOOL = "radio-arrange"
MODEL = "declared_receiver_protection_circle"

MAX_STATIONS = 500
MAX_FIXED = 300
MAX_PLAN = 300
MAX_REQUESTS = 14
MAX_CENTERS = 6
MAX_ALLOWED = 3
MAX_SWITCH_SECONDS = 3600
MAX_DURATION = timedelta(days=7)

STATION_FIELDS = frozenset(("id", "lat", "lon", "protection_radius_m"))
TRANSMISSION_FIELDS = frozenset(
    ("id", "station_id", "receiver_id", "center_hz", "bandwidth_hz", "start", "end")
)
REQUEST_FIELDS = frozenset(
    ("id", "station_id", "receiver_id", "bandwidth_hz", "start", "end",
     "allowed_centers_hz", "required", "utility")
)
RECEIVER_FIELDS = frozenset(("id", "ready_at", "initial_center_hz", "switches"))
SWITCH_FIELDS = frozenset(("from_hz", "to_hz", "seconds"))
ROOT_FIELDS = frozenset(
    ("stations", "fixed_transmissions", "centers_hz", "requests", "receivers")
)


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


def _strict_int(value, label, minimum):
    # ``bool`` is a subclass of ``int``; the contract rejects booleans.
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _strict_bool(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _time_window(start_value, end_value, label):
    start = stamp(start_value)
    end = stamp(end_value)
    if not timedelta(0) < end - start <= MAX_DURATION:
        raise ValueError(f"{label} duration must be positive and <= 7 days")
    return start, end


def _validate_stations(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STATIONS:
        raise ValueError(f"stations must be an array of 1..{MAX_STATIONS} objects")
    stations = {}
    for index, row in enumerate(raw):
        label = f"stations[{index}]"
        _object(row, label)
        _exact_keys(row, STATION_FIELDS, label)
        sid = _text(row["id"], f"{label}.id")
        if sid in stations:
            raise ValueError(f"duplicate station id: {sid}")
        lat = row["lat"]
        lon = row["lon"]
        if type(lat) not in (int, float) or type(lat) is bool:
            raise ValueError(f"{label}.lat must be a finite number")
        if type(lon) not in (int, float) or type(lon) is bool:
            raise ValueError(f"{label}.lon must be a finite number")
        lat_f = float(lat)
        lon_f = float(lon)
        if not _finite(lat_f) or not _finite(lon_f):
            raise ValueError(f"{label} coordinates must be finite")
        if not -90 <= lat_f <= 90:
            raise ValueError(f"{label}.lat out of range")
        if not -180 <= lon_f <= 180:
            raise ValueError(f"{label}.lon out of range")
        radius = _strict_int(row["protection_radius_m"],
                             f"{label}.protection_radius_m", 1)
        stations[sid] = {
            "id": sid, "lat": lat, "lon": lon, "protection_radius_m": radius,
        }
    return stations


def _finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def _validate_centers(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CENTERS:
        raise ValueError(f"centers_hz must be an array of 1..{MAX_CENTERS} integers")
    centers = []
    for index, value in enumerate(raw):
        centers.append(_strict_int(value, f"centers_hz[{index}]", 1))
    if len(set(centers)) != len(centers):
        raise ValueError("centers_hz values must be distinct")
    return sorted(centers)


def _transmission_core(row, label, stations, centers, require_center):
    """Validate the shared emission fields; returns a parsed task dict."""
    sid = _text(row["station_id"], f"{label}.station_id")
    rid = _text(row["receiver_id"], f"{label}.receiver_id")
    if sid not in stations:
        raise ValueError(f"{label}.station_id does not reference a station")
    if rid not in stations:
        raise ValueError(f"{label}.receiver_id does not reference a station")
    if sid == rid:
        raise ValueError(f"{label} station_id and receiver_id must differ")
    bandwidth = _strict_int(row["bandwidth_hz"], f"{label}.bandwidth_hz", 1)
    start_dt, end_dt = _time_window(row["start"], row["end"], label)
    center = None
    if require_center:
        center = _strict_int(row["center_hz"], f"{label}.center_hz", 1)
        if center not in centers:
            raise ValueError(f"{label}.center_hz must be one of centers_hz")
        if bandwidth > 2 * center:
            raise ValueError(f"{label} occupied band extends below zero")
    return {
        "station_id": sid,
        "receiver_id": rid,
        "center_hz": center,
        "bandwidth_hz": bandwidth,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def _validate_fixed(raw, stations, centers, used_ids):
    if not isinstance(raw, list) or len(raw) > MAX_FIXED:
        raise ValueError(f"fixed_transmissions must be an array of <= {MAX_FIXED}")
    fixed = []
    for index, row in enumerate(raw):
        label = f"fixed_transmissions[{index}]"
        _object(row, label)
        _exact_keys(row, TRANSMISSION_FIELDS, label)
        tid = _text(row["id"], f"{label}.id")
        if tid in used_ids:
            raise ValueError(f"duplicate transmission id: {tid}")
        parsed = _transmission_core(row, label, stations, centers, True)
        parsed["id"] = tid
        parsed["source"] = "fixed"
        fixed.append(parsed)
        used_ids.add(tid)
    return fixed


def _validate_requests(raw, stations, centers, used_ids):
    if not isinstance(raw, list) or len(raw) > MAX_REQUESTS:
        raise ValueError(f"requests must be an array of <= {MAX_REQUESTS}")
    requests = []
    for index, row in enumerate(raw):
        label = f"requests[{index}]"
        _object(row, label)
        _exact_keys(row, REQUEST_FIELDS, label)
        rid = _text(row["id"], f"{label}.id")
        if rid in used_ids:
            raise ValueError(f"request id duplicates another id: {rid}")
        parsed = _transmission_core(row, label, stations, centers, False)
        allowed = row["allowed_centers_hz"]
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= MAX_ALLOWED:
            raise ValueError(
                f"{label}.allowed_centers_hz must be a nonempty array of "
                f"1..{MAX_ALLOWED} integers"
            )
        allowed_values = []
        for pos, value in enumerate(allowed):
            allowed_values.append(
                _strict_int(value, f"{label}.allowed_centers_hz[{pos}]", 1)
            )
        if len(set(allowed_values)) != len(allowed_values):
            raise ValueError(f"{label}.allowed_centers_hz values must be distinct")
        center_set = set(centers)
        if any(value not in center_set for value in allowed_values):
            raise ValueError(
                f"{label}.allowed_centers_hz must be a subset of centers_hz"
            )
        allowed_values.sort()
        # Every candidate band must stay at or above zero Hz.
        if parsed["bandwidth_hz"] > 2 * allowed_values[0]:
            raise ValueError(f"{label} candidate band extends below zero")
        required = _strict_bool(row["required"], f"{label}.required")
        utility = integer(row["utility"], f"{label}.utility", 1)
        if type(row["utility"]) is bool or utility > 1_000_000:
            raise ValueError(f"{label}.utility must be an integer in 1..1000000")
        parsed.update({
            "id": rid,
            "allowed_centers_hz": allowed_values,
            "required": required,
            "utility": utility,
            "source": "request",
        })
        requests.append(parsed)
        used_ids.add(rid)
    return requests


def _validate_receivers(raw, stations, centers, fixed, requests):
    if not isinstance(raw, list):
        raise ValueError("receivers must be an array")
    used_receivers = {row["receiver_id"] for row in fixed}
    used_receivers.update(row["receiver_id"] for row in requests)
    configs = {}
    for index, row in enumerate(raw):
        label = f"receivers[{index}]"
        _object(row, label)
        _exact_keys(row, RECEIVER_FIELDS, label)
        rid = _text(row["id"], f"{label}.id")
        if rid in configs:
            raise ValueError(f"duplicate receiver config id: {rid}")
        if rid not in stations:
            raise ValueError(f"{label}.id does not reference a station")
        ready_at = stamp(row["ready_at"])
        initial = _strict_int(row["initial_center_hz"],
                              f"{label}.initial_center_hz", 1)
        if initial not in centers:
            raise ValueError(f"{label}.initial_center_hz must be one of centers_hz")
        configs[rid] = {"id": rid, "ready_at": ready_at,
                        "initial_center_hz": initial, "switches": row["switches"]}
    if set(configs) != used_receivers:
        missing = sorted(used_receivers - set(configs))
        extra = sorted(set(configs) - used_receivers)
        raise ValueError(
            "receivers must cover exactly the receiver_ids in use; "
            f"missing={missing} extra={extra}"
        )
    center_set = set(centers)
    expected_pairs = {
        (a, b) for a in centers for b in centers if a != b
    }
    for rid, config in configs.items():
        label = f"receivers[{rid}].switches"
        raw_switches = config["switches"]
        if not isinstance(raw_switches, list):
            raise ValueError(f"{label} must be an array")
        seen = set()
        table = {}
        for pos, row in enumerate(raw_switches):
            item_label = f"{label}[{pos}]"
            _object(row, item_label)
            _exact_keys(row, SWITCH_FIELDS, item_label)
            source = _strict_int(row["from_hz"], f"{item_label}.from_hz", 1)
            target = _strict_int(row["to_hz"], f"{item_label}.to_hz", 1)
            seconds = row["seconds"]
            if type(seconds) is bool or not isinstance(seconds, int) \
                    or not 0 <= seconds <= MAX_SWITCH_SECONDS:
                raise ValueError(
                    f"{item_label}.seconds must be an integer in "
                    f"0..{MAX_SWITCH_SECONDS}"
                )
            if source not in center_set or target not in center_set:
                raise ValueError(f"{item_label} channels must be centers_hz values")
            if source == target:
                raise ValueError(f"{item_label} same-channel switches are implicit")
            pair = (source, target)
            if pair in seen:
                raise ValueError(f"{item_label} duplicate directed switch: {pair}")
            seen.add(pair)
            table[pair] = seconds
        if seen != expected_pairs:
            raise ValueError(
                f"{label} must cover exactly each directed pair of distinct "
                f"centers once; expected {len(expected_pairs)}, got {len(seen)}"
            )
        config["switch_table"] = table
    return configs


def parse_input(data):
    """Validate the whole arrangement document; raise ``ValueError`` on defects."""
    _object(data, "root")
    _exact_keys(data, ROOT_FIELDS, "root")
    stations = _validate_stations(data["stations"])
    centers = _validate_centers(data["centers_hz"])
    used_ids = set()
    fixed = _validate_fixed(data["fixed_transmissions"], stations, centers, used_ids)
    requests = _validate_requests(data["requests"], stations, centers, used_ids)
    receivers = _validate_receivers(
        data["receivers"], stations, centers, fixed, requests
    )
    return {
        "stations": stations,
        "centers": centers,
        "fixed": fixed,
        "requests": requests,
        "receivers": receivers,
    }


# ---------------------------------------------------------------------------
# radio / device feasibility primitives
# ---------------------------------------------------------------------------
def _task_band(task):
    center = task["center_hz"]
    bandwidth = task["bandwidth_hz"]
    return 2 * center - bandwidth, 2 * center + bandwidth


def _time_overlap(a, b):
    return max(a["start_dt"], b["start_dt"]) < min(a["end_dt"], b["end_dt"])


def _band_overlap(a, b):
    a_low, a_high = _task_band(a)
    b_low, b_high = _task_band(b)
    return max(a_low, b_low) < min(a_high, b_high)


class RadioGraph:
    """Caches great-circle distances and answers directed conflict questions."""

    def __init__(self, stations):
        self.stations = stations
        self._distances = {}

    def distance(self, receiver_id, transmitter_id):
        key = (receiver_id, transmitter_id)
        value = self._distances.get(key)
        if value is None:
            value = distance_m(
                self.stations[receiver_id], self.stations[transmitter_id]
            )
            self._distances[key] = value
        return value

    def directed_conflict(self, victim, source):
        if not _time_overlap(victim, source):
            return False
        if not _band_overlap(victim, source):
            return False
        receiver = self.stations[victim["receiver_id"]]
        return self.distance(victim["receiver_id"], source["station_id"]) \
            <= receiver["protection_radius_m"]

    def incompatible(self, a, b):
        """Either directed relation forbids putting both tasks in one plan."""
        return self.directed_conflict(a, b) or self.directed_conflict(b, a)


def _switch_seconds(table, center_a, center_b):
    if center_a == center_b:
        return 0
    return table[(center_a, center_b)]


def evaluate_receiver(config, tasks):
    """Return ``(feasible, switch_count)`` for one receiver's full task list.

    The switch table is only consulted for actually adjacent tasks.  Equal
    gaps are allowed; same-channel transitions cost zero and emit no action.
    """
    ordered = sorted(tasks, key=lambda row: (row["start_dt"], row["end_dt"], row["id"]))
    for previous, current in zip(ordered, ordered[1:]):
        if _time_overlap(previous, current):
            return False, 0
    if not ordered:
        return True, 0
    table = config["switch_table"]
    first = ordered[0]
    lead = timedelta(seconds=_switch_seconds(
        table, config["initial_center_hz"], first["center_hz"]))
    if config["ready_at"] + lead > first["start_dt"]:
        return False, 0
    switches = 0 if config["initial_center_hz"] == first["center_hz"] else 1
    for previous, current in zip(ordered, ordered[1:]):
        gap = current["start_dt"] - previous["end_dt"]
        needed = timedelta(seconds=_switch_seconds(
            table, previous["center_hz"], current["center_hz"]))
        if gap < needed:
            return False, 0
        if previous["center_hz"] != current["center_hz"]:
            switches += 1
    return True, switches


# ---------------------------------------------------------------------------
# exhaustive optimization
# ---------------------------------------------------------------------------
def _option_task(request, center):
    task = {key: request[key] for key in (
        "id", "station_id", "receiver_id", "bandwidth_hz",
        "start_dt", "end_dt", "source")}
    task["center_hz"] = center
    task["request_id"] = request["id"]
    task["low_twice_hz"], task["high_twice_hz"] = _task_band(task)
    return task


def _vector_key(vector):
    # Any integer sorts before null; integers compare numerically.
    return tuple((1,) if value is None else (0, value) for value in vector)


class Optimizer:
    def __init__(self, parsed):
        self.stations = parsed["stations"]
        self.centers = parsed["centers"]
        self.fixed = parsed["fixed"]
        self.requests = sorted(parsed["requests"], key=lambda row: row["id"])
        self.receivers = parsed["receivers"]
        self.radio = RadioGraph(self.stations)
        self.cap = MAX_PLAN - len(self.fixed)
        self.best = None  # (utility, switches, vector)
        self._prepare()

    def _prepare(self):
        for task in self.fixed:
            task["low_twice_hz"], task["high_twice_hz"] = _task_band(task)
        self.fixed_by_receiver = {}
        for task in self.fixed:
            self.fixed_by_receiver.setdefault(task["receiver_id"], []).append(task)
        # Switch counts induced by fixed tasks alone, including the initial
        # tuning to each receiver's first fixed reception.  They must survive
        # backtracking to a state with no selected requests on that receiver.
        self._baseline_switch_count = {}
        for rid, config in self.receivers.items():
            _, switches = evaluate_receiver(
                config, self.fixed_by_receiver.get(rid, []))
            self._baseline_switch_count[rid] = switches
        # One option per (request index, allowed channel).
        self.options = []
        for index, request in enumerate(self.requests):
            row = []
            for center in request["allowed_centers_hz"]:
                row.append(_option_task(request, center))
            self.options.append(row)
        self.dead = [set() for _ in self.requests]
        self.incompatible = {}
        self._precompute_radio()
        self._receiver_lists = {rid: [] for rid in self.receivers}
        self._receiver_switch_count = {rid: 0 for rid in self.receivers}
        self._utility_upper = []
        running = 0
        for request in reversed(self.requests):
            running += request["utility"]
            self._utility_upper.append(running)
        self._utility_upper.reverse()

    def _precompute_radio(self):
        for index, options in enumerate(self.options):
            for option in options:
                key = (index, option["center_hz"])
                blocked = set()
                for fixed_task in self.fixed:
                    if self.radio.incompatible(option, fixed_task):
                        blocked.add(("fixed", fixed_task["id"]))
                    if option["receiver_id"] == fixed_task["receiver_id"] \
                            and _time_overlap(option, fixed_task):
                        blocked.add(("overlap", fixed_task["id"]))
                if blocked:
                    self.dead[index].add(option["center_hz"])
                self.incompatible[key] = set()
        for i in range(len(self.requests)):
            for j in range(i + 1, len(self.requests)):
                for option_a in self.options[i]:
                    for option_b in self.options[j]:
                        clash = self.radio.incompatible(option_a, option_b)
                        if not clash and option_a["receiver_id"] == \
                                option_b["receiver_id"] \
                                and _time_overlap(option_a, option_b):
                            clash = True
                        if clash:
                            self.incompatible[(i, option_a["center_hz"])].add(
                                (j, option_b["center_hz"]))
                            self.incompatible[(j, option_b["center_hz"])].add(
                                (i, option_a["center_hz"]))

    def baseline_feasible(self):
        """Fixed transmissions alone must already work on every receiver."""
        for rid, config in self.receivers.items():
            ok, _ = evaluate_receiver(
                config, self.fixed_by_receiver.get(rid, []))
            if not ok:
                return False
        return True

    def run(self):
        if not self.baseline_feasible() or self.cap < sum(
                1 for row in self.requests if row["required"]):
            return None
        self._receiver_switch_count = dict(self._baseline_switch_count)
        self._dfs(0, [], 0, 0, set())
        if self.best is None:
            return None
        return {
            "vector": list(self.best[2]),
            "utility": self.best[0],
            "switch_count": self.best[1],
        }

    def _dfs(self, index, vector, utility, selected_count, selected_keys):
        if index == len(self.requests):
            switches = sum(self._receiver_switch_count.values())
            candidate = (utility, switches, tuple(vector))
            if self.best is None or self._better(candidate, self.best):
                self.best = candidate
            return
        request = self.requests[index]
        upper = utility + self._utility_upper[index]
        # Sound bound for objective 1: if even selecting every remaining
        # request cannot reach the incumbent utility, the subtree is hopeless.
        if self.best is not None and upper < self.best[0]:
            return
        partial_switches = sum(self._receiver_switch_count.values())
        if self.best is not None and upper == self.best[0]:
            # Tying the utility requires selecting EVERY remaining request
            # (all utilities are positive).  Adding receptions can never
            # decrease a receiver's switch count, so the partial count is a
            # lower bound on every completion's objective-2 value.
            if partial_switches > self.best[1]:
                return  # objective 2 lost regardless of the vector
            if partial_switches == self.best[1] \
                    and self._prefix_greater(vector):
                # Objective 2 can at best tie; the vector already loses.
                return
        # Branch-level vector bound, only when objective 2 cannot be won
        # outright (partial count already equals the incumbent's count).
        incumbent_here = None
        if self.best is not None and upper == self.best[0] \
                and partial_switches == self.best[1] \
                and self._prefix_equal(vector):
            incumbent_here = self.best[2][index]
        branches = list(request["allowed_centers_hz"])
        if not request["required"]:
            branches.append(None)  # null sorts after every integer
        for center in branches:
            if incumbent_here is not None and (
                    center is None or center > incumbent_here):
                # Remaining branches are lexicographically larger at this slot.
                break
            if center is None:
                self._dfs(index + 1, vector + [None], utility,
                          selected_count, selected_keys)
                continue
            if center in self.dead[index]:
                continue
            option_key = (index, center)
            if option_key in selected_keys or any(
                    other in selected_keys
                    for other in self.incompatible[option_key]):
                continue
            if selected_count + 1 > self.cap:
                continue
            option = self.options[index][
                request["allowed_centers_hz"].index(center)]
            if not self._extend(option):
                continue
            selected_keys.add(option_key)
            self._dfs(index + 1, vector + [center],
                      utility + request["utility"],
                      selected_count + 1, selected_keys)
            selected_keys.discard(option_key)
            self._rollback(option)

    def _prefix_greater(self, vector):
        for pos, value in enumerate(vector):
            incumbent = self.best[2][pos]
            if value == incumbent:
                continue
            if value is None:
                return True
            if incumbent is None or value > incumbent:
                return True
            return False
        return False

    def _prefix_equal(self, vector):
        return all(value == self.best[2][pos]
                   for pos, value in enumerate(vector))

    @staticmethod
    def _better(candidate, best):
        if candidate[0] != best[0]:
            return candidate[0] > best[0]
        if candidate[1] != best[1]:
            return candidate[1] < best[1]
        return _vector_key(candidate[2]) < _vector_key(best[2])

    def _extend(self, option):
        rid = option["receiver_id"]
        tasks = self.fixed_by_receiver.get(rid, []) + self._receiver_lists[rid] \
            + [option]
        ok, switches = evaluate_receiver(self.receivers[rid], tasks)
        if not ok:
            return False
        self._receiver_switch_count[rid] = switches
        self._receiver_lists[rid].append(option)
        return True

    def _rollback(self, option):
        rid = option["receiver_id"]
        self._receiver_lists[rid].remove(option)
        if not self._receiver_lists[rid]:
            self._receiver_switch_count[rid] = self._baseline_switch_count[rid]
            return
        tasks = self.fixed_by_receiver.get(rid, []) + self._receiver_lists[rid]
        _, switches = evaluate_receiver(self.receivers[rid], tasks)
        self._receiver_switch_count[rid] = switches


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------
def _utc(value):
    return value.isoformat()


def _transmission_record(task):
    return {
        "id": task["id"],
        "station_id": task["station_id"],
        "receiver_id": task["receiver_id"],
        "center_hz": task["center_hz"],
        "bandwidth_hz": task["bandwidth_hz"],
        "start": _utc(task["start_dt"]),
        "end": _utc(task["end_dt"]),
    }


def _build_plan(stations, fixed, requests, vector):
    transmissions = [_transmission_record(task) for task in fixed]
    for request, center in zip(requests, vector):
        if center is not None:
            task = _option_task(request, center)
            transmissions.append(_transmission_record(task))
    plan = {
        "stations": [
            {
                "id": sid,
                "lat": stations[sid]["lat"],
                "lon": stations[sid]["lon"],
                "protection_radius_m": stations[sid]["protection_radius_m"],
            }
            for sid in sorted(stations)
        ],
        "transmissions": sorted(transmissions, key=lambda row: row["id"]),
    }
    return plan


def _build_schedules(parsed, selected_tasks_by_id):
    schedules = []
    for rid in sorted(parsed["receivers"]):
        config = parsed["receivers"][rid]
        tasks = [task for task in parsed["fixed"]
                 if task["receiver_id"] == rid]
        tasks.extend(task for task in selected_tasks_by_id.values()
                     if task["receiver_id"] == rid)
        tasks.sort(key=lambda row: (row["start_dt"], row["end_dt"], row["id"]))
        timeline = []
        current = config["initial_center_hz"]
        for task in tasks:
            if task["center_hz"] != current:
                seconds = config["switch_table"][(current, task["center_hz"])]
                end_dt = task["start_dt"]
                start_dt = end_dt - timedelta(seconds=seconds)
                timeline.append({
                    "kind": "switch",
                    "receiver_id": rid,
                    "from_hz": current,
                    "to_hz": task["center_hz"],
                    "seconds": seconds,
                    "start": _utc(start_dt),
                    "end": _utc(end_dt),
                })
                current = task["center_hz"]
            timeline.append({
                "kind": "reception",
                "transmission_id": task["id"],
                "source": task["source"],
                "station_id": task["station_id"],
                "receiver_id": rid,
                "center_hz": task["center_hz"],
                "bandwidth_hz": task["bandwidth_hz"],
                "start": _utc(task["start_dt"]),
                "end": _utc(task["end_dt"]),
            })
        schedules.append({
            "receiver_id": rid,
            "ready_at": _utc(config["ready_at"]),
            "initial_center_hz": config["initial_center_hz"],
            "timeline": timeline,
        })
    return schedules


def _request_entries(requests, vector):
    entries = []
    for request, center in zip(requests, vector):
        entries.append({
            "request_id": request["id"],
            "selected": center is not None,
            "center_hz": center,
            "required": request["required"],
            "utility": request["utility"],
            "allowed_centers_hz": list(request["allowed_centers_hz"]),
        })
    return entries


def _fixed_only_plan(parsed):
    return _build_plan(parsed["stations"], parsed["fixed"], [], [])


def _infeasible_report(parsed):
    fixed_plan = _fixed_only_plan(parsed)
    fixed_report = service.run(fixed_plan)
    requests = sorted(parsed["requests"], key=lambda row: row["id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "status": "infeasible",
        "status_detail": (
            "no feasible plan covers every fixed transmission and every "
            "required request under the receiver occupation/tuning rules and "
            "the directed radio conflict rules"
        ),
        "objective": None,
        "requests": [
            {
                "request_id": row["id"],
                "selected": False,
                "center_hz": None,
                "required": row["required"],
                "utility": row["utility"],
                "allowed_centers_hz": list(row["allowed_centers_hz"]),
            }
            for row in requests
        ],
        "plan": None,
        "conflict_report": fixed_report,
        "receiver_schedules": None,
        "summary": {
            "status": "infeasible",
            "stations": len(parsed["stations"]),
            "fixed_transmissions": len(parsed["fixed"]),
            "requests_total": len(requests),
            "requests_selected": 0,
            "required_total": sum(1 for row in requests if row["required"]),
            "plan_transmissions": None,
            "selected_utility": None,
            "total_switch_count": None,
            "retained_fixed_conflicts": fixed_report["summary"]["directed_conflicts"],
        },
    }


def arrange(data):
    """Validate, optimize and build the deterministic arrangement report."""
    parsed = parse_input(data)
    requests = sorted(parsed["requests"], key=lambda row: row["id"])
    optimizer = Optimizer(parsed)
    outcome = optimizer.run()
    if outcome is None:
        return _infeasible_report(parsed)
    vector = outcome["vector"]
    plan = _build_plan(parsed["stations"], parsed["fixed"], requests, vector)
    conflict_report = service.run(plan)
    # Invariant guaranteed by the optimizer: every retained directed conflict
    # has fixed transmissions on both ends.
    fixed_ids = {row["id"] for row in parsed["fixed"]}
    for row in conflict_report["conflicts"]:
        if row["victim_transmission_id"] not in fixed_ids or \
                row["interferer_transmission_id"] not in fixed_ids:
            raise AssertionError("optimizer admitted a request-related conflict")
    selected_tasks = {}
    for request, center in zip(requests, vector):
        if center is not None:
            selected_tasks[request["id"]] = _option_task(request, center)
    schedules = _build_schedules(parsed, selected_tasks)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "status": "optimal",
        "status_detail": "global optimum under the declared lexicographic objectives",
        "objective": {
            "selected_utility": outcome["utility"],
            "total_switch_count": outcome["switch_count"],
            "assignment_vector": list(vector),
        },
        "requests": _request_entries(requests, vector),
        "plan": plan,
        "conflict_report": conflict_report,
        "receiver_schedules": schedules,
        "summary": {
            "status": "optimal",
            "stations": len(parsed["stations"]),
            "fixed_transmissions": len(parsed["fixed"]),
            "requests_total": len(requests),
            "requests_selected": sum(1 for value in vector if value is not None),
            "required_total": sum(1 for row in requests if row["required"]),
            "plan_transmissions": len(plan["transmissions"]),
            "selected_utility": outcome["utility"],
            "total_switch_count": outcome["switch_count"],
            "retained_fixed_conflicts": conflict_report["summary"]["directed_conflicts"],
        },
    }
