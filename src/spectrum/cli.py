import argparse
from .io import load, publish
from .service import run


def main():
    parser = argparse.ArgumentParser(description="无线电频率协调")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"radio-coordinate: {exc}\n")


if __name__ == "__main__":
    main()
