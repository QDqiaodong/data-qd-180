"""Offline generator for the near-limit ``radio-protection`` churn case.

Run from the repository root::

    PYTHONPATH=src python3 fixtures/protection/generate_large.py /tmp/protection-large

The generator writes ``plan.json`` (500 stations, 300 transmissions),
``facts.json`` (20,000 station-fact events) and ``facts-baseline.json`` (only
the authoritative layer).  Both fact documents are valid inputs and produce
identical protection conclusions: 19,500 of the events come from a chatty,
*less* authoritative source and never change the effective declarations, and
one authoritative restatement sits on an unused station.

Everything is original synthetic data (MIT), offline and deterministic.
"""
import json
import sys
from pathlib import Path

WINDOW_START = "2026-08-01T10:00:00+00:00"
WINDOW_END = "2026-08-01T11:00:00+00:00"
DAY_START = "2026-08-01T00:00:00+00:00"
DAY_END = "2026-08-02T00:00:00+00:00"
PUBLISHED = "2026-08-01T09:00:00+00:00"
REVISED = "2026-08-01T12:00:00+00:00"

STATIONS = 500
TRANSMISSIONS = 300
MAX_EVENTS = 20_000
CHATTER_EVENTS = MAX_EVENTS - STATIONS - 1  # leave room for 500 duty + 1 survey


def station(index):
    return {
        "id": f"S{index:03d}",
        # A deterministic lattice, never real coordinates.
        "lat": round(((index * 37) % 200 - 100) / 50.0, 4),
        "lon": round(((index * 71) % 360 - 180) / 50.0, 4),
        "protection_radius_m": 1,
    }


def build_plan():
    stations = [station(index) for index in range(STATIONS)]
    transmissions = []
    for index in range(TRANSMISSIONS):
        transmissions.append({
            "id": f"T{index:03d}",
            "station_id": f"S{index % 300:03d}",
            "receiver_id": f"S{300 + index % 100:03d}",
            "center_hz": 100_000_000,
            "bandwidth_hz": 10_000,
            "start": WINDOW_START,
            "end": WINDOW_END,
        })
    return {"stations": stations, "transmissions": transmissions}


def authoritative_events():
    return [
        {
            "source_id": "duty",
            "station_id": f"S{index:03d}",
            "fact_id": "duty",
            "known_at": "2026-08-01T08:00:00+00:00",
            "op": "assert",
            "effective_start": DAY_START,
            "effective_end": DAY_END,
            "lat": station(index)["lat"],
            "lon": station(index)["lon"],
            "protection_radius_m": 1,
        }
        for index in range(STATIONS)
    ]


def chatter_events():
    """A less authoritative source filing many irrelevant revisions."""
    rows = []
    for version in range(CHATTER_EVENTS):
        index = version % STATIONS
        rows.append({
            "source_id": "chatter",
            "station_id": f"S{index:03d}",
            "fact_id": f"chatter-{version:05d}",
            "known_at": "2026-08-01T08:30:00+00:00",
            "op": "assert",
            "effective_start": DAY_START,
            "effective_end": DAY_END,
            # Deliberately different coordinates: this layer must never be
            # reached while the duty layer covers the station.
            "lat": round(station(index)["lat"] + 0.5, 4),
            "lon": round(station(index)["lon"] + 0.5, 4),
            "protection_radius_m": 500_000,
        })
    return rows


def restatement_event():
    # S499 is referenced by no transmission; the restatement changes only an
    # unused station's own timeline, never any candidate pair.
    return {
        "source_id": "survey",
        "station_id": "S499",
        "fact_id": "survey-unused",
        "known_at": "2026-08-01T11:30:00+00:00",
        "op": "assert",
        "effective_start": DAY_START,
        "effective_end": DAY_END,
        "lat": station(499)["lat"],
        "lon": station(499)["lon"],
        "protection_radius_m": 2,
    }


def document(events):
    return {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "published_as_of": PUBLISHED,
        "revised_as_of": REVISED,
        "sources": [
            {"id": "duty", "priority": 5},
            {"id": "survey", "priority": 1},
            {"id": "chatter", "priority": 50},
        ],
        "events": events,
    }


def build_large():
    return build_plan(), document(
        authoritative_events() + chatter_events() + [restatement_event()])


def build_baseline():
    return build_plan(), document(authoritative_events())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    target = Path(argv[0]) if argv else Path("run/protection-large")
    target.mkdir(parents=True, exist_ok=True)
    plan, facts = build_large()
    _, baseline = build_baseline()
    for name, value in (("plan.json", plan), ("facts.json", facts),
                        ("facts-baseline.json", baseline)):
        (target / name).write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8")
    print(json.dumps({
        "target": str(target),
        "stations": len(plan["stations"]),
        "transmissions": len(plan["transmissions"]),
        "events": len(facts["events"]),
        "baseline_events": len(baseline["events"]),
        "facts_bytes": (target / "facts.json").stat().st_size,
    }))


if __name__ == "__main__":
    main()
