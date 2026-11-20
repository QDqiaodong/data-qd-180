import json
import unittest
from pathlib import Path
from spectrum.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class RadioTests(unittest.TestCase):
    def test_directed_overlap_evidence(self):
        result = run(fixture())
        self.assertEqual(result["summary"], {"transmissions": 3, "directed_conflicts": 1,
                                            "affected_receptions": 1})
        row = result["conflicts"][0]
        self.assertEqual((row["victim_transmission_id"], row["interferer_transmission_id"]),
                         ("link-1", "link-2"))
        self.assertEqual(row["overlap_bandwidth_hz"], "5000")
        self.assertEqual(row["overlap_start"], "2026-08-01T10:30:00+00:00")
        self.assertAlmostEqual(row["separation_m"], 555.975, places=2)
        self.assertEqual(result["clear_transmission_ids"], ["link-2", "link-3"])

    def test_touching_time_and_frequency_are_clear(self):
        data = fixture()
        data["transmissions"][1]["start"] = "2026-08-01T11:00:00+00:00"
        self.assertEqual(run(data)["conflicts"], [])
        data = fixture()
        data["transmissions"][1]["center_hz"] = 100010000
        self.assertEqual(run(data)["conflicts"], [])

    def test_timezone_equivalence(self):
        data = fixture()
        data["transmissions"][0]["start"] = "2026-08-01T12:00:00+02:00"
        data["transmissions"][0]["end"] = "2026-08-01T13:00:00+02:00"
        self.assertEqual(run(data), run(fixture()))

    def test_half_hertz_boundaries_preserved(self):
        data = fixture()
        for row in data["transmissions"][:2]:
            row["center_hz"] = 100
            row["bandwidth_hz"] = 3
        row = run(data)["conflicts"][0]
        self.assertEqual(row["overlap_low_hz"], "197/2")
        self.assertEqual(row["overlap_high_hz"], "203/2")
        self.assertEqual(row["overlap_bandwidth_hz"], "3")

    def test_coordinate_and_reference_errors(self):
        data = fixture()
        data["stations"][0]["lat"] = 91
        with self.assertRaisesRegex(ValueError, "coordinate"):
            run(data)
        data = fixture()
        data["transmissions"][0]["receiver_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown"):
            run(data)
