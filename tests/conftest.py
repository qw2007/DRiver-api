import pytest
from fastapi.testclient import TestClient

from driver_platform.main import create_app
from driver_platform.tools import ToolRegistry, build_default_registry


@pytest.fixture
def registry() -> ToolRegistry:
    return build_default_registry()


@pytest.fixture
def client(registry: ToolRegistry) -> TestClient:
    return TestClient(create_app(registry))
