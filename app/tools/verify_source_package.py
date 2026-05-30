import argparse

from app.tools.verify_lkoh_source_package import SourcePackageVerifier, print_summary, save_source_package_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify real financial source package for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--live", action="store_true", help="Check live content-type metadata with timeout.")
    parser.add_argument("--tls-diagnostics", action="store_true", help="Write TLS diagnostics if live TLS verification fails.")
    args = parser.parse_args(argv)
    report = SourcePackageVerifier(args.ticker).verify(
        args.period_from, args.period_to, live=args.live, tls_diagnostics=args.tls_diagnostics
    )
    path = save_source_package_report(report)
    print_summary(report, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
