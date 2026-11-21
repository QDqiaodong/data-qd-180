"""CLI for receive-protection daily report recomputation (``radio-protection``).

Conventions mirror ``radio-coordinate``: strict JSON in, one freshly created
atomic report out, exit code 2 on any invalid input or existing output target,
exit code 0 for every valid business result (including missing facts,
disputes and an empty correction list).
"""
import argparse

from .io import load, publish
from .protection import build_protection_report


def main():
    parser = argparse.ArgumentParser(
        prog="radio-protection",
        description="按计划与台站资料重算并更正接收保护日报",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--station-facts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        plan = load(args.source)
        facts = load(args.station_facts)
        result = build_protection_report(plan, facts)
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-protection: {exc}\n")


if __name__ == "__main__":
    main()
