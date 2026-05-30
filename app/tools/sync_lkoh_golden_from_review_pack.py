import argparse

from app.services.validation.lkoh_manual_verification import sync_golden_from_review_pack


def print_summary(report: dict) -> None:
    print(
        "\n".join(
            [
                f"review_pack_count: {report['review_pack_count']}",
                f"existing_golden_checks: {report['existing_golden_checks']}",
                f"added_checks: {report['added_checks']}",
                f"skipped_duplicates: {report['skipped_duplicates']}",
                f"total_golden_checks: {report['total_golden_checks']}",
                f"golden_path: {report['golden_path']}",
                f"checklist_path: {report['checklist_path']}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync LKOH golden YAML from manual review pack.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    report = sync_golden_from_review_pack(args.period_from, args.period_to)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
