"""Recompute and correct receive-protection daily reports from station facts.

The original :mod:`spectrum.service` report assumes one fixed coordinate and
protection radius per station.  During an activity temporary stations move and
protection radii are re-surveyed, and the surveying and duty teams file facts
independently.  This module builds *two* publication views of the same plan:

* the **published** view: station facts known at the original publication
  cut-off (``published_as_of``);
* the **revised** view: station facts known at the recomputation cut-off
  (``revised_as_of``).

Both views reuse the declared protection-circle relation from
:mod:`spectrum.interference` unchanged (great-circle distance, radius
inclusive, exact half-Hz band).  Missing or disputed station facts never fall
back to the coordinates in the plan: such a directed pair is simply
``undetermined`` for that interval.

Everything is event based.  A station timeline only changes when the most
authoritative layer of effective declarations changes, so a chatty
low-priority source that never changes the effective layer costs O(1) sweeps,
and pairs whose times or bands do not overlap are never examined.  Nothing is
expanded per second or microsecond.

Public entry point: :func:`build_protection_report`.
"""
import bisect
import heapq
import math
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from . import model
from .interference import distance_m

# ---------------------------------------------------------------------------
# contract constants
# ---------------------------------------------------------------------------
ROOT_KEYS = frozenset((
    "window_start", "window_end", "published_as_of", "revised_as_of",
    "sources", "events",
))
SOURCE_KEYS = frozenset(("id", "priority"))
EVENT_KEYS = frozenset((
    "source_id", "station_id", "fact_id", "known_at", "op",
))
ASSERT_KEYS = EVENT_KEYS | frozenset((
    "effective_start", "effective_end", "lat", "lon", "protection_radius_m",
))

MAX_EVENTS = 20_000
MAX_WINDOW = timedelta(days=7)

SCHEMA_VERSION = 1
TOOL = "radio-protection"
MODEL = "declared_receiver_protection_circle"
MODE = "station_facts_review"

_FRACTION_RE = re.compile(r"[.,](\d+)")


# ---------------------------------------------------------------------------
# strict parsing helpers
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


def _int(value, label, minimum=0):
    # JSON booleans are ints in Python; the contract rejects them.
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _coordinate(value, label, bound):
    if type(value) not in (int, float) or not math.isfinite(value) \
            or not -bound <= value <= bound:
        raise ValueError(f"invalid coordinate {label}")
    return float(value)


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


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _microseconds(delta):
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 \
        + delta.microseconds


# ---------------------------------------------------------------------------
# station-facts document
# ---------------------------------------------------------------------------
def _parse_sources(raw):
    if not isinstance(raw, list):
        raise ValueError("sources must be an array")
    sources = {}
    for index, row in enumerate(raw):
        label = f"sources[{index}]"
        _object(row, label)
        _exact_keys(row, SOURCE_KEYS, label)
        source_id = _text(row["id"], f"{label}.id")
        if source_id in sources:
            raise ValueError(f"duplicate source id: {source_id}")
        sources[source_id] = _int(row["priority"], f"{label}.priority", 0)
    return sources


def _parse_event(index, row, sources, station_ids):
    label = f"events[{index}]"
    _object(row, label)
    op = row.get("op")
    if op == "assert":
        _exact_keys(row, ASSERT_KEYS, label)
    elif op == "withdraw":
        _exact_keys(row, EVENT_KEYS, label)
    else:
        raise ValueError(f"{label}.op must be 'assert' or 'withdraw'")
    source_id = _text(row["source_id"], f"{label}.source_id")
    if source_id not in sources:
        raise ValueError(f"{label}.source_id does not reference a source")
    station_id = _text(row["station_id"], f"{label}.station_id")
    if station_id not in station_ids:
        raise ValueError(f"{label}.station_id does not reference a plan station")
    fact_id = _text(row["fact_id"], f"{label}.fact_id")
    known_at = _time(row["known_at"], f"{label}.known_at")
    event = {
        "source_id": source_id,
        "station_id": station_id,
        "fact_id": fact_id,
        "known_at_dt": known_at,
        "known_at": _iso(known_at),
        "op": op,
    }
    if op == "assert":
        effective_start = _time(
            row["effective_start"], f"{label}.effective_start")
        effective_end = _time(row["effective_end"], f"{label}.effective_end")
        if effective_end <= effective_start:
            raise ValueError(f"{label} effective interval must have positive length")
        event.update({
            "effective_start_dt": effective_start,
            "effective_end_dt": effective_end,
            "lat": _coordinate(row["lat"], f"{label}.lat", 90),
            "lon": _coordinate(row["lon"], f"{label}.lon", 180),
            "protection_radius_m": _int(
                row["protection_radius_m"],
                f"{label}.protection_radius_m", 1),
        })
    return event


def parse_station_facts(data, station_ids):
    """Validate the complete station-facts document.

    Every event is validated, including events later than the recomputation
    cut-off.  Contract violations raise :class:`ValueError`.
    """
    _object(data, "station facts root")
    _exact_keys(data, ROOT_KEYS, "station facts root")

    window_start = _time(data["window_start"], "window_start")
    window_end = _time(data["window_end"], "window_end")
    span = window_end - window_start
    if not timedelta(0) < span <= MAX_WINDOW:
        raise ValueError("window must have positive length and be <= 7 days")

    published_as_of = _time(data["published_as_of"], "published_as_of")
    revised_as_of = _time(data["revised_as_of"], "revised_as_of")
    if published_as_of >= revised_as_of:
        raise ValueError("published_as_of must be earlier than revised_as_of")

    sources = _parse_sources(data["sources"])

    raw_events = data["events"]
    if not isinstance(raw_events, list):
        raise ValueError("events must be an array")
    if len(raw_events) > MAX_EVENTS:
        raise ValueError(f"events must contain at most {MAX_EVENTS} entries")

    events = []
    seen = set()
    for index, row in enumerate(raw_events):
        event = _parse_event(index, row, sources, station_ids)
        key = (event["source_id"], event["station_id"], event["fact_id"])
        version_key = (key, event["known_at_dt"])
        if version_key in seen:
            raise ValueError(
                "a fact may have at most one version at the same UTC known_at")
        seen.add(version_key)
        events.append(event)

    facts = {}
    for event in events:
        key = (event["source_id"], event["station_id"], event["fact_id"])
        facts.setdefault(key, []).append(event)

    for key, versions in facts.items():
        versions.sort(key=lambda item: item["known_at_dt"])
        declared = False
        withdrawn = False
        for version in versions:
            if version["op"] == "assert":
                declared = True
                withdrawn = False
            else:
                if not declared or withdrawn:
                    raise ValueError(
                        "a fact must start with an assertion and may only be "
                        "withdrawn while it is still declared")
                withdrawn = True

    return {
        "window_start_dt": window_start,
        "window_end_dt": window_end,
        "window": {"start": _iso(window_start), "end": _iso(window_end)},
        "published_as_of_dt": published_as_of,
        "revised_as_of_dt": revised_as_of,
        "published_as_of": _iso(published_as_of),
        "revised_as_of": _iso(revised_as_of),
        "sources": sources,
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# one publication view: effective declarations and station timelines
# ---------------------------------------------------------------------------
def _latest_version(versions, cutoff):
    """Last version with known_at <= cutoff, or None."""
    index = bisect.bisect_right(
        [item["known_at_dt"] for item in versions], cutoff)
    return versions[index - 1] if index else None


def _declarations(facts, sources, cutoff, window_start, window_end):
    """Latest assertion per fact as-of ``cutoff``, clipped to the window."""
    by_station = {}
    for (source_id, station_id, fact_id), versions in facts.items():
        version = _latest_version(versions, cutoff)
        if version is None or version["op"] != "assert":
            continue
        start = max(version["effective_start_dt"], window_start)
        end = min(version["effective_end_dt"], window_end)
        if start >= end:
            continue
        value = (version["lat"], version["lon"],
                 version["protection_radius_m"])
        by_station.setdefault(station_id, []).append({
            "source_id": source_id,
            "station_id": station_id,
            "fact_id": fact_id,
            "known_at": version["known_at"],
            "priority": sources[source_id],
            "start_dt": start,
            "end_dt": end,
            "value": value,
        })
    return by_station


def _build_value(declarations, priority_values):
    """Values, evidence and status for the most authoritative layer."""
    values = tuple(sorted(priority_values))
    evidence = []
    for value, indices in priority_values.items():
        for index in indices:
            declaration = declarations[index]
            evidence.append((
                declaration["source_id"],
                declaration["fact_id"],
                declaration["known_at"],
                declaration["priority"],
                value,
            ))
    evidence.sort(key=lambda item: (item[0], item[1], item[2]))
    status = "resolved" if len(values) == 1 else "disputed"
    evidence = tuple(evidence)
    return status, values, evidence, (status, values, evidence)


def _station_timeline(declarations, window_start, window_end):
    """Contiguous timeline over the whole window for one station."""
    if not declarations:
        return [{
            "start_dt": window_start,
            "end_dt": window_end,
            "status": "missing",
            "values": (),
            "evidence": (),
        }]

    starts = {}
    ends = {}
    boundaries = {window_start, window_end}
    for index, declaration in enumerate(declarations):
        start, end = declaration["start_dt"], declaration["end_dt"]
        starts.setdefault(start, []).append(index)
        ends.setdefault(end, []).append(index)
        boundaries.add(start)
        boundaries.add(end)

    priority_counts = {}
    priority_values = {}
    priority_revision = {}
    heap = []

    def add(index):
        declaration = declarations[index]
        priority = declaration["priority"]
        value = declaration["value"]
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
        layer = priority_values.setdefault(priority, {})
        layer.setdefault(value, set()).add(index)
        priority_revision[priority] = priority_revision.get(priority, 0) + 1
        heapq.heappush(heap, priority)

    def remove(index):
        declaration = declarations[index]
        priority = declaration["priority"]
        value = declaration["value"]
        priority_counts[priority] -= 1
        layer = priority_values[priority]
        layer[value].discard(index)
        if not layer[value]:
            del layer[value]
        priority_revision[priority] = priority_revision.get(priority, 0) + 1

    def minimum_priority():
        while heap and priority_counts.get(heap[0], 0) <= 0:
            heapq.heappop(heap)
        return heap[0] if heap else None

    result = []
    last_cache_id = None
    last_key = None

    ordered = sorted(boundaries)
    for position, point in enumerate(ordered):
        for index in ends.get(point, ()):  # ending at point: already inactive
            remove(index)
        for index in starts.get(point, ()):  # starting at point: now active
            add(index)

        following = ordered[position + 1] if position + 1 < len(ordered) else None
        if following is None or point >= following:
            continue

        priority = minimum_priority()
        if priority is None:
            cache_id = ("missing",)
        else:
            cache_id = (priority, priority_revision.get(priority, 0))

        if cache_id != last_cache_id:
            if priority is None:
                key = ("missing", (), (), ("missing", (), ()))
            else:
                key = _build_value(declarations, priority_values[priority])
            last_cache_id = cache_id
            last_key = key
        key = last_key
        status, values, evidence, _comparison = key

        if result and result[-1]["end_dt"] == point \
                and result[-1]["_key"] == _comparison:
            result[-1]["end_dt"] = following
        else:
            result.append({
                "start_dt": point,
                "end_dt": following,
                "status": status,
                "values": values,
                "evidence": evidence,
                "_key": _comparison,
            })

    return result


def _timeline_output(segments):
    output = []
    for segment in segments:
        output.append({
            "start": _iso(segment["start_dt"]),
            "end": _iso(segment["end_dt"]),
            "status": segment["status"],
            "values": [
                {"lat": value[0], "lon": value[1],
                 "protection_radius_m": value[2]}
                for value in segment["values"]
            ],
            "evidence": [
                {
                    "source_id": item[0],
                    "fact_id": item[1],
                    "known_at": item[2],
                    "priority": item[3],
                    "lat": item[4][0],
                    "lon": item[4][1],
                    "protection_radius_m": item[4][2],
                }
                for item in segment["evidence"]
            ],
        })
    return output


# ---------------------------------------------------------------------------
# directed protection relations for one view
# ---------------------------------------------------------------------------
def _locate(segments, starts, moment):
    index = bisect.bisect_right(starts, moment) - 1
    if index < 0:
        index = 0
    return index


def _pair_segments(receiver_timeline, receiver_index,
                   transmitter_timeline, transmitter_index, start, end):
    """Yield (a, b, receiver_segment, transmitter_segment) over [start, end)."""
    index = receiver_index
    other = transmitter_index
    while index < len(receiver_timeline) and other < len(transmitter_timeline):
        receiver = receiver_timeline[index]
        transmitter = transmitter_timeline[other]
        # Stop as soon as both timelines lie beyond the pair interval, so a
        # station change outside the candidate pair costs nothing here.
        if receiver["start_dt"] >= end or transmitter["start_dt"] >= end:
            break
        a = max(start, receiver["start_dt"], transmitter["start_dt"])
        b = min(end, receiver["end_dt"], transmitter["end_dt"])
        if a < b:
            yield a, b, receiver, transmitter
        if receiver["end_dt"] <= transmitter["end_dt"]:
            index += 1
        else:
            other += 1


def _point(segment):
    value = segment["values"][0]
    return {"lat": value[0], "lon": value[1],
            "protection_radius_m": value[2]}


def _append_merged(rows, row, merge_fields):
    if rows and rows[-1]["end_dt"] == row["start_dt"] \
            and all(rows[-1][field] == row[field] for field in merge_fields):
        rows[-1]["end_dt"] = row["end_dt"]
    else:
        rows.append(row)


def _build_view(stations, transmissions, parsed, view):
    timelines = {}
    for station_id in sorted(stations):
        declarations = view["declarations"].get(station_id, [])
        timelines[station_id] = _station_timeline(
            declarations, parsed["window_start_dt"], parsed["window_end_dt"])

    timeline_starts = {
        station_id: [segment["start_dt"] for segment in segments]
        for station_id, segments in timelines.items()
    }

    ordered_ids = sorted(transmissions)
    conflicts = []
    undetermined = []
    per_victim_conflicts = {}
    per_victim_undetermined = {}

    for victim_id in ordered_ids:
        victim = transmissions[victim_id]
        receiver_id = victim["receiver_id"]
        receiver_timeline = timelines[receiver_id]
        receiver_starts = timeline_starts[receiver_id]
        for interferer_id in ordered_ids:
            if victim_id == interferer_id:
                continue
            interferer = transmissions[interferer_id]
            start = max(victim["start_dt"], interferer["start_dt"],
                        parsed["window_start_dt"])
            end = min(victim["end_dt"], interferer["end_dt"],
                      parsed["window_end_dt"])
            if start >= end:
                continue
            low = max(victim["low_twice_hz"], interferer["low_twice_hz"])
            high = min(victim["high_twice_hz"], interferer["high_twice_hz"])
            if low >= high:
                continue

            transmitter_id = interferer["station_id"]
            transmitter_timeline = timelines[transmitter_id]
            transmitter_starts = timeline_starts[transmitter_id]
            # Pair boundaries are the station-timeline boundaries; start/end
            # are inside the window, so both timelines cover them.
            receiver_index = _locate(
                receiver_timeline, receiver_starts, start)
            transmitter_index = _locate(
                transmitter_timeline, transmitter_starts, start)

            iterator = _pair_segments(
                receiver_timeline, receiver_index,
                transmitter_timeline, transmitter_index,
                start, end)
            for a, b, receiver_segment, transmitter_segment in iterator:
                receiver_known = receiver_segment["status"] == "resolved"
                transmitter_known = transmitter_segment["status"] == "resolved"
                if receiver_known and transmitter_known:
                    receiver_point = _point(receiver_segment)
                    transmitter_point = _point(transmitter_segment)
                    separation = distance_m(receiver_point, transmitter_point)
                    if separation <= receiver_point["protection_radius_m"]:
                        row = {
                            "victim_transmission_id": victim_id,
                            "interferer_transmission_id": interferer_id,
                            "receiver_id": receiver_id,
                            "interferer_station_id": transmitter_id,
                            "start_dt": a,
                            "end_dt": b,
                            "overlap_low_hz": str(Fraction(low, 2)),
                            "overlap_high_hz": str(Fraction(high, 2)),
                            "overlap_bandwidth_hz": str(Fraction(high - low, 2)),
                            "separation_m": round(separation, 3),
                        }
                        _append_merged(conflicts, row, (
                            "victim_transmission_id",
                            "interferer_transmission_id",
                            "receiver_id",
                            "interferer_station_id",
                            "overlap_low_hz",
                            "overlap_high_hz",
                            "overlap_bandwidth_hz",
                            "separation_m",
                        ))
                        per_victim_conflicts.setdefault(
                            victim_id, []).append((a, b, interferer_id))
                else:
                    endpoints = []
                    if not receiver_known:
                        endpoints.append({
                            "role": "receiver",
                            "station_id": receiver_id,
                            "status": receiver_segment["status"],
                        })
                    if not transmitter_known:
                        endpoints.append({
                            "role": "interferer",
                            "station_id": transmitter_id,
                            "status": transmitter_segment["status"],
                        })
                    row = {
                        "victim_transmission_id": victim_id,
                        "interferer_transmission_id": interferer_id,
                        "receiver_id": receiver_id,
                        "interferer_station_id": transmitter_id,
                        "start_dt": a,
                        "end_dt": b,
                        "overlap_low_hz": str(Fraction(low, 2)),
                        "overlap_high_hz": str(Fraction(high, 2)),
                        "overlap_bandwidth_hz": str(Fraction(high - low, 2)),
                        "endpoints": endpoints,
                    }
                    _append_merged(undetermined, row, (
                        "victim_transmission_id",
                        "interferer_transmission_id",
                        "receiver_id",
                        "interferer_station_id",
                        "overlap_low_hz",
                        "overlap_high_hz",
                        "overlap_bandwidth_hz",
                        "endpoints",
                    ))
                    per_victim_undetermined.setdefault(
                        victim_id, []).append((a, b, interferer_id))

    reception, internal_reception = _reception_timelines(
        transmissions, parsed, per_victim_conflicts, per_victim_undetermined)

    return {
        "as_of": view["as_of"],
        "stations": [
            {"station_id": station_id,
             "segments": _timeline_output(timelines[station_id])}
            for station_id in sorted(stations)
        ],
        "report": {
            "conflicts": [_row_output(row) for row in conflicts],
            "undetermined": [_row_output(row) for row in undetermined],
            "reception_timelines": reception,
        },
        "_reception": internal_reception,
    }


def _row_output(row):
    output = {
        "victim_transmission_id": row["victim_transmission_id"],
        "interferer_transmission_id": row["interferer_transmission_id"],
        "receiver_id": row["receiver_id"],
        "interferer_station_id": row["interferer_station_id"],
        "start": _iso(row["start_dt"]),
        "end": _iso(row["end_dt"]),
        "overlap_low_hz": row["overlap_low_hz"],
        "overlap_high_hz": row["overlap_high_hz"],
        "overlap_bandwidth_hz": row["overlap_bandwidth_hz"],
    }
    if "separation_m" in row:
        output["separation_m"] = row["separation_m"]
    if "endpoints" in row:
        output["endpoints"] = [dict(item) for item in row["endpoints"]]
    return output


# ---------------------------------------------------------------------------
# per-task receive timelines and duration totals
# ---------------------------------------------------------------------------
def _reception_timelines(transmissions, parsed, conflict_intervals,
                         undetermined_intervals):
    window_start = parsed["window_start_dt"]
    window_end = parsed["window_end_dt"]
    output = []
    internal_map = {}
    for transmission_id in sorted(transmissions):
        task = transmissions[transmission_id]
        start = max(task["start_dt"], window_start)
        end = min(task["end_dt"], window_end)
        durations = {"affected": 0, "undetermined": 0, "clear": 0}
        if start >= end:
            output.append({
                "transmission_id": transmission_id,
                "segments": [],
                "durations_us": durations,
            })
            internal_map[transmission_id] = []
            continue

        determined_starts = {}
        determined_ends = {}
        undetermined_starts = {}
        undetermined_ends = {}
        boundaries = {start, end}

        for a, b, interferer_id in conflict_intervals.get(
                transmission_id, ()):
            determined_starts.setdefault(a, set()).add(interferer_id)
            determined_ends.setdefault(b, set()).add(interferer_id)
            boundaries.add(a)
            boundaries.add(b)
        for a, b, interferer_id in undetermined_intervals.get(
                transmission_id, ()):
            undetermined_starts.setdefault(a, set()).add(interferer_id)
            undetermined_ends.setdefault(b, set()).add(interferer_id)
            boundaries.add(a)
            boundaries.add(b)

        determined = set()
        undetermined = set()
        segments = []
        ordered = sorted(boundaries)
        for position, point in enumerate(ordered):
            for interferer_id in determined_ends.get(point, ()):
                determined.discard(interferer_id)
            for interferer_id in undetermined_ends.get(point, ()):
                undetermined.discard(interferer_id)
            for interferer_id in determined_starts.get(point, ()):
                determined.add(interferer_id)
            for interferer_id in undetermined_starts.get(point, ()):
                undetermined.add(interferer_id)

            following = ordered[position + 1] if position + 1 < len(ordered) else None
            if following is None:
                continue
            status = ("affected" if determined
                      else ("undetermined" if undetermined else "clear"))
            determined_list = tuple(sorted(determined))
            undetermined_list = tuple(sorted(undetermined))
            if segments and segments[-1]["end_dt"] == point \
                    and segments[-1]["status"] == status \
                    and segments[-1]["determined"] == determined_list \
                    and segments[-1]["undetermined"] == undetermined_list:
                segments[-1]["end_dt"] = following
            else:
                segments.append({
                    "start_dt": point,
                    "end_dt": following,
                    "status": status,
                    "determined": determined_list,
                    "undetermined": undetermined_list,
                })
            durations[status] += _microseconds(following - point)

        public_segments = []
        internal = []
        for segment in segments:
            determined_list = list(segment["determined"])
            undetermined_list = list(segment["undetermined"])
            public_segments.append({
                "start": _iso(segment["start_dt"]),
                "end": _iso(segment["end_dt"]),
                "status": segment["status"],
                "interferer_transmission_ids": determined_list,
                "undetermined_interferer_transmission_ids": undetermined_list,
            })
            internal.append({
                "start_dt": segment["start_dt"],
                "end_dt": segment["end_dt"],
                "status": segment["status"],
                "determined": determined_list,
                "undetermined": undetermined_list,
            })
        output.append({
            "transmission_id": transmission_id,
            "segments": public_segments,
            "durations_us": durations,
        })
        internal_map[transmission_id] = internal
    return output, internal_map


# ---------------------------------------------------------------------------
# correction list (published -> revised)
# ---------------------------------------------------------------------------
def _state_key(segment):
    return (segment["status"], tuple(segment["determined"]),
            tuple(segment["undetermined"]))


def _state_value(segment):
    return {
        "status": segment["status"],
        "interferer_transmission_ids": list(segment["determined"]),
        "undetermined_interferer_transmission_ids": list(segment["undetermined"]),
    }


def _corrections(transmissions, parsed, published, revised):
    window_start = parsed["window_start_dt"]
    window_end = parsed["window_end_dt"]
    corrections = []
    for transmission_id in sorted(transmissions):
        task = transmissions[transmission_id]
        start = max(task["start_dt"], window_start)
        end = min(task["end_dt"], window_end)
        if start >= end:
            continue
        before_segments = published["_reception"].get(transmission_id, [])
        after_segments = revised["_reception"].get(transmission_id, [])
        boundaries = {start, end}
        for segment in before_segments:
            if segment["start_dt"] < end and segment["end_dt"] > start:
                boundaries.add(max(segment["start_dt"], start))
                boundaries.add(min(segment["end_dt"], end))
        for segment in after_segments:
            if segment["start_dt"] < end and segment["end_dt"] > start:
                boundaries.add(max(segment["start_dt"], start))
                boundaries.add(min(segment["end_dt"], end))

        ordered = sorted(boundaries)
        before_index = 0
        after_index = 0
        for position, point in enumerate(ordered):
            following = ordered[position + 1] if position + 1 < len(ordered) else None
            if following is None:
                continue
            while before_index < len(before_segments) \
                    and before_segments[before_index]["end_dt"] <= point:
                before_index += 1
            while after_index < len(after_segments) \
                    and after_segments[after_index]["end_dt"] <= point:
                after_index += 1
            before = before_segments[before_index]
            after = after_segments[after_index]
            before_key = _state_key(before)
            after_key = _state_key(after)
            if before_key == after_key:
                continue
            segment = {
                "transmission_id": transmission_id,
                "start_dt": point,
                "end_dt": following,
                "before_key": before_key,
                "after_key": after_key,
                "before": _state_value(before),
                "after": _state_value(after),
            }
            if corrections and corrections[-1]["transmission_id"] == transmission_id \
                    and corrections[-1]["end_dt"] == point \
                    and corrections[-1]["before_key"] == before_key \
                    and corrections[-1]["after_key"] == after_key:
                corrections[-1]["end_dt"] = following
            else:
                corrections.append(segment)
    return corrections


def _correction_output(correction):
    return {
        "transmission_id": correction["transmission_id"],
        "start": _iso(correction["start_dt"]),
        "end": _iso(correction["end_dt"]),
        "before": correction["before"],
        "after": correction["after"],
    }


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def build_protection_report(plan, station_facts):
    """Validate both documents and build the two views plus corrections.

    ``plan`` follows the original ``stations`` / ``transmissions`` contract;
    its station IDs define the identity list but its coordinates and radii are
    never used for protection conclusions.  ``station_facts`` follows the
    strict station-facts contract.  Raises :class:`ValueError` on any contract
    violation; missing/disputed facts and an empty correction list are valid
    business results.
    """
    stations, transmissions = model.validate(plan)
    parsed = parse_station_facts(station_facts, set(stations))

    views = {}
    for label, cutoff_key, as_of_key in (
            ("published", "published_as_of_dt", "published_as_of"),
            ("revised", "revised_as_of_dt", "revised_as_of")):
        declarations = _declarations(
            parsed["facts"], parsed["sources"], parsed[cutoff_key],
            parsed["window_start_dt"], parsed["window_end_dt"])
        view = {
            "as_of": parsed[as_of_key],
            "declarations": declarations,
        }
        views[label] = _build_view(
            stations, transmissions, parsed, view)

    corrections = _corrections(
        transmissions, parsed, views["published"], views["revised"])

    published_conflicts = len(views["published"]["report"]["conflicts"])
    published_undetermined = len(views["published"]["report"]["undetermined"])
    revised_conflicts = len(views["revised"]["report"]["conflicts"])
    revised_undetermined = len(views["revised"]["report"]["undetermined"])

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "model": MODEL,
        "mode": MODE,
        "window": parsed["window"],
        "published_as_of": parsed["published_as_of"],
        "revised_as_of": parsed["revised_as_of"],
        "views": {
            "published": {
                "as_of": views["published"]["as_of"],
                "stations": views["published"]["stations"],
                "report": views["published"]["report"],
            },
            "revised": {
                "as_of": views["revised"]["as_of"],
                "stations": views["revised"]["stations"],
                "report": views["revised"]["report"],
            },
        },
        "corrections": [
            _correction_output(correction) for correction in corrections
        ],
        "summary": {
            "stations": len(stations),
            "transmissions": len(transmissions),
            "published_directed_conflicts": published_conflicts,
            "published_undetermined": published_undetermined,
            "revised_directed_conflicts": revised_conflicts,
            "revised_undetermined": revised_undetermined,
            "corrections": len(corrections),
        },
    }
