"""Tests for the post-event execution review (``radio-coordinate --observations``).

Two layers, mirroring the arranger test style:

* an *independent* brute-force oracle that enumerates every feasible integer
  offset vector itself from the raw contract rules (it never imports any
  solver helper from :mod:`spectrum.observations`), checks every pair
  classification and every group extremum, and re-derives witnesses;
* hand-computed scenarios covering every situation mandated by the task
  statement, including the R/T/S/U exclusion example, strict contract
  validation and CLI/atomic-publish behavior.
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spectrum.observations import (
    build_review_report,
    review_executions,
)
from spectrum.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIX = ROOT / "fixtures" / "observations"
ORIGIN = "2026-08-01T10:00:00+00:00"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def station(sid, lat=0.0, lon=0.0, radius=1000):
    return {"id": sid, "lat": lat, "lon": lon, "protection_radius_m": radius}


def emission(tid, transmitter, receiver, center=100, bandwidth=10,
             start="2026-08-01T10:00:10+00:00",
             end="2026-08-01T10:00:11+00:00"):
    return {
        "id": tid, "station_id": transmitter, "receiver_id": receiver,
        "center_hz": center, "bandwidth_hz": bandwidth,
        "start": start, "end": end,
    }


def epoch(eid, low, high, origin=ORIGIN):
    return {"id": eid, "utc_origin": origin,
            "offset_min_us": low, "offset_max_us": high}


def sync(sid, left, right, low, high):
    return {"id": sid, "left_epoch_id": left, "right_epoch_id": right,
            "min_us": low, "max_us": high}


def execution(tid, eid, start_tick, end_tick, center=100, bandwidth=10):
    return {
        "transmission_id": tid, "epoch_id": eid,
        "start_tick_us": start_tick, "end_tick_us": end_tick,
        "center_hz": center, "bandwidth_hz": bandwidth,
    }


def pair(victim, interferer):
    return {"victim_transmission_id": victim,
            "interferer_transmission_id": interferer}


def group(gid, pairs):
    return {"id": gid, "pairs": pairs}


def rtu_plan():
    return {
        "stations": [
            station("R", 0.0, 0.0, 1000),
            station("T", 0.0, 0.001, 1),
            station("S", 0.0, 0.002, 1),
            station("U", 10.0, 10.0, 1),
        ],
        "transmissions": [
            emission("v", "T", "R",
                     start="2026-08-01T10:00:10+00:00",
                     end="2026-08-01T10:00:11+00:00"),
            emission("a", "S", "U",
                     start="2026-08-01T10:00:09+00:00",
                     end="2026-08-01T10:00:10+00:00"),
            emission("b", "S", "U",
                     start="2026-08-01T10:00:11+00:00",
                     end="2026-08-01T10:00:12+00:00"),
        ],
    }


def rtu_obs(e1_low=-1_000_000, e1_high=1_000_000, a_center=100,
            syncs=None, groups_=None, epochs=None):
    if epochs is None:
        epochs = [epoch("E0", 0, 0), epoch("E1", e1_low, e1_high)]
    return {
        "clock_epochs": epochs,
        "relative_syncs": [] if syncs is None else syncs,
        "executions": [
            execution("v", "E0", 10_000_000, 11_000_000),
            execution("a", "E1", 9_000_000, 10_000_000, center=a_center),
            execution("b", "E1", 11_000_000, 12_000_000),
        ],
        "groups": [group("g-exclusion", [pair("v", "a"), pair("v", "b")])]
        if groups_ is None else groups_,
    }


# ---------------------------------------------------------------------------
# independent brute-force oracle (raw contract rules only)
# ---------------------------------------------------------------------------
class BruteForceOracle:
    """Enumerates the complete feasible offset region microsecond by microsecond.

    Only usable for small domains; deliberately re-implements every rule from
    scratch instead of importing observation-module internals.  Geometry is
    still taken from the single source of truth in
    :mod:`spectrum.interference`, as the arranger oracle does.
    """

    def __init__(self, plan, observations):
        self.plan = plan
        self.stations = {row["id"]: row for row in plan["stations"]}
        self.plan_rows = {row["id"]: row for row in plan["transmissions"]}
        self.epochs = {row["id"]: row for row in observations["clock_epochs"]}
        self.epoch_ids = sorted(self.epochs)
        self.syncs = observations["relative_syncs"]
        self.executions = {
            row["transmission_id"]: row for row in observations["executions"]}
        self.groups = observations["groups"]
        self._axis_origin = datetime(1, 1, 1, tzinfo=timezone.utc)
        # Geometry and occupied bands never depend on the offset vector, so
        # the independent oracle evaluates each directed pair's static guard
        # exactly once instead of recomputing per microsecond world.
        from spectrum.interference import distance_m
        self._static = {}
        for victim_id, victim in self.executions.items():
            for interferer_id, interferer in self.executions.items():
                if victim_id == interferer_id:
                    continue
                vlo, vhi = self._band(victim)
                ilo, ihi = self._band(interferer)
                band_ok = max(vlo, ilo) < min(vhi, ihi)
                receiver = self.stations[
                    self.plan_rows[victim_id]["receiver_id"]]
                transmitter = self.stations[
                    self.plan_rows[interferer_id]["station_id"]]
                geo_ok = distance_m(receiver, transmitter) \
                    <= receiver["protection_radius_m"]
                self._static[(victim_id, interferer_id)] = band_ok and geo_ok
        self.vectors = self._enumerate()

    def _origin_us(self, epoch_id):
        origin = datetime.fromisoformat(
            self.epochs[epoch_id]["utc_origin"]).astimezone(timezone.utc)
        delta = origin - self._axis_origin
        return (delta.days * 86_400 + delta.seconds) * 1_000_000 \
            + delta.microseconds

    def _feasible(self, vector):
        for sync in self.syncs:
            difference = vector[sync["right_epoch_id"]] \
                - vector[sync["left_epoch_id"]]
            if not sync["min_us"] <= difference <= sync["max_us"]:
                return False
        return True

    def _enumerate(self):
        domains = [
            range(self.epochs[eid]["offset_min_us"],
                  self.epochs[eid]["offset_max_us"] + 1)
            for eid in self.epoch_ids
        ]
        result = []
        for values in itertools.product(*domains):
            vector = dict(zip(self.epoch_ids, values))
            if self._feasible(vector):
                result.append(vector)
        return result

    def feasible(self):
        return bool(self.vectors)

    def joint_bounds(self):
        return {
            eid: (
                min(vector[eid] for vector in self.vectors),
                max(vector[eid] for vector in self.vectors),
            )
            for eid in self.epoch_ids
        }

    def _window(self, observed, vector):
        epoch_id = observed["epoch_id"]
        base = self._origin_us(epoch_id)
        offset = vector[epoch_id]
        return (base + observed["start_tick_us"] + offset,
                base + observed["end_tick_us"] + offset)

    def _band(self, observed):
        center = observed["center_hz"]
        bandwidth = observed["bandwidth_hz"]
        return 2 * center - bandwidth, 2 * center + bandwidth

    def _window_axes(self):
        """Absolute base time (without per-vector offset) for every endpoint."""
        axes = {}
        for tid, observed in self.executions.items():
            base = self._origin_us(observed["epoch_id"])
            axes[tid] = (base + observed["start_tick_us"],
                         base + observed["end_tick_us"],
                         observed["epoch_id"])
        return axes

    def pair_fires(self, victim_id, interferer_id, vector, axes=None):
        if not self._static[(victim_id, interferer_id)]:
            return False
        if axes is None:
            axes = self._window_axes()
        v_start, v_end, v_epoch = axes[victim_id]
        i_start, i_end, i_epoch = axes[interferer_id]
        vs = v_start + vector[v_epoch]
        ve = v_end + vector[v_epoch]
        ins = i_start + vector[i_epoch]
        ine = i_end + vector[i_epoch]
        # Half-open windows: touching endpoints give no positive overlap.
        return max(vs, ins) < min(ve, ine)

    def classify(self, victim_id, interferer_id):
        if victim_id not in self.executions \
                or interferer_id not in self.executions:
            return "unobserved"
        if not self._static[(victim_id, interferer_id)]:
            return "absent"
        axes = self._window_axes()
        v_start, v_end, v_epoch = axes[victim_id]
        i_start, i_end, i_epoch = axes[interferer_id]
        ever_true = ever_false = False
        for vector in self.vectors:
            vs = v_start + vector[v_epoch]
            ve = v_end + vector[v_epoch]
            ins = i_start + vector[i_epoch]
            ine = i_end + vector[i_epoch]
            fires = max(vs, ins) < min(ve, ine)
            ever_true = ever_true or fires
            ever_false = ever_false or not fires
            if ever_true and ever_false:
                return "possible"
        return "certain" if ever_true else "absent"

    def group_extremum(self, group, want_max):
        keys = [(row["victim_transmission_id"],
                 row["interferer_transmission_id"]) for row in group["pairs"]]
        live_keys = [key for key in keys if self._static[key]]
        axes = self._window_axes()
        prepared = []
        for victim, interferer in live_keys:
            v_start, v_end, v_epoch = axes[victim]
            i_start, i_end, i_epoch = axes[interferer]
            prepared.append((v_start, v_end, v_epoch,
                             i_start, i_end, i_epoch))
        best_count = None
        best_vector = None
        for vector in self.vectors:
            count = 0
            for v_start, v_end, v_epoch, i_start, i_end, i_epoch in prepared:
                vs = v_start + vector[v_epoch]
                ve = v_end + vector[v_epoch]
                ins = i_start + vector[i_epoch]
                ine = i_end + vector[i_epoch]
                if max(vs, ins) < min(ve, ine):
                    count += 1
            ordered = tuple(vector[eid] for eid in self.epoch_ids)
            if best_count is None:
                take = True
            elif want_max:
                take = count > best_count or \
                    (count == best_count and ordered < best_vector)
            else:
                take = count < best_count or \
                    (count == best_count and ordered < best_vector)
            if take:
                best_count, best_vector = count, ordered
        return best_count, best_vector


# ---------------------------------------------------------------------------
# mandated R/T/S/U exclusion scenario
# ---------------------------------------------------------------------------
class MandatedExclusionScenario(unittest.TestCase):
    def test_individual_pairs_possible_but_group_never_both(self):
        plan, observations = rtu_plan(), rtu_obs()
        report = build_review_report(plan, observations)
        review = report["review"]
        self.assertEqual(review["status"], "reviewed")
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]
        }
        self.assertEqual(statuses[("v", "a")], "possible")
        self.assertEqual(statuses[("v", "b")], "possible")
        group = review["group_review"][0]
        self.assertEqual((group["min_conflicts"], group["max_conflicts"]),
                         (0, 1))
        min_offsets = {
            row["epoch_id"]: row["offset_us"]
            for row in group["min_witness"]["epoch_offsets_us"]}
        max_offsets = {
            row["epoch_id"]: row["offset_us"]
            for row in group["max_witness"]["epoch_offsets_us"]}
        # Exact tie-break witnesses required by the task statement.
        self.assertEqual(min_offsets, {"E0": 0, "E1": 0})
        self.assertEqual(max_offsets, {"E0": 0, "E1": -1_000_000})
        self.assertEqual(group["min_witness"]["occurring_pairs"], [])
        self.assertEqual(
            [(row["victim_transmission_id"], row["interferer_transmission_id"])
             for row in group["max_witness"]["occurring_pairs"]],
            [("v", "b")])
        # Witnesses are complete single-world plans the original service can
        # re-check, and it reproduces exactly the declared conflict count.
        for witness, expected in (
                (group["min_witness"], 0), (group["max_witness"], 1)):
            checked = run(witness["observed_plan"])
            self.assertEqual(
                checked["summary"]["directed_conflicts"], expected)
            self.assertEqual(
                checked["summary"]["directed_conflicts"],
                witness["conflict_count"])

    def test_fixed_offset_makes_one_certain_one_absent(self):
        review = review_executions(rtu_plan(), rtu_obs(500_000, 500_000))
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]
        }
        self.assertEqual(statuses[("v", "a")], "certain")
        self.assertEqual(statuses[("v", "b")], "absent")
        group = review["group_review"][0]
        self.assertEqual(group["min_conflicts"], 1)
        self.assertEqual(group["max_conflicts"], 1)
        for witness in (group["min_witness"], group["max_witness"]):
            offsets = {row["epoch_id"]: row["offset_us"]
                       for row in witness["epoch_offsets_us"]}
            self.assertEqual(offsets, {"E0": 0, "E1": 500_000})

    def test_retuned_observed_channel_removes_conflict_but_keeps_plan(self):
        report = build_review_report(
            rtu_plan(), rtu_obs(500_000, 500_000, a_center=200))
        review = report["review"]
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]
        }
        self.assertEqual(statuses[("v", "a")], "absent")
        self.assertEqual(statuses[("v", "b")], "absent")
        group = review["group_review"][0]
        self.assertEqual((group["min_conflicts"], group["max_conflicts"]),
                         (0, 0))
        # The planned report is carried verbatim and never rewritten by
        # observed findings.
        self.assertEqual(
            report["plan_report"], run(rtu_plan()))

    def test_matches_independent_brute_force(self):
        plan, observations = rtu_plan(), rtu_obs()
        oracle = BruteForceOracle(plan, observations)
        review = review_executions(plan, observations)
        for row in review["execution_review"]["rows"]:
            expected = oracle.classify(
                row["victim_transmission_id"],
                row["interferer_transmission_id"])
            self.assertEqual(row["status"], expected)
        group = review["group_review"][0]
        for want_max, got_count, got_witness in (
                (False, group["min_conflicts"], group["min_witness"]),
                (True, group["max_conflicts"], group["max_witness"])):
            count, vector = oracle.group_extremum(
                observations["groups"][0], want_max)
            self.assertEqual(got_count, count)
            witness_vector = tuple(
                row["offset_us"] for row in got_witness["epoch_offsets_us"])
            self.assertEqual(witness_vector, vector)


# ---------------------------------------------------------------------------
# clock evidence semantics
# ---------------------------------------------------------------------------
class ClockEvidenceTests(unittest.TestCase):
    def test_relative_chain_tightens_joint_bounds(self):
        observations = rtu_obs(syncs=[
            sync("s1", "E0", "E1", 200, 400),
        ])
        review = review_executions(rtu_plan(), observations)
        clock = {row["epoch_id"]: row for row in review["clock_review"]}
        # E0 fixed at 0, so E1 is jointly reachable only inside [200, 400].
        self.assertEqual(
            (clock["E1"]["joint_offset_min_us"],
             clock["E1"]["joint_offset_max_us"]),
            (200, 400))

    def test_multiple_syncs_on_same_pair_intersect(self):
        # Two distinct IDs constrain the same ordered pair; both must hold,
        # so the joint interval is their intersection, not either alone.
        observations = rtu_obs(syncs=[
            sync("s-wide", "E0", "E1", 0, 500),
            sync("s-narrow", "E0", "E1", 250, 400)])
        review = review_executions(rtu_plan(), observations)
        clock = {row["epoch_id"]: row for row in review["clock_review"]}
        self.assertEqual(
            (clock["E1"]["joint_offset_min_us"],
             clock["E1"]["joint_offset_max_us"]),
            (250, 400))

    def test_reversed_sync_direction_flips_the_sign(self):
        # E1 - E0 in [100, 200] written as E0 - E1 in [-200, -100] is the
        # identical constraint and must yield identical joint bounds.
        forward = review_executions(
            rtu_plan(), rtu_obs(syncs=[sync("s", "E0", "E1", 100, 200)]))
        reversed_ = review_executions(
            rtu_plan(), rtu_obs(syncs=[sync("s", "E1", "E0", -200, -100)]))
        self.assertEqual(
            json.dumps(forward["clock_review"], sort_keys=True),
            json.dumps(reversed_["clock_review"], sort_keys=True))

    def test_two_hop_chain_tightens_third_epoch(self):
        observations = {
            "clock_epochs": [
                epoch("E0", 0, 0), epoch("E1", -1_000_000, 1_000_000),
                epoch("E2", -1_000_000, 1_000_000)],
            "relative_syncs": [
                sync("s1", "E0", "E1", 100, 300),
                sync("s2", "E1", "E2", 100, 300)],
            "executions": [
                execution("v", "E0", 10_000_000, 11_000_000),
                execution("a", "E1", 9_000_000, 10_000_000),
                execution("b", "E2", 11_000_000, 12_000_000)],
            "groups": [],
        }
        review = review_executions(rtu_plan(), observations)
        clock = {row["epoch_id"]: row for row in review["clock_review"]}
        self.assertEqual(
            (clock["E1"]["joint_offset_min_us"],
             clock["E1"]["joint_offset_max_us"]), (100, 300))
        self.assertEqual(
            (clock["E2"]["joint_offset_min_us"],
             clock["E2"]["joint_offset_max_us"]), (200, 600))

    def test_disconnected_epochs_keep_independent_domains(self):
        observations = {
            "clock_epochs": [
                epoch("E0", 0, 0), epoch("E1", -5, 5),
                epoch("E2", 10, 20)],
            "relative_syncs": [],
            "executions": [
                execution("v", "E0", 10_000_000, 11_000_000),
                execution("a", "E1", 9_000_000, 10_000_000),
                execution("b", "E2", 11_000_000, 12_000_000)],
            "groups": [],
        }
        review = review_executions(rtu_plan(), observations)
        clock = {row["epoch_id"]: row for row in review["clock_review"]}
        self.assertEqual(
            (clock["E1"]["joint_offset_min_us"],
             clock["E1"]["joint_offset_max_us"]), (-5, 5))
        self.assertEqual(
            (clock["E2"]["joint_offset_min_us"],
             clock["E2"]["joint_offset_max_us"]), (10, 20))

    def test_jointly_contradictory_loop_is_inconsistent(self):
        # Each record alone is satisfiable; the three together form a
        # negative cycle (E2-E0 >= 200 but the closing evidence caps it at
        # 100), which only the joint system can detect.
        observations = {
            "clock_epochs": [
                epoch("E0", -1_000_000, 1_000_000),
                epoch("E1", -1_000_000, 1_000_000),
                epoch("E2", -1_000_000, 1_000_000)],
            "relative_syncs": [
                sync("s1", "E0", "E1", 100, 200),
                sync("s2", "E1", "E2", 100, 200),
                sync("s3", "E0", "E2", 0, 100)],
            "executions": [
                execution("v", "E0", 10_000_000, 11_000_000),
                execution("a", "E1", 9_000_000, 10_000_000),
                execution("b", "E2", 11_000_000, 12_000_000)],
            "groups": [],
        }
        report = build_review_report(rtu_plan(), observations)
        review = report["review"]
        self.assertEqual(review["status"], "inconsistent_clocks")
        self.assertIsNone(review["clock_review"])
        self.assertIsNone(review["execution_review"])
        self.assertIsNone(review["group_review"])
        # The planned report survives intact.
        self.assertEqual(
            report["plan_report"]["summary"], run(rtu_plan())["summary"])

    def test_single_integer_microsecond_feasible(self):
        observations = rtu_obs(syncs=[
            sync("s1", "E0", "E1", 7, 7)])
        review = review_executions(rtu_plan(), observations)
        clock = {row["epoch_id"]: row for row in review["clock_review"]}
        self.assertEqual(
            (clock["E1"]["joint_offset_min_us"],
             clock["E1"]["joint_offset_max_us"]), (7, 7))
        oracle = BruteForceOracle(rtu_plan(), observations)
        self.assertEqual(len(oracle.vectors), 1)

    def test_same_epoch_translation_keeps_relative_time_fixed(self):
        # Both observations on one epoch with a wide domain: shifting the
        # single shared offset translates both windows together, so overlap
        # is invariant.
        plan = rtu_plan()
        observations = {
            "clock_epochs": [epoch("E0", -500_000, 500_000)],
            "relative_syncs": [],
            "executions": [
                execution("v", "E0", 10_000_000, 11_000_000),
                execution("a", "E0", 10_500_000, 12_000_000)],
            "groups": [group("g", [pair("v", "a")])],
        }
        review = review_executions(plan, observations)
        row = next(item for item in review["execution_review"]["rows"]
                   if item["victim_transmission_id"] == "v"
                   and item["interferer_transmission_id"] == "a")
        self.assertEqual(row["status"], "certain")
        group_row = review["group_review"][0]
        self.assertEqual(group_row["min_conflicts"], 1)
        self.assertEqual(group_row["max_conflicts"], 1)

    def test_wide_domain_does_not_expand_per_microsecond(self):
        # Domain spans 7.2e9 microseconds; the cut-based search must finish
        # immediately and still match the hand-computed exclusion result.
        observations = rtu_obs(-3_600_000_000, 3_600_000_000)
        review = review_executions(rtu_plan(), observations)
        group = review["group_review"][0]
        self.assertEqual((group["min_conflicts"], group["max_conflicts"]),
                         (0, 1))
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "a")], "possible")
        self.assertEqual(statuses[("v", "b")], "possible")

    def test_offset_timezone_equivalence(self):
        shifted = rtu_obs()
        shifted["clock_epochs"][0]["utc_origin"] = \
            "2026-08-01T12:00:00+02:00"
        shifted["clock_epochs"][1]["utc_origin"] = \
            "2026-08-01T12:00:00+02:00"
        baseline = review_executions(rtu_plan(), rtu_obs())
        equivalent = review_executions(rtu_plan(), shifted)
        self.assertEqual(
            json.dumps(baseline, sort_keys=True),
            json.dumps(equivalent, sort_keys=True))


# ---------------------------------------------------------------------------
# pair semantics
# ---------------------------------------------------------------------------
class PairSemanticsTests(unittest.TestCase):
    def test_unobserved_takes_priority_and_is_never_substituted(self):
        observations = rtu_obs()
        observations["executions"] = [
            row for row in observations["executions"]
            if row["transmission_id"] != "b"]
        # Groups may only reference observed pairs; the unobserved pair (v, b)
        # is still covered below through the full directed pair table.
        observations["groups"] = [group("g-a", [pair("v", "a")])]
        review = review_executions(rtu_plan(), observations)
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "b")], "unobserved")
        self.assertEqual(statuses[("b", "v")], "unobserved")
        self.assertEqual(
            review["execution_review"]["unobserved_transmission_ids"], ["b"])
        self.assertEqual(statuses[("v", "a")], "possible")

    def test_empty_executions_is_valid_and_all_rows_unobserved(self):
        observations = {"clock_epochs": [epoch("E0", 0, 0)],
                        "relative_syncs": [], "executions": [], "groups": []}
        review = review_executions(rtu_plan(), observations)
        self.assertEqual(review["status"], "reviewed")
        self.assertTrue(all(
            row["status"] == "unobserved"
            for row in review["execution_review"]["rows"]))
        self.assertEqual(
            review["execution_review"]["unobserved_transmission_ids"],
            ["a", "b", "v"])
        self.assertEqual(review["group_review"], [])

    def test_half_hertz_band_and_direction_asymmetry(self):
        # F sits inside R's protection circle, while j's receiver W is far
        # away and its 1 m circle excludes T: the directed radio relation is
        # genuinely asymmetric, never collapsed into an undirected edge.
        plan = {
            "stations": [
                station("R", 0.0, 0.0, 1000),
                station("T", 0.0, 0.001, 1),
                station("F", 0.0, 0.0005, 1),
                station("W", 10.0, 10.0, 1)],
            "transmissions": [
                emission("v", "T", "R", center=100, bandwidth=3),
                emission("j", "F", "W", center=100, bandwidth=3,
                         start="2026-08-01T10:00:10+00:00",
                         end="2026-08-01T10:00:11+00:00"),
            ],
        }
        observations = {
            "clock_epochs": [epoch("E0", 0, 0)],
            "relative_syncs": [],
            "executions": [
                execution("v", "E0", 10_000_000, 11_000_000,
                          center=100, bandwidth=3),
                execution("j", "E0", 10_000_500, 10_500_000,
                          center=100, bandwidth=3)],
            "groups": [],
        }
        review = review_executions(plan, observations)
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "j")], "certain")
        self.assertEqual(statuses[("j", "v")], "absent")
        # An odd (half-Hz) bandwidth validates, keeps an occupied band above
        # zero Hz and is classified with integer microseconds only.
        self.assertEqual(observations["executions"][0]["bandwidth_hz"], 3)

    def test_touching_endpoint_is_absent_even_when_bands_overlap(self):
        plan = rtu_plan()
        observations = rtu_obs(0, 0)
        # E1 fixed at 0: a ends exactly when v starts, so the half-open
        # windows only touch.
        review = review_executions(plan, observations)
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "a")], "absent")


# ---------------------------------------------------------------------------
# group jointness
# ---------------------------------------------------------------------------
class GroupJointnessTests(unittest.TestCase):
    def test_two_possible_pairs_with_at_least_one_always_firing(self):
        # Two pairs that partition the nonzero offset domain, while a third
        # pair covers the zero vector: every feasible world therefore fires
        # at least one pair even though no a/b row is certain on its own.
        plan = rtu_plan()
        observations = rtu_obs(-1_000_000, 1_000_000)
        plan["transmissions"].append(
            emission("c", "S", "U",
                     start="2026-08-01T10:00:09+00:00",
                     end="2026-08-01T10:00:12+00:00"))
        observations["executions"].append(
            execution("c", "E1", 9_000_000, 12_000_000))
        observations["groups"] = [
            group("g-cover",
                  [pair("v", "a"), pair("v", "b"), pair("v", "c")])]
        review = review_executions(plan, observations)
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in review["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "c")], "certain")
        group_row = review["group_review"][0]
        # a and b are each only possible, yet every vector fires at least c,
        # so the minimum (1) exceeds the count of certain a/b rows (0).
        self.assertEqual(group_row["min_conflicts"], 1)
        self.assertEqual(group_row["max_conflicts"], 2)
        oracle = BruteForceOracle(plan, observations)
        min_count, min_vector = oracle.group_extremum(
            observations["groups"][0], False)
        max_count, max_vector = oracle.group_extremum(
            observations["groups"][0], True)
        self.assertEqual(group_row["min_conflicts"], min_count)
        self.assertEqual(group_row["max_conflicts"], max_count)
        self.assertEqual(
            tuple(item["offset_us"]
                  for item in group_row["min_witness"]["epoch_offsets_us"]),
            min_vector)
        self.assertEqual(
            tuple(item["offset_us"]
                  for item in group_row["max_witness"]["epoch_offsets_us"]),
            max_vector)

    def test_two_groups_use_different_extreme_worlds(self):
        plan = rtu_plan()
        observations = rtu_obs()
        observations["groups"] = [
            group("g-a", [pair("v", "a")]),
            group("g-b", [pair("v", "b")])]
        review = review_executions(plan, observations)
        by_id = {item["group_id"]: item for item in review["group_review"]}
        a_max = {item["epoch_id"]: item["offset_us"]
                 for item in by_id["g-a"]["max_witness"]["epoch_offsets_us"]}
        b_max = {item["epoch_id"]: item["offset_us"]
                 for item in by_id["g-b"]["max_witness"]["epoch_offsets_us"]}
        # Each single-pair group reaches its extremum 1 at the
        # lexicographically smallest firing vector; the two groups attain it
        # in different worlds (a fires only on positive E1, b only on
        # negative E1, whose smallest firing value is the domain lower end).
        self.assertEqual(a_max["E1"], 1)
        self.assertEqual(b_max["E1"], -1_000_000)
        # Each witness is internally one coherent world re-checkable by the
        # untouched original service; only that group's own pair is counted.
        pair_source = {
            item["id"]: {
                (pair_item["victim_transmission_id"],
                 pair_item["interferer_transmission_id"])
                for pair_item in item["pairs"]}
            for item in observations["groups"]}
        for group_row in by_id.values():
            group_keys = pair_source[group_row["group_id"]]
            for witness in (group_row["min_witness"],
                            group_row["max_witness"]):
                service_rows = run(witness["observed_plan"])["conflicts"]
                firing = {
                    (item["victim_transmission_id"],
                     item["interferer_transmission_id"])
                    for item in service_rows} & group_keys
                self.assertEqual(len(firing), witness["conflict_count"])


# ---------------------------------------------------------------------------
# randomized cross-check against the brute-force oracle
# ---------------------------------------------------------------------------
class OracleCrossCheck(unittest.TestCase):
    def _random_doc(self, seed):
        import random
        rng = random.Random(seed)
        epoch_count = rng.randint(1, 3)
        epochs = [
            epoch(f"E{i}", rng.randint(-4, 0), rng.randint(0, 4))
            for i in range(epoch_count)]
        syncs = []
        for sid in range(rng.randint(0, 3)):
            i, j = rng.sample(range(epoch_count), 2) if epoch_count >= 2 else (0, 0)
            if i == j:
                break
            low = rng.randint(-3, 2)
            syncs.append(sync(f"sync{sid}", f"E{i}", f"E{j}",
                              low, low + rng.randint(0, 4)))
        task_count = rng.randint(2, 4)
        stations = [station("R", 0.0, 0.0, 1000),
                    station("T", 0.0, 0.001, 1)]
        transmissions = []
        executions = []
        for tid in range(task_count):
            transmitter = "T"
            receiver = "R"
            transmissions.append(emission(
                f"t{tid}", transmitter, receiver,
                start="2026-08-01T10:00:00+00:00",
                end="2026-08-01T10:00:30+00:00"))
            eid = f"E{rng.randrange(epoch_count)}"
            start_tick = rng.randint(9_000_000, 12_000_000)
            executions.append(execution(
                f"t{tid}", eid, start_tick,
                start_tick + rng.randint(500_000, 2_000_000)))
        plan = {"stations": stations, "transmissions": transmissions}
        pair_keys = [(f"t{i}", f"t{j}")
                     for i in range(task_count) for j in range(task_count)
                     if i != j]
        rng.shuffle(pair_keys)
        groups = []
        if pair_keys:
            groups.append(group("g0",
                                [pair(*keys) for keys in pair_keys[:6]]))
        return plan, {
            "clock_epochs": epochs, "relative_syncs": syncs,
            "executions": executions, "groups": groups}

    def test_many_small_instances_match_oracle(self):
        for seed in range(60):
            plan, observations = self._random_doc(seed)
            try:
                review = review_executions(plan, observations)
            except ValueError:
                continue
            oracle = BruteForceOracle(plan, observations)
            if not oracle.feasible():
                self.assertEqual(review["status"], "inconsistent_clocks")
                continue
            self.assertEqual(review["status"], "reviewed")
            for eid, (low, high) in oracle.joint_bounds().items():
                clock = next(row for row in review["clock_review"]
                             if row["epoch_id"] == eid)
                self.assertEqual(clock["joint_offset_min_us"], low)
                self.assertEqual(clock["joint_offset_max_us"], high)
            observed = set(observations["executions"] and
                           {row["transmission_id"]
                            for row in observations["executions"]})
            for row in review["execution_review"]["rows"]:
                if row["victim_transmission_id"] not in observed or \
                        row["interferer_transmission_id"] not in observed:
                    self.assertEqual(row["status"], "unobserved")
                    continue
                expected = oracle.classify(
                    row["victim_transmission_id"],
                    row["interferer_transmission_id"])
                self.assertEqual(row["status"], expected,
                                 f"seed={seed} row={row}")
            for group_row, group_doc in zip(
                    review["group_review"], observations["groups"]):
                min_count, min_vector = oracle.group_extremum(
                    group_doc, False)
                max_count, max_vector = oracle.group_extremum(
                    group_doc, True)
                self.assertEqual(group_row["min_conflicts"], min_count)
                self.assertEqual(group_row["max_conflicts"], max_count)
                self.assertEqual(
                    tuple(row["offset_us"] for row in
                          group_row["min_witness"]["epoch_offsets_us"]),
                    min_vector)
                self.assertEqual(
                    tuple(row["offset_us"] for row in
                          group_row["max_witness"]["epoch_offsets_us"]),
                    max_vector)
                group_keys = {
                    (item["victim_transmission_id"],
                     item["interferer_transmission_id"])
                    for item in group_doc["pairs"]}
                for witness in (group_row["min_witness"],
                                group_row["max_witness"]):
                    service_rows = run(witness["observed_plan"])["conflicts"]
                    group_firing = sum(
                        1 for item in service_rows
                        if (item["victim_transmission_id"],
                            item["interferer_transmission_id"]) in group_keys)
                    self.assertEqual(
                        group_firing, witness["conflict_count"])


# ---------------------------------------------------------------------------
# strict validation
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = rtu_obs()

    def assert_invalid(self, observations):
        with self.assertRaises(ValueError):
            review_executions(rtu_plan(), observations)

    def test_root_must_have_exact_keys(self):
        broken = dict(self.base)
        del broken["groups"]
        self.assert_invalid(broken)
        broken = dict(self.base)
        broken["extra"] = 1
        self.assert_invalid(broken)

    def test_bad_field_shapes_rejected(self):
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"][0]["offset_min_us"] = True
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["center_hz"] = 1.5
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["start_tick_us"] = -1
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["end_tick_us"] = \
            broken["executions"][0]["start_tick_us"]
        self.assert_invalid(broken)

    def test_duplicate_and_unknown_identities_rejected(self):
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"].append(dict(broken["clock_epochs"][0]))
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"].append(dict(broken["executions"][0]))
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["epoch_id"] = "nope"
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["transmission_id"] = "ghost"
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["groups"][0]["pairs"].append(
            dict(broken["groups"][0]["pairs"][0]))
        self.assert_invalid(broken)

    def test_group_pairs_require_observations_and_distinct_sides(self):
        broken = json.loads(json.dumps(self.base))
        broken["groups"][0]["pairs"][0]["victim_transmission_id"] = "v"
        broken["groups"][0]["pairs"][0]["interferer_transmission_id"] = "v"
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["executions"] = [
            row for row in broken["executions"]
            if row["transmission_id"] != "b"]
        self.assert_invalid(broken)

    def test_epoch_and_sync_bounds_validated(self):
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"][1]["offset_min_us"] = 3_600_000_001
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"][1]["offset_max_us"] = \
            broken["clock_epochs"][1]["offset_min_us"] - 1
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["relative_syncs"] = [
            sync("s", "E0", "E1", 7_200_000_001, 7_200_000_001)]
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["relative_syncs"] = [sync("s", "E0", "E0", 0, 0)]
        self.assert_invalid(broken)

    def test_non_representable_datetime_rejected(self):
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"][0]["utc_origin"] = \
            "9999-12-31T23:59:59+00:00"
        self.assert_invalid(broken)

    def test_band_crossing_zero_and_bad_origin_rejected(self):
        broken = json.loads(json.dumps(self.base))
        broken["executions"][0]["center_hz"] = 2
        broken["executions"][0]["bandwidth_hz"] = 10
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"][0]["utc_origin"] = "2026-08-01T10:00:00"
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"] = "no"
        self.assert_invalid(broken)

    def test_duplicate_sync_id_and_dangling_sync_rejected(self):
        broken = json.loads(json.dumps(self.base))
        broken["relative_syncs"] = [
            sync("same", "E0", "E1", 0, 1),
            sync("same", "E0", "E1", 2, 3)]
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["relative_syncs"] = [sync("s", "E0", "E9", 0, 1)]
        self.assert_invalid(broken)

    def test_count_limits_enforced(self):
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"] = [
            epoch(f"E{i}", 0, 0) for i in range(9)]
        self.assert_invalid(broken)
        broken = json.loads(json.dumps(self.base))
        broken["clock_epochs"] = []
        self.assert_invalid(broken)
        # A group may hold at most 12 distinct pairs; reuse one to exceed it.
        broken = json.loads(json.dumps(self.base))
        repeated = broken["groups"][0]["pairs"][0]
        broken["groups"][0]["pairs"] = [dict(repeated) for _ in range(13)]
        self.assert_invalid(broken)
        # Empty pair list is rejected too.
        broken = json.loads(json.dumps(self.base))
        broken["groups"][0]["pairs"] = []
        self.assert_invalid(broken)

    def test_array_reordering_does_not_change_business_result(self):
        reordered = json.loads(json.dumps(self.base))
        reordered["clock_epochs"] = list(reversed(reordered["clock_epochs"]))
        reordered["executions"] = list(reversed(reordered["executions"]))
        reordered["groups"][0]["pairs"] = list(
            reversed(reordered["groups"][0]["pairs"]))
        self.assertEqual(
            json.dumps(review_executions(rtu_plan(), self.base), sort_keys=True),
            json.dumps(review_executions(rtu_plan(), reordered), sort_keys=True))


# ---------------------------------------------------------------------------
# CLI and publishing
# ---------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.env = dict(os.environ, PYTHONPATH=str(SRC))

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, source, output, observations=None):
        args = [sys.executable, "-m", "spectrum.cli",
                "--source", str(source), "--output", str(output)]
        if observations is not None:
            args += ["--observations", str(observations)]
        return subprocess.run(args, capture_output=True, text=True,
                              env=self.env, timeout=60)

    def test_without_observations_keeps_original_contract(self):
        source = self.dir / "plan.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(rtu_plan()), encoding="utf-8")
        completed = self._run(source, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(
            report["summary"], run(rtu_plan())["summary"])
        self.assertNotIn("review", report)

    def test_observation_mode_publishes_review_report(self):
        source = self.dir / "plan.json"
        obs = self.dir / "obs.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(rtu_plan()), encoding="utf-8")
        obs.write_text(json.dumps(rtu_obs()), encoding="utf-8")
        completed = self._run(source, output, obs)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["mode"], "execution_review")
        self.assertEqual(report["review"]["status"], "reviewed")
        self.assertEqual(
            report["plan_report"]["summary"], run(rtu_plan())["summary"])

    def test_inconsistent_clocks_exits_0_and_keeps_plan_report(self):
        source = self.dir / "plan.json"
        obs = self.dir / "obs.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(rtu_plan()), encoding="utf-8")
        obs.write_text((FIX / "chain-obs-inconsistent.json").read_text(),
                       encoding="utf-8")
        completed = self._run(source, output, obs)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["review"]["status"], "inconsistent_clocks")
        self.assertIsNone(report["review"]["group_review"])

    def test_malformed_observation_exits_2_and_preserves_target(self):
        source = self.dir / "plan.json"
        obs = self.dir / "bad.json"
        output = self.dir / "report.json"
        source.write_text(json.dumps(rtu_plan()), encoding="utf-8")
        bad = rtu_obs()
        bad["clock_epochs"][0]["id"] = 7
        obs.write_text(json.dumps(bad), encoding="utf-8")
        completed = self._run(source, output, obs)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())
        # An existing target must survive a failed second run.
        good_output = self.dir / "good.json"
        good_obs = self.dir / "good-obs.json"
        good_obs.write_text(json.dumps(rtu_obs()), encoding="utf-8")
        first = self._run(source, good_output, good_obs)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = good_output.read_bytes()
        second = self._run(source, good_output, obs)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(good_output.read_bytes(), before)


# ---------------------------------------------------------------------------
# shipped offline fixtures
# ---------------------------------------------------------------------------
class FixtureTests(unittest.TestCase):
    def test_mandated_fixture_files_match_contract_numbers(self):
        plan = json.loads((FIX / "rtu-plan.json").read_text())
        exclusion = json.loads(
            (FIX / "rtu-obs-exclusion.json").read_text())
        review = review_executions(plan, exclusion)
        group = review["group_review"][0]
        self.assertEqual((group["min_conflicts"], group["max_conflicts"]),
                         (0, 1))
        self.assertEqual(
            [row["offset_us"] for row in
             group["min_witness"]["epoch_offsets_us"]], [0, 0])
        self.assertEqual(
            [row["offset_us"] for row in
             group["max_witness"]["epoch_offsets_us"]], [0, -1_000_000])
        fixed = review_executions(
            plan, json.loads((FIX / "rtu-obs-fixed.json").read_text()))
        self.assertEqual(
            (fixed["group_review"][0]["min_conflicts"],
             fixed["group_review"][0]["max_conflicts"]), (1, 1))
        retuned = review_executions(
            plan, json.loads((FIX / "rtu-obs-retuned.json").read_text()))
        statuses = {
            (row["victim_transmission_id"], row["interferer_transmission_id"]):
                row["status"]
            for row in retuned["execution_review"]["rows"]}
        self.assertEqual(statuses[("v", "a")], "absent")
        empty = review_executions(
            plan, json.loads((FIX / "rtu-obs-empty.json").read_text()))
        self.assertTrue(all(
            row["status"] == "unobserved"
            for row in empty["execution_review"]["rows"]))
        wide = review_executions(
            plan, json.loads((FIX / "rtu-obs-wide.json").read_text()))
        self.assertEqual(
            (wide["group_review"][0]["min_conflicts"],
             wide["group_review"][0]["max_conflicts"]), (0, 1))
        inconsistent = build_review_report(
            plan, json.loads((FIX / "chain-obs-inconsistent.json").read_text()))
        self.assertEqual(
            inconsistent["review"]["status"], "inconsistent_clocks")


if __name__ == "__main__":
    unittest.main()
