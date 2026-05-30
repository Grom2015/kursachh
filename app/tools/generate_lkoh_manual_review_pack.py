import argparse

from app.db.init_db import init_db
from app.services.validation.lkoh_manual_verification import generate_manual_review_pack, manual_review_pack_path


def print_summary(report: dict) -> None:
    print(
        "\n".join(
            [
                f"facts_selected: {report['facts_selected']}",
                f"periods_covered: {', '.join(report['periods_covered'])}",
                f"metric_codes_covered: {', '.join(report['metric_codes_covered'])}",
                f"output_path: {manual_review_pack_path()}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate LKOH manual fact review pack.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    init_db()
    report = generate_manual_review_pack(args.period_from, args.period_to, limit=args.limit)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
