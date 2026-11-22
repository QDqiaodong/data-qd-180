"""CLI for the privacy-reviewed reception bulletin (``radio-bulletin``).

Conventions mirror the other offline commands: strict JSON in for both
files, one freshly created atomic bulletin out, exit code 2 on any invalid
input, conflicting history or existing output target, and exit code 0 for
every valid business result including bulletins that withhold requests.
No withheld value, private cell value or plan detail is ever printed.
"""
import argparse

from .bulletin import build_bulletin
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(
        prog="radio-bulletin",
        description=(
            "为冻结计划生成可公开的接收统计公告，并按保护差距审查每个口径"
        ),
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        plan = load(args.source)
        rules = load(args.rules)
        result = build_bulletin(plan, rules)
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-bulletin: {exc}\n")


if __name__ == "__main__":
    main()
