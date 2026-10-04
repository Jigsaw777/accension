"""Offline reusable protocol contract fixtures; never require paid credentials."""
import json
from types import SimpleNamespace
import httpx
import pytest
from local_ai_router.config import Settings
from local_ai_router.engine import Engine
from local_ai_router.schema import Model, Provider
from local_ai_router.errors import AuthenticationRequired, RateLimited, ProviderUnavailable
from local_ai_router.provider_sdk import PluginRegistry, ProviderManifest


CHAT_PROVIDERS=["openai","openai-compatible","openrouter","groq","together","fireworks","mistral","cerebras","huggingface","lmstudio","vllm","llamacpp","ollama"]


@pytest.mark.parametrize("kind",CHAT_PROVIDERS)
async def test_chat_provider_generation_contract(tmp_path,kind):
    manifest=PluginRegistry().get(kind).manifest()
    assert ProviderManifest.model_validate(manifest).plugin_api_version=="1"
    seen=[]
    def handle(request):
        seen.append(request)
        body=json.loads(request.content)
        assert body["model"]=="actual-deployment" and "messages" in body
        assert str(request.url).endswith("/chat/completions")
        return httpx.Response(200,json={"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":7,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}}})
    p=Provider(kind=kind,endpoint="http://localhost:1234/v1" if manifest.local else "https://fixture.invalid/v1",local=manifest.local,auth="none")
    m=Model(id="alias",provider="fixture",deployment_name="actual-deployment",input_price=1,output_price=2)
    e=Engine(Settings(home=tmp_path,providers={"fixture":p},models=[m]),httpx.MockTransport(handle))
    try:
        result=await e.providers.generate(m,[{"role":"user","content":"hello"}],"executor","r","s",.05,max_output=16)
        assert result.text=="ok" and result.usage.cached_tokens==2 and len(seen)==1
        assert e.store.costs()["calls"]==1
    finally:
        await e.close()


@pytest.mark.parametrize("kind",["gemini","vertex"])
async def test_gemini_and_vertex_native_auth_usage_and_inventory(tmp_path,kind):
    seen=[]
    def handle(request):
        seen.append(request)
        if request.method=="GET":
            return httpx.Response(200,json={"models":[{"name":"models/actual","supportedGenerationMethods":["generateContent"],"inputTokenLimit":20000}]})
        assert request.url.path.endswith("/models/actual:generateContent")
        assert json.loads(request.content)["generationConfig"]["maxOutputTokens"]==16
        if kind=="vertex":assert request.headers["Authorization"]=="Bearer fixture-adc"
        else:assert request.headers["x-goog-api-key"]=="fixture-gemini"
        return httpx.Response(200,json={"candidates":[{"content":{"parts":[{"text":"native"}]}}],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3,"thoughtsTokenCount":2,"cachedContentTokenCount":1}})
    p=Provider(kind=kind,endpoint="https://fixture.invalid/v1",project="project-fixture",region="global",auth="google_adc" if kind=="vertex" else "api_key",credential_ref="provider/fixture",model_ids=["actual"])
    m=Model(id="alias",provider="p",deployment_name="actual",input_price=1,output_price=1)
    e=Engine(Settings(home=tmp_path,providers={"p":p},models=[m]),httpx.MockTransport(handle))
    e.providers.auth.vault.read=lambda ref:"fixture-gemini"
    e.providers.auth.google_credentials[(p.project,p.service_account_file)]=SimpleNamespace(valid=True,token="fixture-adc")
    try:
        inventory=await e.providers.discover("p",p)
        assert inventory.models[0].deployment_name=="actual"
        result=await e.providers.generate(m,[{"role":"user","content":"hello"}],"executor","r","s",.05,max_output=16)
        assert result.text=="native" and result.usage.output_tokens==5 and result.usage.cached_tokens==1
    finally:
        await e.close()


async def test_bedrock_chain_inventory_and_converse_contract(tmp_path,monkeypatch):
    calls=[]
    class Client:
        def list_foundation_models(self):
            return {"modelSummaries":[{"modelId":"regional-deployment","inferenceTypesSupported":["ON_DEMAND"],"outputModalities":["TEXT"]}]}
        def can_paginate(self,name):return False
        def converse(self,**kwargs):
            calls.append(kwargs)
            return {"output":{"message":{"content":[{"text":"native"}]}},"usage":{"inputTokens":5,"outputTokens":3,"cacheReadInputTokens":2}}
    p=Provider(kind="bedrock",region="us-east-1",auth="aws_chain")
    m=Model(id="alias",provider="p",deployment_name="regional-deployment",input_price=1,output_price=2)
    e=Engine(Settings(home=tmp_path,providers={"p":p},models=[m]))
    monkeypatch.setattr(e.providers.auth,"aws_client",lambda provider,service:Client())
    try:
        inventory=await e.providers.discover("p",p)
        assert inventory.models[0].deployment_name=="regional-deployment"
        result=await e.providers.generate(m,[{"role":"user","content":"hello"}],"executor","r","s",.05,max_output=16)
        assert result.text=="native" and result.usage.input_tokens==7
        assert calls[0]["modelId"]==m.deployment_name and calls[0]["inferenceConfig"]["maxTokens"]==16
    finally:await e.close()


@pytest.mark.parametrize("status,kind",[(401,AuthenticationRequired),(429,RateLimited),(500,ProviderUnavailable)])
async def test_http_error_contract_is_secret_safe(tmp_path,status,kind):
    e=Engine(Settings(home=tmp_path,providers={"p":Provider(kind="openai",endpoint="https://fixture.invalid",auth="none")},models=[Model(id="m",provider="p",deployment_name="m",input_price=1,output_price=1)]),httpx.MockTransport(lambda req:httpx.Response(status,json={"error":{"message":"password=fixture-private-value"}},headers={"Retry-After":"7"})))
    try:
        with pytest.raises(kind) as caught:
            await e.providers.generate(e.settings.models[0],[{"role":"user","content":"hello"}],"executor","r","s",.05,max_output=16)
        assert "fixture-private-value" not in str(caught.value)
        assert e.store.costs()["uncertain_calls"]==1
        if status==429:assert caught.value.retry_after==7
    finally:await e.close()


async def test_embedding_uses_supplied_input_and_lexical_fallback_needs_none(tmp_path):
    def handle(req):
        assert req.url.path=="/v1/embeddings" and json.loads(req.content)["input"]==["repository symbol"]
        return httpx.Response(200,json={"data":[{"embedding":[0.2,-0.4]}],"usage":{"prompt_tokens":3}})
    p=Provider(kind="openai-compatible",endpoint="http://localhost:1234/v1",local=True,auth="none")
    m=Model(id="embed",provider="p",deployment_name="embed",supports_embeddings=True,input_price=0,output_price=0)
    e=Engine(Settings(home=tmp_path,providers={"p":p},models=[m]),httpx.MockTransport(handle))
    try:assert await e.providers.embed(m,["repository symbol"],"r","s",0)==[[.2,-.4]]
    finally:await e.close()
