"""Tests for the offline channel arranger ``radio-arrange``.

Two layers:

* hand-computed scenarios with independent expected values (including every
  scenario mandated by the task statement);
* an independent brute-force oracle that enumerates *all* assignments itself,
  using only the raw contract rules, and checks the optimizer's lexicographic
  winner on many generated instances.

The oracle deliberately does not import any optimizer helper: it re-derives
radio conflicts with :mod:`spectrum.interference` and re-implements the
receiver/switch rules from the contract text.
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spectrum.arrange import (
    MAX_ALLOWED,
    MAX_CENTERS,
    MAX_FIXED,
    MAX_PLAN,
    MAX_REQUESTS,
    arrange,
    parse_input,
)
from spectrum.interference import conflicts, distance_m

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# ---------------------------------------------------------------------------
# small builders
# ---------------------------------------------------------------------------
T = "2026-09-01T{h:02d}:{m:02d}:00+00:00"


def at(hour, minute=0):
    return T.format(h=hour, m=minute)


def station(sid, lat=0.0, lon=0.0, radius=1000):
    return {"id": sid, "lat": lat, "lon": lon, "protection_radius_m": radius}


def emission(tid, transmitter, receiver, center, bandwidth=10,
             start=at(10, 0), end=at(11, 0)):
    return {
        "id": tid, "station_id": transmitter, "receiver_id": receiver,
        "center_hz": center, "bandwidth_hz": bandwidth,
        "start": start, "end": end,
    }


def request(rid, transmitter, receiver, allowed, required=False, utility=100,
            bandwidth=10, start=at(10, 0), end=at(11, 0)):
    return {
        "id": rid, "station_id": transmitter, "receiver_id": receiver,
        "bandwidth_hz": bandwidth, "start": start, "end": end,
        "allowed_centers_hz": list(allowed), "required": required,
        "utility": utility,
    }


def switch_row(a, b, seconds):
    return {"from_hz": a, "to_hz": b, "seconds": seconds}


def full_switch_table(centers, seconds=600, overrides=None):
    overrides = overrides or {}
    rows = []
    for a in centers:
        for b in centers:
            if a == b:
                continue
            rows.append(switch_row(a, b, overrides.get((a, b), seconds)))
    return rows


def receiver_config(rid, centers, initial, ready=at(9, 0), seconds=600,
                    overrides=None):
    return {
        "id": rid, "ready_at": ready, "initial_center_hz": initial,
        "switches": full_switch_table(centers, seconds, overrides),
    }


def make_doc(stations, centers, requests_=(), fixed=(), receivers=()):
    used = {row["receiver_id"] for row in fixed}
    used.update(row["receiver_id"] for row in requests_)
    if not receivers:
        receivers = [receiver_config(rid, centers, centers[0])
                     for rid in sorted(used)]
    return {
        "stations": list(stations),
        "fixed_transmissions": list(fixed),
        "centers_hz": list(centers),
        "requests": list(requests_),
        "receivers": receivers,
    }


# ---------------------------------------------------------------------------
# independent brute-force oracle
# ---------------------------------------------------------------------------
class Oracle:
    """Enumerates every assignment from the contract rules only."""

    def __init__(self, doc):
        self.doc = doc
        self.stations = {row["id"]: row for row in doc["stations"]}
        self.centers = doc["centers_hz"]
        self.fixed = doc["fixed_transmissions"]
        self.requests = sorted(doc["requests"], key=lambda row: row["id"])
        self.configs = {row["id"]: row for row in doc["receivers"]}
        self.tables = {}
        for cfg in doc["receivers"]:
            self.tables[cfg["id"]] = {
                (row["from_hz"], row["to_hz"]): row["seconds"]
                for row in cfg["switches"]
            }

    def _dt(self, value):
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    def _all_transmissions(self, vector):
        rows = [dict(row) for row in self.fixed]
        for req, center in zip(self.requests, vector):
            if center is not None:
                rows.append(emission(
                    req["id"], req["station_id"], req["receiver_id"], center,
                    req["bandwidth_hz"], req["start"], req["end"]))
        return rows

    def _radio_ok(self, transmissions):
        """No directed conflict may touch a selected request."""
        fixed_ids = {row["id"] for row in self.fixed}
        rows = conflicts(self.stations, self._as_validated(transmissions))
        for row in rows:
            if row["victim_transmission_id"] not in fixed_ids or \
                    row["interferer_transmission_id"] not in fixed_ids:
                return False
        return True

    def _as_validated(self, transmissions):
        parsed = {}
        for row in transmissions:
            start = self._dt(row["start"])
            end = self._dt(row["end"])
            parsed[row["id"]] = {
                **row,
                "start_dt": start,
                "end_dt": end,
                "low_twice_hz": 2 * row["center_hz"] - row["bandwidth_hz"],
                "high_twice_hz": 2 * row["center_hz"] + row["bandwidth_hz"],
            }
        return parsed

    def _receiver_ok_and_switches(self, transmissions):
        total_switches = 0
        by_receiver = {}
        for row in transmissions:
            by_receiver.setdefault(row["receiver_id"], []).append(row)
        for rid, rows in by_receiver.items():
            cfg = self.configs[rid]
            table = self.tables[rid]
            ordered = sorted(
                rows,
                key=lambda row: (self._dt(row["start"]),
                                 self._dt(row["end"]), row["id"]))
            for prev, cur in zip(ordered, ordered[1:]):
                if self._dt(prev["start"]) < self._dt(cur["end"]) and \
                        self._dt(cur["start"]) < self._dt(prev["end"]):
                    return None
            # A receiver with no reception emits no switch at all.
            if not ordered:
                continue
            first = ordered[0]
            lead = table.get((cfg["initial_center_hz"], first["center_hz"]), 0)
            if self._dt(cfg["ready_at"]) + timedelta(seconds=lead) > \
                    self._dt(first["start"]):
                return None
            if cfg["initial_center_hz"] != first["center_hz"]:
                total_switches += 1
            for prev, cur in zip(ordered, ordered[1:]):
                gap = self._dt(cur["start"]) - self._dt(prev["end"])
                need = table.get((prev["center_hz"], cur["center_hz"]), 0)
                if gap < timedelta(seconds=need):
                    return None
                if prev["center_hz"] != cur["center_hz"]:
                    total_switches += 1
        return total_switches

    def solve(self):
        domains = []
        for req in self.requests:
            choices = list(req["allowed_centers_hz"])
            if not req["required"]:
                choices.append(None)
            domains.append(choices)
        best = None
        for vector in itertools.product(*domains):
            if len(self.fixed) + sum(v is not None for v in vector) > MAX_PLAN:
                continue
            transmissions = self._all_transmissions(list(vector))
            if not self._radio_ok(transmissions):
                continue
            switches = self._receiver_ok_and_switches(transmissions)
            if switches is None:
                continue
            utility = sum(
                req["utility"] for req, value in
                zip(self.requests, vector) if value is not None)
            # Contract ordering: maximize utility, minimize switches, then
            # minimize the vector (any integer < null; integers numeric).
            key = (utility, -switches,
                   tuple((1,) if v is None else (0, -v) for v in vector))
            if best is None or key > best[0]:
                best = (key, list(vector), utility, switches)
        if best is None:
            return None
        return {"vector": best[1], "utility": best[2],
                "switch_count": best[3]}


# ---------------------------------------------------------------------------
# mandated A/B/C scenarios
# ---------------------------------------------------------------------------
ABC_STATIONS = [
    station("R", 0.0, 0.0, 1000),
    station("TA", 0.0, 0.02, 1),
    station("TB", 0.1, 0.02, 1),
    station("TC", 0.2, 0.02, 1),
    station("TI", 0.0, 0.0005, 1),
    station("U", 10.0, 10.0, 1000),
]
ABC_CENTERS = [100, 200, 300]
ABC_OVERRIDES = {(100, 200): 600, (200, 300): 600, (100, 300): 3600}


def abc_requests():
    return [
        request("A", "TA", "R", [100], required=True, utility=100,
                start=at(10, 0), end=at(10, 10)),
        request("B", "TB", "R", [200], required=False, utility=50,
                start=at(10, 20), end=at(10, 30)),
        request("C", "TC", "R", [300], required=True, utility=100,
                start=at(10, 40), end=at(10, 50)),
    ]


def abc_doc(direct_100_300=3600, with_interferer=False):
    overrides = dict(ABC_OVERRIDES)
    overrides[(100, 300)] = direct_100_300
    receivers = [receiver_config(
        "R", ABC_CENTERS, 100, ready=at(10, 0), seconds=600,
        overrides=overrides)]
    fixed = []
    if with_interferer:
        fixed.append(emission(
            "F", "TI", "U", 200, 10, at(10, 20), at(10, 30)))
        receivers.append(receiver_config(
            "U", ABC_CENTERS, 200, ready=at(9, 0), seconds=600,
            overrides=overrides))
    # Deep copies so mutation-based validation tests cannot leak state into
    # other tests sharing the module-level constants.
    return make_doc(
        [dict(row) for row in ABC_STATIONS],
        list(ABC_CENTERS),
        [dict(row) for row in abc_requests()],
        [dict(row) for row in fixed],
        [dict(row, switches=[dict(sw) for sw in row["switches"]])
         for row in receivers])


class MandatedScenario(unittest.TestCase):
    def test_all_three_selected_via_intermediate_reception(self):
        report = arrange(abc_doc())
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 200, 300])
        self.assertEqual(report["objective"]["selected_utility"], 250)
        self.assertEqual(report["objective"]["total_switch_count"], 2)
        timeline = report["receiver_schedules"][0]["timeline"]
        kinds = [row["kind"] for row in timeline]
        self.assertEqual(kinds, [
            "reception", "switch", "reception", "switch", "reception"])
        # Tuning occupies exactly the seconds immediately before the task.
        first_switch = timeline[1]
        self.assertEqual((first_switch["from_hz"], first_switch["to_hz"]),
                         (100, 200))
        self.assertEqual(first_switch["start"], at(10, 10))
        self.assertEqual(first_switch["end"], at(10, 20))
        second_switch = timeline[3]
        self.assertEqual((second_switch["from_hz"], second_switch["to_hz"]),
                         (200, 300))
        self.assertEqual(second_switch["start"], at(10, 30))
        self.assertEqual(second_switch["end"], at(10, 40))
        # No phantom direct 100->300 constraint was imposed on A/C.
        self.assertNotIn((100, 300),
                         [(row.get("from_hz"), row.get("to_hz"))
                          for row in timeline if row["kind"] == "switch"])

    def test_without_b_direct_switch_breaks_a_and_c(self):
        # The same receiver/timing data, B removed: the A->C adjacency needs
        # the declared 3600s direct switch inside a 30-minute gap.
        doc = abc_doc()
        doc["requests"] = [row for row in doc["requests"] if row["id"] != "B"]
        report = arrange(doc)
        self.assertEqual(report["status"], "infeasible")
        self.assertIsNone(report["plan"])
        self.assertIsNone(report["objective"])
        self.assertEqual(
            [row["center_hz"] for row in report["requests"]], [None, None])

    def test_fixed_interferer_blocks_b_and_makes_required_set_infeasible(self):
        report = arrange(abc_doc(with_interferer=True))
        self.assertEqual(report["status"], "infeasible")
        self.assertIsNone(report["plan"])
        # The fixed-only conflict report is still published as evidence.
        self.assertEqual(
            report["conflict_report"]["summary"]["transmissions"], 1)
        self.assertEqual(
            report["summary"]["retained_fixed_conflicts"], 0)

    def test_shorter_direct_switch_keeps_a_c_and_drops_b(self):
        report = arrange(abc_doc(direct_100_300=1800,
                                 with_interferer=True))
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, None, 300])
        self.assertEqual(report["objective"]["selected_utility"], 200)
        # A already sits on the initial channel; the only switch is the
        # adjacent A->C 100->300 tuning, which fits the 30-minute gap.
        self.assertEqual(report["objective"]["total_switch_count"], 1)
        timeline = report["receiver_schedules"][0]["timeline"]
        self.assertEqual([row["kind"] for row in timeline],
                         ["reception", "switch", "reception"])
        switch = timeline[1]
        self.assertEqual((switch["from_hz"], switch["to_hz"]), (100, 300))
        self.assertEqual(switch["seconds"], 1800)
        # Tuning is packed immediately before C starts: 10:40 - 1800s.
        self.assertEqual(switch["start"], "2026-09-01T10:10:00+00:00")
        self.assertEqual(switch["end"], at(10, 40))

    def test_interferer_geometry_is_directional_and_isolated(self):
        # With the interferer present, an oracle evaluation shows F->B is the
        # only blocking relation and F's own receiver U never sees A/B/C.
        doc = abc_doc(with_interferer=True)
        oracle = Oracle(doc)
        transmissions = oracle._all_transmissions([100, 200, 300])
        rows = conflicts(oracle.stations,
                         oracle._as_validated(transmissions))
        pairs = sorted((row["victim_transmission_id"],
                        row["interferer_transmission_id"]) for row in rows)
        self.assertIn(("B", "F"), pairs)
        self.assertNotIn(("F", "B"), pairs)
        self.assertTrue(all(receiver == "R" for receiver, _ in
                            [(r["receiver_id"], r["interferer_transmission_id"])
                             for r in rows]))


# ---------------------------------------------------------------------------
# objective behavior with independent expected values
# ---------------------------------------------------------------------------
class ObjectiveTests(unittest.TestCase):
    def test_local_high_utility_choice_lowers_global_total(self):
        # One receiver, far transmitters so only the device rule matters.
        # The high-utility q1 overlaps both small tasks; the two small tasks
        # do not overlap each other and can both be received.  A greedy
        # highest-utility-first choice takes q1 (100) and then nothing else;
        # the global optimum takes q0+q2 (60+60 = 120).
        centers = [100]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1), station("T2", 12, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100], utility=60,
                    start=at(10, 0), end=at(10, 20)),
            request("q1", "T1", "R", [100], utility=100,
                    start=at(10, 10), end=at(10, 40)),
            request("q2", "T2", "R", [100], utility=60,
                    start=at(10, 30), end=at(10, 50)),
        ]
        doc = make_doc(stations, centers, requests_)
        report = arrange(doc)
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, None, 100])
        self.assertEqual(report["objective"]["selected_utility"], 120)
        # A greedy highest-utility-first run would have stopped at q1 (100).
        self.assertLess(100, 120)

    def test_switch_count_breaks_equal_utility_tie(self):
        # Both plans earn the same utility; one needs an extra switch.
        centers = [100, 200]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100, 200], utility=50,
                    start=at(10, 0), end=at(10, 10)),
            request("q1", "T1", "R", [100, 200], utility=50,
                    start=at(10, 20), end=at(10, 30)),
        ]
        cfg = receiver_config("R", centers, 100, ready=at(9, 0), seconds=600)
        doc = make_doc(stations, centers, requests_,
                       receivers=[cfg])
        report = arrange(doc)
        # [100, 100] keeps the initial channel: zero switches, same utility.
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 100])
        self.assertEqual(report["objective"]["total_switch_count"], 0)

    def test_assignment_vector_breaks_final_tie(self):
        # Same utility and same switch count; the lexicographically smaller
        # vector (smaller numeric center first) must win.
        centers = [100, 200]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100, 200], utility=10,
                    start=at(10, 0), end=at(10, 10)),
            request("q1", "T1", "R", [100, 200], utility=10,
                    start=at(11, 0), end=at(11, 10)),
        ]
        cfg = receiver_config("R", centers, 100, ready=at(9, 0), seconds=60)
        doc = make_doc(stations, centers, requests_, receivers=[cfg])
        report = arrange(doc)
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 100])
        # null compares greater than any integer: dropping q1 while selecting
        # q0 cannot beat selecting both at equal cost.
        self.assertEqual(report["objective"]["selected_utility"], 20)

    def test_asymmetric_switch_direction_is_respected(self):
        # Each request is forced onto one channel.  The table is asymmetric:
        # 100->200 cheap (60 s), 200->100 expensive (600 s).  The receiver
        # becomes ready well before the first task, and the inter-task gap is
        # only 90 s, so which directed transition is needed decides.
        centers = [100, 200]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        cheap = {(100, 200): 60, (200, 100): 600}
        requests_ = [
            request("q0", "T0", "R", [100], required=True,
                    start=at(10, 0), end=at(10, 10)),
            request("q1", "T1", "R", [200], required=True,
                    start="2026-09-01T10:11:30+00:00",
                    end="2026-09-01T10:21:30+00:00"),
        ]
        cfg = receiver_config("R", centers, 100, ready=at(9, 0),
                              overrides=cheap)
        report = arrange(make_doc(stations, centers, requests_,
                                  receivers=[cfg]))
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 200])
        self.assertEqual(report["objective"]["total_switch_count"], 1)
        actions = [row for row in report["receiver_schedules"][0]["timeline"]
                   if row["kind"] == "switch"]
        self.assertEqual([(row["from_hz"], row["to_hz"]) for row in actions],
                         [(100, 200)])

        # Reverse the channel order: now the adjacency consumes the expensive
        # 200->100 direction, which cannot fit a 90-second gap.
        requests_flipped = [
            request("q0", "T0", "R", [200], required=True,
                    start=at(10, 0), end=at(10, 10)),
            request("q1", "T1", "R", [100], required=True,
                    start="2026-09-01T10:11:30+00:00",
                    end="2026-09-01T10:21:30+00:00"),
        ]
        flipped = arrange(make_doc(stations, centers, requests_flipped,
                                   receivers=[cfg]))
        self.assertEqual(flipped["status"], "infeasible")

        # Widen the gap beyond 600 s and the same expensive direction becomes
        # feasible: the directed pair, not its unordered channel set, decides.
        requests_wide = [
            request("q0", "T0", "R", [200], required=True,
                    start=at(10, 0), end=at(10, 10)),
            request("q1", "T1", "R", [100], required=True,
                    start=at(10, 25), end=at(10, 35)),
        ]
        wide = arrange(make_doc(stations, centers, requests_wide,
                                receivers=[cfg]))
        self.assertEqual(wide["status"], "optimal")
        self.assertEqual(
            wide["objective"]["assignment_vector"], [200, 100])
        wide_actions = [row for row in wide["receiver_schedules"][0]["timeline"]
                        if row["kind"] == "switch"]
        self.assertEqual(
            [(row["from_hz"], row["to_hz"]) for row in wide_actions],
            [(100, 200), (200, 100)])

    def test_initial_tuning_equality_boundary(self):
        centers = [100, 200]
        stations = [station("R"), station("T0", 10, 10, 1)]
        requests_ = [request("q0", "T0", "R", [200], required=True,
                             start=at(10, 10), end=at(10, 20))]
        # ready 10:00, switch 600s, task 10:10 -> equality is allowed.
        cfg = receiver_config("R", centers, 100, ready=at(10, 0), seconds=600)
        report = arrange(make_doc(stations, centers, requests_,
                                  receivers=[cfg]))
        self.assertEqual(report["status"], "optimal")
        switch = report["receiver_schedules"][0]["timeline"][0]
        self.assertEqual(switch["start"], at(10, 0))
        self.assertEqual(switch["end"], at(10, 10))

    def test_initial_tuning_too_late_is_infeasible(self):
        centers = [100, 200]
        stations = [station("R"), station("T0", 10, 10, 1)]
        requests_ = [request("q0", "T0", "R", [200], required=True,
                             start=at(10, 9), end=at(10, 20))]
        cfg = receiver_config("R", centers, 100, ready=at(10, 0), seconds=600)
        report = arrange(make_doc(stations, centers, requests_,
                                  receivers=[cfg]))
        self.assertEqual(report["status"], "infeasible")


# ---------------------------------------------------------------------------
# radio / geometry rules
# ---------------------------------------------------------------------------
class RadioRuleTests(unittest.TestCase):
    def test_touching_time_and_frequency_boundaries_are_clear(self):
        centers = [100, 200]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("T0", 0.0, 0.001, 1),
                    station("T1", 0.0, 0.002, 1)]
        # Half-open time: ending exactly when the next starts is no overlap.
        requests_ = [
            request("q0", "T0", "R", [100], required=True,
                    start=at(10, 0), end=at(10, 30)),
            request("q1", "T1", "R", [100], required=True,
                    start=at(10, 30), end=at(11, 0)),
        ]
        doc = make_doc(stations, centers, requests_)
        report = arrange(doc)
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(report["conflict_report"]["conflicts"], [])

    def test_half_hertz_bands_conflict_correctly(self):
        centers = [100, 101]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("T0", 0.0, 0.001, 1),
                    station("T1", 0.0, 0.002, 1)]
        requests_ = [
            request("q0", "T0", "R", [100], bandwidth=3,
                    start=at(10, 0), end=at(11, 0)),
            request("q1", "T1", "R", [101], bandwidth=3,
                    start=at(10, 0), end=at(11, 0)),
        ]
        report = arrange(make_doc(stations, centers, requests_))
        # Bands [98.5,101.5) and [99.5,102.5) overlap; same receiver also
        # overlaps in time, so both cannot be selected.
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, None])

    def test_same_channel_same_receiver_overlap_is_device_conflict(self):
        centers = [100]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100], utility=10,
                    start=at(10, 0), end=at(10, 20)),
            request("q1", "T1", "R", [100], utility=10,
                    start=at(10, 10), end=at(10, 30)),
        ]
        report = arrange(make_doc(stations, centers, requests_))
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, None])
        # Even on one channel, the device cannot do two receptions at once.
        timeline = report["receiver_schedules"][0]["timeline"]
        self.assertEqual(len(timeline), 1)

    def test_fixed_to_fixed_conflict_is_retained_not_fatal(self):
        centers = [100]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("T0", 0.0, 0.001, 1),
                    station("T1", 0.0, 0.002, 1),
                    station("U", 20, 20, 1)]
        fixed = [
            emission("f0", "T0", "R", 100, start=at(10, 0), end=at(11, 0)),
            emission("f1", "T1", "R", 100, start=at(10, 0), end=at(11, 0)),
        ]
        # Fixed tasks overlap on the same receiver: this pre-existing plan is
        # itself device-infeasible, so arrangement reports infeasible while
        # still publishing the fixed-only radio report.
        report = arrange(make_doc(stations, centers, fixed=fixed))
        self.assertEqual(report["status"], "infeasible")
        self.assertGreaterEqual(
            report["conflict_report"]["summary"]["directed_conflicts"], 2)

    def test_fixed_fixed_radio_conflict_alone_is_retained(self):
        # Two fixed tasks on *different* receivers, one inside the other's
        # protection circle: pre-existing directed conflict, device-feasible.
        centers = [100]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("S", 0.0, 0.001, 5000),
                    station("T0", 0.0, 0.002, 1),
                    station("T1", 0.0, 0.003, 1)]
        fixed = [
            emission("f0", "T0", "R", 100, start=at(10, 0), end=at(11, 0)),
            emission("f1", "T1", "S", 100, start=at(10, 0), end=at(11, 0)),
        ]
        report = arrange(make_doc(stations, centers, fixed=fixed))
        self.assertEqual(report["status"], "optimal")
        pairs = sorted((row["victim_transmission_id"],
                        row["interferer_transmission_id"])
                       for row in report["conflict_report"]["conflicts"])
        self.assertEqual(pairs, [("f0", "f1"), ("f1", "f0")])
        self.assertEqual(report["summary"]["retained_fixed_conflicts"], 2)

    def test_bidirectional_fixed_request_screening(self):
        centers = [100]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("T0", 0.0, 0.001, 1),
                    station("T1", 0.0, 0.002, 1),
                    station("U", 20, 20, 1)]
        fixed = [emission("f0", "T1", "U", 100,
                          start=at(10, 0), end=at(11, 0))]
        # The request's receiver R is close to f0's transmitter; equivalently
        # f0 (victim at U) is far from the request's transmitter T0.  Only the
        # f0->request direction exists, and it still forbids the selection.
        requests_ = [request("q0", "T0", "R", [100], required=False,
                             start=at(10, 0), end=at(11, 0))]
        report = arrange(make_doc(stations, centers, requests_, fixed))
        self.assertEqual(
            report["objective"]["assignment_vector"], [None])

    def test_receiver_isolation_lets_other_stations_continue(self):
        centers = [100]
        stations = [station("R", 0.0, 0.0, 1),
                    station("Q", 0.0, 0.0, 1),
                    station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100], utility=10,
                    start=at(10, 0), end=at(10, 20)),
            request("q1", "T1", "Q", [100], utility=10,
                    start=at(10, 0), end=at(10, 20)),
        ]
        report = arrange(make_doc(stations, centers, requests_))
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 100])
        self.assertEqual(len(report["receiver_schedules"]), 2)


# ---------------------------------------------------------------------------
# edge cases and limits
# ---------------------------------------------------------------------------
class EdgeCaseTests(unittest.TestCase):
    def test_empty_requests_is_feasible(self):
        stations = [station("R"), station("T0", 10, 10, 1)]
        fixed = [emission("f0", "T0", "R", 100)]
        doc = make_doc(stations, [100], fixed=fixed)
        report = arrange(doc)
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(report["requests"], [])
        self.assertEqual(report["objective"]["selected_utility"], 0)
        self.assertEqual(
            [row["id"] for row in report["plan"]["transmissions"]], ["f0"])

    def test_no_tasks_at_all_is_feasible_with_no_receiver_configs(self):
        doc = {
            "stations": [station("R")],
            "fixed_transmissions": [],
            "centers_hz": [100],
            "requests": [],
            "receivers": [],
        }
        report = arrange(doc)
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(report["plan"]["transmissions"], [])
        self.assertEqual(report["receiver_schedules"], [])
        self.assertEqual(report["objective"]["total_switch_count"], 0)

    def test_stray_receiver_config_without_any_task_is_rejected(self):
        doc = {
            "stations": [station("R")],
            "fixed_transmissions": [],
            "centers_hz": [100],
            "requests": [],
            "receivers": [receiver_config("R", [100], 100)],
        }
        with self.assertRaises(ValueError):
            arrange(doc)

    def test_all_optional_requests_dropped_when_unplaceable(self):
        centers = [100]
        stations = [station("R"), station("T0", 10, 10, 1)]
        # Two overlapping optional requests on one channel/receiver.
        requests_ = [
            request("q0", "T0", "R", [100],
                    start=at(10, 0), end=at(10, 30)),
            request("q1", "T0", "R", [100],
                    start=at(10, 10), end=at(10, 40)),
        ]
        report = arrange(make_doc(stations, centers, requests_))
        self.assertEqual(report["status"], "optimal")
        # One is still selectable, so the optimum keeps exactly one.
        self.assertEqual(report["summary"]["requests_selected"], 1)

    def test_every_optional_request_is_dropped_against_a_fixed_blocker(self):
        # A fixed reception occupies the only receiver/channel for the whole
        # window; every optional request overlaps it and must be dropped.
        centers = [100]
        stations = [station("R", 0.0, 0.0, 1),
                    station("T0", 10.0, 10.0, 1),
                    station("T1", 11.0, 10.0, 1)]
        fixed = [emission("f0", "T1", "R", 100,
                          start=at(9, 0), end=at(12, 0))]
        requests_ = [
            request("q0", "T0", "R", [100], utility=10,
                    start=at(10, 0), end=at(10, 30)),
            request("q1", "T0", "R", [100], utility=20,
                    start=at(11, 0), end=at(11, 30)),
        ]
        report = arrange(make_doc(stations, centers, requests_, fixed))
        self.assertEqual(report["status"], "optimal")
        self.assertEqual(report["summary"]["requests_selected"], 0)
        self.assertEqual(
            report["objective"]["assignment_vector"], [None, None])
        self.assertEqual(report["objective"]["selected_utility"], 0)
        # The fixed plan still survives untouched in the output.
        self.assertEqual(
            [row["id"] for row in report["plan"]["transmissions"]], ["f0"])

    def test_fixed_tasks_pre_empting_one_device_make_it_infeasible(self):
        # Two fixed receptions overlap on the same single device.  Nothing in
        # requests can repair that: the fixed baseline itself is infeasible.
        centers = [100]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        fixed = [
            emission("f0", "T0", "R", 100,
                     start=at(10, 0), end=at(10, 30)),
            emission("f1", "T1", "R", 100,
                     start=at(10, 20), end=at(10, 50)),
        ]
        doc = make_doc(stations, centers, fixed=fixed)
        report = arrange(doc)
        self.assertEqual(report["status"], "infeasible")
        self.assertIsNone(report["plan"])
        self.assertIn("no feasible plan", report["status_detail"])

    def test_required_requests_mutually_interfering_are_infeasible(self):
        # Two required requests on the same receiver/channel, overlapping in
        # time: each must be selected, but the device cannot do both.
        centers = [100]
        stations = [station("R"), station("T0", 10, 10, 1),
                    station("T1", 11, 10, 1)]
        requests_ = [
            request("q0", "T0", "R", [100], required=True,
                    start=at(10, 0), end=at(10, 30)),
            request("q1", "T1", "R", [100], required=True,
                    start=at(10, 20), end=at(10, 50)),
        ]
        report = arrange(make_doc(stations, centers, requests_))
        self.assertEqual(report["status"], "infeasible")

    def test_required_request_radio_blocked_on_every_channel_is_infeasible(self):
        # A fixed interferer covers each allowed channel of a required task.
        centers = [100, 200]
        stations = [station("R", 0.0, 0.0, 5000),
                    station("T0", 10.0, 10.0, 1),
                    station("TX0", 0.0, 0.001, 1),
                    station("TX1", 0.0, 0.002, 1),
                    station("X", 40.0, 40.0, 1)]
        requests_ = [request("q0", "T0", "R", [100, 200], required=True,
                             start=at(10, 0), end=at(11, 0))]
        fixed = [
            emission("b100", "TX0", "X", 100,
                     start=at(10, 0), end=at(11, 0)),
            emission("b200", "TX1", "X", 200,
                     start=at(10, 0), end=at(11, 0)),
        ]
        report = arrange(make_doc(stations, centers, requests_, fixed))
        self.assertEqual(report["status"], "infeasible")
        # Fixed-only evidence is still published for inspection.
        self.assertEqual(
            report["conflict_report"]["summary"]["transmissions"], 2)

    def test_plan_transmission_cap_enforced(self):
        # 300 fixed transmissions already fill the plan; one required request
        # on a separate receiver can only be satisfied by adding a 301st
        # transmission, which the contract forbids.
        centers = [100]
        stations = [station("R", 0.0, 0.0, 1),
                    station("Q", 50.0, 50.0, 1)]
        base_time = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        fixed = []
        for i in range(MAX_PLAN):
            start = base_time + timedelta(minutes=10 * i)
            stations.append(station(f"T{i}", 20.0 + i * 0.001, 20.0, 1))
            fixed.append(emission(
                f"f{i:03d}", f"T{i}", "R", 100,
                start=start.isoformat(),
                end=(start + timedelta(minutes=5)).isoformat()))
        stations.append(station("TQ", 60.0, 60.0, 1))
        q_start = base_time + timedelta(days=1)
        requests_ = [request(
            "q0", "TQ", "Q", [100], required=True,
            start=q_start.isoformat(),
            end=(q_start + timedelta(minutes=10)).isoformat())]
        report = arrange(make_doc(stations, centers, requests_, fixed))
        self.assertEqual(report["status"], "infeasible")

    def test_non_adjacent_switch_differs_from_sum_of_adjacent_ones(self):
        # 100->300 declared 3600s while 100->200 and 200->300 are 600s each.
        # With an intermediate reception only adjacent pairs are consulted.
        doc = abc_doc()
        table = doc["receivers"][0]["switches"]
        direct = {(r["from_hz"], r["to_hz"]): r["seconds"] for r in table}
        self.assertNotEqual(direct[(100, 300)],
                            direct[(100, 200)] + direct[(200, 300)])
        report = arrange(doc)
        self.assertEqual(
            report["objective"]["assignment_vector"], [100, 200, 300])

    def test_optional_intermediate_task_not_inserted_as_phantom_tuning(self):
        # If B is dropped, the optimizer must not "tune via 200" without a
        # reception there: only the declared direct 100->300 value counts.
        doc = abc_doc()
        doc["requests"][1]["required"] = False
        doc["requests"] = [row for row in doc["requests"]
                           if row["id"] != "B"]
        report = arrange(doc)
        self.assertEqual(report["status"], "infeasible")


# ---------------------------------------------------------------------------
# deterministic serialization
# ---------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_reordering_and_timezone_notation_do_not_change_report(self):
        base = abc_doc()
        report = arrange(base)
        shuffled = json.loads(json.dumps(base))
        shuffled["stations"].reverse()
        shuffled["requests"].reverse()
        shuffled["receivers"].reverse()
        shuffled["centers_hz"].reverse()
        self.assertEqual(arrange(shuffled), report)
        tz = json.loads(json.dumps(base))
        tz["requests"][0]["start"] = "2026-09-01T12:00:00+02:00"
        tz["requests"][0]["end"] = "2026-09-01T12:10:00+02:00"
        self.assertEqual(arrange(tz), report)

    def test_plan_is_directly_runnable_through_radio_coordinate(self):
        from spectrum.service import run
        report = arrange(abc_doc())
        rerun = run(report["plan"])
        self.assertEqual(rerun["conflicts"], report["conflict_report"]["conflicts"])
        self.assertEqual(rerun["summary"], report["conflict_report"]["summary"])


# ---------------------------------------------------------------------------
# validation: exit-2 class defects
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def base(self):
        return abc_doc()

    def assert_invalid(self, mutate):
        doc = self.base()
        mutate(doc)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            arrange(doc)

    def test_boolean_rejected_for_every_integer_field(self):
        def mutate(doc):
            doc["centers_hz"][0] = True
        self.assert_invalid(mutate)
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "utility", True))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "bandwidth_hz", True))
        self.assert_invalid(lambda d: d["receivers"][0].__setitem__(
            "initial_center_hz", True))
        self.assert_invalid(lambda d: d["receivers"][0]["switches"][0]
                            .__setitem__("seconds", True))
        self.assert_invalid(lambda d: d["stations"][0].__setitem__(
            "protection_radius_m", True))

    def test_boolean_rejected_for_required(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "required", "yes"))

    def test_bad_switch_table_shapes(self):
        def missing_pair(doc):
            doc["receivers"][0]["switches"].pop()
        self.assert_invalid(missing_pair)

        def duplicate_pair(doc):
            doc["receivers"][0]["switches"].append(
                dict(doc["receivers"][0]["switches"][0]))
        self.assert_invalid(duplicate_pair)

        def same_channel_row(doc):
            doc["receivers"][0]["switches"].append(
                switch_row(100, 100, 5))
        self.assert_invalid(same_channel_row)

        def out_of_range_seconds(doc):
            doc["receivers"][0]["switches"][0]["seconds"] = 3601
        self.assert_invalid(out_of_range_seconds)

        def negative_seconds(doc):
            doc["receivers"][0]["switches"][0]["seconds"] = -1
        self.assert_invalid(negative_seconds)

    def test_receiver_coverage_must_be_exact(self):
        self.assert_invalid(lambda d: d["receivers"].pop())
        self.assert_invalid(lambda d: d["receivers"].append(
            receiver_config("ZZ", d["centers_hz"], 100)))

    def test_dangling_references_rejected(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "station_id", "nope"))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "receiver_id", "nope"))
        self.assert_invalid(lambda d: d["fixed_transmissions"].append(
            emission("fx", "TA", "nope", 100)))

    def test_center_contract_violations(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "allowed_centers_hz", []))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "allowed_centers_hz", [100, 100]))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "allowed_centers_hz", [999]))
        self.assert_invalid(lambda d: d["centers_hz"].__setitem__(
            slice(None), [100, 100]))
        self.assert_invalid(lambda d: d["centers_hz"].append(700)
                            if len(d["centers_hz"]) < MAX_CENTERS else None)

    def test_limits_and_duplicates(self):
        self.assert_invalid(lambda d: d["requests"].extend([
            request(f"x{i}", "TA", "R", [100]) for i in range(MAX_REQUESTS)]))
        self.assert_invalid(lambda d: d["requests"].append(
            request("A", "TA", "R", [100])))  # duplicate request id
        with_interferer = abc_doc(with_interferer=True)
        with self.assertRaises(ValueError):
            with_interferer["requests"].append(
                request("F", "TA", "R", [100]))  # clashes with fixed id
            arrange(with_interferer)

    def test_band_crossing_zero_rejected(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "bandwidth_hz", 1000))
        self.assert_invalid(lambda d: d["fixed_transmissions"].append(
            emission("neg", "TA", "R", 100, bandwidth=1000)))

    def test_unknown_and_missing_root_fields(self):
        self.assert_invalid(lambda d: d.__setitem__("extra", 1))
        self.assert_invalid(lambda d: d.pop("centers_hz"))

    def test_utility_and_required_bounds(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "utility", 0))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "utility", 1_000_001))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "utility", 1.5))
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "required", 0))

    def test_time_and_station_shape_errors(self):
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "start", "2026-09-01T11:00:00"))  # no offset
        self.assert_invalid(lambda d: d["requests"][0].__setitem__(
            "end", d["requests"][0]["start"]))  # non-positive duration
        self.assert_invalid(lambda d: d["stations"][0].__setitem__(
            "lat", 91))
        self.assert_invalid(lambda d: d["stations"][0].__setitem__(
            "lon", "east"))
        self.assert_invalid(lambda d: d["stations"].__setitem__(
            slice(None), []))  # at least one station

    def test_fixed_center_must_be_global_center(self):
        self.assert_invalid(lambda d: d["fixed_transmissions"].append(
            emission("g0", "TA", "R", 999)))

    def test_allowed_centers_max_three(self):
        doc = self.base()
        doc["centers_hz"] = [100, 200, 300, 400]
        doc["requests"][0]["allowed_centers_hz"] = [100, 200, 300, 400]
        for cfg in doc["receivers"]:
            cfg["switches"] = full_switch_table(doc["centers_hz"])
        with self.assertRaises(ValueError):
            arrange(doc)
        self.assertEqual(MAX_ALLOWED, 3)
        self.assertEqual(MAX_CENTERS, 6)
        self.assertEqual(MAX_REQUESTS, 14)
        self.assertEqual(MAX_FIXED, 300)

    def test_switch_table_size_for_six_centers(self):
        doc = self.base()
        doc["centers_hz"] = [100, 200, 300, 400, 500, 600]
        for cfg in doc["receivers"]:
            cfg["initial_center_hz"] = 100
            cfg["switches"] = full_switch_table(doc["centers_hz"])
        for row in doc["requests"]:
            row["allowed_centers_hz"] = row["allowed_centers_hz"][:1]
        parsed = parse_input(doc)
        self.assertEqual(
            len(parsed["receivers"]["R"]["switch_table"]), 6 * 5)


# ---------------------------------------------------------------------------
# randomized cross-checks against the independent oracle
# ---------------------------------------------------------------------------
class OracleCrossCheck(unittest.TestCase):
    def _random_doc(self, rng):
        center_count = rng.choice([1, 2, 3, 4, 6])
        centers = [100 * (i + 1) for i in range(center_count)]
        receiver_count = rng.choice([1, 1, 2, 3])
        stations = [
            station(f"R{k}", rng.choice([0.0, 20.0]),
                    rng.choice([0.0, 20.0]), rng.choice([1, 4000]))
            for k in range(receiver_count)
        ]
        transmitter_count = rng.randint(2, 10)
        for i in range(transmitter_count):
            stations.append(station(
                f"T{i}", rng.choice([0.0, 0.01, 20.0]),
                rng.choice([0.0, 0.01, 20.0]), 1))
        fixed = []
        base_time = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        for i in range(rng.randint(0, 3)):
            rid = f"R{rng.randrange(receiver_count)}"
            start = base_time + timedelta(minutes=rng.randrange(240))
            fixed.append(emission(
                f"f{i}", f"T{rng.randrange(transmitter_count)}", rid,
                rng.choice(centers),
                start=start.isoformat(),
                end=(start + timedelta(minutes=rng.choice([5, 15, 40])))
                .isoformat()))
        requests_ = []
        n_requests = rng.randint(0, 7)
        for i in range(n_requests):
            allowed = rng.sample(centers, rng.randint(
                1, min(MAX_ALLOWED, len(centers))))
            start = base_time + timedelta(minutes=rng.randrange(300))
            duration = rng.choice([5, 15, 40])
            requests_.append(request(
                f"q{i:02d}",
                f"T{rng.randrange(transmitter_count)}",
                f"R{rng.randrange(receiver_count)}",
                allowed,
                required=rng.random() < 0.4,
                utility=rng.randint(1, 1000),
                bandwidth=rng.choice([10, 20]),
                start=start.isoformat(),
                end=(start + timedelta(minutes=duration)).isoformat()))
        used_receivers = {row["receiver_id"] for row in fixed}
        used_receivers.update(row["receiver_id"] for row in requests_)
        receivers = []
        for rid in sorted(used_receivers):
            overrides = {
                (a, b): rng.choice([0, 30, 60, 300, 900])
                for a in centers for b in centers if a != b
            }
            receivers.append(receiver_config(
                rid, centers, rng.choice(centers),
                ready=at(9, rng.choice([0, 30])), overrides=overrides))
        doc = make_doc(stations, centers, requests_, fixed, receivers)
        return doc

    def test_matches_brute_force_on_many_instances(self):
        import random
        rng = random.Random(20260901)
        checked = 0
        for _ in range(120):
            doc = self._random_doc(rng)
            try:
                report = arrange(doc)
            except ValueError:
                continue
            oracle = Oracle(doc).solve()
            if oracle is None:
                self.assertEqual(report["status"], "infeasible")
                continue
            self.assertEqual(report["status"], "optimal")
            self.assertEqual(
                report["objective"]["assignment_vector"], oracle["vector"])
            self.assertEqual(
                report["objective"]["selected_utility"], oracle["utility"])
            self.assertEqual(
                report["objective"]["total_switch_count"],
                oracle["switch_count"])
            checked += 1
        self.assertGreater(checked, 60)

    def test_oracle_covers_mandated_family(self):
        for interferer in (False, True):
            for direct in (1800, 3600):
                doc = abc_doc(direct, interferer)
                expected = Oracle(doc).solve()
                report = arrange(doc)
                if expected is None:
                    self.assertEqual(report["status"], "infeasible")
                else:
                    self.assertEqual(report["status"], "optimal")
                    self.assertEqual(
                        report["objective"]["assignment_vector"],
                        expected["vector"])
                    self.assertEqual(
                        report["objective"]["total_switch_count"],
                        expected["switch_count"])


# ---------------------------------------------------------------------------
# report shape and CLI behavior
# ---------------------------------------------------------------------------
class ReportAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_report_fields_and_schedule_traceability(self):
        report = arrange(abc_doc())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["tool"], "radio-arrange")
        self.assertEqual(report["model"],
                         "declared_receiver_protection_circle")
        entries = {row["request_id"]: row for row in report["requests"]}
        self.assertEqual(entries["A"]["selected"], True)
        self.assertEqual(entries["A"]["center_hz"], 100)
        self.assertEqual(entries["B"]["allowed_centers_hz"], [200])
        schedule = report["receiver_schedules"][0]
        self.assertEqual(schedule["receiver_id"], "R")
        receptions = [row for row in schedule["timeline"]
                      if row["kind"] == "reception"]
        self.assertEqual(
            [(row["transmission_id"], row["source"]) for row in receptions],
            [("A", "request"), ("B", "request"), ("C", "request")])
        for row in receptions:
            self.assertIn("station_id", row)
            self.assertIn("bandwidth_hz", row)
        # Sorted stations and transmissions give canonical plan order.
        self.assertEqual(
            [row["id"] for row in report["plan"]["stations"]],
            sorted(row["id"] for row in report["plan"]["stations"]))

    def test_fixed_schedule_entries_are_marked_fixed(self):
        doc = abc_doc(with_interferer=True, direct_100_300=1800)
        report = arrange(doc)
        u_schedule = next(row for row in report["receiver_schedules"]
                          if row["receiver_id"] == "U")
        self.assertEqual(u_schedule["timeline"][0]["source"], "fixed")
        self.assertEqual(
            u_schedule["timeline"][0]["transmission_id"], "F")

    def test_cli_writes_atomically_and_preserves_existing_target(self):
        source = self.dir / "input.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(abc_doc()), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(SRC))
        run = lambda: subprocess.run(
            [sys.executable, "-m", "spectrum.arrange_cli",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True, env=env, timeout=60)
        first = run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(output.exists())
        before = output.read_bytes()
        second = run()
        self.assertEqual(second.returncode, 2)
        self.assertIn("radio-arrange", second.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_cli_invalid_input_exits_2_without_report(self):
        source = self.dir / "bad.json"
        output = self.dir / "report.json"
        bad = abc_doc()
        bad["centers_hz"] = [100, 100]
        source.write_text(json.dumps(bad), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(SRC))
        completed = subprocess.run(
            [sys.executable, "-m", "spectrum.arrange_cli",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())

    def test_infeasible_business_result_exits_0(self):
        source = self.dir / "input.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(abc_doc(with_interferer=True)),
                          encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(SRC))
        completed = subprocess.run(
            [sys.executable, "-m", "spectrum.arrange_cli",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "infeasible")

    def test_distance_helper_remains_original_model(self):
        # The arranger must not reimplement geometry differently.
        stations = {"R": station("R"), "T": station("T", 0.0, 0.001, 1)}
        value = distance_m(stations["R"], stations["T"])
        self.assertAlmostEqual(value, 111.195, places=2)


if __name__ == "__main__":
    unittest.main()
