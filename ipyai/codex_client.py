import asyncio,html,inspect,json,os,re,shlex
from collections import defaultdict
from types import SimpleNamespace

THINKING = "🧠🧠🧠"
_HIST_SP = ("\n\nIf the current user input contains a <conversation-history> block, treat it as serialized prior turns. "
    "Each <turn> contains one <user> and one <assistant> entry.")


class AsyncStreamFormatter:
    async def format_stream(self, stream):
        async for o in stream: yield o


class FullResponse(str):
    @property
    def content(self): return str(self)


def contents(res): return SimpleNamespace(content=str(res))


status_re = re.compile(r"<status>(.*?)</status>", flags=re.DOTALL)
re_tools = re.compile(r"(<details class='tool-usage-details'>\s*<summary>(?P<summary>.*?)</summary>\s*```json\s*(.*?)\s*```\s*</details>)",
    flags=re.DOTALL)


def _pkg_version():
    try:
        from importlib.metadata import version
        return version("ipyai")
    except Exception: return "0"


def _codex_cmd():
    raw = os.environ.get("IPYAI_CODEX_CMD", "codex")
    return [*shlex.split(raw), "app-server", "--listen", "stdio://"]


def _json(obj): return json.dumps(obj, ensure_ascii=False, default=str)


def _history_xml(hist):
    if not hist: return ""
    parts = ["<conversation-history>"]
    for i in range(0, len(hist), 2):
        user = hist[i]
        assistant = hist[i+1] if i+1 < len(hist) else ""
        parts.append("<turn>")
        parts.append(f"<user>{user}</user>")
        if assistant: parts.append(f"<assistant>{assistant}</assistant>")
        parts.append("</turn>")
    parts.append("</conversation-history>\n")
    return "".join(parts)


def _dynamic_tools(tools):
    res = []
    for o in tools or []:
        fn = dict(o.get("function") or {})
        name = fn.get("name")
        if not name: continue
        res.append(dict(name=name, description=fn.get("description") or "", inputSchema=fn.get("parameters") or dict(type="object")))
    return res or None


def _search_cfg(search):
    levels = dict(l="low", m="medium", h="high")
    if search not in levels: return None
    return dict(tools=dict(web_search=dict(context_size=levels[search])))


def _effort_level(level): return dict(l="low", m="medium", h="high").get(level, level or None)


def _tool_summary(name, args):
    if not args: return f"<code>{html.escape(name)}()</code>"
    bits = ", ".join(f"{k}={v!r}" for k,v in sorted((args or {}).items()))
    return f"<code>{html.escape(name)}({html.escape(bits)})</code>"


def _tool_block(summary, payload):
    return ("<details class='tool-usage-details'>\n"
        f"<summary>{summary}</summary>\n\n"
        "```json\n"
        f"{_json(payload)}\n"
        "```\n\n"
        "</details>\n")


def _content_items_text(items):
    if not items: return ""
    parts = []
    for o in items:
        if o.get("type") == "inputText": parts.append(o.get("text", ""))
        elif o.get("type") == "inputImage": parts.append(o.get("imageUrl", ""))
    return "\n".join(o for o in parts if o)


async def _call_tool(ns, name, args):
    fn = ns.get(name)
    if not callable(fn): return dict(success=False, contentItems=[dict(type="inputText", text=f"Error: tool {name!r} is not defined")])
    try:
        res = fn(**(args or {}))
        if inspect.isawaitable(res): res = await res
        text = res if isinstance(res, str) else _json(res)
        return dict(success=True, contentItems=[dict(type="inputText", text=text)])
    except Exception as e: return dict(success=False, contentItems=[dict(type="inputText", text=f"Error: {e}")])


class _CodexAppServer:
    def __init__(self):
        self.proc = None
        self.pending = {}
        self.events = None
        self.read_task = self.err_task = None
        self.req_id = 0
        self.init_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.initialized = False

    async def _start(self):
        if self.proc and self.proc.returncode is None: return
        self.proc = await asyncio.create_subprocess_exec(*_codex_cmd(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.pending = {}
        self.events = asyncio.Queue()
        self.read_task = asyncio.create_task(self._read_stdout())
        self.err_task = asyncio.create_task(self._drain_stderr())
        self.initialized = False

    async def _drain_stderr(self):
        while self.proc and self.proc.stderr:
            if not await self.proc.stderr.readline(): break

    async def _read_stdout(self):
        try:
            while self.proc and self.proc.stdout:
                raw = await self.proc.stdout.readline()
                if not raw: break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line: continue
                try: msg = json.loads(line)
                except Exception: continue
                if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done(): fut.set_result(msg)
                else: await self.events.put(msg)
        finally:
            err = RuntimeError("codex app-server exited")
            for fut in self.pending.values():
                if not fut.done(): fut.set_exception(err)
            self.pending.clear()

    async def _send(self, msg):
        await self._start()
        self.proc.stdin.write((_json(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.req_id += 1
        rid = str(self.req_id)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self._send(dict(id=rid, method=method, params=params))
        msg = await fut
        if "error" in msg: raise RuntimeError(msg["error"])
        return msg.get("result")

    async def respond(self, rid, result): await self._send(dict(id=rid, result=result))

    async def notify(self, method, params=None):
        msg = dict(method=method)
        if params is not None: msg["params"] = params
        await self._send(msg)

    async def ensure_initialized(self):
        async with self.init_lock:
            if self.initialized and self.proc and self.proc.returncode is None: return
            await self._start()
            await self.request("initialize", dict(clientInfo=dict(name="ipyai", title="ipyai", version=_pkg_version()),
                capabilities=dict(experimentalApi=True)))
            await self.notify("initialized")
            self.initialized = True

    async def turn_stream(self, prompt, *, model=None, sp="", hist=None, tools=None, ns=None, think=None, search=None, output_schema=None):
        await self.ensure_initialized()
        async with self.turn_lock:
            thread = await self.request("thread/start", self._thread_params(model=model, sp=sp, tools=tools, search=search))
            thread_id = thread["thread"]["id"]
            turn = await self.request("turn/start", self._turn_params(thread_id, prompt, hist=hist, think=think, output_schema=output_schema))
            turn_id = turn["turn"]["id"]
            async for chunk in self._consume_turn(thread_id, turn_id, ns or {}): yield chunk

    def _thread_params(self, *, model=None, sp="", tools=None, search=None):
        params = dict(cwd=os.getcwd(), approvalPolicy="never", sandbox="workspace-write", ephemeral=True, personality="pragmatic")
        if model: params["model"] = model
        if sp: params["developerInstructions"] = sp + _HIST_SP
        if (dtools := _dynamic_tools(tools)): params["dynamicTools"] = dtools
        if (cfg := _search_cfg(search)): params["config"] = cfg
        return params

    def _turn_params(self, thread_id, prompt, *, hist=None, think=None, output_schema=None):
        text = _history_xml(hist) + prompt
        params = dict(threadId=thread_id, input=[dict(type="text", text=text, text_elements=[])], cwd=os.getcwd(),
            approvalPolicy="never", personality="pragmatic")
        if (effort := _effort_level(think)): params["effort"] = effort
        if output_schema is not None: params["outputSchema"] = output_schema
        return params

    async def _consume_turn(self, thread_id, turn_id, ns):
        agent_seen,cmd_output = set(),defaultdict(str)
        saw_text = thinking = False
        while True:
            msg = await self.events.get()
            method = msg.get("method")
            params = msg.get("params") or {}
            if "id" in msg and method:
                if params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                    await self.respond(msg["id"], await self._handle_request(method, params, ns))
                continue
            if params.get("threadId") not in (None, thread_id): continue
            if params.get("turnId") not in (None, turn_id): continue
            if method == "error": raise RuntimeError(params)
            if method == "item/started" and (item := params.get("item")):
                if item.get("type") == "reasoning" and not saw_text and not thinking:
                    thinking = True
                    yield THINKING
                elif item.get("type") == "agentMessage" and saw_text and item.get("phase") == "final_answer": yield "\n\n"
                continue
            if method == "item/agentMessage/delta":
                if thinking and not saw_text:
                    yield "\n\n"
                    thinking = False
                saw_text = True
                agent_seen.add(params.get("itemId"))
                yield params.get("delta", "")
                continue
            if method == "item/commandExecution/outputDelta":
                cmd_output[params.get("itemId")] += params.get("delta", "")
                continue
            if method == "item/completed" and (item := params.get("item")):
                if (text := self._completed_item_text(item, agent_seen, cmd_output)):
                    if item.get("type") != "agentMessage" and saw_text and not text.startswith("\n"): text = "\n" + text
                    saw_text = True
                    yield text
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("error"): raise RuntimeError(turn["error"])
                break

    async def _handle_request(self, method, params, ns):
        if method == "item/tool/call": return await _call_tool(ns, params.get("tool"), params.get("arguments") or {})
        if method == "item/commandExecution/requestApproval": return dict(decision="accept")
        if method == "item/fileChange/requestApproval": return dict(decision="accept")
        if method == "item/permissions/requestApproval": return dict(permissions={}, scope="turn")
        if method == "item/tool/requestUserInput": return dict(answers={})
        if method == "mcpServer/elicitation/request": return dict(action="decline")
        return {}

    def _completed_item_text(self, item, agent_seen, cmd_output):
        typ = item.get("type")
        if typ == "agentMessage":
            if item.get("id") in agent_seen: return ""
            return item.get("text", "")
        if typ == "dynamicToolCall":
            payload = dict(id=item.get("id"), call=dict(function=item.get("tool"), arguments=item.get("arguments") or {}),
                result=_content_items_text(item.get("contentItems")), success=item.get("success"))
            return _tool_block(_tool_summary(item.get("tool", "tool"), item.get("arguments") or {}), payload)
        if typ == "commandExecution":
            output = item.get("aggregatedOutput")
            if output is None: output = cmd_output.get(item.get("id"), "")
            payload = dict(id=item.get("id"), call=dict(function="commandExecution", arguments=dict(command=item.get("command"), cwd=item.get("cwd"))),
                result=output, exit_code=item.get("exitCode"), status=item.get("status"))
            return _tool_block(f"<code>{html.escape(item.get('command') or '')}</code>", payload)
        return ""


_client = None


def get_codex_client():
    global _client
    if _client is None: _client = _CodexAppServer()
    return _client


class AsyncChat:
    def __init__(self, model=None, sp="", ns=None, hist=None, tools=None, cache=None):
        self.model,self.sp,self.ns,self.hist,self.tools = model,sp,ns or {},hist or [],tools or []
        self.cache = cache

    async def __call__(self, prompt, stream=False, think=None, search=None, output_schema=None, **kwargs):
        stream_it = get_codex_client().turn_stream(prompt, model=self.model, sp=self.sp, hist=self.hist, tools=self.tools, ns=self.ns,
            think=think, search=search, output_schema=output_schema)
        if stream: return stream_it
        text = "".join([o async for o in stream_it])
        return FullResponse(text)
