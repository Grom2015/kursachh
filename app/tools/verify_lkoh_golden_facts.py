import argparse

from app.db.init_db import init_db
from app.services.validation.lkoh_manual_verification import golden_report_path, verify_golden_checks


def print_summary(report: dict) -> None:
    print(
        "\n".join(
            [
                f"status: {report['status']}",
                f"review_pack_status: {report['review_pack_status']}",
                f"review_pack_verified: {report['review_pack_verified']}",
                f"review_pack_accuracy: {report['review_pack_accuracy']}",
                f"full_golden_dataset_status: {report['full_golden_dataset_status']}",
                f"total_checks_count: {report['total_checks_count']}",
                f"verified_checks_count: {report['verified_checks_count']}",
                f"passed_count: {report['passed_count']}",
                f"failed_count: {report['failed_count']}",
                f"pending_count: {report['pending_count']}",
                f"mismatch_count: {report['mismatch_count']}",
                f"not_found_count: {report['not_found_count']}",
                f"needs_review_count: {report['needs_review_count']}",
                f"accuracy: {report['accuracy']}",
                f"report_path: {golden_report_path()}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify LKOH extracted facts against manual golden checks.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    init_db()
    report = verify_golden_checks(args.period_from, args.period_to)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
