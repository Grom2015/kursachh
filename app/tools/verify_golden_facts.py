import argparse
import json

import yaml

from app.tools.generate_manual_review_pack import golden_path


def verify(ticker: str, period_from: str, period_to: str) -> dict:
    path = golden_path(ticker)
    if not path.exists():
        report = empty_report(ticker, period_from, period_to, ["Golden checks file not found."])
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        checks = data.get("checks", []) or []
        review_checks = [check for check in checks if check.get("origin") == "review_pack_sync"]
        verified = [check for check in checks if check.get("manual_status") == "verified"]
        review_verified = [check for check in review_checks if check.get("manual_status") == "verified"]
        pending = [check for check in checks if check.get("manual_status") == "pending"]
        passed, failures = compare_verified(verified)
        review_passed, review_failures = compare_verified(review_verified)
        accuracy = passed / len(verified) if verified else None
        review_accuracy = review_passed / len(review_verified) if review_verified else None
        report = {
            "company": ticker.upper(),
            "period_from": period_from,
            "period_to": period_to,
            "review_pack_total": len(review_checks),
            "review_pack_verified": len(review_verified),
            "review_pack_passed": review_passed,
            "review_pack_failed": len(review_failures),
            "review_pack_accuracy": review_accuracy,
            "review_pack_status": review_status(review_checks, review_verified, review_accuracy),
            "review_pack_sample_status": review_status(review_checks, review_verified, review_accuracy),
            "full_golden_dataset_status": full_status(verified, pending, accuracy),
            "total_checks_count": len(checks),
            "verified_checks_count": len(verified),
            "passed_count": passed,
            "failed_count": len(failures),
            "pending_count": len(pending),
            "accuracy": accuracy,
            "status": full_status(verified, pending, accuracy),
            "failures": failures + review_failures,
            "warnings": ["Manual verification comparison is pending for this peer pilot."] if not verified else [],
            "next_actions": ["Analyst must fill expected_value and mark checks verified before claiming manual QA."],
        }
    out = path.parent / f"{ticker.casefold()}_2021_golden_verification_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def empty_report(ticker: str, period_from: str, period_to: str, warnings: list[str]) -> dict:
    return {
        "company": ticker.upper(),
        "period_from": period_from,
        "period_to": period_to,
        "review_pack_total": 0,
        "review_pack_verified": 0,
        "review_pack_passed": 0,
        "review_pack_failed": 0,
        "review_pack_accuracy": None,
        "review_pack_status": "NO_REVIEW_PACK_CHECKS",
        "review_pack_sample_status": "NO_REVIEW_PACK_CHECKS",
        "full_golden_dataset_status": "NO_VERIFIED_CHECKS",
        "total_checks_count": 0,
        "verified_checks_count": 0,
        "passed_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "accuracy": None,
        "status": "NO_VERIFIED_CHECKS",
        "warnings": warnings,
        "next_actions": ["Generate a manual review pack first."],
    }


def compare_verified(checks: list[dict]) -> tuple[int, list[dict]]:
    passed = 0
    failures = []
    for check in checks:
        expected = check.get("expected_value")
        actual = check.get("actual_extracted_value")
        if expected is None:
            failures.append(
                {
                    "period": check.get("period"),
                    "metric_code": check.get("metric_code"),
                    "reason": "expected_value missing",
                }
            )
            continue
        if actual is None:
            failures.append(
                {
                    "period": check.get("period"),
                    "metric_code": check.get("metric_code"),
                    "reason": "actual value missing",
                }
            )
            continue
        allowed = max(0.01, abs(float(expected)) * 0.01)
        if abs(float(actual) - float(expected)) <= allowed:
            passed += 1
        else:
            failures.append({"period": check.get("period"), "metric_code": check.get("metric_code"), "reason": "value mismatch"})
    return passed, failures


def review_status(review_checks: list[dict], review_verified: list[dict], accuracy: float | None) -> str:
    if not review_checks:
        return "NO_REVIEW_PACK_CHECKS"
    if not review_verified:
        return "NO_VERIFIED_CHECKS"
    if accuracy is not None and accuracy < 0.9:
        return "FAIL"
    if len(review_verified) < len(review_checks):
        return "IN_PROGRESS"
    return "PASS"


def full_status(verified: list[dict], pending: list[dict], accuracy: float | None) -> str:
    if not verified:
        return "NO_VERIFIED_CHECKS"
    if accuracy is not None and accuracy < 0.9:
        return "FAIL"
    if pending:
        return "IN_PROGRESS"
    return "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify ticker golden facts.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    report = verify(args.ticker, args.period_from, args.period_to)
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
                f"accuracy: {report['accuracy']}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
