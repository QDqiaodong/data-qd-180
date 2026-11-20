"""CLI for the offline channel arranger (``radio-arrange``).

Conventions mirror ``radio-coordinate``: strict JSON in, one freshly created
atomic report out, exit code 2 on any invalid input or existing output target,
exit code 0 for both ``optimal`` and ``infeasible`` business reports.
"""
import argparse

from .arrange import arrange
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(
        prog="radio-arrange",
        description="离线频道编排：生成建议及设备调谐日程，不创建提案、不修改台账",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = arrange(load(args.source))
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-arrange: {exc}\n")


if __name__ == "__main__":
    main()
