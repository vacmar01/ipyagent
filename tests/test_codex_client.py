import json

import ipyai.codex_client as cc


class FakeClient:
    def __init__(self): self.calls = []

    async def turn_stream(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        yield "first "
        yield "second"


async def test_asyncchat_collects_stream(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(cc, "get_codex_client", lambda: fake)
    tools = [dict(type="function", function=dict(name="demo", description="Demo", parameters=dict(type="object")))]
    chat = cc.AsyncChat(model="gpt-5.4", sp="system", ns=dict(x=1), hist=["u1", "a1"], tools=tools)

    res = await chat("prompt", think="m", search="h")

    assert str(res) == "first second"
    prompt,kwargs = fake.calls[0]
    assert prompt == "prompt"
    assert kwargs["model"] == "gpt-5.4"
    assert kwargs["sp"] == "system"
    assert kwargs["hist"] == ["u1", "a1"]
    assert kwargs["tools"] == tools
    assert kwargs["think"] == "m"
    assert kwargs["search"] == "h"


def test_dynamic_tools_maps_openai_style_schema():
    tools = [dict(type="function", function=dict(name="demo", description="Demo tool", parameters=dict(type="object",
        properties=dict(x=dict(type="integer")))))]

    assert cc._dynamic_tools(tools) == [dict(name="demo", description="Demo tool", inputSchema=dict(type="object",
        properties=dict(x=dict(type="integer"))))]


async def test_call_tool_handles_async_and_json_result():
    async def demo(x): return dict(total=x + 1)

    res = await cc._call_tool(dict(demo=demo), "demo", dict(x=4))

    assert res["success"] is True
    assert json.loads(res["contentItems"][0]["text"]) == dict(total=5)


def test_completed_dynamic_tool_block_is_parseable():
    client = cc._CodexAppServer()
    item = dict(type="dynamicToolCall", id="call_1", tool="load_skill", arguments=dict(path="/tmp/s"), success=True,
        contentItems=[dict(type="inputText", text="---\nallowed-tools: helper\n---\nbody")])
    text = client._completed_item_text(item, set(), {})

    m = cc.re_tools.search(text)
    assert m is not None
    payload = json.loads(m.group(3))
    assert payload["call"]["function"] == "load_skill"
    assert "allowed-tools" in payload["result"]
