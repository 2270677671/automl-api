from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
LOCAL_MARKDOWN_LINK = re.compile(r"\[[^]]*]\((?!https?://|mailto:)([^)]+)\)")


def test_example_request_bodies_cover_the_three_supported_backends() -> None:
    requests = {}
    for path in sorted((EXAMPLES / "requests").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        requests[document["objective"]["backend_id"]] = document

        assert document["dataset_version_id"] == "dsv_REPLACE_ME"
        assert document["autonomy"]["mode"] == "GUIDED"
        assert document["autonomy"]["production_deploy"] == "DISABLED"
        assert document["policy"]["allow_pii"] is False
        assert document["policy"]["allow_external_llm"] is False
        assert document["budget"]["max_llm_tokens"] == 0

    assert set(requests) == {"sklearn", "autogluon", "tabpfn"}
    assert requests["sklearn"]["objective"]["target_column"] is None
    assert requests["autogluon"]["objective"]["task_type"] == "BINARY_CLASSIFICATION"
    assert requests["tabpfn"]["objective"]["task_type"] == "REGRESSION"


def test_example_datasets_have_expected_schema_and_enough_rows() -> None:
    expected_headers = {
        "customer_churn.csv": [
            "tenure_months",
            "monthly_fee",
            "support_tickets",
            "plan_type",
            "churned",
        ],
        "regression.csv": [
            "feature_a",
            "feature_b",
            "feature_c",
            "segment",
            "target",
        ],
    }
    for filename, expected_header in expected_headers.items():
        with (EXAMPLES / "data" / filename).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        assert rows[0] == expected_header
        assert len(rows) >= 61
        assert all(len(row) == len(expected_header) for row in rows[1:])


def test_python_examples_compile_without_importing_optional_sdk() -> None:
    for path in sorted((EXAMPLES / "python").glob("*.py")):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_documentation_local_links_resolve() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "docs" / "README.md",
        ROOT / "docs" / "api-user-guide-with-examples.md",
        ROOT / "docs" / "reproduction-guide.md",
        ROOT / "docs" / "user-manual.md",
        EXAMPLES / "README.md",
        ROOT / "packages" / "python_sdk" / "README.md",
    )
    failures = []
    for document in documents:
        content = document.read_text(encoding="utf-8")
        for raw_target in LOCAL_MARKDOWN_LINK.findall(content):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target:
                continue
            resolved = (document.parent / unquote(target)).resolve()
            if not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)} -> {raw_target}")
    assert not failures, "unresolved local links:\n" + "\n".join(failures)
