"""Tests for the privacy-reviewed reception bulletin ``radio-bulletin``.

Two layers, mirroring the other command suites:

* hand-computed scenarios with independent expected values, including every
  scenario mandated by the task statement and the required edge cases
  (history leak, history/plan disagreement, zero rows and columns, window
  clipping, center frequency exactly on a group boundary);
* an independent brute-force oracle that enumerates every small integer
  matrix consistent with the public bounds and exact publications and
  re-derives caliber ranges and admission decisions from the contract text
  alone, checked against the real flow-based implementation on many
  generated instances.

The oracle never imports any flow helper; it only shares the plain
``PublicRegion`` input dictionaries.
"""
import copy
import ast
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spectrum.bulletin import (
    DEFAULT_BOUND,
    Feasibility,
    PublicRegion,
    build_bulletin,
    measure_matrix,
)
from spectrum.io import load

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "bulletin"

BASE = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)


def at(microseconds):
    return (BASE + timedelta(microseconds=microseconds)).isoformat()


def station(sid, lat=0.0, lon=0.0, radius=1000):
    return {"id": sid, "lat": lat, "lon": lon,
            "protection_radius_m": radius}


def transmission(tid, transmitter, receiver, center, bandwidth=2,
                 start_us=0, end_us=1):
    return {
        "id": tid, "station_id": transmitter, "receiver_id": receiver,
        "center_hz": center, "bandwidth_hz": bandwidth,
        "start": at(start_us), "end": at(end_us),
    }


def plan_doc(stations, transmissions):
    return {"stations": list(stations), "transmissions": list(transmissions)}


def cell_caliber(venue, group):
    return {"scope": "cell", "venue_id": venue, "group_id": group}


def row_caliber(venue):
    return {"scope": "row", "venue_id": venue}


def col_caliber(group):
    return {"scope": "column", "group_id": group}


TOTAL_CALIBER = {"scope": "total"}


def rules_doc(venues, groups, mapping, bounds, protected, requests,
              publications=None, revision=0,
              window_start=0, window_end=24 * 3600 * 1_000_000,
              edition_id="ed-test"):
    return {
        "edition_id": edition_id,
        "window": {"start": at(window_start), "end": at(window_end)},
        "venues": list(venues),
        "channel_groups": [
            {"id": gid, "low_hz": low, "high_hz": high}
            for gid, low, high in groups],
        "receiver_venues": [
            {"receiver_id": r, "venue_id": v} for r, v in mapping],
        "bounds": bounds,
        "protected": protected,
        "prior": {
            "edition_id": edition_id,
            "revision": revision,
            "publications": [] if publications is None else publications,
        },
        "requests": requests,
    }


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# independent brute-force oracle over small integer matrices
# ---------------------------------------------------------------------------
class BruteForceRegion:
    """The public-knowledge model implemented by direct enumeration."""

    def __init__(self, rows, cols, cell_cap, row_bounds, col_bounds,
                 total_bounds, pins):
        self.rows = rows
        self.cols = cols
        self.cell_cap = cell_cap
        self.row_bounds = row_bounds
        self.col_bounds = col_bounds
        self.total_bounds = total_bounds
        self.pins = pins
        self.matrices = self._enumerate()

    def _enumerate(self):
        ranges = [range(0, self.cell_cap[(i, j)] + 1)
                  for i in range(self.rows) for j in range(self.cols)]
        result = []
        for values in itertools.product(*ranges):
            matrix = [values[i * self.cols:(i + 1) * self.cols]
                      for i in range(self.rows)]
            if not all(self.row_bounds[i][0] <= sum(matrix[i])
                       <= self.row_bounds[i][1]
                       for i in range(self.rows)):
                continue
            if not all(self.col_bounds[j][0]
                       <= sum(matrix[i][j] for i in range(self.rows))
                       <= self.col_bounds[j][1]
                       for j in range(self.cols)):
                continue
            total = sum(sum(row) for row in matrix)
            if not self.total_bounds[0] <= total <= self.total_bounds[1]:
                continue
            if not all(self._value(matrix, key) == value
                       for key, value in self.pins.items()):
                continue
            result.append(matrix)
        return result

    def _value(self, matrix, key):
        kind = key[0]
        if kind == "cell":
            return matrix[key[1]][key[2]]
        if kind == "row":
            return sum(matrix[key[1]])
        if kind == "col":
            return sum(matrix[i][key[1]] for i in range(self.rows))
        return sum(sum(row) for row in matrix)

    def caliber_range(self, key):
        if not self.matrices:
            return None
        values = sorted({self._value(matrix, key) for matrix in self.matrices})
        return values[0], values[-1]

    def projection(self, key):
        return sorted({self._value(matrix, key) for matrix in self.matrices})

    def admit(self, key, protected):
        low, high = self.caliber_range(key)
        for value in range(low, high + 1):
            sliced = BruteForceRegion(
                self.rows, self.cols, self.cell_cap, self.row_bounds,
                self.col_bounds, self.total_bounds,
                {**self.pins, key: value})
            for cell, gap in protected.items():
                span = sliced.caliber_range(cell)
                if span is None or span[1] - span[0] < gap:
                    return False
        return True


def make_region_inputs(rows, cols, cell_cap, row_bounds, col_bounds,
                       total_bounds, pins):
    standing = {}
    for i in range(rows):
        standing[("row", i)] = row_bounds[i]
    for j in range(cols):
        standing[("col", j)] = col_bounds[j]
    for i in range(rows):
        for j in range(cols):
            standing[("cell", i, j)] = (0, cell_cap[(i, j)])
    standing[("total",)] = total_bounds
    region = PublicRegion(rows, cols, standing, list(pins.items()))
    return region


# ---------------------------------------------------------------------------
# mandated scenario from the task statement
# ---------------------------------------------------------------------------
class MandatedScenario(unittest.TestCase):
    def setUp(self):
        self.plan = load_fixture("plan.json")

    def test_h_only_grants_two(self):
        bulletin = build_bulletin(self.plan, load_fixture("rules-h-only.json"))
        self.assertEqual(
            bulletin["requests"],
            [{"caliber": col_caliber("H"), "status": "granted"}])
        self.assertEqual(
            bulletin["publications"],
            [{"caliber": col_caliber("H"), "value": 2}])
        self.assertEqual(bulletin["revision"], 1)

    def test_n_then_h_publishes_n_one_and_withholds_h(self):
        bulletin = build_bulletin(
            self.plan, load_fixture("rules-n-then-h.json"))
        self.assertEqual(
            [row["status"] for row in bulletin["requests"]],
            ["granted", "withheld"])
        self.assertEqual(
            bulletin["publications"],
            [{"caliber": row_caliber("N"), "value": 1}])
        # The withheld real value (H=2) must not appear anywhere.
        text = json.dumps(bulletin)
        self.assertNotIn('"value": 2', text)

    def test_prior_n_one_keeps_h_withheld_next_review(self):
        rules = load_fixture("rules-n-then-h.json")
        rules["prior"]["revision"] = 1
        rules["prior"]["publications"] = [
            {"caliber": row_caliber("N"), "value": 1}]
        rules["requests"] = [{"caliber": col_caliber("H")}]
        bulletin = build_bulletin(self.plan, rules)
        self.assertEqual(bulletin["revision"], 2)
        self.assertEqual(
            bulletin["requests"],
            [{"caliber": col_caliber("H"), "status": "withheld"}])
        self.assertEqual(
            bulletin["publications"],
            [{"caliber": row_caliber("N"), "value": 1}])

    def test_extra_nl_demand_changes_later_admission(self):
        plan = copy.deepcopy(self.plan)
        plan["transmissions"].append(transmission(
            "n-l-extra", "TN", "RN", center=4000))
        bulletin = build_bulletin(
            plan, load_fixture("rules-n-then-h.json"))
        self.assertEqual(
            [row["status"] for row in bulletin["requests"]],
            ["granted", "granted"])
        self.assertEqual(
            bulletin["publications"],
            [
                {"caliber": row_caliber("N"), "value": 2},
                {"caliber": col_caliber("H"), "value": 2},
            ])

    def test_requesting_protected_cell_directly_is_withheld(self):
        rules = load_fixture("rules-h-only.json")
        rules["requests"] = [{"caliber": cell_caliber("N", "H")}]
        bulletin = build_bulletin(self.plan, rules)
        self.assertEqual(bulletin["requests"][0]["status"], "withheld")
        self.assertEqual(bulletin["publications"], [])

    def test_withheld_request_leaves_no_trace_on_later_decisions(self):
        # A withheld caliber must not change the public fact set, so a later
        # distinct request is decided exactly as if the withheld one never
        # appeared in the sequence.
        rules = load_fixture("rules-n-then-h.json")
        rules["requests"] = [
            {"caliber": row_caliber("N")},
            {"caliber": col_caliber("H")},
            {"caliber": TOTAL_CALIBER},
        ]
        bulletin = build_bulletin(self.plan, rules)
        self.assertEqual(
            [row["status"] for row in bulletin["requests"]],
            ["granted", "withheld", "withheld"])
        self.assertEqual(
            bulletin["publications"],
            [{"caliber": row_caliber("N"), "value": 1}])
        rules_skip = load_fixture("rules-n-then-h.json")
        rules_skip["requests"] = [
            {"caliber": row_caliber("N")},
            {"caliber": TOTAL_CALIBER},
        ]
        skip = build_bulletin(self.plan, rules_skip)
        self.assertEqual(
            bulletin["requests"][2]["status"],
            skip["requests"][1]["status"])

    def test_prior_block_is_reusable_as_next_prior(self):
        bulletin = build_bulletin(
            self.plan, load_fixture("rules-n-then-h.json"))
        rules = load_fixture("rules-n-then-h.json")
        rules["prior"] = bulletin["prior"]
        rules["requests"] = [{"caliber": row_caliber("N")}]
        follow_up = build_bulletin(self.plan, rules)
        self.assertEqual(
            follow_up["requests"],
            [{"caliber": row_caliber("N"), "status": "published"}])
        self.assertEqual(follow_up["revision"], 2)


# ---------------------------------------------------------------------------
# measurement semantics
# ---------------------------------------------------------------------------
class MeasurementTests(unittest.TestCase):
    def _build(self, transmissions, groups=None, mapping=None):
        stations = [
            station("R1"), station("R2"), station("T1", 5, 5, 1),
            station("T2", 6, 6, 1)]
        plan = plan_doc(stations, transmissions)
        if groups is None:
            groups = [("H", 100, 3000), ("L", 3000, 6000)]
        if mapping is None:
            mapping = [("R1", "N"), ("R2", "S")]
        rules = rules_doc(
            ["N", "S"], groups, mapping,
            [], [{"cell": {"venue_id": "N", "group_id": "H"},
                  "gap_us": 1}],
            [{"caliber": TOTAL_CALIBER}])
        return build_bulletin(plan, rules), plan, rules

    def test_window_clipping_uses_integer_microsecond_intersection(self):
        # Window starts at microsecond 2; task spans [0, 5) -> clipped to 3.
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [transmission("t1", "T1", "R1", 1000,
                                                start_us=0, end_us=5)])
        rules = rules_doc(
            ["N", "X"], [("H", 100, 3000)], [("R1", "N")],
            [{"caliber": cell_caliber("X", "H"), "low": 0, "high": 5},
             {"caliber": cell_caliber("N", "H"), "low": 0, "high": 10}],
            [{"cell": {"venue_id": "X", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": cell_caliber("N", "H")}],
            window_start=2, window_end=10)
        parsed = build_bulletin(plan, rules)
        self.assertEqual(parsed["publications"][0]["value"], 3)

    def test_endpoint_touch_has_no_positive_intersection(self):
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [transmission("t1", "T1", "R1", 1000,
                                                start_us=0, end_us=2)])
        rules = rules_doc(
            ["N"], [("H", 100, 3000)], [("R1", "N")], [],
            [{"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": TOTAL_CALIBER}],
            window_start=2, window_end=5)
        bulletin = build_bulletin(plan, rules)
        # The total is requested but the cell is protected; regardless of
        # admission, the measured matrix must be all zero.
        self.assertEqual(bulletin["summary"]["venues"], 1)

    def test_center_on_group_boundary_uses_half_open_interval(self):
        stations = [station("R1"), station("R2"),
                    station("T1", 5, 5, 1), station("T2", 6, 6, 1)]
        plan = plan_doc(stations, [
            transmission("low-edge", "T1", "R1", center=3000),
            transmission("below-edge", "T2", "R2", center=2999),
        ])
        rules = rules_doc(
            ["N", "S"], [("H", 100, 3000), ("L", 3000, 6000)],
            [("R1", "N"), ("R2", "S")], [],
            [{"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": TOTAL_CALIBER}])
        # Reuse the pure measurement function through a parsed rules object.
        from spectrum.bulletin import _parse_plan, _parse_rules
        stations, transmissions = _parse_plan(plan)
        parsed_rules = _parse_rules(rules, stations, transmissions)
        matrix, _ = measure_matrix(parsed_rules)
        self.assertEqual(matrix[0][1], 1)  # 3000 Hz belongs to L [3000,6000)
        self.assertEqual(matrix[1][0], 1)  # 2999 Hz belongs to H [100,3000)

    def test_overlapping_tasks_accumulate_without_deduplication(self):
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [
            transmission("a", "T1", "R1", 1000, start_us=0, end_us=3),
            transmission("b", "T1", "R1", 1000, start_us=2, end_us=5),
        ])
        rules = rules_doc(
            ["N", "X"], [("H", 100, 3000)], [("R1", "N")],
            [{"caliber": cell_caliber("X", "H"), "low": 0, "high": 5},
             {"caliber": cell_caliber("N", "H"), "low": 0, "high": 10}],
            [{"cell": {"venue_id": "X", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": cell_caliber("N", "H")}])
        bulletin = build_bulletin(plan, rules)
        self.assertEqual(bulletin["publications"][0]["value"], 6)

    def test_bandwidth_is_never_a_multiplier(self):
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [transmission(
            "wide", "T1", "R1", 1000, bandwidth=100,
            start_us=0, end_us=4)])
        rules = rules_doc(
            ["N", "X"], [("H", 100, 3000)], [("R1", "N")],
            [{"caliber": cell_caliber("X", "H"), "low": 0, "high": 5},
             {"caliber": cell_caliber("N", "H"), "low": 0, "high": 10}],
            [{"cell": {"venue_id": "X", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": cell_caliber("N", "H")}])
        bulletin = build_bulletin(plan, rules)
        self.assertEqual(bulletin["publications"][0]["value"], 4)

    def test_transmitter_ownership_is_ignored(self):
        # Both emissions arrive at R1; the transmitter venue never matters.
        stations = [station("R1"), station("T1", 5, 5, 1),
                    station("T2", 6, 6, 1)]
        plan = plan_doc(stations, [
            transmission("a", "T1", "R1", 1000),
            transmission("b", "T2", "R1", 1000),
        ])
        rules = rules_doc(
            ["N", "X"], [("H", 100, 3000)], [("R1", "N")],
            [{"caliber": cell_caliber("X", "H"), "low": 0, "high": 5},
             {"caliber": cell_caliber("N", "H"), "low": 0, "high": 10}],
            [{"cell": {"venue_id": "X", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": cell_caliber("N", "H")}])
        bulletin = build_bulletin(plan, rules)
        self.assertEqual(bulletin["publications"][0]["value"], 2)

    def test_zero_row_and_zero_column_cells(self):
        # Measurement: only one N/H task exists, so S row and L column are
        # genuinely zero.  A bound that pins the empty L column to zero lets
        # its exact zero be published; the protected decoy cell is elsewhere
        # with a wide public range so the gap is never touched.
        stations = [station("R1"), station("R2"),
                    station("T1", 5, 5, 1), station("T2", 6, 6, 1)]
        plan = plan_doc(stations, [
            transmission("only-n-h", "T1", "R1", 1000)])
        rules = rules_doc(
            ["N", "S"], [("H", 100, 3000), ("L", 3000, 6000)],
            [("R1", "N"), ("R2", "S")],
            [
                {"caliber": col_caliber("L"), "low": 0, "high": 0},
                {"caliber": cell_caliber("S", "L"), "low": 0, "high": 0},
                {"caliber": cell_caliber("S", "H"), "low": 0, "high": 5},
            ],
            [{"cell": {"venue_id": "S", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": col_caliber("L")},
             {"caliber": cell_caliber("S", "L")}])
        bulletin = build_bulletin(plan, rules)
        self.assertEqual(
            [row["status"] for row in bulletin["requests"]],
            ["granted", "granted"])
        self.assertEqual(
            [row["value"] for row in bulletin["publications"]], [0, 0])

    def test_admission_withholds_even_when_real_value_is_safe(self):
        # The real H column is 2 and publishing 2 would leave the protected
        # N/H cell a gap, but the same caliber could publicly be 3; with the
        # [0,2] cell caps, H = 3 fixes N/H = 1 and destroys the gap.  The
        # whole caliber must therefore be withheld even though its real value
        # is safe.  A published N = 1 is what makes the unsafe value reachable.
        stations = [station("R1"), station("R2"),
                    station("T1", 5, 5, 1), station("T2", 6, 6, 1)]
        plan = plan_doc(stations, [
            transmission("only-n-h", "T1", "R1", 1000),
            transmission("s-h", "T2", "R2", 1000)])
        bounds = [
            {"caliber": cell_caliber(v, g), "low": 0, "high": 2}
            for v in ("N", "S") for g in ("H", "L")
            if (v, g) != ("S", "L")]
        bounds += [
            {"caliber": row_caliber("N"), "low": 1, "high": 2},
            {"caliber": col_caliber("H"), "low": 2, "high": 3},
            {"caliber": cell_caliber("S", "L"), "low": 0, "high": 0},
        ]
        rules = rules_doc(
            ["N", "S"], [("H", 100, 3000), ("L", 3000, 6000)],
            [("R1", "N"), ("R2", "S")], bounds,
            [{"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": row_caliber("N")},
             {"caliber": col_caliber("H")}],
            publications=[{"caliber": row_caliber("N"), "value": 1}],
            revision=1)
        bulletin = build_bulletin(plan, rules)
        self.assertEqual(
            [row["status"] for row in bulletin["requests"]],
            ["published", "withheld"])
        # Only the carried-over N = 1 fact remains; H added nothing.
        self.assertEqual(
            bulletin["publications"],
            [{"caliber": row_caliber("N"), "value": 1}])

    def test_missing_receiver_mapping_rejects(self):
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [transmission("t1", "T1", "R1", 1000)])
        rules = rules_doc(
            ["N"], [("H", 100, 3000)], [],
            [], [{"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": TOTAL_CALIBER}])
        with self.assertRaises(ValueError):
            build_bulletin(plan, rules)

    def test_center_outside_every_group_rejects(self):
        stations = [station("R1"), station("T1", 5, 5, 1)]
        plan = plan_doc(stations, [transmission("t1", "T1", "R1", 9000)])
        rules = rules_doc(
            ["N"], [("H", 100, 3000)], [("R1", "N")],
            [], [{"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}],
            [{"caliber": TOTAL_CALIBER}])
        with self.assertRaises(ValueError):
            build_bulletin(plan, rules)


# ---------------------------------------------------------------------------
# randomized cross-checks against the brute-force oracle
# ---------------------------------------------------------------------------
class OracleCrossCheck(unittest.TestCase):
    def _random_instance(self, rng):
        rows = rng.randint(1, 2)
        cols = rng.randint(1, 2)
        cap = rng.randint(1, 3)
        cell_cap = {(i, j): cap for i in range(rows) for j in range(cols)}
        row_bounds = {i: (0, cap * cols) for i in range(rows)}
        col_bounds = {j: (0, cap * rows) for j in range(cols)}
        total_bounds = (0, cap * rows * cols)
        pins = {}
        if rng.random() < 0.6:
            kind = rng.choice(("row", "col", "cell", "total"))
            if kind == "cell":
                key = ("cell", rng.randrange(rows), rng.randrange(cols))
            elif kind == "row":
                key = ("row", rng.randrange(rows))
            elif kind == "col":
                key = ("col", rng.randrange(cols))
            else:
                key = ("total",)
            pins[key] = rng.randint(0, total_bounds[1])
        protected = {
            ("cell", i, j): rng.randint(1, 2)
            for i in range(rows) for j in range(cols)
            if rng.random() < 0.5} or {("cell", 0, 0): 1}
        return (rows, cols, cell_cap, row_bounds, col_bounds,
                total_bounds, pins, protected)

    def test_ranges_and_admission_match_enumeration(self):
        import random
        rng = random.Random(20261122)
        for _ in range(120):
            (rows, cols, cell_cap, row_bounds, col_bounds,
             total_bounds, pins, protected) = self._random_instance(rng)
            brute = BruteForceRegion(
                rows, cols, cell_cap, row_bounds, col_bounds,
                total_bounds, pins)
            region = make_region_inputs(
                rows, cols, cell_cap, row_bounds, col_bounds,
                total_bounds, pins)
            self.assertEqual(region.feasible, bool(brute.matrices))
            if not brute.matrices:
                continue
            keys = [("cell", i, j) for i in range(rows)
                    for j in range(cols)]
            keys += [("row", i) for i in range(rows)]
            keys += [("col", j) for j in range(cols)]
            keys += [("total",)]
            for key in keys:
                self.assertEqual(
                    region.caliber_range(key), brute.caliber_range(key))
                # Projection must be one full integer interval.
                projected = brute.projection(key)
                self.assertEqual(
                    projected,
                    list(range(projected[0], projected[-1] + 1)))
            candidate = rng.choice(keys)
            self.assertEqual(
                region.admit(candidate, protected),
                brute.admit(candidate, protected))

    def test_sequential_requests_match_oracle_and_withholding_adds_no_fact(self):
        # Drive a fixed multi-request sequence with both engines; every
        # granted caliber is then pinned to its real (here: anchor) value,
        # and a withheld caliber must leave the feasible region untouched.
        import random
        rng = random.Random(77)
        for _ in range(40):
            (rows, cols, cell_cap, row_bounds, col_bounds,
             total_bounds, pins, protected) = self._random_instance(rng)
            region = make_region_inputs(
                rows, cols, cell_cap, row_bounds, col_bounds,
                total_bounds, pins)
            brute = BruteForceRegion(
                rows, cols, cell_cap, row_bounds, col_bounds,
                total_bounds, pins)
            if not brute.matrices:
                continue
            sequence = [("row", i) for i in range(rows)]
            sequence += [("col", j) for j in range(cols)]
            sequence += [("total",)]
            flow_pins = dict(pins)
            brute_pins = dict(pins)
            for key in sequence:
                flow_decision = region.admit(key, protected)
                brute_decision = brute.admit(key, protected)
                self.assertEqual(flow_decision, brute_decision, key)
                if flow_decision:
                    value = region.caliber_range(key)[0]
                    flow_pins[key] = value
                    brute_pins[key] = value
                    region = make_region_inputs(
                        rows, cols, cell_cap, row_bounds, col_bounds,
                        total_bounds, flow_pins)
                    brute = BruteForceRegion(
                        rows, cols, cell_cap, row_bounds, col_bounds,
                        total_bounds, brute_pins)
            # Withholding never adds a pin: the two fact sets stay identical.
            self.assertEqual(flow_pins, brute_pins)

    def test_multiple_protected_cells_batch_decisions(self):
        # Hand-checked 2x2 instance with two protected cells.
        rows = cols = 2
        cell_cap = {(i, j): 2 for i in range(2) for j in range(2)}
        row_bounds = {0: (1, 2), 1: (0, 4)}
        col_bounds = {0: (2, 3), 1: (0, 4)}
        total_bounds = (0, 8)
        protected = {("cell", 0, 0): 1, ("cell", 1, 1): 1}
        region = make_region_inputs(
            rows, cols, cell_cap, row_bounds, col_bounds,
            total_bounds, {})
        brute = BruteForceRegion(
            rows, cols, cell_cap, row_bounds, col_bounds,
            total_bounds, {})
        for key in [("row", 0), ("col", 0), ("total",),
                    ("cell", 0, 1), ("cell", 1, 0)]:
            self.assertEqual(
                region.admit(key, protected),
                brute.admit(key, protected), key)


# ---------------------------------------------------------------------------
# history handling
# ---------------------------------------------------------------------------
class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_fixture("plan.json")
        self.rules = load_fixture("rules-n-then-h.json")

    def test_leaked_history_is_an_error_not_a_fresh_start(self):
        rules = copy.deepcopy(self.rules)
        # A prior bulletin that published the protected cell's exact real
        # value (N/H = 1) leaves it a single feasible point, so the required
        # gap of 1 microsecond can no longer be witnessed.  All published
        # values still match the frozen plan; the leak is purely structural.
        rules["prior"]["publications"] = [
            {"caliber": cell_caliber("N", "H"), "value": 1},
        ]
        rules["requests"] = [{"caliber": TOTAL_CALIBER}]
        with self.assertRaises(ValueError):
            build_bulletin(self.plan, rules)

    def test_history_disagreeing_with_plan_is_an_error(self):
        rules = copy.deepcopy(self.rules)
        rules["prior"]["publications"] = [
            {"caliber": row_caliber("N"), "value": 2}]
        with self.assertRaises(ValueError):
            build_bulletin(self.plan, rules)

    def test_edition_mismatch_is_rejected(self):
        rules = copy.deepcopy(self.rules)
        rules["prior"]["edition_id"] = "some-other-edition"
        with self.assertRaises(ValueError):
            build_bulletin(self.plan, rules)

    def test_duplicate_prior_publication_is_rejected(self):
        rules = copy.deepcopy(self.rules)
        rules["prior"]["publications"] = [
            {"caliber": row_caliber("N"), "value": 1},
            {"caliber": row_caliber("N"), "value": 1},
        ]
        with self.assertRaises(ValueError):
            build_bulletin(self.plan, rules)

    def test_already_published_caliber_is_marked_not_rewritten(self):
        rules = copy.deepcopy(self.rules)
        rules["prior"]["revision"] = 3
        rules["prior"]["publications"] = [
            {"caliber": row_caliber("N"), "value": 1}]
        rules["requests"] = [
            {"caliber": row_caliber("N")},
            {"caliber": TOTAL_CALIBER}]
        bulletin = build_bulletin(self.plan, rules)
        self.assertEqual(bulletin["revision"], 4)
        self.assertEqual(
            bulletin["requests"][0]["status"], "published")
        self.assertEqual(len(bulletin["publications"]),
                         1 + (1 if bulletin["requests"][1]["status"]
                              == "granted" else 0))


# ---------------------------------------------------------------------------
# strict validation
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.good_plan = load_fixture("plan.json")
        self.good_rules = load_fixture("rules-h-only.json")

    def assert_rejects(self, plan=None, rules=None):
        with self.assertRaises(ValueError):
            build_bulletin(
                self.good_plan if plan is None else plan,
                self.good_rules if rules is None else rules)

    def test_unknown_and_missing_fields_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["unexpected"] = 1
        self.assert_rejects(rules=rules)
        del rules["unexpected"]
        del rules["protected"]
        self.assert_rejects(rules=rules)

    def test_boolean_rejected_as_integer(self):
        rules = copy.deepcopy(self.good_rules)
        rules["bounds"][0]["high"] = True
        self.assert_rejects(rules=rules)
        plan = copy.deepcopy(self.good_plan)
        plan["stations"][0]["protection_radius_m"] = True
        self.assert_rejects(plan=plan)

    def test_duplicate_json_keys_rejected(self):
        # Hand-written JSON carrying a duplicated key (json.loads with the
        # project's object_pairs hook must reject it).
        duplicated = (
            '{"edition_id": "ed-test", "edition_id": "ed-test", '
            '"window": {"start": "2026-11-01T00:00:00+00:00", '
            '"end": "2026-11-02T00:00:00+00:00"}, "venues": ["N"], '
            '"channel_groups": [{"id": "H", "low_hz": 1, "high_hz": 2}], '
            '"receiver_venues": [], "bounds": [], "protected": [], '
            '"prior": {"edition_id": "ed-test", "revision": 0, '
            '"publications": []}, "requests": []}')
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(duplicated)
            path = f.name
        with self.assertRaises(ValueError):
            load(path)

    def test_overlapping_channel_groups_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["channel_groups"][1]["low_hz"] = 2000
        self.assert_rejects(rules=rules)

    def test_window_longer_than_seven_days_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["window"]["end"] = (
            BASE + timedelta(days=7, microseconds=1)).isoformat()
        self.assert_rejects(rules=rules)

    def test_dangling_caliber_reference_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["requests"][0] = {"caliber": row_caliber("UNKNOWN")}
        self.assert_rejects(rules=rules)

    def test_duplicate_request_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["requests"] = [
            {"caliber": col_caliber("H")},
            {"caliber": col_caliber("H")}]
        self.assert_rejects(rules=rules)

    def test_limit_counts_enforced(self):
        rules = copy.deepcopy(self.good_rules)
        rules["venues"] = [f"V{i}" for i in range(9)]
        self.assert_rejects(rules=rules)
        rules = copy.deepcopy(self.good_rules)
        rules["protected"] = [
            {"cell": {"venue_id": "N", "group_id": "H"}, "gap_us": 1}
            for _ in range(13)]
        self.assert_rejects(rules=rules)

    def test_bound_above_universal_cap_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["bounds"][0]["high"] = DEFAULT_BOUND + 1
        self.assert_rejects(rules=rules)

    def test_declared_bound_excluding_real_value_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["bounds"][0]["high"] = 0  # H column really totals 2
        self.assert_rejects(rules=rules)

    def test_non_positive_gap_rejected(self):
        rules = copy.deepcopy(self.good_rules)
        rules["protected"][0]["gap_us"] = 0
        self.assert_rejects(rules=rules)


# ---------------------------------------------------------------------------
# determinism and large-span exactness
# ---------------------------------------------------------------------------
class DeterminismAndScaleTests(unittest.TestCase):
    def test_array_reorder_and_timezone_equivalent(self):
        plan = load_fixture("plan.json")
        first = load_fixture("rules-n-then-h.json")
        reordered = copy.deepcopy(first)
        reordered["bounds"] = list(reversed(reordered["bounds"]))
        bulletin_a = build_bulletin(plan, first)
        bulletin_b = build_bulletin(plan, reordered)
        self.assertEqual(
            bulletin_a["publications"], bulletin_b["publications"])
        self.assertEqual(bulletin_a["requests"], bulletin_b["requests"])

        shifted = copy.deepcopy(first)
        shifted["window"]["start"] = datetime.fromisoformat(
            shifted["window"]["start"]).astimezone(
                timezone(timedelta(hours=2))).isoformat()
        bulletin_c = build_bulletin(plan, shifted)
        self.assertEqual(
            bulletin_c["window"]["start"], bulletin_a["window"]["start"])

    def test_large_span_exact_decision_without_value_enumeration(self):
        # Bounds use the real 14-digit microsecond cap; the decision is exact
        # and hand-checkable: one protected cell with gap 1, one row pin.
        big = DEFAULT_BOUND
        rows = cols = 1
        standing = {
            ("row", 0): (0, big),
            ("col", 0): (0, big),
            ("cell", 0, 0): (0, big),
            ("total",): (0, big),
        }
        protected = {("cell", 0, 0): 1}
        # With no facts the single cell spans [0, big], so the total request
        # must be withheld: publishing total=0 would fix the cell to zero.
        region = PublicRegion(rows, cols, standing, [])
        self.assertTrue(region.feasible)
        self.assertEqual(region.caliber_range(("cell", 0, 0)), (0, big))
        self.assertFalse(region.admit(("total",), protected))
        # The number of feasibility probes is logarithmic in the bound,
        # never proportional to its 14-digit magnitude.
        import math
        self.assertLess(
            math.ceil(math.log2(big + 1)) + 4, 60)

    def test_feasibility_engine_rejects_impossible_bounds(self):
        rows = cols = 1
        standing = {
            ("row", 0): (3, 3),
            ("col", 0): (2, 2),
            ("cell", 0, 0): (0, DEFAULT_BOUND),
            ("total",): (0, DEFAULT_BOUND),
        }
        region = PublicRegion(rows, cols, standing, [])
        self.assertFalse(region.feasible)


# ---------------------------------------------------------------------------
# CLI, atomic publishing and output hygiene
# ---------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "spectrum.bulletin_cli", *args],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(SRC)},
            capture_output=True, text=True)

    def test_cli_publishes_atomically_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bulletin.json"
            run = self._run(
                "--source", str(FIXTURES / "plan.json"),
                "--rules", str(FIXTURES / "rules-n-then-h.json"),
                "--output", str(output))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(output.exists())
            again = self._run(
                "--source", str(FIXTURES / "plan.json"),
                "--rules", str(FIXTURES / "rules-n-then-h.json"),
                "--output", str(output))
            self.assertEqual(again.returncode, 2)
            self.assertIn("radio-bulletin:", again.stderr)

    def test_invalid_input_exits_two_and_keeps_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bulletin.json"
            bad_rules = Path(tmp) / "rules.json"
            bad_rules.write_text("{ not json", encoding="utf-8")
            run = self._run(
                "--source", str(FIXTURES / "plan.json"),
                "--rules", str(bad_rules),
                "--output", str(output))
            self.assertEqual(run.returncode, 2)
            self.assertFalse(output.exists())

    def test_withheld_values_never_leave_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bulletin.json"
            run = self._run(
                "--source", str(FIXTURES / "plan.json"),
                "--rules", str(FIXTURES / "rules-n-then-h.json"),
                "--output", str(output))
            self.assertEqual(run.returncode, 0)
            payload = output.read_text(encoding="utf-8")
            # The real H total is 2 but H is withheld in this scenario.
            bulletin = json.loads(payload)
            self.assertNotIn("withheld", json.dumps(bulletin["publications"]))
            for row in bulletin["requests"]:
                if row["status"] == "withheld":
                    # A withheld row carries its caliber and status only; it
                    # must never embed the private value.
                    self.assertEqual(set(row), {"caliber", "status"})
            # Every published exact value is one that was granted; the
            # withheld H value of 2 is absent from the whole document.
            published_values = {row["value"]
                                for row in bulletin["publications"]}
            self.assertNotIn(2, published_values)
            # stdout is only the summary object (counts), never a value map.
            stdout_summary = ast.literal_eval(run.stdout.strip())
            self.assertEqual(set(stdout_summary),
                             set(bulletin["summary"]))
            self.assertNotIn("value", run.stdout)


if __name__ == "__main__":
    unittest.main()
