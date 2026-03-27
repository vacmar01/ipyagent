#!/usr/bin/env python3
"""Send a prompt to codex app-server and log all JSON-RPC messages to a trace file.

Usage:
    python tools/trace_appserver.py "your prompt here"
    python tools/trace_appserver.py -m gpt-5.4-mini "your prompt"
    python tools/trace_appserver.py -t h "think hard about this"

Trace is written to tools/trace.jsonl (one JSON object per line).
"""
import asyncio,json,os,shlex,sys,time

def _codex_cmd():
    raw = os.environ.get("IPYAI_CODEX_CMD", "codex")
    return [*shlex.split(raw), "app-server", "--listen", "stdio://"]

TRACE_PATH = os.path.join(os.path.dirname(__file__), "trace.jsonl")

async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("prompt")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("-t", "--think", default="m", help="Effort level: l/m/h")
    p.add_argument("-o", "--output", default=TRACE_PATH)
    args = p.parse_args()

    effort_map = dict(l="low", m="medium", h="high")
    effort = effort_map.get(args.think, args.think)

    proc = await asyncio.create_subprocess_exec(
        *_codex_cmd(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    req_id = 0
    pending = {}
    events = asyncio.Queue()
    trace = open(args.output, "w")

    def log(direction, msg):
        trace.write(json.dumps(dict(dir=direction, ts=time.time(), msg=msg), ensure_ascii=False) + "\n")
        trace.flush()

    async def reader():
        while proc.stdout:
            raw = await proc.stdout.readline()
            if not raw: break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line: continue
            try: msg = json.loads(line)
            except Exception: continue
            log("<<<", msg)
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                fut = pending.pop(msg["id"], None)
                if fut and not fut.done(): fut.set_result(msg)
            else:
                await events.put(msg)

    read_task = asyncio.create_task(reader())

    async def send(msg):
        log(">>>", msg)
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def request(method, params):
        nonlocal req_id
        req_id += 1
        rid = str(req_id)
        fut = asyncio.get_running_loop().create_future()
        pending[rid] = fut
        await send(dict(id=rid, method=method, params=params))
        resp = await fut
        if "error" in resp: raise RuntimeError(resp["error"])
        return resp.get("result")

    async def notify(method, params=None):
        msg = dict(method=method)
        if params is not None: msg["params"] = params
        await send(msg)

    async def respond(rid, result):
        await send(dict(id=rid, result=result))

    # Initialize
    await request("initialize", dict(
        clientInfo=dict(name="trace", title="trace", version="0"),
        capabilities=dict(experimentalApi=True)))
    await notify("initialized")

    # Start thread
    thread_params = dict(cwd=os.getcwd(), approvalPolicy="never", sandbox="workspace-write", ephemeral=True, personality="pragmatic")
    if args.model: thread_params["model"] = args.model
    thread_resp = await request("thread/start", thread_params)
    thread_id = thread_resp["thread"]["id"]
    print(f"thread: {thread_id}, model: {thread_resp.get('model')}, effort: {thread_resp.get('reasoningEffort')}")

    # Start turn
    turn_params = dict(
        threadId=thread_id,
        input=[dict(type="text", text=args.prompt, text_elements=[])],
        cwd=os.getcwd(), approvalPolicy="never", personality="pragmatic")
    if effort: turn_params["effort"] = effort
    turn_params["summary"] = "detailed"
    turn_resp = await request("turn/start", turn_params)
    turn_id = turn_resp["turn"]["id"]

    # Consume events
    while True:
        msg = await events.get()
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # Auto-approve requests
        if "id" in msg and method:
            if method == "item/commandExecution/requestApproval":
                await respond(msg["id"], dict(decision="accept"))
            elif method == "item/fileChange/requestApproval":
                await respond(msg["id"], dict(decision="accept"))
            elif method == "item/permissions/requestApproval":
                await respond(msg["id"], dict(permissions={}, scope="turn"))
            elif method == "item/tool/requestUserInput":
                await respond(msg["id"], dict(answers={}))
            else:
                await respond(msg["id"], {})
            continue

        # Print events
        if method == "item/started":
            item = params.get("item", {})
            print(f"\n[item/started] type={item.get('type')} id={item.get('id')}")
        elif method == "item/reasoning/textDelta":
            print(params.get("delta", ""), end="", flush=True)
        elif method == "item/reasoning/summaryTextDelta":
            print(params.get("delta", ""), end="", flush=True)
        elif method == "item/reasoning/summaryPartAdded":
            print(f"\n  [summaryPart {params.get('summaryIndex')}] ", end="", flush=True)
        elif method == "item/agentMessage/delta":
            print(params.get("delta", ""), end="", flush=True)
        elif method == "item/commandExecution/outputDelta":
            print(params.get("delta", ""), end="", flush=True)
        elif method == "item/completed":
            item = params.get("item", {})
            print(f"\n[item/completed] type={item.get('type')}")
        elif method == "turn/completed":
            print("\n--- turn completed ---")
            break
        elif method == "error":
            print(f"\n[error] {params}")
            break
        else:
            print(f"\n[{method}] {json.dumps(params)[:200]}")

    trace.close()
    print(f"Trace: {args.output}")
    proc.kill()

if __name__ == "__main__":
    asyncio.run(main())
