import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum import workbench
from spectrum.errors import WorkbenchError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "workbench"

# Fixed synthetic geometry from the task statement.
MAIN_STATIONS = [
    {"id": "R", "lat": 0, "lon": 0, "protection_radius_m": 1000},
    {"id": "T", "lat": 0, "lon": 0.001, "protection_radius_m": 1},
    {"id": "S", "lat": 0, "lon": 0.002, "protection_radius_m": 1},
    {"id": "U", "lat": 10, "lon": 10, "protection_radius_m": 1},
]


def emission(identifier, station, receiver, center, bandwidth=10,
             start="2026-08-01T10:00:00+00:00", end="2026-08-01T11:00:00+00:00"):
    return {
        "id": identifier,
        "station_id": station,
        "receiver_id": receiver,
        "center_hz": center,
        "bandwidth_hz": bandwidth,
        "start": start,
        "end": end,
    }


def main_plan():
    return {
        "stations": [dict(row) for row in MAIN_STATIONS],
        "transmissions": [
            emission("v", "T", "R", 100),
            emission("s", "S", "U", 200),
        ],
    }


def change(proposal_id, base_revision, upserts=(), remove_ids=()):
    return {
        "proposal_id": proposal_id,
        "base_revision": base_revision,
        "upserts": [dict(row) for row in upserts],
        "remove_ids": list(remove_ids),
    }


def write(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def raised(callable_, code):
    try:
        callable_()
    except WorkbenchError as exc:
        if exc.code != code:
            raise AssertionError(f"expected {code}, got {exc.code}: {exc.message}")
        return exc
    raise AssertionError(f"expected {code}, nothing raised")


def evidence_pairs(report):
    return sorted(
        (row["interferer_transmission_id"], row["victim_transmission_id"])
        for row in report["conflicts"]
    )


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.ledger = str(self.dir / "ledger.json")

    def tearDown(self):
        self.temp.cleanup()

    def init(self, plan=None, ledger=None):
        return workbench.init_ledger(ledger or self.ledger, plan or main_plan())

    def propose(self, value, ledger=None):
        return workbench.propose(ledger or self.ledger, value)

    def preview(self, proposal_id, ledger=None):
        return workbench.preview(ledger or self.ledger, proposal_id)

    def apply(self, proposal_id, ledger=None):
        return workbench.apply_proposal(ledger or self.ledger, proposal_id)

    def approve(self, proposal_id, receiver, review_id, ledger=None):
        return workbench.approve(
            ledger or self.ledger, proposal_id, receiver, review_id
        )

    def show(self, revision=None, ledger=None):
        return workbench.show(ledger or self.ledger, revision)

    def revision(self):
        return self.show()["current_revision"]

    def bytes_of(self, ledger=None):
        return Path(ledger or self.ledger).read_bytes()

    def review(self, preview, receiver="R"):
        for item in preview["reviews"]:
            if item["receiver_id"] == receiver:
                return item
        return None


class MandatedScenario(LedgerCase):
    def test_stale_evidence_is_not_carried_across_proposals(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        first = self.preview("a")
        self.assertEqual(first["current_revision"], 0)
        self.assertEqual(first["status"], "approval_required")
        self.assertEqual(evidence_pairs(first["candidate_report"]), [("s", "v")])
        review = self.review(first)
        self.assertTrue(review["review_id"].startswith("review-"))
        self.assertFalse(review["approved"])
        self.approve("a", "R", review["review_id"])

        # B is based on revision 0 too, changes a different emission id x and
        # does not need any receiver confirmation.
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        b_preview = self.preview("b")
        self.assertEqual(b_preview["reviews"], [])
        self.assertTrue(b_preview["can_merge"])
        b_result = self.apply("b")
        self.assertEqual(b_result["revision"], 1)
        self.assertEqual(b_result["report"]["conflicts"], [])

        # A's write premise still holds, but R now faces both s and x.
        second = self.preview("a")
        self.assertEqual(second["current_revision"], 1)
        self.assertEqual(second["write_conflicts"], [])
        self.assertEqual(second["status"], "approval_required")
        self.assertEqual(evidence_pairs(second["candidate_report"]),
                         [("s", "v"), ("x", "v")])
        new_review = self.review(second)
        self.assertNotEqual(new_review["review_id"], review["review_id"])
        self.assertFalse(new_review["approved"])
        self.assertEqual(len(new_review["evidence"]), 2)

        # The saved revision-0 confirmation is insufficient now.
        raised(lambda: self.apply("a"), "approval_required")
        raised(lambda: self.approve("a", "R", review["review_id"]),
               "review_mismatch")
        self.approve("a", "R", new_review["review_id"])
        result = self.apply("a")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(evidence_pairs(result["report"]), [("s", "v"), ("x", "v")])
        self.assertEqual(
            [row["id"] for row in self.show()["plan"]["transmissions"]],
            ["s", "v", "x"],
        )
        # x is still present and current.
        self.assertTrue(self.show()["is_current"])
        self.assertEqual(self.revision(), 2)

    def test_review_identity_is_deterministic_and_evidence_bound(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        one = self.review(self.preview("a"))
        two = self.review(self.preview("a"))
        self.assertEqual(one["review_id"], two["review_id"])
        self.approve("a", "R", one["review_id"])
        third = self.review(self.preview("a"))
        self.assertTrue(third["approved"])
        self.assertEqual(third["review_id"], one["review_id"])

        # A different overlap changes the evidence identity.
        self.propose(change("a2", 0, [emission("v", "T", "R", 201)]))
        other = self.review(self.preview("a2"))
        self.assertNotEqual(other["review_id"], one["review_id"])

    def test_approval_is_bound_to_ledger_and_proposal(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        identity = self.review(self.preview("a"))["review_id"]

        # Same plan and proposal in another ledger yields another identity.
        other = str(self.dir / "other.json")
        self.init(ledger=other)
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]), ledger=other)
        foreign = self.review(self.preview("a", ledger=other))["review_id"]
        self.assertNotEqual(identity, foreign)
        raised(lambda: self.approve("a", "R", identity, ledger=other),
               "review_mismatch")

        # A different proposal with identical content is a different owner.
        self.propose(change("a-copy", 0, [emission("v", "T", "R", 200)]))
        copy_identity = self.review(self.preview("a-copy"))["review_id"]
        self.assertNotEqual(identity, copy_identity)
        raised(lambda: self.approve("a-copy", "R", identity), "review_mismatch")


class PremiseChecks(LedgerCase):
    def test_whole_batch_rejected_on_same_id_change(self):
        self.init()
        self.propose(change("batch", 0, [
            emission("v", "T", "R", 200),
            emission("y", "S", "U", 500),
        ]))
        self.propose(change("rival", 0, [emission("v", "T", "R", 300)]))
        self.assertEqual(self.apply("rival")["revision"], 1)

        before = self.bytes_of()
        error = raised(lambda: self.apply("batch"), "write_conflict")
        self.assertEqual(
            [item["transmission_id"] for item in error.details["conflicts"]], ["v"]
        )
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.bytes_of(), before)
        # No partial operation: y was never written.
        self.assertNotIn("y", [row["id"] for row in self.show()["plan"]["transmissions"]])

    def test_premise_satisfied_when_value_returns_to_base(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        identity = self.review(self.preview("a"))["review_id"]
        self.approve("a", "R", identity)

        self.propose(change("away", 0, [emission("v", "T", "R", 300)]))
        self.apply("away")
        raised(lambda: self.preview("a")["can_merge"] or self.apply("a"),
               "write_conflict")

        self.propose(change("back", 1, [emission("v", "T", "R", 100)]))
        self.apply("back")
        preview = self.preview("a")
        self.assertEqual(preview["write_conflicts"], [])
        # The old confirmation was for a two-row evidence set; it stays valid
        # only if the required identity matches, which it does here.
        self.assertTrue(self.review(preview)["approved"])
        self.assertTrue(preview["can_merge"])

    def test_remove_and_upsert_apply_together(self):
        self.init()
        self.propose(change("mix", 0,
                            [emission("t2", "T", "R", 600)],
                            ["s"]))
        preview = self.preview("mix")
        self.assertEqual(preview["candidate_report"]["summary"],
                         {"transmissions": 2, "directed_conflicts": 0,
                          "affected_receptions": 0})
        result = self.apply("mix")
        self.assertEqual(result["revision"], 1)
        ids = [row["id"] for row in self.show()["plan"]["transmissions"]]
        self.assertEqual(ids, ["t2", "v"])


class CandidateValidity(LedgerCase):
    def test_merge_time_limit_failure_is_invalid_candidate(self):
        stations = [
            {"id": "R", "lat": 0, "lon": 0, "protection_radius_m": 1},
            {"id": "T", "lat": 10, "lon": 10, "protection_radius_m": 1},
        ]
        transmissions = [
            emission(f"e{index:03d}", "T", "R", 1000 + 100 * index)
            for index in range(299)
        ]
        self.init({"stations": stations, "transmissions": transmissions})
        self.propose(change("p", 0, [emission("p", "T", "R", 999999)]))
        self.propose(change("q", 0, [emission("q", "T", "R", 998999)]))
        self.assertEqual(self.apply("q")["revision"], 1)
        preview = self.preview("p")
        self.assertEqual(preview["status"], "invalid_candidate")
        self.assertFalse(preview["can_merge"])
        error = raised(lambda: self.apply("p"), "invalid_candidate")
        self.assertIn("300", error.message)
        self.assertEqual(self.revision(), 1)
        # A later revision may make room again, but this is re-checked then.
        self.propose(change("drop", 1, [], ["q"]))
        self.apply("drop")
        self.assertEqual(self.revision(), 2)
        self.assertEqual(self.preview("p")["status"], "mergeable")

    def test_propose_rejects_invalid_candidate(self):
        self.init()
        raised(
            lambda: self.propose(change("bad", 0, [emission("n", "Z", "R", 100)])),
            "invalid_candidate",
        )
        self.assertNotIn("bad", self.ledger_text()["proposals"])

    def ledger_text(self):
        return json.loads(Path(self.ledger).read_text())


class ShrinkAndClear(LedgerCase):
    def setUp(self):
        super().setUp()
        stations = MAIN_STATIONS + [
            {"id": "X", "lat": 0, "lon": 0.003, "protection_radius_m": 1}
        ]
        self.init({
            "stations": stations,
            "transmissions": [
                emission("v", "T", "R", 100),
                emission("s", "S", "U", 100),
                emission("x", "X", "U", 101),
            ],
        })

    def test_shrunk_but_nonempty_evidence_still_needs_confirmation(self):
        current = self.show()["report"]
        self.assertEqual(evidence_pairs(current), [("s", "v"), ("x", "v")])
        self.propose(change("shrink", 0, [emission("x", "X", "U", 900)]))
        preview = self.preview("shrink")
        self.assertEqual(preview["status"], "approval_required")
        self.assertEqual(evidence_pairs(preview["candidate_report"]), [("s", "v")])
        self.assertEqual(self.review(preview)["receiver_id"], "R")
        raised(lambda: self.apply("shrink"), "approval_required")

    def test_fully_cleared_evidence_needs_no_confirmation(self):
        self.propose(change("clear", 0, [emission("v", "T", "R", 900)]))
        preview = self.preview("clear")
        self.assertEqual(preview["candidate_report"]["conflicts"], [])
        self.assertEqual(preview["reviews"], [])
        self.assertTrue(preview["can_merge"])
        self.assertEqual(self.apply("clear")["revision"], 1)

    def test_unchanged_evidence_needs_no_confirmation(self):
        # A new far-away emission leaves R's full evidence set identical.
        self.propose(change("quiet", 0, [emission("z", "S", "U", 5000)]))
        preview = self.preview("quiet")
        self.assertEqual(preview["reviews"], [])
        self.assertTrue(preview["can_merge"])
        self.assertEqual(evidence_pairs(preview["candidate_report"]),
                         evidence_pairs(self.show()["report"]))


class Normalization(LedgerCase):
    def test_offset_time_and_reordering_are_same_business_value(self):
        self.init()
        first = change("norm", 0, [emission("v", "T", "R", 200)])
        self.propose(first)
        displaced = change("norm", 0, [emission(
            "v", "T", "R", 200,
            start="2026-08-01T12:00:00+02:00",
            end="2026-08-01T13:00:00+02:00")])
        resent = self.propose(displaced)
        self.assertFalse(resent["created"])
        self.assertEqual(len(self.preview("norm")["candidate_report"]["conflicts"]), 1)

        # Reordered upserts are the same proposal content, not a new proposal.
        left = self.dir / "left.json"
        self.init(plan=main_plan(), ledger=str(left))
        self.propose(change("order", 0, [
            emission("y", "S", "U", 700),
            emission("z", "S", "U", 900),
        ]), ledger=str(left))
        right = change("order", 0, [
            emission("z", "S", "U", 900),
            emission("y", "S", "U", 700),
        ])
        self.assertFalse(self.propose(right, ledger=str(left))["created"])

    def test_same_id_different_content_is_rejected(self):
        self.init()
        self.propose(change("p", 0, [emission("v", "T", "R", 200)]))
        raised(lambda: self.propose(change("p", 0, [emission("v", "T", "R", 201)])),
               "proposal_conflict")


class Approvals(LedgerCase):
    def test_reuse_across_unrelated_revision_and_restoration(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        original = self.review(self.preview("a"))["review_id"]
        self.approve("a", "R", original)

        # Independent change: a new emission well away from R's band.
        self.propose(change("w", 0, [emission("w", "S", "U", 900)]))
        self.assertEqual(self.apply("w")["revision"], 1)
        preview = self.preview("a")
        self.assertEqual(preview["current_revision"], 1)
        self.assertTrue(self.review(preview)["approved"])
        self.assertEqual(self.review(preview)["review_id"], original)
        self.assertEqual(self.apply("a")["revision"], 2)

    def test_evidence_change_and_restoration(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        original = self.review(self.preview("a"))["review_id"]
        self.approve("a", "R", original)

        # Moving s changes the overlap seen by R for A's candidate.
        self.propose(change("move", 0, [emission("s", "S", "U", 202)]))
        self.apply("move")
        changed = self.preview("a")
        self.assertEqual(changed["status"], "approval_required")
        other = self.review(changed)["review_id"]
        self.assertNotEqual(other, original)
        self.assertFalse(self.review(changed)["approved"])
        raised(lambda: self.apply("a"), "approval_required")

        self.propose(change("restore", 1, [emission("s", "S", "U", 200)]))
        self.apply("restore")
        restored = self.preview("a")
        self.assertEqual(self.review(restored)["review_id"], original)
        self.assertTrue(self.review(restored)["approved"])
        self.assertEqual(self.apply("a")["revision"], 3)

    def test_duplicate_approval_is_idempotent(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        identity = self.review(self.preview("a"))["review_id"]
        self.approve("a", "R", identity)
        self.approve("a", "R", identity)
        ledger = json.loads(Path(self.ledger).read_text())
        self.assertEqual(ledger["approvals"]["a"]["R"], [identity])

    def test_rejections_leave_the_ledger_untouched(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        identity = self.review(self.preview("a"))["review_id"]
        before = self.bytes_of()
        raised(lambda: self.approve("a", "Q", identity), "approval_not_required")
        raised(lambda: self.approve("a", "R", "review-" + "0" * 64),
               "review_mismatch")
        raised(lambda: self.approve("missing", "R", identity), "proposal_not_found")
        self.assertEqual(self.bytes_of(), before)

        self.propose(change("rival", 0, [emission("v", "T", "R", 300)]))
        self.apply("rival")
        before = self.bytes_of()
        raised(lambda: self.approve("a", "R", identity), "write_conflict")
        self.assertEqual(self.bytes_of(), before)

    def test_approve_after_apply_is_rejected(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 700)]))
        self.apply("b")
        before = self.bytes_of()
        raised(lambda: self.approve("b", "R", "review-" + "0" * 64),
               "proposal_already_applied")
        self.assertEqual(self.bytes_of(), before)


class ReplayAndShow(LedgerCase):
    def test_replay_returns_first_result_after_plan_advanced(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        first = self.apply("b")
        self.propose(change("w", 1, [emission("w", "S", "U", 900)]))
        self.assertEqual(self.apply("w")["revision"], 2)
        again = self.apply("b")
        self.assertEqual(again, first)
        self.assertEqual(again["revision"], 1)
        self.assertEqual(self.revision(), 2)
        # Preview of a submitted proposal does not re-judge it.
        preview = self.preview("b")
        self.assertEqual(preview["status"], "already_applied")
        self.assertEqual(preview["candidate_report"], first["report"])

    def test_show_distinguishes_revisions(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        self.apply("b")
        now = self.show()
        zero = self.show(0)
        self.assertEqual((now["revision"], zero["revision"]), (1, 0))
        self.assertTrue(now["is_current"])
        self.assertFalse(zero["is_current"])
        self.assertEqual(zero["source_proposal_id"], None)
        self.assertEqual(now["source_proposal_id"], "b")
        self.assertEqual(len(zero["plan"]["transmissions"]), 2)
        self.assertEqual(len(now["plan"]["transmissions"]), 3)
        self.assertEqual(self.show(0)["plan"], zero["plan"])
        raised(lambda: self.show(9), "revision_not_found")


class InputContract(LedgerCase):
    def test_init_rejects_existing_ledger_and_bad_sources(self):
        self.init()
        raised(lambda: self.init(), "ledger_exists")
        fresh = str(self.dir / "fresh.json")
        plan = main_plan()
        plan["transmissions"][0]["extra"] = 1
        raised(lambda: self.init(plan, ledger=fresh), "invalid_input")
        self.assertFalse(Path(fresh).exists())

    def test_proposal_shape(self):
        self.init()
        bad = change("x", 0, [emission("v", "T", "R", 200)])
        bad["base_revision"] = True
        raised(lambda: self.propose(bad), "invalid_input")
        bad = change("x", 5, [emission("v", "T", "R", 200)])
        raised(lambda: self.propose(bad), "revision_not_found")
        raised(lambda: self.propose(change("x", 0, [], [])), "invalid_input")
        raised(lambda: self.propose(change("x", 0, [
            emission("v", "T", "R", 200), emission("v", "T", "R", 201)])),
            "invalid_input")
        raised(lambda: self.propose(change("x", 0, [], ["nope"])), "invalid_input")
        raised(lambda: self.propose(change("x", 0, [
            emission("v", "T", "R", 100)])), "invalid_input")
        raised(lambda: self.propose(change("x", 0, [
            emission("v", "T", "R", 200)], ["v"])), "invalid_input")
        missing = change("x", 0, [emission("v", "T", "R", 200)])
        del missing["upserts"][0]["bandwidth_hz"]
        raised(lambda: self.propose(missing), "invalid_input")
        extra = change("x", 0, [emission("v", "T", "R", 200)])
        extra["upserts"][0]["note"] = "x"
        raised(lambda: self.propose(extra), "invalid_input")
        raised(lambda: self.propose(change("x", 0, [
            emission("v", "Q", "R", 200)])), "invalid_candidate")
        self.assertEqual(self.revision(), 0)


class Durability(LedgerCase):
    def test_interrupted_write_keeps_whole_old_state(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        before = self.bytes_of()
        with mock.patch("spectrum.ledger.os.replace", side_effect=OSError("boom")):
            raised(lambda: self.apply("b"), "ledger_io_error")
        self.assertEqual(self.bytes_of(), before)
        self.assertEqual(self.revision(), 0)
        leftovers = [p.name for p in self.dir.iterdir() if p.name.startswith(".ledger-")]
        self.assertEqual(leftovers, [])
        # A retry sees no submission and completes normally.
        self.assertEqual(self.apply("b")["revision"], 1)

    def test_committed_state_is_one_complete_snapshot(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        self.apply("b")
        ledger = json.loads(Path(self.ledger).read_text())
        self.assertEqual(len(ledger["revisions"]), 2)
        self.assertEqual(ledger["proposals"]["b"]["submitted"]["revision"], 1)
        self.assertEqual(ledger["revisions"][-1]["proposal_id"], "b")
        self.assertEqual(self.show()["plan"]["transmissions"],
                         ledger["revisions"][-1]["plan"]["transmissions"])


class ProcessLevel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.ledger = str(self.dir / "ledger.json")
        self.env = dict(os.environ, PYTHONPATH=str(SRC))
        write(self.dir / "plan.json", main_plan())
        self.cli("init", "--ledger", self.ledger, "--source",
                 str(self.dir / "plan.json"))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, check=True):
        completed = subprocess.run(
            [sys.executable, "-m", "spectrum.workbench_cli", *args],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        if check:
            assert completed.returncode == 0, completed.stderr
        return completed

    def call(self, *args):
        return json.loads(self.cli(*args).stdout)

    def proposal_file(self, name, value):
        path = self.dir / f"{name}.json"
        write(path, value)
        return str(path)

    def test_restart_continues_preview_and_approval(self):
        path = self.proposal_file("a", change("a", 0, [
            emission("v", "T", "R", 200)]))
        self.call("propose", "--ledger", self.ledger, "--source", path)
        first = self.call("preview", "--ledger", self.ledger, "--proposal", "a")
        identity = first["reviews"][0]["review_id"]
        # Each command is a new process; nothing is cached in memory.
        second = self.call("preview", "--ledger", self.ledger, "--proposal", "a")
        self.assertEqual(second["reviews"][0]["review_id"], identity)
        approved = self.call("approve", "--ledger", self.ledger, "--proposal", "a",
                             "--receiver", "R", "--review-id", identity)
        self.assertEqual(approved["review_id"], identity)
        third = self.call("preview", "--ledger", self.ledger, "--proposal", "a")
        self.assertTrue(third["reviews"][0]["approved"])
        self.assertTrue(third["can_merge"])
        result = self.call("apply", "--ledger", self.ledger, "--proposal", "a")
        self.assertEqual(result["revision"], 1)
        shown = self.call("show", "--ledger", self.ledger)
        self.assertEqual(shown["revision"], 1)
        self.assertEqual(evidence_pairs(shown["report"]), [("s", "v")])

    def test_cli_errors_are_distinguishable(self):
        failed = self.cli("show", "--ledger", str(self.dir / "nope.json"),
                          check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)["error"], "ledger_not_found")
        failed = self.cli("init", "--ledger", self.ledger, "--source",
                          str(self.dir / "plan.json"), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)["error"], "ledger_exists")
        failed = self.cli("preview", "--ledger", self.ledger,
                          "--proposal", "absent", check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)["error"], "proposal_not_found")

    def test_two_apply_processes_interleave_without_double_commit(self):
        # Both proposals race on the same emission id v; each base revision is
        # 0, so at most one write premise can still hold when they meet.
        for name, center in (("p1", 200), ("p2", 201)):
            path = self.proposal_file(name, change(name, 0, [
                emission("v", "T", "R", center)]))
            self.call("propose", "--ledger", self.ledger, "--source", path)
            preview = self.call("preview", "--ledger", self.ledger,
                                "--proposal", name)
            identity = preview["reviews"][0]["review_id"]
            self.call("approve", "--ledger", self.ledger, "--proposal", name,
                      "--receiver", "R", "--review-id", identity)

        command = [sys.executable, "-m", "spectrum.workbench_cli", "apply",
                   "--ledger", self.ledger, "--proposal"]
        runners = [
            subprocess.Popen(command + [name], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env)
            for name in ("p1", "p2")
        ]
        outcomes = [runner.communicate(timeout=60) + (runner.returncode,)
                    for runner in runners]
        codes = sorted(outcome[2] for outcome in outcomes)
        self.assertEqual(codes, [0, 2])
        successes = [json.loads(out[0]) for out in outcomes if out[2] == 0]
        failures = [json.loads(out[1]) for out in outcomes if out[2] == 2]
        self.assertEqual(successes[0]["revision"], 1)
        self.assertEqual(failures[0]["error"], "write_conflict")
        shown = self.call("show", "--ledger", self.ledger)
        self.assertEqual(shown["current_revision"], 1)
        ledger = json.loads(Path(self.ledger).read_text())
        self.assertEqual([row["revision"] for row in ledger["revisions"]], [0, 1])
        winner = successes[0]["proposal_id"]
        loser = "p2" if winner == "p1" else "p1"
        self.assertIsNotNone(ledger["proposals"][winner]["submitted"])
        self.assertIsNone(ledger["proposals"][loser]["submitted"])

    def test_concurrent_approvals_are_not_lost(self):
        for name in ("p1", "p2"):
            path = self.proposal_file(name, change(name, 0, [
                emission("v", "T", "R", 200)]))
            self.call("propose", "--ledger", self.ledger, "--source", path)
        identities = {
            name: self.call("preview", "--ledger", self.ledger,
                            "--proposal", name)["reviews"][0]["review_id"]
            for name in ("p1", "p2")
        }
        runners = [
            subprocess.Popen(
                [sys.executable, "-m", "spectrum.workbench_cli", "approve",
                 "--ledger", self.ledger, "--proposal", name, "--receiver", "R",
                 "--review-id", identities[name]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=self.env)
            for name in ("p1", "p2")
        ]
        for runner in runners:
            runner.communicate(timeout=60)
            self.assertEqual(runner.returncode, 0)
        ledger = json.loads(Path(self.ledger).read_text())
        for name in ("p1", "p2"):
            self.assertEqual(ledger["approvals"][name]["R"], [identities[name]])
        for name in ("p1", "p2"):
            preview = self.call("preview", "--ledger", self.ledger,
                                "--proposal", name)
            self.assertTrue(preview["reviews"][0]["approved"])


class Compatibility(unittest.TestCase):
    def test_original_service_unchanged(self):
        from spectrum.service import run

        data = json.loads((ROOT / "fixtures" / "input.json").read_text())
        result = run(data)
        self.assertEqual(result["summary"], {
            "transmissions": 3, "directed_conflicts": 1,
            "affected_receptions": 1,
        })
        self.assertEqual(result["conflicts"][0]["overlap_bandwidth_hz"], "5000")

    def test_shipped_fixtures_run_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = str(Path(directory) / "ledger.json")
            workbench.init_ledger(
                ledger, json.loads((FIXTURES / "plan-v0.json").read_text())
            )
            for name in ("proposal-b.json", "proposal-a.json",
                         "proposal-w.json",
                         "proposal-reorder.json"):
                workbench.propose(
                    ledger, json.loads((FIXTURES / name).read_text())
                )
            self.assertEqual(
                workbench.preview(ledger, "proposal-b")["reviews"], []
            )
            self.assertEqual(
                workbench.apply_proposal(ledger, "proposal-b")["revision"], 1
            )
            preview = workbench.preview(ledger, "proposal-a")
            self.assertEqual(evidence_pairs(preview["candidate_report"]),
                             [("s", "v"), ("x", "v")])


if __name__ == "__main__":
    unittest.main()


class DirectedEvidence(unittest.TestCase):
    def test_direction_is_part_of_evidence_identity(self):
        from spectrum.records import make_review_id

        forward = [{
            "victim_transmission_id": "v", "interferer_transmission_id": "s",
            "receiver_id": "R", "interferer_station_id": "S",
            "overlap_start": "2026-08-01T10:00:00+00:00",
            "overlap_end": "2026-08-01T11:00:00+00:00",
            "overlap_low_hz": "195", "overlap_high_hz": "205",
            "overlap_bandwidth_hz": "10", "separation_m": "222.390",
        }]
        reverse = [dict(forward[0],
                        victim_transmission_id="s",
                        interferer_transmission_id="v",
                        receiver_id="U", interferer_station_id="T")]
        self.assertNotEqual(
            make_review_id("ledger", "p", "R", forward),
            make_review_id("ledger", "p", "U", reverse),
        )

    def test_identity_ignores_row_order_time_offset_and_float_spelling(self):
        from spectrum.records import make_review_id

        one = {
            "victim_transmission_id": "v", "interferer_transmission_id": "s",
            "receiver_id": "R", "interferer_station_id": "S",
            "overlap_start": "2026-08-01T10:00:00+00:00",
            "overlap_end": "2026-08-01T11:00:00+00:00",
            "overlap_low_hz": "195", "overlap_high_hz": "205",
            "overlap_bandwidth_hz": "10", "separation_m": "222.390",
        }
        two = dict(one,
                   victim_transmission_id="v2",
                   overlap_start="2026-08-01T12:00:00+02:00",
                   separation_m="1.000")
        self.assertEqual(
            make_review_id("ledger", "p", "R", [one, two]),
            make_review_id("ledger", "p", "R", [two, one]),
        )
        changed = dict(one, separation_m="222.391")
        self.assertNotEqual(
            make_review_id("ledger", "p", "R", [one]),
            make_review_id("ledger", "p", "R", [changed]),
        )


class IndependentMerges(LedgerCase):
    def test_two_commits_of_disjoint_ids_both_land(self):
        self.init()
        self.propose(change("p1", 0, [emission("n1", "S", "U", 5000)]))
        self.propose(change("p2", 0, [emission("n2", "S", "U", 6000)]))
        self.assertEqual(self.apply("p1")["revision"], 1)
        self.assertEqual(self.apply("p2")["revision"], 2)
        ids = [row["id"] for row in self.show()["plan"]["transmissions"]]
        self.assertEqual(ids, ["n1", "n2", "s", "v"])

    def test_second_commit_sees_new_interference_after_first(self):
        self.init()
        self.propose(change("edit", 0, [emission("v", "T", "R", 200)]))
        self.propose(change("add", 0, [emission("x", "S", "U", 200)]))
        preview = self.preview("edit")
        self.approve("edit", "R", self.review(preview)["review_id"])
        self.assertEqual(self.apply("add")["revision"], 1)
        raised(lambda: self.apply("edit"), "approval_required")
        self.assertEqual(self.revision(), 1)
        updated = self.preview("edit")
        self.assertEqual(len(self.review(updated)["evidence"]), 2)
        self.approve("edit", "R", self.review(updated)["review_id"])
        self.assertEqual(self.apply("edit")["revision"], 2)


class CrashSafety(LedgerCase):
    def test_stale_temp_from_killed_commit_is_swept(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        stale = self.dir / (".ledger.json.tmp-dead")
        stale.write_text('{"format": "partial')
        self.assertEqual(self.apply("b")["revision"], 1)
        self.assertFalse(stale.exists())
        self.assertEqual(self.revision(), 1)

    def test_process_killed_mid_commit_leaves_old_state(self):
        self.init()
        self.propose(change("b", 0, [emission("x", "S", "U", 200)]))
        before = self.bytes_of()
        script = (
            "import json, os, signal, sys, tempfile\n"
            "sys.path.insert(0, %r)\n"
            "import spectrum.ledger as ledger\n"
            "from spectrum import workbench\n"
            "original = ledger.LedgerFile.commit\n"
            "def dying(self, data):\n"
            "    fd, temp = tempfile.mkstemp(prefix='.' + self.path.name +"
            " '.tmp-', dir=self.path.parent)\n"
            "    os.write(fd, b'{\"partial\":')\n"
            "    os.fsync(fd)\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
            "ledger.LedgerFile.commit = dying\n"
            "workbench.apply_proposal(%r, 'b')\n"
        ) % (str(SRC), str(self.ledger))
        completed = subprocess.run([sys.executable, "-c", script],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, -9)
        self.assertEqual(self.bytes_of(), before)
        self.assertEqual(self.revision(), 0)
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name.startswith(".ledger.json.tmp-")]
        self.assertEqual(len(leftovers), 1)
        # A retry after the crash completes and sweeps the stale temp file.
        self.assertEqual(self.apply("b")["revision"], 1)
        self.assertEqual(
            [p.name for p in self.dir.iterdir()
             if p.name.startswith(".ledger.json.tmp-")], [])
        self.assertEqual(self.revision(), 1)


class MoreContractDetails(LedgerCase):
    def test_bool_is_not_an_integer(self):
        self.init()
        row = emission("n", "S", "U", 700)
        row["center_hz"] = True
        raised(lambda: self.propose(change("x", 0, [row])), "invalid_input")
        raised(lambda: self.show(True), "invalid_input")

    def test_offset_equivalent_upsert_matches_base(self):
        self.init()
        # Same business value as base record v, only different offset notation.
        raised(lambda: self.propose(change("x", 0, [emission(
            "v", "T", "R", 100,
            start="2026-08-01T12:00:00+02:00",
            end="2026-08-01T13:00:00+02:00")])), "invalid_input")

    def test_init_array_order_does_not_change_the_plan(self):
        plan = main_plan()
        plan["stations"].reverse()
        plan["transmissions"].reverse()
        self.init(plan)
        shown = self.show()
        self.assertEqual(
            [row["id"] for row in shown["plan"]["stations"]],
            sorted(row["id"] for row in MAIN_STATIONS),
        )
        self.assertEqual([row["id"] for row in shown["plan"]["transmissions"]],
                         ["s", "v"])
        self.assertEqual(shown["report"]["conflicts"], [])

    def test_remove_then_readd_in_one_operation_is_rejected_as_crossing(self):
        self.init()
        raised(lambda: self.propose(change(
            "x", 0, [emission("s", "S", "U", 300)], ["s"])), "invalid_input")

    def test_preview_does_not_advance_revision(self):
        self.init()
        self.propose(change("a", 0, [emission("v", "T", "R", 200)]))
        for _ in range(3):
            self.preview("a")
        self.assertEqual(self.revision(), 0)
        self.assertEqual(len(self.show()["plan"]["transmissions"]), 2)


class ConcurrentIndependentApplies(unittest.TestCase):
    def test_two_independent_commits_both_land_once(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            ledger = str(directory / "ledger.json")
            env = dict(os.environ, PYTHONPATH=str(SRC))
            workbench.init_ledger(ledger, main_plan())
            for name in ("p1", "p2"):
                workbench.propose(
                    ledger,
                    change(name, 0, [emission(f"n-{name}", "S", "U",
                                              5000 + int(name[1:]))]),
                )
            command = [sys.executable, "-m", "spectrum.workbench_cli", "apply",
                       "--ledger", ledger, "--proposal"]
            runners = [
                subprocess.Popen(command + [name], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env)
                for name in ("p1", "p2")
            ]
            outcomes = [runner.communicate(timeout=60) + (runner.returncode,)
                        for runner in runners]
            self.assertEqual([outcome[2] for outcome in outcomes], [0, 0])
            revisions = sorted(json.loads(out[0])["revision"] for out in outcomes)
            self.assertEqual(revisions, [1, 2])
            stored = json.loads(Path(ledger).read_text())
            self.assertEqual([row["revision"] for row in stored["revisions"]],
                             [0, 1, 2])
            self.assertEqual(len({row["proposal_id"]
                                  for row in stored["revisions"][1:]}), 2)
            ids = [row["id"] for row in stored["revisions"][-1]["plan"]["transmissions"]]
            self.assertEqual(ids, ["n-p1", "n-p2", "s", "v"])
            self.assertEqual(stored["revisions"][-1]["report"]["conflicts"], [])


class MultiReceiver(LedgerCase):
    def setUp(self):
        super().setUp()
        stations = [
            {"id": "R", "lat": 0, "lon": 0, "protection_radius_m": 1000},
            {"id": "T", "lat": 0, "lon": 0.001, "protection_radius_m": 1},
            {"id": "S", "lat": 0, "lon": 0.002, "protection_radius_m": 1},
            {"id": "R2", "lat": 0, "lon": 0.02, "protection_radius_m": 1000},
            {"id": "T2", "lat": 0, "lon": 0.0205, "protection_radius_m": 1},
            {"id": "S2", "lat": 0, "lon": 0.021, "protection_radius_m": 1},
            {"id": "U", "lat": 10, "lon": 10, "protection_radius_m": 1},
        ]
        transmissions = [
            emission("v1", "T", "R", 100),
            emission("s1", "S", "U", 200),
            emission("v2", "T2", "R2", 100),
            emission("s2", "S2", "U", 200),
        ]
        self.init({"stations": stations, "transmissions": transmissions})

    def test_each_receiver_approves_its_own_full_evidence(self):
        self.propose(change("both", 0, [
            emission("v1", "T", "R", 200),
            emission("v2", "T2", "R2", 200),
        ]))
        preview = self.preview("both")
        self.assertEqual(preview["status"], "approval_required")
        self.assertEqual([item["receiver_id"] for item in preview["reviews"]],
                         ["R", "R2"])
        self.assertEqual(evidence_pairs(preview["candidate_report"]),
                         [("s1", "v1"), ("s2", "v2")])
        r_review = self.review(preview, "R")
        self.approve("both", "R", r_review["review_id"])
        after = self.preview("both")
        self.assertEqual([item["receiver_id"] for item in after["reviews"]
                          if not item["approved"]], ["R2"])
        error = raised(lambda: self.apply("both"), "approval_required")
        self.assertEqual(
            [item["receiver_id"] for item in error.details["missing"]], ["R2"]
        )
        self.assertEqual(self.revision(), 0)
        self.approve("both", "R2", self.review(after, "R2")["review_id"])
        result = self.apply("both")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(sorted(item["receiver_id"] for item in result["reviews"]),
                         ["R", "R2"])
