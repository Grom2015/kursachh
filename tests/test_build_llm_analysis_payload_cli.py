import json
import shutil
from pathlib import Path

from app.tools import build_llm_analysis_payload as cli


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _runtime_root() -> Path:
    root = Path("tests/runtime_llm_payload_cli")
    shutil.rmtree(root, ignore_errors=True)
    _write_json(
        root / "data" / "validation" / "coverage" / "moex_ifrs_2021_universal_coverage_scan.json",
        {
            "company_items": [
                {
                    "ticker": "CLI",
                    "company_name": "CLI",
                    "coverage_level": "FULL_STATEMENT_READY",
                    "blockers": [],
                    "source_package_status": "READY",
                }
            ]
        },
    )
    _write_json(
        root / "data" / "validation" / "CLI" / "2021Q4_2021Q4_financial_ratios.json",
        {
            "metrics": [
                {
                    "metric_code": "net_margin",
                    "metric_name": "Net Margin",
                    "period": "2021Q4",
                    "value": 0.1,
                    "display_value": "10.0%",
                    "unit": "ratio",
                    "status": "calculated",
                    "formula": "net_income / revenue",
                    "inputs": {},
                    "warnings": [],
                    "methodology_notes": [],
                }
            ]
        },
    )
    return root


def test_cli_writes_json(monkeypatch, capsys):
    root = _runtime_root()
    original_init = cli.LLMAnalysisPayloadBuilder.__init__

    def patched_init(self, root=None):
        original_init(self, root=root or root_path)

    root_path = root
    monkeypatch.setattr(cli.LLMAnalysisPayloadBuilder, "__init__", patched_init)

    payload, path = cli.build_llm_analysis_payload("CLI", "2021Q4", "2021Q4")

    assert path.exists()
    assert payload["company"]["ticker"] == "CLI"
    assert payload["financial_ratios"]["calculated"][0]["metric_code"] == "net_margin"

    exit_code = cli.main(["CLI", "2021Q4", "2021Q4", "--json-only"])
    assert exit_code == 0
    assert "llm_analysis_payload.json" in capsys.readouterr().out


def teardown_module():
    shutil.rmtree("tests/runtime_llm_payload_cli", ignore_errors=True)
