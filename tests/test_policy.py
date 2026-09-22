import pytest

from mcp_doctor.analyzers import analyze
from mcp_doctor.models import ServerInspection, ToolDefinition
from mcp_doctor.policy import AnalysisPolicy, PolicyError, Suppression, Thresholds, load_policy
from mcp_doctor.report import render_text


def _ambiguous_tools() -> list[ToolDefinition]:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    return [
        ToolDefinition(
            name="find_orders",
            description="Use when matching orders from customer text and account details.",
            input_schema=schema,
        ),
        ToolDefinition(
            name="search_orders",
            description="Use when matching orders from customer text and account details.",
            input_schema=schema,
        ),
    ]


def test_loads_strict_toml_policy(tmp_path) -> None:
    path = tmp_path / "doctor.toml"
    path.write_text(
        """
version = 1
disabled_rules = ["crud-wrapper-smells"]

[thresholds]
semantic_overlap = 0.8
""".strip()
    )

    policy = load_policy(path)

    assert policy.thresholds.semantic_overlap == 0.8
    assert not policy.rule_enabled("crud-wrapper-smells.family")


def test_rejects_unknown_policy_fields(tmp_path) -> None:
    path = tmp_path / "doctor.toml"
    path.write_text("unknown = true")

    with pytest.raises(PolicyError, match="Invalid analysis policy"):
        load_policy(path)


def test_rejects_suppression_without_an_exact_target(tmp_path) -> None:
    path = tmp_path / "doctor.toml"
    path.write_text(
        """
version = 1

[[suppressions]]
rule = "description-quality.short"
reason = "Too broad to be safe."
""".strip()
    )

    with pytest.raises(PolicyError, match="must specify tools or a subject"):
        load_policy(path)


def test_exact_suppression_is_visible_but_not_active() -> None:
    policy = AnalysisPolicy(
        thresholds=Thresholds(semantic_overlap=0.7),
        suppressions=(
            Suppression(
                rule="semantic-overlap.ambiguous-pair",
                tools=("find_orders", "search_orders"),
                reason="Intentional aliases during a migration.",
            ),
        ),
    )

    report = analyze(ServerInspection(target="test", tools=_ambiguous_tools()), policy)
    overlap = next(check for check in report.checks if check.code == "semantic-overlap")

    assert overlap.passed
    assert overlap.findings[0].suppressed
    assert overlap.findings[0].suppression_reason == "Intentional aliases during a migration."
    assert report.suppressed_count == 1
    assert report.warning_count == 0
    assert "Policy: custom" in render_text(report)


def test_semantic_threshold_changes_pair_detection() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    inspection = ServerInspection(
        target="test",
        tools=[
            ToolDefinition(name="find_orders", input_schema=schema),
            ToolDefinition(name="search_orders", input_schema=schema),
        ],
    )

    default_report = analyze(inspection)
    custom_report = analyze(
        inspection,
        AnalysisPolicy(thresholds=Thresholds(semantic_overlap=0.6)),
    )

    assert default_report.metrics["overlapping_tool_pairs"] == 0
    assert custom_report.metrics["overlapping_tool_pairs"] == 1
