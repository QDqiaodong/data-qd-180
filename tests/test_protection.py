"""Tests for station-fact report recomputation (``radio-protection``).

Layers, mirroring the existing suites:

* hand-computed mandated scenarios (the R / v / a / b correction example and
  all required edge cases);
* an independent probe oracle that re-derives every station status, directed
  conflict/undetermined relation and reception state from the raw contract
  text (it never imports any helper from :mod:`spectrum.protection`) and
  compares it with the published report at every change boundary and halfway
  between adjacent boundaries;
* strict contract-validation, CLI/atomic-publish and near-limit performance
  tests, including a generator for churn unrelated to the active window.
"""
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spectrum.interference import distance_m
from spectrum.protection import build_protection_report
from spectrum.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIX = ROOT / "fixtures" / "protection"

W0 = "2026-08-01T10:00:00+00:00"
W1 = "2026-08-01T11:00:00+00:00"
DAY0 = "2026-08-01T00:00:00+00:00"
DAY1 = "2026-08-02T00:00:00+00:00"
PUB = "2026-08-01T09:00:00+00:00"
REV = "2026-08-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def station(sid, lat=0.0, lon=0.0, radius=1):
    return {"id": sid, "lat": lat, "lon": lon, "protection_radius_m": radius}


def task(tid, transmitter, receiver, center=100, bandwidth=10, start=W0, end=W1):
    return {
        "id": tid, "station_id": transmitter, "receiver_id": receiver,
        "center_hz": center, "bandwidth_hz": bandwidth,
        "start": start, "end": end,
    }


def assert_event(source, site, fact, known, start=DAY0, end=DAY1,
                 lat=0.0, lon=0.0, radius=300):
    return {
        "source_id": source, "station_id": site, "fact_id": fact,
        "known_at": known, "op": "assert",
        "effective_start": start, "effective_end": end,
        "lat": lat, "lon": lon, "protection_radius_m": radius,
    }


def withdraw_event(source, site, fact, known):
    return {
        "source_id": source, "station_id": site, "fact_id": fact,
        "known_at": known, "op": "withdraw",
    }


def facts_doc(events, sources=None, window=(W0, W1), published=PUB, revised=REV):
    return {
        "window_start": window[0], "window_end": window[1],
        "published_as_of": published, "revised_as_of": revised,
        "sources": sources if sources is not None else [
            {"id": "duty", "priority": 5},
            {"id": "survey", "priority": 1},
        ],
        "events": events,
    }


def base_plan():
    return {
        "stations": [
            station("R", 0, 0),
            station("T", 10, 10),
            station("A", 0, 0.002),
            station("B", 0, 0.001),
            station("U", 20, 20),
        ],
        "transmissions": [
            task("v", "T", "R"),
            task("a", "A", "U", start=W0, end="2026-08-01T10:40:00+00:00"),
            task("b", "B", "U", start="2026-08-01T10:20:00+00:00", end=W1),
        ],
    }


def base_events(source="duty", known="2026-08-01T08:00:00+00:00", radius=300):
    rows = [
        assert_event(source, "R", "r-base", known, radius=radius),
        assert_event(source, "T", "t-base", known, lat=10, lon=10, radius=1),
        assert_event(source, "A", "a-base", known, lon=0.002, radius=1),
        assert_event(source, "B", "b-base", known, lon=0.001, radius=1),
        assert_event(source, "U", "u-base", known, lat=20, lon=20, radius=1),
    ]
    return rows


def narrow_radius(radius=150, start="2026-08-01T10:15:00+00:00",
                  end="2026-08-01T10:35:00+00:00",
                  known="2026-08-01T11:30:00+00:00", source="survey",
                  fact="r-narrow"):
    return assert_event(source, "R", fact, known, start, end, radius=radius)


def reception(report, view, transmission_id):
    for row in report["views"][view]["report"]["reception_timelines"]:
        if row["transmission_id"] == transmission_id:
            return row
    raise AssertionError(f"no reception timeline for {transmission_id}")


def station_segments(report, view, station_id):
    for row in report["views"][view]["stations"]:
        if row["station_id"] == station_id:
            return row["segments"]
    raise AssertionError(f"no station timeline for {station_id}")


def compact(segments):
    return [
        (row["start"][11:19], row["end"][11:19], row["status"])
        for row in segments
    ]


# ---------------------------------------------------------------------------
# mandated correction example
# ---------------------------------------------------------------------------
class MandatedExample(unittest.TestCase):
    def setUp(self):
        self.report = build_protection_report(
            base_plan(), facts_doc(base_events() + [narrow_radius()]))

    def test_published_view_affects_v_for_sixty_minutes(self):
        row = reception(self.report, "published", "v")
        self.assertEqual(row["durations_us"], {
            "affected": 3_600_000_000,
            "undetermined": 0,
            "clear": 0,
        })
        self.assertEqual(compact(row["segments"]), [
            ("10:00:00", "10:20:00", "affected"),
            ("10:20:00", "10:40:00", "affected"),
            ("10:40:00", "11:00:00", "affected"),
        ])
        # a and b both fall inside the 300 m circle in the published view.
        self.assertEqual(
            row["segments"][0]["interferer_transmission_ids"], ["a"])
        self.assertEqual(
            row["segments"][1]["interferer_transmission_ids"], ["a", "b"])

    def test_revised_view_affects_v_for_fifty_five_minutes(self):
        row = reception(self.report, "revised", "v")
        self.assertEqual(row["durations_us"], {
            "affected": 3_300_000_000,
            "undetermined": 0,
            "clear": 300_000_000,
        })

    def test_exact_corrections(self):
        self.assertEqual(
            [
                (
                    row["transmission_id"],
                    row["start"], row["end"],
                    row["before"]["status"],
                    row["before"]["interferer_transmission_ids"],
                    row["after"]["status"],
                    row["after"]["interferer_transmission_ids"],
                )
                for row in self.report["corrections"]
            ],
            [
                ("v", "2026-08-01T10:15:00+00:00",
                 "2026-08-01T10:20:00+00:00",
                 "affected", ["a"], "clear", []),
                ("v", "2026-08-01T10:20:00+00:00",
                 "2026-08-01T10:35:00+00:00",
                 "affected", ["a", "b"], "affected", ["b"]),
            ],
        )

    def test_station_timeline_reports_both_views(self):
        published = station_segments(self.report, "published", "R")
        self.assertEqual(compact(published), [
            ("10:00:00", "11:00:00", "resolved")])
        self.assertEqual(
            published[0]["values"],
            [{"lat": 0.0, "lon": 0.0, "protection_radius_m": 300}])
        revised = station_segments(self.report, "revised", "R")
        self.assertEqual(compact(revised), [
            ("10:00:00", "10:15:00", "resolved"),
            ("10:15:00", "10:35:00", "resolved"),
            ("10:35:00", "11:00:00", "resolved"),
        ])
        self.assertEqual(
            [row["values"][0]["protection_radius_m"] for row in revised],
            [300, 150, 300])
        # Every version that justifies the segment stays visible.
        self.assertEqual(
            [(item["source_id"], item["fact_id"]) for item in revised[1]["evidence"]],
            [("survey", "r-narrow")])
        self.assertEqual(revised[1]["evidence"][0]["known_at"],
                         "2026-08-01T11:30:00+00:00")

    def test_window_cutoffs_and_version_are_reported(self):
        self.assertEqual(self.report["window"],
                         {"start": W0, "end": W1})
        self.assertEqual(self.report["published_as_of"], PUB)
        self.assertEqual(self.report["revised_as_of"], REV)
        self.assertEqual(self.report["schema_version"], 1)
        self.assertEqual(self.report["tool"], "radio-protection")
        self.assertEqual(self.report["views"]["published"]["as_of"], PUB)
        self.assertEqual(self.report["views"]["revised"]["as_of"], REV)


# ---------------------------------------------------------------------------
# required edge cases
# ---------------------------------------------------------------------------
class EdgeCases(unittest.TestCase):
    def test_peer_dispute_covers_lower_priority_and_is_undetermined(self):
        events = base_events() + [
            narrow_radius(150, source="survey", fact="r-peer-a"),
            narrow_radius(160, source="survey", fact="r-peer-b"),
        ]
        report = build_protection_report(base_plan(), facts_doc(events))
        revised = station_segments(report, "revised", "R")
        self.assertEqual(compact(revised), [
            ("10:00:00", "10:15:00", "resolved"),
            ("10:15:00", "10:35:00", "disputed"),
            ("10:35:00", "11:00:00", "resolved"),
        ])
        self.assertEqual(
            sorted(value["protection_radius_m"]
                   for value in revised[1]["values"]),
            [150, 160])
        # The lower-priority 300 m layer is never used to break the tie.
        self.assertEqual(
            revised[1]["values"],
            [{"lat": 0.0, "lon": 0.0, "protection_radius_m": 150},
             {"lat": 0.0, "lon": 0.0, "protection_radius_m": 160}])
        row = reception(report, "revised", "v")
        self.assertEqual(row["durations_us"], {
            "affected": 2_400_000_000,
            "undetermined": 1_200_000_000,
            "clear": 0,
        })
        self.assertEqual(compact(row["segments"]), [
            ("10:00:00", "10:15:00", "affected"),
            ("10:15:00", "10:20:00", "undetermined"),
            ("10:20:00", "10:35:00", "undetermined"),
            ("10:35:00", "10:40:00", "affected"),
            ("10:40:00", "11:00:00", "affected"),
        ])
        undetermined = report["views"]["revised"]["report"]["undetermined"]
        self.assertEqual(len(undetermined), 2)
        for row in undetermined:
            self.assertEqual(row["victim_transmission_id"], "v")
            self.assertEqual(row["endpoints"], [{
                "role": "receiver", "station_id": "R", "status": "disputed"}])
        self.assertEqual(
            report["corrections"][0]["after"][
                "undetermined_interferer_transmission_ids"],
            ["a"])

    def test_withdrawal_after_publication_restores_lower_priority(self):
        events = base_events() + [
            narrow_radius(150, known="2026-08-01T08:30:00+00:00"),
            withdraw_event("survey", "R", "r-narrow", "2026-08-01T11:30:00+00:00"),
        ]
        report = build_protection_report(base_plan(), facts_doc(events))
        # Published knew only the 150 m declaration.
        self.assertEqual(
            reception(report, "published", "v")["durations_us"]["affected"],
            3_300_000_000)
        # Revised has no effective survey assertion, so the 300 m duty fact
        # is authoritative again (not "no data").
        self.assertEqual(
            reception(report, "revised", "v")["durations_us"]["affected"],
            3_600_000_000)
        self.assertEqual(
            station_segments(report, "revised", "R")[0]["evidence"][0]["source_id"],
            "duty")
        self.assertEqual(self.report_corrections(report), [
            ("v", "10:15:00", "10:20:00", "clear", [], "affected", ["a"]),
            ("v", "10:20:00", "10:35:00", "affected", ["b"],
             "affected", ["a", "b"]),
        ])

    def test_shortened_declaration_does_not_reuse_superseded_tail(self):
        events = base_events() + [
            narrow_radius(150, known="2026-08-01T08:30:00+00:00"),
            narrow_radius(150, start="2026-08-01T10:20:00+00:00",
                          end="2026-08-01T10:30:00+00:00"),
        ]
        report = build_protection_report(base_plan(), facts_doc(events))
        revised = station_segments(report, "revised", "R")
        self.assertEqual(compact(revised), [
            ("10:00:00", "10:20:00", "resolved"),
            ("10:20:00", "10:30:00", "resolved"),
            ("10:30:00", "11:00:00", "resolved"),
        ])
        # If the superseded 10:30..10:35 tail were reused, a would stay clear
        # there and v would be affected 55 minutes instead of 60.
        self.assertEqual(
            reception(report, "revised", "v")["durations_us"]["affected"],
            3_600_000_000)
        self.assertEqual(self.report_corrections(report), [
            ("v", "10:15:00", "10:20:00", "clear", [], "affected", ["a"]),
            ("v", "10:30:00", "10:35:00", "affected", ["b"],
             "affected", ["a", "b"]),
        ])

    def test_determined_and_undetermined_evidence_share_one_segment(self):
        events = base_events() + [
            # Same-priority peer conflict on the interferer station B only;
            # station R stays resolved, so a is still certainly interfering.
            # Both arrive after publication, so they only affect revision.
            assert_event("survey", "B", "b-peer", "2026-08-01T11:30:00+00:00",
                         "2026-08-01T10:20:00+00:00",
                         "2026-08-01T10:45:00+00:00",
                         lon=0.001, radius=1),
            assert_event("survey", "B", "b-peer-2", "2026-08-01T11:30:00+00:00",
                         "2026-08-01T10:20:00+00:00",
                         "2026-08-01T10:45:00+00:00",
                         lon=0.005, radius=1),
        ]
        report = build_protection_report(base_plan(), facts_doc(events))
        row = reception(report, "revised", "v")
        self.assertEqual(compact(row["segments"]), [
            ("10:00:00", "10:20:00", "affected"),
            ("10:20:00", "10:40:00", "affected"),
            ("10:40:00", "10:45:00", "undetermined"),
            ("10:45:00", "11:00:00", "affected"),
        ])
        # Even when already affected, the undetermined set is retained.
        mixed = row["segments"][1]
        self.assertEqual(mixed["interferer_transmission_ids"], ["a"])
        self.assertEqual(
            mixed["undetermined_interferer_transmission_ids"], ["b"])
        self.assertEqual(row["durations_us"], {
            "affected": 3_300_000_000,
            "undetermined": 300_000_000,
            "clear": 0,
        })
        mixed_correction = next(
            correction for correction in report["corrections"]
            if correction["transmission_id"] == "v"
            and correction["start"] == "2026-08-01T10:20:00+00:00")
        self.assertEqual(
            mixed_correction["after"],
            {
                "status": "affected",
                "interferer_transmission_ids": ["a"],
                "undetermined_interferer_transmission_ids": ["b"],
            })

    def test_multiple_changes_on_one_boundary_form_one_correction(self):
        plan = base_plan()
        plan["transmissions"][1]["end"] = W1  # a spans the whole window
        events = [
            event for event in base_events()
            if event["fact_id"] != "b-base"
        ]
        # B starts outside the circle and enters at 10:30; A leaves at the
        # same instant.  Both facts arrive before publication.
        events.append(assert_event(
            "duty", "B", "b-base", "2026-08-01T08:00:00+00:00",
            lon=0.005, radius=1))
        events += [
            assert_event("survey", "A", "a-far", "2026-08-01T11:30:00+00:00",
                         "2026-08-01T10:30:00+00:00", W1,
                         lon=0.02, radius=1),
            assert_event("survey", "B", "b-near", "2026-08-01T11:30:00+00:00",
                         "2026-08-01T10:30:00+00:00", W1,
                         lon=0.001, radius=1),
        ]
        report = build_protection_report(plan, facts_doc(events))
        self.assertEqual(self.report_corrections(report), [
            ("v", "10:30:00", "11:00:00", "affected", ["a"],
             "affected", ["b"]),
        ])

    def test_version_only_change_needs_no_notification(self):
        events = base_events() + [
            # A later, equally authoritative restatement of the same value.
            narrow_radius(300, start=DAY0, end=DAY1,
                          known="2026-08-01T11:30:00+00:00", fact="r-same"),
        ]
        report = build_protection_report(base_plan(), facts_doc(events))
        self.assertEqual(report["corrections"], [])
        # The evidence really did change, and stays traceable per view.
        self.assertEqual(
            station_segments(report, "published", "R")[0]["evidence"][0]["known_at"],
            "2026-08-01T08:00:00+00:00")
        self.assertEqual(
            station_segments(report, "revised", "R")[0]["evidence"][0]["known_at"],
            "2026-08-01T11:30:00+00:00")
        # Distance- or source-only changes are likewise not corrections.
        self.assertEqual(
            reception(report, "revised", "v")["durations_us"],
            reception(report, "published", "v")["durations_us"])

    def test_no_candidate_pairs_but_missing_stations(self):
        plan = {
            "stations": [station("R"), station("T"), station("A"), station("U")],
            "transmissions": [
                task("v", "T", "R"),
                task("a", "A", "U", center=300, end="2026-08-01T10:10:00+00:00"),
            ],
        }
        facts = facts_doc([], sources=[])
        report = build_protection_report(plan, facts)
        self.assertEqual(report["views"]["published"]["report"]["conflicts"], [])
        self.assertEqual(report["views"]["published"]["report"]["undetermined"], [])
        self.assertEqual(report["corrections"], [])
        for row in report["views"]["published"]["stations"]:
            self.assertEqual(compact(row["segments"]),
                             [("10:00:00", "11:00:00", "missing")])
            self.assertEqual(row["segments"][0]["values"], [])
            self.assertEqual(row["segments"][0]["evidence"], [])
        for transmission_id in ("v", "a"):
            self.assertEqual(
                reception(report, "published", transmission_id)["durations_us"],
                {"affected": 0, "undetermined": 0, "clear":
                 (3_600_000_000 if transmission_id == "v" else 600_000_000)})

    def test_task_outside_window_has_empty_timeline_and_zero_durations(self):
        plan = base_plan()
        plan["transmissions"].append(task(
            "night", "T", "R", start="2026-08-02T00:00:00+00:00",
            end="2026-08-02T01:00:00+00:00"))
        report = build_protection_report(plan, facts_doc(base_events()))
        row = reception(report, "published", "night")
        self.assertEqual(row["segments"], [])
        self.assertEqual(row["durations_us"],
                         {"affected": 0, "undetermined": 0, "clear": 0})
        # A task fully outside the window never appears in any evidence.
        for kind in ("conflicts", "undetermined"):
            for evidence in report["views"]["published"]["report"][kind]:
                self.assertNotIn(
                    "night",
                    (evidence["victim_transmission_id"],
                     evidence["interferer_transmission_id"]))

    @staticmethod
    def report_corrections(report):
        return [
            (row["transmission_id"], row["start"][11:19], row["end"][11:19],
             row["before"]["status"],
             row["before"]["interferer_transmission_ids"],
             row["after"]["status"],
             row["after"]["interferer_transmission_ids"])
            for row in report["corrections"]
        ]


# ---------------------------------------------------------------------------
# independent probe oracle
# ---------------------------------------------------------------------------
def _parse(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class Oracle:
    """Re-derives the report from the raw contract at a set of probe times."""

    def __init__(self, plan, facts):
        self.plan = plan
        self.facts = facts
        self.stations = {row["id"]: row for row in plan["stations"]}
        self.tasks = {row["id"]: row for row in plan["transmissions"]}
        self.window_start = _parse(facts["window_start"])
        self.window_end = _parse(facts["window_end"])
        self.published = _parse(facts["published_as_of"])
        self.revised = _parse(facts["revised_as_of"])
        self.priorities = {row["id"]: row["priority"]
                           for row in facts["sources"]}
        self.versions = {}
        for row in facts["events"]:
            key = (row["source_id"], row["station_id"], row["fact_id"])
            self.versions.setdefault(key, []).append(row)
        for rows in self.versions.values():
            rows.sort(key=lambda item: _parse(item["known_at"]))
        self.boundaries = self._boundaries()

    def _boundaries(self):
        points = {self.window_start, self.window_end}
        for row in self.facts["events"]:
            points.add(_parse(row["known_at"]))
            if row["op"] == "assert":
                for key in ("effective_start", "effective_end"):
                    moment = _parse(row[key])
                    points.add(max(self.window_start,
                                   min(self.window_end, moment)))
        for row in self.plan["transmissions"]:
            for key in ("start", "end"):
                moment = _parse(row[key])
                points.add(max(self.window_start,
                               min(self.window_end, moment)))
        return sorted(points)

    def probes(self):
        result = []
        for left, right in zip(self.boundaries, self.boundaries[1:]):
            if left >= right:
                continue
            result.append(left)
            result.append(left + (right - left) // 2)
        return [point for point in result
                if self.window_start <= point < self.window_end]

    def station_state(self, station_id, moment, cutoff):
        covering = []
        for (source, site, fact), rows in self.versions.items():
            if site != station_id:
                continue
            latest = None
            for row in rows:
                if _parse(row["known_at"]) <= cutoff:
                    latest = row
                else:
                    break
            if latest is None or latest["op"] != "assert":
                continue
            if not (_parse(latest["effective_start"]) <= moment
                    < _parse(latest["effective_end"])):
                continue
            covering.append((
                self.priorities[source],
                (float(latest["lat"]), float(latest["lon"]),
                 latest["protection_radius_m"]),
            ))
        if not covering:
            return ("missing", ())
        best = min(item[0] for item in covering)
        values = sorted({item[1] for item in covering if item[0] == best})
        return ("resolved" if len(values) == 1 else "disputed", tuple(values))

    def pair_state(self, victim_id, interferer_id, moment, cutoff):
        victim = self.tasks[victim_id]
        interferer = self.tasks[interferer_id]
        if not (_parse(victim["start"]) <= moment < _parse(victim["end"])):
            return None
        if not (_parse(interferer["start"]) <= moment
                < _parse(interferer["end"])):
            return None
        low = max(victim["center_hz"] - victim["bandwidth_hz"] / 2,
                  interferer["center_hz"] - interferer["bandwidth_hz"] / 2)
        high = min(victim["center_hz"] + victim["bandwidth_hz"] / 2,
                   interferer["center_hz"] + interferer["bandwidth_hz"] / 2)
        if not low < high:
            return None
        receiver_status, receiver_values = self.station_state(
            victim["receiver_id"], moment, cutoff)
        transmitter_status, transmitter_values = self.station_state(
            interferer["station_id"], moment, cutoff)
        if receiver_status != "resolved" or transmitter_status != "resolved":
            return "undetermined"
        receiver = {"lat": receiver_values[0][0], "lon": receiver_values[0][1],
                    "protection_radius_m": receiver_values[0][2]}
        transmitter = {"lat": transmitter_values[0][0],
                       "lon": transmitter_values[0][1],
                       "protection_radius_m": transmitter_values[0][2]}
        separation = distance_m(receiver, transmitter)
        return ("conflict" if separation <= receiver["protection_radius_m"]
                else "clear")

    def reception_state(self, transmission_id, moment, cutoff):
        victim = self.tasks[transmission_id]
        if not (_parse(victim["start"]) <= moment < _parse(victim["end"])):
            return None
        determined = set()
        undetermined = set()
        for interferer_id, interferer in self.tasks.items():
            if interferer_id == transmission_id:
                continue
            start = max(_parse(victim["start"]), _parse(interferer["start"]))
            end = min(_parse(victim["end"]), _parse(interferer["end"]))
            if not start <= moment < end:
                continue
            state = self.pair_state(transmission_id, interferer_id,
                                    moment, cutoff)
            if state == "conflict":
                determined.add(interferer_id)
            elif state == "undetermined":
                undetermined.add(interferer_id)
        status = ("affected" if determined
                  else ("undetermined" if undetermined else "clear"))
        return status, tuple(sorted(determined)), tuple(sorted(undetermined))


def _segment_at(segments, moment, time_key):
    for segment in segments:
        if _parse(segment[time_key[0]]) <= moment < _parse(segment[time_key[1]]):
            return segment
    return None


class OracleComparison(unittest.TestCase):
    def check(self, plan, facts):
        report = build_protection_report(plan, facts)
        oracle = Oracle(plan, facts)
        probes = oracle.probes()
        for view, cutoff in (("published", oracle.published),
                             ("revised", oracle.revised)):
            self.check_invariants(report, view, oracle)
            for moment in probes:
                for station_id in oracle.stations:
                    expected_status, expected_values = oracle.station_state(
                        station_id, moment, cutoff)
                    segment = _segment_at(
                        station_segments(report, view, station_id),
                        moment, ("start", "end"))
                    self.assertIsNotNone(segment, (view, station_id, moment))
                    self.assertEqual(segment["status"], expected_status,
                                     (view, station_id, moment))
                    self.assertEqual(
                        tuple(sorted(
                            (float(value["lat"]), float(value["lon"]),
                             value["protection_radius_m"])
                            for value in segment["values"])),
                        expected_values,
                        (view, station_id, moment))
                for transmission_id in oracle.tasks:
                    expected = oracle.reception_state(
                        transmission_id, moment, cutoff)
                    if expected is None:
                        continue
                    segment = _segment_at(
                        reception(report, view, transmission_id)["segments"],
                        moment, ("start", "end"))
                    self.assertIsNotNone(
                        segment, (view, transmission_id, moment))
                    observed = (segment["status"],
                                tuple(segment["interferer_transmission_ids"]),
                                tuple(segment[
                                    "undetermined_interferer_transmission_ids"]))
                    self.assertEqual(observed, expected,
                                     (view, transmission_id, moment))
                # Every directed row covers exactly the moments where the
                # independent oracle sees that relation.
                for kind, label in (("conflicts", "conflict"),
                                    ("undetermined", "undetermined")):
                    seen = set()
                    for row in report["views"][view]["report"][kind]:
                        if _parse(row["start"]) <= moment < _parse(row["end"]):
                            key = (row["victim_transmission_id"],
                                   row["interferer_transmission_id"])
                            seen.add(key)
                            self.assertEqual(
                                oracle.pair_state(key[0], key[1], moment, cutoff),
                                label, (view, key, moment))
                    for victim_id in oracle.tasks:
                        for interferer_id in oracle.tasks:
                            if victim_id == interferer_id:
                                continue
                            state = oracle.pair_state(
                                victim_id, interferer_id, moment, cutoff)
                            if state == label:
                                self.assertIn(
                                    (victim_id, interferer_id), seen,
                                    (view, victim_id, interferer_id, moment))
        return report

    @staticmethod
    def check_invariants(report, view, oracle):
        # Station timelines tile the whole window with half-open segments.
        for row in report["views"][view]["stations"]:
            segments = row["segments"]
            assert segments[0]["start"] == report["window"]["start"]
            assert segments[-1]["end"] == report["window"]["end"]
            for left, right in zip(segments, segments[1:]):
                assert left["end"] == right["start"]
                assert left["status"] != right["status"] \
                    or left["values"] != right["values"] \
                    or left["evidence"] != right["evidence"]
            for segment in segments:
                assert segment["end"] > segment["start"]
        # Reception timelines tile each task's window intersection and sum to
        # exactly that intersection; evidence rows never overlap a gap.
        for row in report["views"][view]["report"]["reception_timelines"]:
            task = oracle.tasks[row["transmission_id"]]
            start = max(_parse(task["start"]), oracle.window_start)
            end = min(_parse(task["end"]), oracle.window_end)
            if start >= end:
                assert row["segments"] == []
                assert row["durations_us"] == {
                    "affected": 0, "undetermined": 0, "clear": 0}
                continue
            assert row["segments"][0]["start"] == start.isoformat()
            assert row["segments"][-1]["end"] == end.isoformat()
            total = 0
            for left, right in zip(row["segments"], row["segments"][1:]):
                assert left["end"] == right["start"]
            for segment in row["segments"]:
                total += int((_parse(segment["end"])
                              - _parse(segment["start"])).total_seconds()
                             * 1_000_000)
                assert segment["end"] > segment["start"]
            assert total == sum(row["durations_us"].values())

    def test_hand_built_scenarios_match_oracle(self):
        scenarios = [
            (base_plan(), facts_doc(base_events() + [narrow_radius()])),
            (base_plan(), facts_doc(base_events() + [
                narrow_radius(150, source="survey", fact="r-a"),
                narrow_radius(160, source="survey", fact="r-b")])),
            (base_plan(), facts_doc(base_events() + [
                narrow_radius(150, known="2026-08-01T08:30:00+00:00"),
                withdraw_event("survey", "R", "r-narrow",
                               "2026-08-01T11:30:00+00:00")])),
        ]
        for plan, facts in scenarios:
            self.check(plan, facts)

    def test_random_small_documents_match_oracle(self):
        rng = random.Random(20261121)
        for _ in range(60):
            plan, facts = random_document(rng)
            self.check(plan, facts)


def random_document(rng):
    def stamp(minute):
        return (datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
                + timedelta(seconds=minute)).isoformat()

    stations = [station(f"S{i}", lat=0.0, lon=0.0, radius=1)
                for i in range(rng.randint(3, 5))]
    transmissions = []
    for i in range(rng.randint(2, 4)):
        a = rng.choice(stations)["id"]
        b = rng.choice([row["id"] for row in stations if row["id"] != a])
        transmissions.append(task(
            f"T{i}", a, b, center=rng.choice([100, 100, 200]),
            start=stamp(rng.randint(0, 20)),
            end=stamp(rng.randint(40, 60))))
    sources = [{"id": "s0", "priority": rng.randint(0, 2)},
               {"id": "s1", "priority": rng.randint(0, 2)}]
    known_choices = ["2026-08-01T08:00:00+00:00",
                     "2026-08-01T10:30:00+00:00",
                     "2026-08-01T13:00:00+00:00"]
    events = []
    used = set()
    for site in stations:
        for fact_index in range(rng.randint(0, 3)):
            source = rng.choice(sources)["id"]
            fact = f"f{fact_index}"
            known = rng.choice(known_choices)
            if (source, site["id"], fact, known) in used:
                continue
            used.add((source, site["id"], fact, known))
            if rng.random() < 0.2:
                events.append(withdraw_event(source, site["id"], fact, known))
                continue
            start = rng.randint(-10, 55)
            end = start + rng.randint(1, 40)
            events.append(assert_event(
                source, site["id"], fact, known,
                stamp(start), stamp(end),
                lat=round(rng.choice([0.0, 0.001, 0.01, 5.0]), 4),
                lon=round(rng.choice([0.0, 0.001, 0.01, 5.0]), 4),
                radius=rng.choice([1, 100, 300, 1000])))
    # Discard invalid withdraw-only facts so the document stays contractual.
    by_key = {}
    for row in events:
        by_key.setdefault(
            (row["source_id"], row["station_id"], row["fact_id"]), []).append(row)
    legal = []
    for key, rows in by_key.items():
        rows.sort(key=lambda item: _parse(item["known_at"]))
        declared = False
        for row in rows:
            if row["op"] == "assert":
                declared = True
                legal.append(row)
            elif declared:
                declared = False
                legal.append(row)
    rng.shuffle(legal)
    return {
        "stations": stations,
        "transmissions": transmissions,
    }, facts_doc(legal, sources=sources,
                 window=(stamp(0), stamp(59)),
                 published="2026-08-01T09:00:00+00:00",
                 revised="2026-08-01T12:00:00+00:00")


# ---------------------------------------------------------------------------
# strict contract validation
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = base_plan()

    def assert_invalid(self, facts, pattern=None):
        if pattern is None:
            with self.assertRaises(ValueError):
                build_protection_report(self.plan, facts)
        else:
            with self.assertRaisesRegex(ValueError, pattern):
                build_protection_report(self.plan, facts)

    def test_root_fields_are_exact(self):
        broken = facts_doc(base_events())
        broken["extra"] = 1
        self.assert_invalid(broken, "unknown field")
        broken = facts_doc(base_events())
        del broken["window_start"]
        self.assert_invalid(broken, "missing field")
        broken = facts_doc(base_events())
        broken["sources"] = {}
        self.assert_invalid(broken, "array")

    def test_source_and_event_shapes(self):
        broken = facts_doc(base_events(), sources=[{"id": "duty"}])
        self.assert_invalid(broken, "missing field")
        broken = facts_doc(base_events(), sources=[
            {"id": "duty", "priority": 1, "note": "x"}])
        self.assert_invalid(broken, "unknown field")
        broken = facts_doc(base_events(), sources=[
            {"id": "duty", "priority": True}])
        self.assert_invalid(broken, "integer")
        broken = facts_doc(base_events(), sources=[
            {"id": "duty", "priority": -1}])
        self.assert_invalid(broken, "integer")
        broken = facts_doc(base_events(), sources=[
            {"id": "duty", "priority": 1}, {"id": "duty", "priority": 2}])
        self.assert_invalid(broken, "duplicate source")
        broken = facts_doc(base_events())
        broken["events"][0]["op"] = "replace"
        self.assert_invalid(broken, "op")
        broken = facts_doc(base_events())
        broken["events"][0]["unexpected"] = 0
        self.assert_invalid(broken, "unknown field")
        # Withdraw must not carry assertion-only fields.
        broken = facts_doc(base_events())
        custom = dict(broken["events"][0])
        custom["known_at"] = "2026-08-01T08:01:00+00:00"
        custom["op"] = "withdraw"
        broken["events"].append(custom)
        self.assert_invalid(broken, "unknown field")
        # Assert must not omit assertion-only fields.
        broken = facts_doc(base_events())
        custom = dict(broken["events"][0])
        custom["known_at"] = "2026-08-01T08:01:00+00:00"
        for key in ("effective_start", "effective_end", "lat", "lon",
                    "protection_radius_m"):
            del custom[key]
        broken["events"].append(custom)
        self.assert_invalid(broken, "missing field")

    def test_dangling_and_unknown_identifiers(self):
        broken = facts_doc(base_events())
        broken["events"][0]["source_id"] = "ghost"
        self.assert_invalid(broken, "source")
        broken = facts_doc(base_events())
        broken["events"][0]["station_id"] = "ghost"
        self.assert_invalid(broken, "plan station")
        broken = facts_doc(base_events())
        broken["events"][0]["fact_id"] = "   "
        self.assert_invalid(broken, "nonempty")

    def test_effective_interval_and_values(self):
        for start, end in ((W0, W0), ("2026-08-01T10:00:01+00:00", W0)):
            broken = facts_doc(base_events() + [assert_event(
                "survey", "R", "bad", PUB, start, end)])
            self.assert_invalid(broken, "positive length")
        for key, value in (("lat", 91), ("lat", True), ("lon", -181),
                           ("protection_radius_m", 0),
                           ("protection_radius_m", True),
                           ("lon", float("nan"))):
            broken = facts_doc(base_events())
            broken["events"][0][key] = value
            self.assert_invalid(broken)

    def test_time_rules(self):
        broken = facts_doc(base_events())
        broken["events"][0]["known_at"] = "2026-08-01T08:00:00"
        self.assert_invalid(broken, "offset")
        broken = facts_doc(base_events())
        broken["events"][0]["known_at"] = "2026-08-01T08:00:00.1234567+00:00"
        self.assert_invalid(broken, "fractional")
        broken = facts_doc(base_events(), window=(W0, W0))
        self.assert_invalid(broken, "positive length")
        broken = facts_doc(base_events(), window=(
            W0, "2026-08-08T10:00:00.000001+00:00"))
        self.assert_invalid(broken, "7 days")
        broken = facts_doc(base_events(), published=REV, revised=PUB)
        self.assert_invalid(broken, "earlier")
        broken = facts_doc(base_events(), published=REV, revised=REV)
        self.assert_invalid(broken, "earlier")

    def test_version_sequences(self):
        broken = facts_doc(base_events())
        broken["events"].append(withdraw_event(
            "duty", "R", "r-base", "2026-08-01T08:01:00+00:00"))
        broken["events"].append(withdraw_event(
            "duty", "R", "r-base", "2026-08-01T08:02:00+00:00"))
        self.assert_invalid(broken, "withdraw")
        broken = facts_doc(base_events())
        broken["events"].append(assert_event(
            "duty", "R", "r-base", "2026-08-01T08:00:00+00:00"))
        self.assert_invalid(broken, "same UTC known_at")
        broken = facts_doc(base_events())
        broken["events"].append(withdraw_event(
            "duty", "R", "r-fresh", "2026-08-01T08:01:00+00:00"))
        self.assert_invalid(broken, "start with an assertion")

    def test_event_limit_and_arrays(self):
        broken = facts_doc(base_events())
        broken["events"] = [dict(broken["events"][0])
                            for _ in range(20_001)]
        self.assert_invalid(broken, "at most 20000")
        broken = facts_doc(base_events())
        broken["events"] = {}
        self.assert_invalid(broken, "array")

    def test_late_records_are_validated_too(self):
        late = assert_event("survey", "R", "late", "2026-08-03T00:00:00+00:00",
                            lat=200)
        self.assert_invalid(facts_doc(base_events() + [late]), "coordinate")


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def baseline(self):
        return build_protection_report(
            base_plan(), facts_doc(base_events() + [narrow_radius()]))

    def test_reordered_arrays_and_equivalent_offsets_are_identical(self):
        events = base_events() + [narrow_radius()]
        shuffled = [dict(row) for row in reversed(events)]
        for row in shuffled:
            for key in ("known_at", "effective_start", "effective_end"):
                if key in row:
                    parsed = datetime.fromisoformat(row[key])
                    row[key] = parsed.astimezone(
                        timezone(timedelta(hours=2))).isoformat()
                    if key == "known_at" and row["fact_id"] == "r-narrow":
                        row[key] = parsed.astimezone(
                            timezone(timedelta(hours=-5))).isoformat()
        documents = [
            facts_doc(events),
            facts_doc(shuffled, sources=[
                {"id": "survey", "priority": 1},
                {"id": "duty", "priority": 5}]),
        ]
        plans = [base_plan(), {
            "stations": list(reversed(base_plan()["stations"])),
            "transmissions": list(reversed(base_plan()["transmissions"])),
        }]
        reports = [json.dumps(build_protection_report(plan, document),
                              sort_keys=True)
                   for plan, document in zip(plans, documents)]
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0], json.dumps(self.baseline(), sort_keys=True))

    def test_records_after_both_cutoffs_do_not_change_report(self):
        baseline = self.baseline()
        extra = [
            # A brand new fact that only exists after both cutoffs.
            assert_event("survey", "A", "later-fact",
                         "2026-08-01T13:00:00+00:00",
                         "2026-08-01T10:00:00+00:00",
                         "2026-08-01T10:30:00+00:00",
                         lon=0.002, radius=500),
            # A later withdrawal of the revised fact.
            withdraw_event("survey", "R", "r-narrow",
                           "2026-08-01T13:00:00+00:00"),
        ]
        changed = build_protection_report(
            base_plan(), facts_doc(base_events() + [narrow_radius()] + extra))
        self.assertEqual(json.dumps(changed, sort_keys=True),
                         json.dumps(baseline, sort_keys=True))

    def test_sources_order_is_not_business_identity(self):
        first = build_protection_report(base_plan(), facts_doc(
            base_events() + [narrow_radius()]))
        second = build_protection_report(base_plan(), facts_doc(
            base_events() + [narrow_radius()], sources=[
                {"id": "survey", "priority": 1},
                {"id": "duty", "priority": 5}]))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))


# ---------------------------------------------------------------------------
# CLI, atomic publishing and original-command compatibility
# ---------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.env = dict(os.environ, PYTHONPATH=str(SRC))

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _run(self, source, facts, output, module="spectrum.protection_cli"):
        return subprocess.run(
            [sys.executable, "-m", module,
             "--source", str(source),
             "--station-facts", str(facts),
             "--output", str(output)],
            capture_output=True, text=True, env=self.env, timeout=120)

    def test_success_publishes_atomic_report(self):
        source = self._write("plan.json", base_plan())
        facts = self._write("facts.json",
                            facts_doc(base_events() + [narrow_radius()]))
        output = self.dir / "report.json"
        completed = self._run(source, facts, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["summary"]["corrections"], 2)
        self.assertEqual(completed.stdout.strip().replace("'", '"'),
                         json.dumps(report["summary"], ensure_ascii=False))
        # Never overwrite an existing target.
        before = output.read_bytes()
        second = self._run(source, facts, output)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), before)

    def test_business_results_that_are_not_errors_exit_zero(self):
        source = self._write("plan.json", base_plan())
        facts = self._write("facts.json", facts_doc([]))
        output = self.dir / "report.json"
        completed = self._run(source, facts, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["corrections"], [])
        self.assertTrue(all(
            row["segments"][0]["status"] == "missing"
            for row in report["views"]["published"]["stations"]))

    def test_invalid_input_exits_two_and_preserves_target(self):
        source = self._write("plan.json", base_plan())
        good = self._write("good.json", facts_doc(base_events()))
        output = self.dir / "report.json"
        self.assertEqual(self._run(source, good, output).returncode, 0)
        before = output.read_bytes()

        broken = facts_doc(base_events())
        broken["events"][0]["known_at"] = "2026-08-01T08:00:00"
        bad = self._write("bad.json", broken)
        completed = self._run(source, bad, output)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("radio-protection:", completed.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_duplicate_json_keys_are_rejected(self):
        source = self._write("plan.json", base_plan())
        facts = self.dir / "dup.json"
        facts.write_text(
            '{"window_start":"%s","window_end":"%s",'
            '"published_as_of":"%s","revised_as_of":"%s",'
            '"published_as_of":"%s","sources":[],"events":[]}'
            % (W0, W1, PUB, REV, REV), encoding="utf-8")
        output = self.dir / "report.json"
        completed = self._run(source, facts, output)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())

    def test_each_input_has_an_eight_megabyte_limit(self):
        source = self._write("plan.json", base_plan())
        facts = self.dir / "huge.json"
        facts.write_bytes(b'{"window_start":"' + b"x" * 8_000_000 + b'"}')
        output = self.dir / "report.json"
        completed = self._run(source, facts, output)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("8 MB", completed.stderr)
        self.assertFalse(output.exists())

        big_plan = self.dir / "big-plan.json"
        big_plan.write_bytes(b'{"stations":[' + b" " * 8_000_000 + b"]}")
        completed = self._run(big_plan, self._write("ok.json", facts_doc([])),
                              output)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(output.exists())

    def test_original_command_and_public_service_are_unchanged(self):
        reference = run(json.loads((ROOT / "fixtures" / "input.json")
                                   .read_text()))
        completed = subprocess.run(
            [sys.executable, "-m", "spectrum.cli",
             "--source", str(ROOT / "fixtures" / "input.json"),
             "--output", str(self.dir / "legacy.json")],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads((self.dir / "legacy.json").read_text()),
                         reference)


# ---------------------------------------------------------------------------
# shipped offline fixtures
# ---------------------------------------------------------------------------
class FixtureTests(unittest.TestCase):
    def load(self, name):
        return json.loads((FIX / name).read_text())

    def build(self, plan_name, facts_name):
        return build_protection_report(self.load(plan_name),
                                       self.load(facts_name))

    def test_recompute_fixture_matches_mandated_numbers(self):
        report = self.build("plan.json", "recompute-facts.json")
        self.assertEqual(
            reception(report, "published", "v")["durations_us"]["affected"],
            3_600_000_000)
        self.assertEqual(
            reception(report, "revised", "v")["durations_us"]["affected"],
            3_300_000_000)
        self.assertEqual(len(report["corrections"]), 2)

    def test_all_shipped_fixtures_build(self):
        for facts_name in ("dispute-facts.json", "withdraw-facts.json",
                           "shorten-facts.json", "version-only-facts.json",
                           "mixed-facts.json"):
            report = self.build("plan.json", facts_name)
            self.assertEqual(report["window"]["start"], W0)
        self.assertEqual(
            self.build("plan.json", "version-only-facts.json")["corrections"], [])
        self.assertEqual(
            self.build("boundary-plan.json", "boundary-facts.json")
            ["corrections"][0]["start"], "2026-08-01T10:30:00+00:00")
        missing = self.build("missing-plan.json", "missing-facts.json")
        self.assertEqual(missing["views"]["revised"]["report"]["undetermined"],
                         [])
        self.assertEqual(missing["corrections"], [])


# ---------------------------------------------------------------------------
# near-limit offline churn case
# ---------------------------------------------------------------------------
def load_large_generator():
    path = FIX / "generate_large.py"
    spec = importlib.util.spec_from_file_location("protection_large", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LargeScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_large_generator()
        cls.plan, cls.facts = cls.generator.build_large()
        _, cls.baseline = cls.generator.build_baseline()

    def test_document_stays_within_contract_limits(self):
        self.assertEqual(len(self.plan["stations"]), 500)
        self.assertEqual(len(self.plan["transmissions"]), 300)
        self.assertEqual(len(self.facts["events"]), 20_000)
        self.assertEqual(len(self.baseline["events"]), 500)

    def test_large_document_completes_and_churn_is_irrelevant(self):
        started = time.monotonic()
        report = build_protection_report(self.plan, self.facts)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 60, "near-limit document took too long")
        baseline = build_protection_report(self.plan, self.baseline)
        for view in ("published", "revised"):
            self.assertEqual(
                report["views"][view]["report"], baseline["views"][view]["report"])
        self.assertEqual(report["corrections"], baseline["corrections"])
        # The low-priority churn merged away: those stations still expose one
        # whole-window segment, not one segment per filed fact.
        for station_id in ("S000", "S005", "S009"):
            self.assertEqual(
                compact(station_segments(report, "revised", station_id)),
                [("10:00:00", "11:00:00", "resolved")])
        # The unrelated high-priority restatement is nevertheless visible in
        # the station timeline, so no data was silently discarded.
        self.assertEqual(
            station_segments(report, "published", "S499")[0]["values"][0]
            ["protection_radius_m"], 1)
        self.assertEqual(
            station_segments(report, "revised", "S499")[0]["values"][0]
            ["protection_radius_m"], 2)
        self.assertEqual(
            station_segments(baseline, "revised", "S499")[0]["values"][0]
            ["protection_radius_m"], 1)

    def test_generated_files_publish_through_the_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            plan_path = directory / "plan.json"
            facts_path = directory / "facts.json"
            plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
            facts_path.write_text(json.dumps(self.facts), encoding="utf-8")
            self.assertLess(facts_path.stat().st_size, 8_000_000)
            output = directory / "report.json"
            completed = subprocess.run(
                [sys.executable, "-m", "spectrum.protection_cli",
                 "--source", str(plan_path),
                 "--station-facts", str(facts_path),
                 "--output", str(output)],
                capture_output=True, text=True,
                env=dict(os.environ, PYTHONPATH=str(SRC)), timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["summary"]["transmissions"], 300)


if __name__ == "__main__":
    unittest.main()
