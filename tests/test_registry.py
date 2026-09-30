import pytest

from driver_platform.errors import DuplicateToolError, InvalidToolError, ToolNotFoundError
from driver_platform.tools import EchoTool, build_default_registry
from driver_platform.tools.base import Tool
from driver_platform.tools.echo import EchoInput, EchoOutput


class UpperTool(Tool[EchoInput, EchoOutput]):
    name = "upper"
    contract_version = "2.1"
    description = "测试用工具"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, arguments: EchoInput) -> EchoOutput:
        return EchoOutput(echo=arguments.message.upper(), times=arguments.times)


class BadNameTool(UpperTool):
    name = "Bad-Name"


class MissingInputModelTool(UpperTool):
    name = "noinput"
    input_model = None  # type: ignore[assignment]


def test_default_registry_exposes_echo():
    assert build_default_registry().names() == ["echo"]


def test_register_then_get_returns_same_instance():
    registry = build_default_registry()
    tool = UpperTool()

    registry.register(tool)

    assert registry.get("upper") is tool
    assert "upper" in registry
    assert len(registry) == 2


def test_duplicate_name_is_rejected():
    registry = build_default_registry()

    with pytest.raises(DuplicateToolError):
        registry.register(EchoTool())


def test_unknown_tool_raises_tool_not_found():
    with pytest.raises(ToolNotFoundError):
        build_default_registry().get("nope")


@pytest.mark.parametrize("tool", [BadNameTool(), MissingInputModelTool()])
def test_invalid_contract_is_rejected(tool):
    with pytest.raises(InvalidToolError):
        build_default_registry().register(tool)


def test_manifest_carries_contract_and_json_schema():
    manifest = build_default_registry().get("echo").manifest()

    assert manifest.name == "echo"
    assert manifest.contract_version == "1.0"
    assert manifest.input_schema["required"] == ["message"]
    assert manifest.input_schema["properties"]["times"]["maximum"] == 5
    assert manifest.output_schema is not None
