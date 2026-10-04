from types import SimpleNamespace

import pytest

from accension_example import ExampleProvider
from local_ai_router.config import Settings
from local_ai_router.provider_sdk import ProviderContext
from local_ai_router.schema import Provider


@pytest.mark.asyncio
async def test_exact_ids_and_conservative_capabilities(tmp_path):
    provider = Provider(
        kind="example-loopback", local=True, auth="none",
        endpoint="http://127.0.0.1:9000/v1", protocol="openai_chat",
        model_ids=["actual/deployment:latest"],
    )
    manager = SimpleNamespace(
        settings=Settings(home=tmp_path),
        protocol=lambda value: value.protocol,
    )
    plugin = ExampleProvider()
    inventory = await plugin.discover_models(ProviderContext("example", provider, manager))
    assert plugin.manifest().plugin_api_version == "1"
    assert not inventory.complete
    model = inventory.models[0]
    assert model.deployment_name == "actual/deployment:latest"
    assert model.id == "example:actual/deployment:latest"
    assert model.locality == "local" and model.input_price == 0
    assert model.supports_chat_completions
    assert not model.supports_code and not model.supports_tools
    assert not model.supports_structured_output
