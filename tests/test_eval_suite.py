from pathlib import Path

import pytest

from mcp_doctor.evals.models import CapabilityDefinition, CapabilityMap, EvalSuite, EvalTask
from mcp_doctor.evals.suite import (
    EvalConfigError,
    filter_suite,
    load_capability_map,
    load_eval_suite,
    validate_capability_map,
)
from mcp_doctor.models import ServerInspection, ToolDefinition

EXAMPLES = Path(__file__).parents[1] / "examples" / "evals"


def test_loads_and_filters_example_suite() -> None:
    suite = load_eval_suite(EXAMPLES / "crm-suite.toml")
    capability_map = load_capability_map(EXAMPLES / "crm-v1.toml")

    filtered = filter_suite(suite, tags={"orders"})

    assert suite.name == "example-crm"
    assert capability_map.revision == "crm-v1"
    assert [task.id for task in filtered.tasks] == ["find-order-status"]


def test_rejects_unknown_task_filter() -> None:
    suite = EvalSuite(
        version=1,
        name="test",
        capabilities=(CapabilityDefinition(id="thing.read", description="Read a thing."),),
        tasks=(EvalTask(id="read-thing", prompt="Read it", expected_capabilities=("thing.read",)),),
    )

    with pytest.raises(EvalConfigError, match="Unknown task IDs"):
        filter_suite(suite, task_ids={"missing"})


def test_preflight_requires_every_live_tool_to_be_mapped() -> None:
    suite = EvalSuite(
        version=1,
        name="test",
        capabilities=(CapabilityDefinition(id="thing.read", description="Read a thing."),),
        tasks=(EvalTask(id="read-thing", prompt="Read it", expected_capabilities=("thing.read",)),),
    )
    capability_map = CapabilityMap(version=1, suite="test", revision="v1", tools={})
    inspection = ServerInspection(
        target="test",
        tools=[ToolDefinition(name="read_thing", input_schema={"type": "object"})],
    )

    with pytest.raises(EvalConfigError, match="unmapped server tools"):
        validate_capability_map(suite, capability_map, inspection)
