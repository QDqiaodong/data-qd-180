"""Tests for the planned-loss settlement drafter ``radio-settle``.

Two layers:

* hand-computed scenarios with independent expected values (including the
  scenario mandated by the task statement and every required edge case);
* independent brute-force oracles that re-derive the optimum (allocation)
  and the loss groups (per-unit enumeration) from the contract text only,
  checked against the real implementation on many generated instances.

The allocation oracle enumerates every integer allocation over the
(group, operator) cells; the measurement oracle enumerates every
microsecond x half-Hz cell.  Neither imports any solver/sweep helper.
"""
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spectrum.service import run as coordinate_run
from spectrum.settle import (
    MAX_COMMITMENTS,
    MAX_CREDIT_CAPACITY,
    MAX_OPERATORS,
    build_settlement_report,
    solve_settlement,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "settle"

BASE = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)


def at(microseconds):
    return (BASE + timedelta(microseconds=microseconds)).isoformat()


def station(sid, lat=0.0, lon=0.0, radius=1000):
    return {"id": sid, "lat": lat, "lon": lon, "protection_radius_m": radius}


def emission(tid, transmitter, receiver, center, bandwidth=2,
             start=None, end=None):
    return {
        "id": tid, "station_id": transmitter, "receiver_id": receiver,
        "center_hz": center, "bandwidth_hz": bandwidth,
        "start": at(0) if start is None else start,
        "end": at(1) if end is None else end,
    }


def operator(oid, capacity):
    return {"id": oid, "credit_capacity": capacity}


def station_operator(sid, oid):
    return {"station_id": sid, "operator_id": oid}


def commitment(cid, tid, start, end, low, high, priority=0):
    return {
        "id": cid, "transmission_id": tid, "start": start, "end": end,
        "low_twice_hz": low, "high_twice_hz": high, "priority": priority,
    }


def terms_doc(operators, station_ops, commitments):
    return {
        "operators": list(operators),
        "station_operators": list(station_ops),
        "commitments": list(commitments),
    }


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def mandated_plan():
    return load_fixture("plan.json")


def mandated_terms():
    return load_fixture("terms.json")


def group_map(commitment_row):
    return {tuple(row["operator_ids"]): row
            for row in commitment_row["loss_groups"]}


def operator_map(report):
    return {row["operator_id"]: row for row in report["operators"]}


def commitment_map(report):
    return {row["commitment_id"]: row for row in report["commitments"]}


# ---------------------------------------------------------------------------
# mandated scenario from the task statement
# ---------------------------------------------------------------------------
class MandatedScenario(unittest.TestCase):
    def test_full_credit_settles_everything(self):
        report = build_settlement_report(mandated_plan(), mandated_terms())
        rows = commitment_map(report)
        ca, cb = rows["cA"], rows["cB"]
        # Every commitment spans 1 us x 1 Hz = 2 units; cA is covered by
        # both interferers, cB only by jia's emission.
        self.assertEqual(ca["commitment_units"], 2)
        self.assertEqual(cb["commitment_units"], 2)
        self.assertEqual(ca["loss_units"], 2)
        self.assertEqual(cb["loss_units"], 2)
        self.assertEqual(ca["compensable_loss_units"], 2)
        self.assertEqual(cb["compensable_loss_units"], 2)
        self.assertEqual(ca["beneficiary_operator_id"], "丙")
        self.assertEqual(set(group_map(ca)), {("乙", "甲")})
        self.assertEqual(set(group_map(cb)), {("甲",)})
        self.assertEqual(group_map(ca)[("乙", "甲")]["units"], 2)
        self.assertEqual(group_map(cb)[("甲",)]["units"], 2)
        # jia's 2 units are needed by cB, so yi covers cA.
        self.assertEqual(ca["paid_units"], 2)
        self.assertEqual(cb["paid_units"], 2)
        self.assertEqual(group_map(ca)[("乙", "甲")]["payments"],
                         [{"operator_id": "乙", "units": 2}])
        self.assertEqual(group_map(cb)[("甲",)]["payments"],
                         [{"operator_id": "甲", "units": 2}])
        operators = operator_map(report)
        self.assertEqual(operators["甲"]["payments"],
                         [{"commitment_id": "cB", "units": 2}])
        self.assertEqual(operators["乙"]["payments"],
                         [{"commitment_id": "cA", "units": 2}])
        self.assertEqual(operators["甲"]["remaining_credit"], 0)
        self.assertEqual(operators["乙"]["remaining_credit"], 0)
        self.assertEqual(operators["丙"]["receivable_units"], 4)
        self.assertEqual(report["totals"]["paid_units"], 4)
        self.assertEqual(report["totals"]["sum_commitment_paid_units"], 4)
        self.assertEqual(report["totals"]["sum_operator_paid_units"], 4)

    def test_tight_credit_gives_the_only_unit_to_a(self):
        terms = load_fixture("terms-tight.json")
        report = build_settlement_report(mandated_plan(), terms)
        rows = commitment_map(report)
        # Measurement is unchanged by credit or priority.
        self.assertEqual(rows["cA"]["loss_units"], 2)
        self.assertEqual(rows["cB"]["loss_units"], 2)
        self.assertEqual(set(group_map(rows["cA"])), {("乙", "甲")})
        self.assertEqual(set(group_map(rows["cB"])), {("甲",)})
        # Only jia's single unit can be paid; higher-priority cA gets it.
        self.assertEqual(rows["cA"]["paid_units"], 1)
        self.assertEqual(rows["cB"]["paid_units"], 0)
        self.assertEqual(rows["cA"]["unpaid_units"], 1)
        self.assertEqual(rows["cB"]["unpaid_units"], 2)
        self.assertEqual(group_map(rows["cA"])[("乙", "甲")]["payments"],
                         [{"operator_id": "甲", "units": 1}])
        operators = operator_map(report)
        self.assertEqual(operators["甲"]["paid_units"], 1)
        self.assertEqual(operators["乙"]["paid_units"], 0)
        self.assertEqual(operators["丙"]["receivable_units"], 1)
        self.assertEqual(report["totals"]["paid_units"], 1)

    def test_fixture_files_are_used_by_the_cli_demo(self):
        # The fixtures double as the end-to-end demo inputs; keep them
        # loadable through the plain function entry point.
        for name in ("terms.json", "terms-tight.json"):
            report = build_settlement_report(mandated_plan(),
                                             load_fixture(name))
            self.assertEqual(report["tool"], "radio-settle")


# ---------------------------------------------------------------------------
# loss measurement (hand-computed)
# ---------------------------------------------------------------------------
def overlap_plan():
    """One commitment, two interferers overlapping in both dimensions."""
    stations = [
        station("R", 0.0, 0.0, 5000),
        station("TV", 5.0, 5.0, 1),
        station("TP", 0.0, 0.01, 1),
        station("TQ", 0.0, 0.02, 1),
        station("Z", 30.0, 30.0, 1),
    ]
    transmissions = [
        emission("v", "TV", "R", 10, bandwidth=3, start=at(0), end=at(3)),
        emission("iP", "TP", "Z", 9, bandwidth=4, start=at(0), end=at(2)),
        emission("iQ", "TQ", "Z", 12, bandwidth=4, start=at(1), end=at(3)),
    ]
    return {"stations": stations, "transmissions": transmissions}


def overlap_terms():
    return terms_doc(
        [operator("P", 100), operator("Q", 100), operator("ben", 100)],
        [station_operator("R", "ben"), station_operator("TV", "ben"),
         station_operator("TP", "P"), station_operator("TQ", "Q"),
         station_operator("Z", "P")],
        [commitment("c", "v", at(0), at(3), 17, 23)],
    )


class MeasurementTests(unittest.TestCase):
    def test_two_dimensional_partial_overlap(self):
        report = build_settlement_report(overlap_plan(), overlap_terms())
        row = report["commitments"][0]
        self.assertEqual(row["commitment_units"], 18)  # 3 us x 6 half-Hz
        self.assertEqual(row["loss_units"], 14)
        groups = group_map(row)
        self.assertEqual(groups[("P",)]["units"], 8)
        self.assertEqual(groups[("P", "Q")]["units"], 2)
        self.assertEqual(groups[("Q",)]["units"], 4)
        # Evidence rows sum to 16 because the shared cell is listed per
        # interferer; the grouped loss never double-counts it.
        evidence = {row_["interferer_transmission_id"]: row_
                    for row_ in row["evidence"]}
        self.assertEqual(evidence["iP"]["units"], 10)  # 2 us x 5 half-Hz
        self.assertEqual(evidence["iQ"]["units"], 6)   # 2 us x 3 half-Hz
        self.assertEqual(evidence["iP"]["low_twice_hz"], 17)
        self.assertEqual(evidence["iP"]["high_twice_hz"], 22)
        self.assertEqual(evidence["iQ"]["low_twice_hz"], 20)
        self.assertEqual(evidence["iQ"]["high_twice_hz"], 23)
        self.assertEqual(evidence["iP"]["start"], at(0))
        self.assertEqual(evidence["iP"]["end"], at(2))

    def test_half_hertz_and_touching_endpoints(self):
        stations = [
            station("R", 0.0, 0.0, 5000),
            station("TV", 5.0, 5.0, 1),
            station("TX", 0.0, 0.01, 1),
            station("TB", 0.0, 0.02, 1),
            station("TT", 0.0, 0.03, 1),
            station("Z", 30.0, 30.0, 1),
        ]
        transmissions = [
            # Victim band [999.5, 1000.5) Hz == twice-Hz [1999, 2001).
            emission("v", "TV", "R", 1000, bandwidth=1,
                     start=at(0), end=at(2)),
            # Band ends exactly at the commitment low edge: no overlap.
            emission("iBand", "TB", "Z", 999, bandwidth=1,
                     start=at(0), end=at(2)),
            # Time ends exactly at the commitment start: no overlap.
            emission("iTime", "TT", "Z", 1000, bandwidth=1,
                     start=at(-2), end=at(0)),
            # Real overlap: [2000, 2001) twice-Hz = 0.5 Hz for 2 us.
            emission("iHit", "TX", "Z", 1001, bandwidth=2,
                     start=at(0), end=at(2)),
        ]
        terms = terms_doc(
            [operator("X", 10), operator("Y", 10), operator("ben", 10)],
            [station_operator("R", "ben"), station_operator("TV", "ben"),
             station_operator("TX", "X"), station_operator("TB", "Y"),
             station_operator("TT", "Y"), station_operator("Z", "X")],
            [commitment("c", "v", at(0), at(2), 1999, 2001)],
        )
        report = build_settlement_report(
            {"stations": stations, "transmissions": transmissions}, terms)
        row = report["commitments"][0]
        self.assertEqual(row["commitment_units"], 4)  # 2 us x 2 half-Hz
        self.assertEqual(row["loss_units"], 2)        # 2 us x 1 half-Hz
        self.assertEqual([e["interferer_transmission_id"]
                          for e in row["evidence"]], ["iHit"])
        self.assertEqual(row["evidence"][0]["low_twice_hz"], 2000)
        self.assertEqual(row["evidence"][0]["high_twice_hz"], 2001)
        self.assertEqual(group_map(row)[("X",)]["units"], 2)

    def test_same_operator_multiple_sources_counts_once(self):
        stations = [
            station("R", 0.0, 0.0, 5000),
            station("TV", 5.0, 5.0, 1),
            station("T1", 0.0, 0.01, 1),
            station("T2", 0.0, 0.02, 1),
            station("Z", 30.0, 30.0, 1),
        ]
        transmissions = [
            emission("v", "TV", "R", 1000, start=at(0), end=at(1)),
            emission("i1", "T1", "Z", 1000, start=at(0), end=at(1)),
            emission("i2", "T2", "Z", 1000, start=at(0), end=at(1)),
        ]
        terms = terms_doc(
            [operator("X", 10), operator("ben", 10)],
            [station_operator("R", "ben"), station_operator("TV", "ben"),
             station_operator("T1", "X"), station_operator("T2", "X"),
             station_operator("Z", "ben")],
            [commitment("c", "v", at(0), at(1), 1998, 2000)],
        )
        report = build_settlement_report(
            {"stations": stations, "transmissions": transmissions}, terms)
        row = report["commitments"][0]
        self.assertEqual(row["loss_units"], 2)  # not 2+2
        self.assertEqual(set(group_map(row)), {("X",)})
        self.assertEqual(len(row["evidence"]), 2)

    def test_own_only_and_external_mix(self):
        stations = [
            station("R", 0.0, 0.0, 5000),
            station("TV", 5.0, 5.0, 1),
            station("TB", 0.0, 0.01, 1),
            station("TX", 0.0, 0.02, 1),
            station("Z", 30.0, 30.0, 1),
            station("Z2", 31.0, 31.0, 1),
        ]
        transmissions = [
            emission("v", "TV", "R", 1000, start=at(0), end=at(3)),
            # Beneficiary's own emission covers the first 2 us.
            emission("iOwn", "TB", "Z2", 1000, start=at(0), end=at(2)),
            # External emission covers the last 2 us.
            emission("iExt", "TX", "Z", 1000, start=at(1), end=at(3)),
        ]
        terms = terms_doc(
            [operator("X", 10), operator("ben", 10)],
            [station_operator("R", "ben"), station_operator("TV", "ben"),
             station_operator("TB", "ben"), station_operator("TX", "X"),
             station_operator("Z", "X"), station_operator("Z2", "ben")],
            [commitment("c", "v", at(0), at(3), 1998, 2000)],
        )
        report = build_settlement_report(
            {"stations": stations, "transmissions": transmissions}, terms)
        row = report["commitments"][0]
        self.assertEqual(row["loss_units"], 6)
        self.assertEqual(row["own_only_loss_units"], 2)
        self.assertEqual(row["compensable_loss_units"], 4)
        groups = group_map(row)
        self.assertEqual(groups[()]["units"], 2)       # own-only region
        self.assertEqual(groups[("X",)]["units"], 4)   # shared + external
        # The empty set cannot produce compensation.
        self.assertEqual(groups[()]["allocated_units"], 0)
        self.assertEqual(groups[()]["payments"], [])
        self.assertEqual(row["paid_units"], 4)

    def test_commitment_subregion_clips_evidence(self):
        stations = [
            station("R", 0.0, 0.0, 5000),
            station("TV", 5.0, 5.0, 1),
            station("TX", 0.0, 0.01, 1),
            station("Z", 30.0, 30.0, 1),
        ]
        transmissions = [
            emission("v", "TV", "R", 1000, bandwidth=20,
                     start=at(0), end=at(10)),
            emission("i", "TX", "Z", 1000, bandwidth=20,
                     start=at(0), end=at(10)),
        ]
        terms = terms_doc(
            [operator("X", 100), operator("ben", 100)],
            [station_operator("R", "ben"), station_operator("TV", "ben"),
             station_operator("TX", "X"), station_operator("Z", "X")],
            [commitment("c", "v", at(3), at(5), 1995, 2000)],
        )
        report = build_settlement_report(
            {"stations": stations, "transmissions": transmissions}, terms)
        row = report["commitments"][0]
        self.assertEqual(row["commitment_units"], 10)  # 2 us x 5 half-Hz
        self.assertEqual(row["loss_units"], 10)
        evidence = row["evidence"][0]
        self.assertEqual(evidence["start"], at(3))
        self.assertEqual(evidence["end"], at(5))
        self.assertEqual(evidence["low_twice_hz"], 1995)
        self.assertEqual(evidence["high_twice_hz"], 2000)

    def test_protection_radius_threshold(self):
        def build(radius):
            stations = [
                station("R", 0.0, 0.0, radius),
                station("TV", 5.0, 5.0, 1),
                station("TX", 0.0, 0.01, 1),  # ~1111.95 m from R
                station("Z", 30.0, 30.0, 1),
            ]
            transmissions = [
                emission("v", "TV", "R", 1000, start=at(0), end=at(1)),
                emission("i", "TX", "Z", 1000, start=at(0), end=at(1)),
            ]
            terms = terms_doc(
                [operator("X", 10), operator("ben", 10)],
                [station_operator("R", "ben"), station_operator("TV", "ben"),
                 station_operator("TX", "X"), station_operator("Z", "X")],
                [commitment("c", "v", at(0), at(1), 1998, 2000)],
            )
            return build_settlement_report(
                {"stations": stations, "transmissions": transmissions},
                terms)

        inside = build(1112)["commitments"][0]
        outside = build(1111)["commitments"][0]
        self.assertEqual(inside["loss_units"], 2)
        self.assertEqual(outside["loss_units"], 0)
        self.assertEqual(outside["evidence"], [])
        self.assertEqual(outside["loss_groups"], [])

    def test_zero_credit_operator_stays_in_the_set(self):
        report = build_settlement_report(overlap_plan(), terms_doc(
            [operator("P", 0), operator("Q", 100), operator("ben", 100)],
            [station_operator("R", "ben"), station_operator("TV", "ben"),
             station_operator("TP", "P"), station_operator("TQ", "Q"),
             station_operator("Z", "P")],
            [commitment("c", "v", at(0), at(3), 17, 23)],
        ))
        row = report["commitments"][0]
        groups = group_map(row)
        self.assertEqual(groups[("P",)]["units"], 8)  # P kept despite 0
        self.assertEqual(row["compensable_loss_units"], 14)
        # P cannot pay; Q alone covers the groups containing Q.
        self.assertEqual(row["paid_units"], 6)
        self.assertEqual(row["unpaid_units"], 8)

    def test_measurement_ignores_credit_and_priority(self):
        base = build_settlement_report(overlap_plan(), overlap_terms())
        changed = overlap_terms()
        changed["operators"][0]["credit_capacity"] = 0
        changed["operators"][1]["credit_capacity"] = 1
        changed["commitments"][0]["priority"] = 999
        other = build_settlement_report(overlap_plan(), changed)

        def measured(report):
            return [
                ([(group["operator_ids"], group["units"])
                  for group in row["loss_groups"]], row["evidence"])
                for row in report["commitments"]
            ]

        self.assertEqual(measured(base), measured(other))


# ---------------------------------------------------------------------------
# compensation allocation (hand-computed, abstract groups)
# ---------------------------------------------------------------------------
class AllocationTests(unittest.TestCase):
    def test_shared_credit_across_groups_and_commitments(self):
        commitments = [
            {"id": "c1", "priority": 0,
             "groups": [(frozenset({"甲"}), 3), (frozenset({"乙"}), 3)]},
            {"id": "c2", "priority": 1,
             "groups": [(frozenset({"甲"}), 3)]},
        ]
        result = solve_settlement(commitments, {"甲": 3, "乙": 3})
        self.assertEqual(result["total_units"], 6)
        self.assertEqual(result["commitment_paid"], {"c1": 6, "c2": 0})
        self.assertEqual(
            result["operator_paid_by_commitment"]["c1"], {"甲": 3, "乙": 3})
        self.assertEqual(
            result["operator_paid_by_commitment"]["c2"], {"甲": 0, "乙": 0})

    def test_id_order_does_not_starve_the_total(self):
        # cX can be paid by jia or yi, cY only by jia; using jia for cX
        # first (jia sorts after yi, but even a perfect-id-order greedy on
        # cX would be wrong) must not shrink the total.
        commitments = [
            {"id": "cX", "priority": 0,
             "groups": [(frozenset({"甲", "乙"}), 1)]},
            {"id": "cY", "priority": 1,
             "groups": [(frozenset({"甲"}), 1)]},
        ]
        result = solve_settlement(commitments, {"甲": 1, "乙": 1})
        self.assertEqual(result["total_units"], 2)
        self.assertEqual(result["commitment_paid"], {"cX": 1, "cY": 1})
        self.assertEqual(
            result["operator_paid_by_commitment"]["cX"]["乙"], 1)
        self.assertEqual(
            result["operator_paid_by_commitment"]["cX"]["甲"], 0)
        self.assertEqual(
            result["operator_paid_by_commitment"]["cY"]["甲"], 1)

    def test_invoice_tie_breaks_by_operator_id(self):
        # Total and per-commitment amounts are tied across allocations;
        # only the invoice split differs.  The smaller operator id wins.
        commitments = [
            {"id": "c", "priority": 0,
             "groups": [(frozenset({"alpha", "beta"}), 2)]},
        ]
        result = solve_settlement(commitments, {"alpha": 2, "beta": 2})
        self.assertEqual(result["total_units"], 2)
        self.assertEqual(
            result["operator_paid_by_commitment"]["c"],
            {"alpha": 2, "beta": 0})

    def test_invoice_tie_break_respects_commitment_order(self):
        commitments = [
            {"id": "c1", "priority": 0,
             "groups": [(frozenset({"alpha", "beta"}), 1)]},
            {"id": "c2", "priority": 1,
             "groups": [(frozenset({"alpha", "beta"}), 1)]},
        ]
        result = solve_settlement(commitments, {"alpha": 1, "beta": 1})
        self.assertEqual(result["total_units"], 2)
        self.assertEqual(
            result["operator_paid_by_commitment"]["c1"],
            {"alpha": 1, "beta": 0})
        self.assertEqual(
            result["operator_paid_by_commitment"]["c2"],
            {"alpha": 0, "beta": 1})

    def test_priority_beats_commitment_id(self):
        commitments = [
            {"id": "a-first-by-id", "priority": 5,
             "groups": [(frozenset({"alpha"}), 1)]},
            {"id": "b-second-by-id", "priority": 1,
             "groups": [(frozenset({"alpha"}), 1)]},
        ]
        result = solve_settlement(commitments, {"alpha": 1})
        self.assertEqual(result["total_units"], 1)
        self.assertEqual(result["commitment_paid"],
                         {"a-first-by-id": 0, "b-second-by-id": 1})

    def test_zero_credit_everywhere(self):
        commitments = [
            {"id": "c", "priority": 0,
             "groups": [(frozenset({"alpha"}), 5)]},
        ]
        result = solve_settlement(commitments, {"alpha": 0})
        self.assertEqual(result["total_units"], 0)
        self.assertEqual(result["commitment_paid"], {"c": 0})
        self.assertEqual(result["group_payments"], {"c": [{}]})

    def test_empty_set_group_never_pays(self):
        commitments = [
            {"id": "c", "priority": 0,
             "groups": [(frozenset(), 7), (frozenset({"alpha"}), 3)]},
        ]
        result = solve_settlement(commitments, {"alpha": 10})
        self.assertEqual(result["total_units"], 3)
        self.assertEqual(result["group_payments"]["c"][0], {})
        self.assertEqual(result["group_payments"]["c"][1], {"alpha": 3})


# ---------------------------------------------------------------------------
# independent oracles on generated instances
# ---------------------------------------------------------------------------
def oracle_allocation(commitments, capacities):
    """Enumerate every integer allocation and keep the lexicographic best.

    Re-implements the contract from scratch: per-group caps, per-operator
    credit, then the three lexicographic levels.
    """
    order = sorted(
        range(len(commitments)),
        key=lambda i: (commitments[i]["priority"], commitments[i]["id"]))
    ordered_ids = [commitments[i]["id"] for i in order]
    operators = sorted(capacities)
    cells = []
    for ci, row in enumerate(commitments):
        for gi, (ops, _units) in enumerate(row["groups"]):
            for op in sorted(ops):
                cells.append((ci, gi, op))
    group_left = [[units for _ops, units in row["groups"]]
                  for row in commitments]
    op_left = {op: capacities[op] for op in operators}
    assign = [0] * len(cells)
    best = [None]

    def key_of():
        per_commitment = {}
        per_pair = {}
        total = 0
        for (ci, gi, op), value in zip(cells, assign):
            if value:
                cid = commitments[ci]["id"]
                per_commitment[cid] = per_commitment.get(cid, 0) + value
                per_pair[(cid, op)] = per_pair.get((cid, op), 0) + value
                total += value
        return (
            total,
            tuple(per_commitment.get(cid, 0) for cid in ordered_ids),
            tuple(per_pair.get((cid, op), 0)
                  for cid in ordered_ids for op in operators),
        )

    def visit(k):
        if k == len(cells):
            key = key_of()
            if best[0] is None or key > best[0]:
                best[0] = key
            return
        ci, gi, op = cells[k]
        for value in range(min(op_left[op], group_left[ci][gi]) + 1):
            assign[k] = value
            op_left[op] -= value
            group_left[ci][gi] -= value
            visit(k + 1)
            op_left[op] += value
            group_left[ci][gi] += value
        assign[k] = 0

    visit(0)
    return best[0]


def solver_key(commitments, capacities):
    result = solve_settlement(commitments, capacities)
    order = sorted(commitments, key=lambda row: (row["priority"], row["id"]))
    ordered_ids = [row["id"] for row in order]
    operators = sorted(capacities)
    paid = result["commitment_paid"]
    invoice = result["operator_paid_by_commitment"]
    return (
        result["total_units"],
        tuple(paid[cid] for cid in ordered_ids),
        tuple(invoice[cid][op] for cid in ordered_ids for op in operators),
    )


def oracle_measure(rects, beneficiary):
    """Per-cell enumeration of the loss groups on a tiny grid."""
    if not rects:
        return {}
    t0 = min(rect[0] for rect in rects)
    t1 = max(rect[1] for rect in rects)
    f0 = min(rect[2] for rect in rects)
    f1 = max(rect[3] for rect in rects)
    groups = {}
    for t in range(t0, t1):
        for f in range(f0, f1):
            ops = {rect[4] for rect in rects
                   if rect[0] <= t < rect[1] and rect[2] <= f < rect[3]}
            if ops:
                key = frozenset(ops) - {beneficiary}
                groups[key] = groups.get(key, 0) + 1
    return groups


class OracleCrossCheck(unittest.TestCase):
    def test_allocation_matches_brute_force(self):
        from spectrum.settle import _loss_groups  # noqa: F401  (used below)
        rng = random.Random(20261122)
        checked = 0
        for _ in range(250):
            n_ops = rng.randint(1, 3)
            ops = [f"op{k}" for k in range(n_ops)]
            capacities = {op: rng.randint(0, 4) for op in ops}
            commitments = []
            for ci in range(rng.randint(0, 2)):
                groups = []
                for _gi in range(rng.randint(0, 2)):
                    size = rng.randint(0, n_ops)
                    members = frozenset(rng.sample(ops, size))
                    groups.append((members, rng.randint(1, 3)))
                commitments.append({
                    "id": f"c{ci}",
                    "priority": rng.randint(0, 2),
                    "groups": groups,
                })
            self.assertEqual(
                solver_key(commitments, capacities),
                oracle_allocation(commitments, capacities),
                (commitments, capacities))
            self._assert_split_conserved(commitments, capacities)
            checked += 1
        self.assertGreaterEqual(checked, 200)

    def _assert_split_conserved(self, commitments, capacities):
        result = solve_settlement(commitments, capacities)
        invoice = result["operator_paid_by_commitment"]
        splits = result["group_payments"]
        for row in commitments:
            cid = row["id"]
            self.assertEqual(len(splits[cid]), len(row["groups"]))
            for (ops, units), payments in zip(row["groups"], splits[cid]):
                self.assertLessEqual(set(payments), ops)
                self.assertLessEqual(sum(payments.values()), units)
            for op in capacities:
                self.assertEqual(
                    sum(pay.get(op, 0) for pay in splits[cid]),
                    invoice[cid][op])
        for op in capacities:
            owed = sum(invoice[row["id"]][op] for row in commitments)
            self.assertLessEqual(owed, capacities[op])
        self.assertEqual(
            result["total_units"],
            sum(result["commitment_paid"].values()))
        self.assertEqual(
            result["total_units"],
            sum(amount for row in commitments
                for amount in invoice[row["id"]].values()))

    def test_measurement_matches_per_cell_enumeration(self):
        from spectrum.settle import _loss_groups
        rng = random.Random(4242)
        for _ in range(400):
            rects = []
            ops = ["A", "B", "ben"]
            for _i in range(rng.randint(0, 6)):
                t0 = rng.randint(0, 5)
                t1 = rng.randint(t0 + 1, 7)
                f0 = rng.randint(0, 5)
                f1 = rng.randint(f0 + 1, 7)
                rects.append((t0, t1, f0, f1, rng.choice(ops)))
            evidence = [
                {"start_us": t0, "end_us": t1, "low_twice_hz": f0,
                 "high_twice_hz": f1, "operator_id": op}
                for t0, t1, f0, f1, op in rects
            ]
            self.assertEqual(
                _loss_groups(evidence, "ben"),
                oracle_measure(rects, "ben"),
                rects)


# ---------------------------------------------------------------------------
# exact big integers, wide ranges, no per-unit expansion
# ---------------------------------------------------------------------------
class BigIntegerTests(unittest.TestCase):
    def test_wide_time_and_band_are_exact(self):
        plan = {
            "stations": [
                station("R", 0.0, 0.0, 5000),
                station("T", 5.0, 5.0, 1),
                station("I", 0.0, 0.01, 1),
                station("Z", 30.0, 30.0, 1),
            ],
            "transmissions": [
                emission("v", "T", "R", 10 ** 18, bandwidth=10 ** 18,
                         start="2026-09-01T00:00:00+00:00",
                         end="2026-09-08T00:00:00+00:00"),
                emission("i", "I", "Z", 10 ** 18, bandwidth=10 ** 18,
                         start="2026-09-01T00:00:00+00:00",
                         end="2026-09-08T00:00:00+00:00"),
            ],
        }
        terms = terms_doc(
            [operator("A", MAX_CREDIT_CAPACITY), operator("B", 0)],
            [station_operator("R", "B"), station_operator("T", "B"),
             station_operator("I", "A"), station_operator("Z", "A")],
            [commitment("c", "v", "2026-09-01T00:00:00+00:00",
                        "2026-09-08T00:00:00+00:00",
                        10 ** 18, 3 * 10 ** 18)],
        )
        report = build_settlement_report(plan, terms)
        row = report["commitments"][0]
        expected = 604_800_000_000 * 2 * 10 ** 18  # 7 days in us x width
        self.assertEqual(row["commitment_units"], expected)
        self.assertEqual(row["loss_units"], expected)
        self.assertEqual(row["paid_units"], 10 ** 24)
        self.assertEqual(row["unpaid_units"], expected - 10 ** 24)
        operators = operator_map(report)
        self.assertEqual(operators["A"]["remaining_credit"], 0)
        self.assertEqual(operators["B"]["receivable_units"], 10 ** 24)

    def test_many_interferers_one_commitment(self):
        stations = [station("R", 0.0, 0.0, 50000)]
        transmissions = []
        station_ops = [station_operator("R", "ben")]
        operators = [operator("ben", 0)]
        for i in range(120):
            stations.append(station(f"T{i}", 0.0, 0.0005 * (i % 20 + 1), 1))
            station_ops.append(station_operator(f"T{i}", f"op{i % 8}"))
            operators.append(operator(f"op{i % 8}", 10 ** 18))
        operators = list({row["id"]: row for row in operators}.values())
        stations.append(station("Z", 30.0, 30.0, 1))
        station_ops.append(station_operator("Z", "op0"))
        transmissions.append(emission(
            "v", "T0", "R", 1000, bandwidth=30,
            start="2026-09-01T00:00:00+00:00", end="2026-09-01T06:00:00+00:00"))
        for i in range(1, 121):
            transmissions.append(emission(
                f"i{i}", f"T{i % 120}", "Z", 1000 + (i % 40) - 20,
                bandwidth=30 + (i % 25),
                start=f"2026-09-01T00:{i % 60:02d}:00+00:00",
                end=f"2026-09-01T0{5 + (i % 2)}:{i % 60:02d}:00+00:00"))
        terms = terms_doc(
            operators, station_ops,
            [commitment("c", "v", "2026-09-01T01:00:00+00:00",
                        "2026-09-01T05:00:00+00:00", 1975, 2025)])
        report = build_settlement_report(
            {"stations": stations, "transmissions": transmissions}, terms)
        row = report["commitments"][0]
        self.assertGreater(row["loss_units"], 0)
        self.assertEqual(row["loss_units"],
                         sum(g["units"] for g in row["loss_groups"]))
        self.assertEqual(row["paid_units"], row["compensable_loss_units"])


# ---------------------------------------------------------------------------
# strict validation
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def base_plan(self):
        return mandated_plan()

    def base_terms(self):
        return mandated_terms()

    def assert_invalid_plan(self, mutate):
        plan = self.base_plan()
        terms = self.base_terms()
        mutate(plan)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            build_settlement_report(plan, terms)

    def assert_invalid_terms(self, mutate):
        plan = self.base_plan()
        terms = self.base_terms()
        mutate(terms)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            build_settlement_report(plan, terms)

    def test_plan_structure(self):
        self.assert_invalid_plan(lambda p: p.__setitem__("extra", 1))
        self.assert_invalid_plan(lambda p: p.pop("stations"))
        self.assert_invalid_plan(lambda p: p["stations"].__setitem__(
            slice(None), []))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "extra", 1))
        self.assert_invalid_plan(lambda p: p["stations"][0].pop("lat"))
        self.assert_invalid_plan(lambda p: p["stations"].append(
            dict(p["stations"][0])))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "lat", 91))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "lon", "east"))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "lat", True))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "protection_radius_m", 0))
        self.assert_invalid_plan(lambda p: p["stations"][0].__setitem__(
            "protection_radius_m", True))

    def test_plan_transmission_rules(self):
        self.assert_invalid_plan(lambda p: p["transmissions"].append(
            dict(p["transmissions"][0])))  # duplicate id
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "station_id", "nope"))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "receiver_id", "nope"))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "receiver_id", p["transmissions"][0]["station_id"]))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "center_hz", 0))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "center_hz", True))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "bandwidth_hz", True))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "bandwidth_hz", 10 ** 9))  # band below zero
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "start", "2026-09-01T10:00:00"))  # no offset
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "end", p["transmissions"][0]["start"]))  # non-positive
        self.assert_invalid_plan(lambda p: p["transmissions"][0].__setitem__(
            "extra", 1))
        self.assert_invalid_plan(lambda p: p["transmissions"][0].pop("start"))

    def test_plan_limits(self):
        def too_many_transmissions(plan):
            plan["transmissions"] = [
                emission(f"x{i}", "TA", "R", 1000, start=at(i % 5),
                         end=at(i % 5 + 1))
                for i in range(301)
            ]
        self.assert_invalid_plan(too_many_transmissions)

        def too_many_stations(plan):
            plan["stations"] = [
                station(f"S{i}", 0.0, 0.0, 1) for i in range(501)
            ]
        self.assert_invalid_plan(too_many_stations)

    def test_operators_rules(self):
        self.assert_invalid_terms(lambda t: t.__setitem__("extra", 1))
        self.assert_invalid_terms(lambda t: t.pop("operators"))
        self.assert_invalid_terms(lambda t: t["operators"].append(
            operator("甲", 1)))  # duplicate id
        self.assert_invalid_terms(lambda t: t["operators"][0].__setitem__(
            "credit_capacity", -1))
        self.assert_invalid_terms(lambda t: t["operators"][0].__setitem__(
            "credit_capacity", MAX_CREDIT_CAPACITY + 1))
        self.assert_invalid_terms(lambda t: t["operators"][0].__setitem__(
            "credit_capacity", True))
        self.assert_invalid_terms(lambda t: t["operators"][0].__setitem__(
            "credit_capacity", 1.5))
        self.assert_invalid_terms(lambda t: t["operators"][0].__setitem__(
            "extra", 1))
        self.assert_invalid_terms(lambda t: t["operators"].__setitem__(
            slice(None), [operator(f"op{i}", 1)
                          for i in range(MAX_OPERATORS + 1)]))

    def test_station_operators_rules(self):
        self.assert_invalid_terms(lambda t: t["station_operators"].append(
            station_operator("nope", "甲")))  # dangling station
        self.assert_invalid_terms(lambda t: t["station_operators"].append(
            station_operator("R", "甲")))  # station assigned twice
        self.assert_invalid_terms(lambda t: t["station_operators"][0]
                                  .__setitem__("operator_id", "nope"))
        self.assert_invalid_terms(lambda t: t["station_operators"].pop())
        self.assert_invalid_terms(lambda t: t["station_operators"][0]
                                  .__setitem__("extra", 1))

    def test_commitments_rules(self):
        self.assert_invalid_terms(lambda t: t["commitments"].append(
            dict(t["commitments"][0])))  # duplicate id
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "transmission_id", "nope"))
        self.assert_invalid_terms(lambda t: t["commitments"][1].__setitem__(
            "transmission_id", "taskA"))  # one commitment per task
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "start", "2026-09-01T10:00:00"))  # no offset
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "start", "2026-09-01T10:00:00.0000001+00:00"))  # 7 digits
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "end", t["commitments"][0]["start"]))  # non-positive
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "start", "2026-09-01T09:59:59.999999+00:00"))  # before task
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "end", "2026-09-01T10:00:00.000002+00:00"))  # after task
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "low_twice_hz", 1997))  # below occupied band
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "high_twice_hz", 2003))  # above occupied band
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "low_twice_hz", 2000))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "high_twice_hz", 1998))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "low_twice_hz", True))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "priority", -1))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "priority", 1000))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "priority", True))
        self.assert_invalid_terms(lambda t: t["commitments"][0].__setitem__(
            "extra", 1))
        self.assert_invalid_terms(lambda t: t["commitments"][0].pop("start"))
        self.assert_invalid_terms(lambda t: t["commitments"].__setitem__(
            slice(None), [
                commitment(f"c{i}", f"task{i}", at(0), at(1), 1998, 2000)
                for i in range(MAX_COMMITMENTS + 1)
            ]))

    def test_validation_covers_uninvolved_records(self):
        # A transmission that touches no commitment is still fully checked.
        plan = self.base_plan()
        plan["transmissions"].append(
            emission("stray", "TA", "R", 1000, bandwidth=10 ** 9,
                     start=at(0), end=at(1)))
        with self.assertRaises(ValueError):
            build_settlement_report(plan, self.base_terms())

    def test_duplicate_json_keys_rejected(self):
        from spectrum.io import load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load(path)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_reordering_and_timezone_notation_do_not_change_report(self):
        plan = mandated_plan()
        terms = mandated_terms()
        base = build_settlement_report(plan, terms)

        shuffled = json.loads(json.dumps(plan))
        shuffled["stations"].reverse()
        shuffled["transmissions"].reverse()
        shuffled_terms = json.loads(json.dumps(terms))
        shuffled_terms["operators"].reverse()
        shuffled_terms["station_operators"].reverse()
        shuffled_terms["commitments"].reverse()
        self.assertEqual(
            build_settlement_report(shuffled, shuffled_terms), base)

        tz = json.loads(json.dumps(terms))
        tz["commitments"][0]["start"] = "2026-09-01T12:00:00+02:00"
        tz["commitments"][0]["end"] = "2026-09-01T12:00:00.000001+02:00"
        self.assertEqual(build_settlement_report(plan, tz), base)

    def test_report_is_json_round_trip_stable(self):
        report = build_settlement_report(mandated_plan(), mandated_terms())
        self.assertEqual(
            json.loads(json.dumps(report, ensure_ascii=False)), report)


# ---------------------------------------------------------------------------
# normal zero results
# ---------------------------------------------------------------------------
class ZeroResultTests(unittest.TestCase):
    def test_no_commitments(self):
        terms = mandated_terms()
        terms["commitments"] = []
        report = build_settlement_report(mandated_plan(), terms)
        self.assertEqual(report["commitments"], [])
        self.assertEqual(report["totals"]["paid_units"], 0)
        self.assertEqual(len(report["operators"]), 3)
        self.assertEqual(operator_map(report)["丙"]["receivable_units"], 0)

    def test_no_interference(self):
        plan = mandated_plan()
        for row in plan["stations"]:
            if row["id"] == "R":
                row["protection_radius_m"] = 1  # nobody inside
        report = build_settlement_report(plan, mandated_terms())
        rows = commitment_map(report)
        self.assertEqual(rows["cA"]["loss_units"], 0)
        self.assertEqual(rows["cB"]["loss_units"], 0)
        self.assertEqual(rows["cA"]["paid_units"], 0)
        self.assertEqual(report["totals"]["paid_units"], 0)

    def test_only_own_operator_interferes(self):
        plan = mandated_plan()
        # Give every interferer to the beneficiary operator.
        terms = mandated_terms()
        for row in terms["station_operators"]:
            row["operator_id"] = "丙"
        report = build_settlement_report(plan, terms)
        rows = commitment_map(report)
        self.assertEqual(rows["cA"]["loss_units"], 2)
        self.assertEqual(rows["cA"]["own_only_loss_units"], 2)
        self.assertEqual(rows["cA"]["compensable_loss_units"], 0)
        self.assertEqual(rows["cA"]["paid_units"], 0)
        self.assertEqual(set(group_map(rows["cA"])), {()})


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def _run_cli(self, plan_path, terms_path, output_path):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run(
            [sys.executable, "-m", "spectrum.settle_cli",
             "--source", str(plan_path), "--terms", str(terms_path),
             "--output", str(output_path)],
            capture_output=True, text=True, env=env, timeout=60)

    def test_cli_writes_atomically_and_preserves_existing_target(self):
        plan = self._write("plan.json", mandated_plan())
        terms = self._write("terms.json", mandated_terms())
        output = self.dir / "report.json"
        first = self._run_cli(plan, terms, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(output.exists())
        before = output.read_bytes()
        second = self._run_cli(plan, terms, output)
        self.assertEqual(second.returncode, 2)
        self.assertIn("radio-settle", second.stderr)
        self.assertEqual(output.read_bytes(), before)
        report = json.loads(before)
        self.assertEqual(report["totals"]["paid_units"], 4)

    def test_cli_invalid_input_exits_2_without_report(self):
        plan = self._write("plan.json", mandated_plan())
        bad_terms = mandated_terms()
        bad_terms["operators"][0]["credit_capacity"] = True
        terms = self._write("terms.json", bad_terms)
        output = self.dir / "report.json"
        completed = self._run_cli(plan, terms, output)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())

    def test_cli_missing_terms_file_exits_2(self):
        plan = self._write("plan.json", mandated_plan())
        output = self.dir / "report.json"
        completed = self._run_cli(plan, self.dir / "absent.json", output)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())

    def test_cli_zero_result_exits_0(self):
        plan = self._write("plan.json", mandated_plan())
        terms_value = mandated_terms()
        terms_value["commitments"] = []
        terms = self._write("terms.json", terms_value)
        output = self.dir / "report.json"
        completed = self._run_cli(plan, terms, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["commitments"], [])


# ---------------------------------------------------------------------------
# compatibility with the original commands
# ---------------------------------------------------------------------------
class CompatibilityTests(unittest.TestCase):
    def test_settle_plan_runs_through_radio_coordinate(self):
        report = coordinate_run(mandated_plan())
        pairs = sorted((row["victim_transmission_id"],
                        row["interferer_transmission_id"])
                       for row in report["conflicts"])
        self.assertEqual(pairs, [("taskA", "iJia"), ("taskA", "iYi"),
                                 ("taskB", "iJia")])

    def test_inputs_are_not_mutated(self):
        plan = mandated_plan()
        terms = mandated_terms()
        snapshot = json.dumps(
            {"plan": plan, "terms": terms}, ensure_ascii=False,
            sort_keys=True)
        build_settlement_report(plan, terms)
        self.assertEqual(
            json.dumps({"plan": plan, "terms": terms},
                       ensure_ascii=False, sort_keys=True),
            snapshot)


if __name__ == "__main__":
    unittest.main()
