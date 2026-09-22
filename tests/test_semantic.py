from mcp_doctor.analyzers import analyze
from mcp_doctor.models import ServerInspection, ToolDefinition
from mcp_doctor.semantic import compare_tools, has_negative_guidance, has_positive_guidance


def _tool(name: str, description: str, *parameters: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {parameter: {"type": "string"} for parameter in parameters},
        },
    )


def test_synonymous_names_and_matching_schemas_overlap() -> None:
    left = _tool(
        "find_orders",
        "Find orders using free text and customer details.",
        "query",
    )
    right = _tool(
        "search_orders",
        "Search orders using free text and customer details.",
        "query",
    )

    similarity = compare_tools(left, right)

    assert similarity.score == 1.0
    assert similarity.left == "find_orders"
    assert similarity.right == "search_orders"


def test_distinct_mutating_actions_are_not_ambiguous() -> None:
    create = _tool(
        "create_customer",
        "Use when a new customer account needs to be created.",
        "name",
    )
    delete = _tool(
        "delete_customer",
        "Use when an existing customer account must be permanently removed.",
        "customer_id",
    )

    report = analyze(ServerInspection(target="test", tools=[create, delete]))
    semantic = next(check for check in report.checks if check.code == "semantic-overlap")

    assert semantic.passed
    assert semantic.findings == []


def test_ambiguous_pair_is_deterministic_when_tool_order_changes() -> None:
    left = _tool(
        "find_orders",
        "Find matching orders using the supplied free text and customer details.",
        "query",
    )
    right = _tool(
        "search_orders",
        "Search matching orders using the supplied free text and customer details.",
        "query",
    )

    first = analyze(ServerInspection(target="test", tools=[left, right]))
    second = analyze(ServerInspection(target="test", tools=[right, left]))
    first_finding = next(
        finding
        for check in first.checks
        for finding in check.findings
        if finding.rule_id == "semantic-overlap.ambiguous-pair"
    )
    second_finding = next(
        finding
        for check in second.checks
        for finding in check.findings
        if finding.rule_id == "semantic-overlap.ambiguous-pair"
    )

    assert first_finding.fingerprint == second_finding.fingerprint
    assert first_finding.evidence == second_finding.evidence


def test_explicit_boundaries_turn_overlap_into_information() -> None:
    left = _tool(
        "find_orders",
        "Use when locating orders from free text. Do not use for exact order numbers.",
        "query",
    )
    right = _tool(
        "search_orders",
        "Use when locating orders from free text; choose find_orders instead for broad queries.",
        "query",
    )

    report = analyze(ServerInspection(target="test", tools=[left, right]))
    semantic = next(check for check in report.checks if check.code == "semantic-overlap")

    assert semantic.passed
    assert [finding.rule_id for finding in semantic.findings] == ["semantic-overlap.guided-pair"]
    assert has_positive_guidance(left.description)
    assert has_negative_guidance(left.description)


def test_long_description_without_explicit_guidance_is_reported() -> None:
    tool = _tool(
        "synchronize_customer",
        "Synchronize customer profile details with the account system and return its status.",
        "customer_id",
    )

    report = analyze(ServerInspection(target="test", tools=[tool]))
    guidance = next(check for check in report.checks if check.code == "selection-guidance")

    assert not guidance.passed
    assert guidance.findings[0].rule_id == "selection-guidance.missing-positive"
