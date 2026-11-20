import argparse
from .io import load, publish
from .observations import build_review_report
from .service import run


def main():
    parser = argparse.ArgumentParser(description="无线电频率协调")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--observations",
        help="可选：现场设备观测文档；不传时保持原计划冲突报告语义",
    )
    args = parser.parse_args()
    try:
        plan = load(args.source)
        if args.observations is None:
            result = run(plan)
        else:
            result = build_review_report(plan, load(args.observations))
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-coordinate: {exc}\n")


if __name__ == "__main__":
    main()
