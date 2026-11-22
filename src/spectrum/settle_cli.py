"""CLI for the planned-loss settlement draft (``radio-settle``).

Conventions mirror ``radio-protection``: strict JSON in for both files,
one freshly created atomic report out, exit code 2 on any invalid input
or existing output target, exit code 0 for every valid business result
(including zero loss, zero credit, no liable operator and no commitments).
"""
import argparse

from .io import load, publish
from .settle import build_settlement_report


def main():
    parser = argparse.ArgumentParser(
        prog="radio-settle",
        description="按确认计划与结算条款起草频道履约补偿（约定模型下的计划损失）",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--terms", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        plan = load(args.source)
        terms = load(args.terms)
        result = build_settlement_report(plan, terms)
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-settle: {exc}\n")


if __name__ == "__main__":
    main()
