from __future__ import annotations

from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402
from scripts.browser_collect_seed import write_outputs  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_browser_collect_writes_xlsx_csv_json_report_and_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        records = [
            {"name": "Demo Laptop", "price": "999.99", "url": "https://example.com/p/1", "source": "https://example.com"},
            {"name": "Demo Phone", "price": "199.00", "url": "https://example.com/p/2", "source": "https://example.com"},
        ]
        manifest = {
            "status": "ok",
            "query": "demo products",
            "start_url": "https://example.com",
            "screenshot_path": "",
            "raw_html_path": "",
            "errors": [],
        }
        write_outputs(output_dir, records, manifest)
        for filename in ("items.xlsx", "items.csv", "items.json", "report.md", "manifest.json"):
            assert_true(filename, (output_dir / filename).exists())
        workbook = load_workbook(output_dir / "items.xlsx")
        sheet = workbook.active
        assert_true("xlsx_header", [cell.value for cell in sheet[1]] == ["name", "price", "url", "source"])
        assert_true("xlsx_first_row", sheet["A2"].value == "Demo Laptop")
        assert_true("manifest_xlsx", manifest["files"]["xlsx"].endswith("items.xlsx"))


if __name__ == "__main__":
    test_browser_collect_writes_xlsx_csv_json_report_and_manifest()
    print("browser_collect_output_scenarios passed")
