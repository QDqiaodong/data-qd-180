"""Declared protection-circle model, not an RF propagation or licensing model."""
from fractions import Fraction
from math import asin, cos, radians, sin, sqrt


def distance_m(a, b):
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat = lat2 - lat1
    dlon = radians(b["lon"] - a["lon"])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371008.8 * 2 * asin(sqrt(max(0.0, min(1.0, h))))


def conflicts(stations, transmissions):
    rows = []
    for victim_id, victim in sorted(transmissions.items()):
        receiver = stations[victim["receiver_id"]]
        for source_id, source in sorted(transmissions.items()):
            if victim_id == source_id:
                continue
            start = max(victim["start_dt"], source["start_dt"])
            end = min(victim["end_dt"], source["end_dt"])
            lo = max(victim["low_twice_hz"], source["low_twice_hz"])
            hi = min(victim["high_twice_hz"], source["high_twice_hz"])
            if start >= end or lo >= hi:
                continue
            separation = distance_m(receiver, stations[source["station_id"]])
            if separation > receiver["protection_radius_m"]:
                continue
            rows.append({
                "victim_transmission_id": victim_id, "interferer_transmission_id": source_id,
                "receiver_id": victim["receiver_id"], "interferer_station_id": source["station_id"],
                "overlap_start": start.isoformat(), "overlap_end": end.isoformat(),
                "overlap_low_hz": str(Fraction(lo, 2)), "overlap_high_hz": str(Fraction(hi, 2)),
                "overlap_bandwidth_hz": str(Fraction(hi - lo, 2)),
                "separation_m": round(separation, 3),
            })
    return rows
