from .model import validate
from .interference import conflicts


def run(data):
    stations, transmissions = validate(data)
    rows = conflicts(stations, transmissions)
    affected = {row["victim_transmission_id"] for row in rows}
    return {"schema_version": 1, "model": "declared_receiver_protection_circle",
            "conflicts": rows, "clear_transmission_ids": sorted(set(transmissions) - affected),
            "summary": {"transmissions": len(transmissions), "directed_conflicts": len(rows),
                        "affected_receptions": len(affected)}}
