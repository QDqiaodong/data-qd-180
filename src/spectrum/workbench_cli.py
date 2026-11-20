"""CLI for the cross-process plan-change workbench (``radio-workbench``)."""
import argparse
import json
import sys

from . import workbench
from .errors import WorkbenchError
from .io import load


def _emit(value):
    sys.stdout.write(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    sys.stdout.flush()


def _fail(parser, code, message, **details):
    payload = {"error": code, "message": message}
    payload.update(details)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()
    parser.exit(2)


class _Parser(argparse.ArgumentParser):
    """Report usage errors as one JSON object on stderr, still exiting 2."""

    def error(self, message):
        sys.stderr.write(
            json.dumps(
                {"error": "invalid_input", "message": message},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        sys.stderr.flush()
        raise SystemExit(2)


def build_parser():
    parser = _Parser(
        prog="radio-workbench",
        description="本地跨进程计划变更工作台（固定站点，修订式发射计划）",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="用源计划建立修订 0")
    init.add_argument("--ledger", required=True)
    init.add_argument("--source", required=True)

    propose = commands.add_parser("propose", help="登记不可变提案")
    propose.add_argument("--ledger", required=True)
    propose.add_argument("--source", required=True)

    preview = commands.add_parser("preview", help="针对当前计划重新推演提案")
    preview.add_argument("--ledger", required=True)
    preview.add_argument("--proposal", required=True)

    approve = commands.add_parser("approve", help="记录接收站对当前证据的确认")
    approve.add_argument("--ledger", required=True)
    approve.add_argument("--proposal", required=True)
    approve.add_argument("--receiver", required=True)
    approve.add_argument("--review-id", required=True)

    apply_cmd = commands.add_parser("apply", help="核验并提交整份提案")
    apply_cmd.add_argument("--ledger", required=True)
    apply_cmd.add_argument("--proposal", required=True)

    show = commands.add_parser("show", help="返回已提交修订的完整计划与报告")
    show.add_argument("--ledger", required=True)
    show.add_argument("--revision", type=int)
    return parser


def run(args):
    if args.command == "init":
        return workbench.init_ledger(args.ledger, load(args.source))
    if args.command == "propose":
        return workbench.propose(args.ledger, load(args.source))
    if args.command == "preview":
        return workbench.preview(args.ledger, args.proposal)
    if args.command == "approve":
        return workbench.approve(
            args.ledger, args.proposal, args.receiver, args.review_id
        )
    if args.command == "apply":
        return workbench.apply_proposal(args.ledger, args.proposal)
    if args.command == "show":
        return workbench.show(args.ledger, args.revision)
    raise WorkbenchError("invalid_input", f"unknown command: {args.command}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _emit(run(args))
    except WorkbenchError as exc:
        _fail(parser, exc.code, exc.message, **exc.details)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        _fail(parser, "invalid_input", str(exc))


if __name__ == "__main__":
    main()
