from datetime import datetime, timezone, timedelta
from math import isfinite
from .io import integer, unique


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("time must be ISO-8601 with offset")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("time requires explicit offset")
    return result.astimezone(timezone.utc)


def validate(data):
    stations = unique(data["stations"], "station")
    transmissions = unique(data["transmissions"], "transmission")
    if not 1 <= len(stations) <= 500 or len(transmissions) > 300:
        raise ValueError("limits: 1..500 stations, 300 transmissions")
    for station in stations.values():
        for key, bound in (("lat", 90), ("lon", 180)):
            value = station[key]
            if type(value) not in (int, float) or not isfinite(value) or not -bound <= value <= bound:
                raise ValueError(f"invalid coordinate {key}")
        integer(station["protection_radius_m"], "protection_radius_m", 1)
    parsed = {}
    for key, row in transmissions.items():
        if row["station_id"] not in stations or row["receiver_id"] not in stations:
            raise ValueError("unknown transmitter or receiver")
        if row["station_id"] == row["receiver_id"]:
            raise ValueError("transmitter and receiver must differ")
        center = integer(row["center_hz"], "center_hz", 1)
        bandwidth = integer(row["bandwidth_hz"], "bandwidth_hz", 1)
        if bandwidth > 2 * center:
            raise ValueError("occupied band extends below zero")
        start, end = stamp(row["start"]), stamp(row["end"])
        if not timedelta(0) < end - start <= timedelta(days=7):
            raise ValueError("transmission duration must be positive and <=7 days")
        parsed[key] = {**row, "start_dt": start, "end_dt": end,
                       "low_twice_hz": 2 * center - bandwidth,
                       "high_twice_hz": 2 * center + bandwidth}
    return stations, parsed
