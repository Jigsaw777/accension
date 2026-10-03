import json
import sys
from pathlib import Path

import httpx

from local_ai_router.config import load
from local_ai_router.engine import Engine
from local_ai_router.schema import Classification


async def test_example_discovers_and_calls_an_arbitrary_local_model(settings):
    example = Path(__file__).resolve().parents[1] / 'config/examples/local-models.yaml'
    (settings.home / 'config/local.yaml').write_bytes(example.read_bytes())
    configured = load(settings.home)
    assert not configured.routing.laya_enabled
    seen = []

    def respond(request):
        assert request.url.host == '127.0.0.1' and request.url.port == 8080
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'another-family:custom-build'}]})
        assert request.url.path == '/v1/chat/completions'
        seen.append(json.loads(request.content)['model'])
        return httpx.Response(200, json={'choices': [{'message': {'content': 'hello'}}],
                                        'usage': {'prompt_tokens': 5, 'completion_tokens': 2}})

    engine = Engine(configured, httpx.MockTransport(respond))
    try:
        await engine.discovery.refresh(True)
        model = engine.router.candidates(Classification(task_family='explanation'), allow_cloud=False)[0]
        assert not engine.router.candidates(Classification(task_family='coding'), allow_cloud=False)
        result = await engine.providers.generate(model, [{'role': 'user', 'content': 'Explain hello'}],
                                                 'executor', 'custom-local', 'custom-local', 0)
        assert result.text == 'hello' and seen == ['another-family:custom-build']
    finally:
        await engine.close()


async def test_alternative_classifier_executable_and_tool(engine, tmp_path):
    server = tmp_path / 'custom_classifier.py'
    server.write_text('''import json
from mcp.server.fastmcp import FastMCP
app = FastMCP("test-classifier")
@app.tool()
def classify_local(state: dict, min_confidence: float, schema: dict) -> str:
    assert state["request"] and "properties" in schema
    return json.dumps({"values": {"task_family": "coding", "complexity": 20, "risk": 10,
                                 "planning_required": False},
                       "confidence": {"task_family": 0.95, "complexity": 0.95,
                                      "risk": 0.95, "planning_required": 0.95}})
app.run(transport="stdio")
''', encoding='utf-8')
    policy = engine.settings.routing
    policy.laya_enabled = True
    policy.laya_command = sys.executable
    policy.laya_args = [str(server)]
    policy.classifier_tool = 'classify_local'
    policy.laya_timeout = 15
    result = await engine.router.classify('Implement a parser', 'custom-classifier')
    assert result.complexity == 20 and 'LAYA_ACCEPTED' in result.reason_codes


async def test_missing_optional_classifier_falls_back(engine, tmp_path):
    policy = engine.settings.routing
    policy.laya_enabled = True
    policy.laya_command = str(tmp_path / 'not-installed')
    policy.laya_timeout = 0.2
    result = await engine.router.classify('Implement a parser', 'missing-classifier')
    assert result.task_family == 'coding' and 'LAYA_TIMEOUT_OR_UNAVAILABLE' in result.reason_codes
