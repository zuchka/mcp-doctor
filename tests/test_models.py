from mcp_doctor.models import Finding, Severity


def test_finding_fingerprint_ignores_presentation_and_tool_order() -> None:
    first = Finding(
        rule_id="semantic-overlap.ambiguous-pair",
        severity=Severity.WARNING,
        tools=["search_orders", "find_orders"],
        message="First wording.",
        evidence={"score": 0.9},
    )
    second = Finding(
        rule_id="semantic-overlap.ambiguous-pair",
        severity=Severity.WARNING,
        tools=["find_orders", "search_orders"],
        message="Reworded finding.",
        evidence={"score": 0.8},
    )

    assert first.fingerprint == second.fingerprint
