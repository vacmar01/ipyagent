"""pi backend client for ipyagent - RPC communication with pi coding agent"""

import asyncio
import json
import os
from pathlib import Path


def _blockquote(text):
    return (
        "".join(f"> {line}\n" if line.strip() else ">\n" for line in text.splitlines())
        if text
        else ""
    )


def _history_xml(hist):
    """Convert history list to XML format for conversation context"""
    if not hist:
        return ""
    parts = ["<conversation-history>"]
    for i in range(0, len(hist), 2):
        user = hist[i]
        assistant = hist[i + 1] if i + 1 < len(hist) else ""
        parts.append("<turn>")
        parts.append(f"<user>{user}</user>")
        if assistant:
            parts.append(f"<assistant>{assistant}</assistant>")
        parts.append("</turn>")
    parts.append("</conversation-history>\n")
    return "".join(parts)


def _compact_call(name, args=None, result="", exit_code=None):
    """Format a tool/function call in compact form.

    Args:
        name: Tool/command name
        args: Dict of arguments (optional)
        result: Output/result text
        exit_code: Exit code (shown if non-zero)
    """
    # Format call signature with args if present
    if args is not None:
        call = f"{name}({', '.join(f'{k}={v!r}' for k, v in sorted(args.items()))})"
    else:
        call = f"{name}()"

    # Add exit code indicator if non-zero
    status = "" if exit_code in (None, 0) else f" [exit {exit_code}]"

    # Truncate result
    res = (result or "").strip().replace("\n", " ")
    if len(res) > 80:
        res = res[:77] + "..."

    return f"\n\n🔧 {call}{status} => {res}\n" if res else f"\n\n🔧 {call}{status}\n"


def _fenced_block(text, info=""):
    """Create a fenced code block with appropriate delimiters."""
    text = text or ""
    fence = "~" * 3
    while fence in text:
        fence += "~"
    if text and not text.endswith("\n"):
        text += "\n"
    info = info or ""
    return f"{fence}{info}\n{text}{fence}\n"


class PiToolBridge:
    """Unix socket server that dispatches tool calls between pi extension and Python namespace"""

    def __init__(self, user_ns):
        self.user_ns = user_ns
        self.socket_path = None
        self.server = None
        self._writer = None
        self._running = False
        self._tools = {}
        self._ready = asyncio.Event()

    def _get_socket_path(self):
        """Get socket path, preferring $XDG_RUNTIME_DIR with fallback to /tmp"""
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir:
            return Path(runtime_dir) / f"ipyagent-{os.getpid()}.sock"
        return Path(f"/tmp/ipyagent-{os.getpid()}.sock")

    async def start(self):
        """Start the Unix socket server"""
        self.socket_path = self._get_socket_path()

        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        self._running = True

    async def wait_ready(self, timeout=10):
        """Wait until the extension has connected and tools have been sent"""
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def stop(self):
        """Stop the server and cleanup socket"""
        self._running = False
        self._ready.clear()

        # Close connection if exists
        if self._writer:
            self._writer.close()
            self._writer = None

        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

        # Remove socket file
        if self.socket_path and self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path = None

    async def _handle_connection(self, reader, writer):
        """Handle a new connection from the pi extension"""
        self._writer = writer
        self._ready.set()

        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                await self._process_message(line, writer)
        finally:
            self._writer = None
            writer.close()
            await writer.wait_closed()

    async def _process_message(self, data, writer):
        """Process a JSON message from the pi extension"""
        msg = {}
        try:
            msg = json.loads(data.decode("utf-8"))
            method = msg.get("method")

            if method == "tool_call":
                result = await self._handle_tool_call(msg)
                response = {
                    "method": "tool_result",
                    "request_id": msg.get("request_id"),
                    "success": result["success"],
                    "result": result.get("result"),
                }
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()

        except Exception as e:
            response = {
                "method": "tool_result",
                "request_id": msg.get("request_id"),
                "success": False,
                "result": str(e),
            }
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()

    async def _handle_tool_call(self, msg):
        """Execute a tool call in the Python namespace"""
        name = msg.get("name")
        args = msg.get("args", {})

        fn = self.user_ns.get(name)
        if not callable(fn):
            return {
                "success": False,
                "result": f"Tool {name!r} not found or not callable",
            }

        try:
            result = fn(**args)
            if asyncio.iscoroutine(result):
                result = await result
            return {
                "success": True,
                "result": result if isinstance(result, str) else json.dumps(result),
            }
        except Exception as e:
            return {"success": False, "result": str(e)}

    async def register_tools(self, tools):
        """Register tools and send them to the connected client"""
        for tool in tools:
            name = tool["function"]["name"]
            self._tools[name] = tool

        await self._send_tools(tools)

    async def _send_tools(self, tools):
        """Send register_tools message to the connected client"""
        if not tools or not self._writer:
            return

        msg = {"method": "register_tools", "tools": tools}
        data = json.dumps(msg).encode("utf-8") + b"\n"
        self._writer.write(data)
        await self._writer.drain()


class PiClient:
    """Manages pi subprocess for RPC communication"""

    def __init__(self, provider, model, system_prompt="", user_ns=None):
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.user_ns = user_ns or {}
        self.proc = None
        self.bridge = None

    async def start(self):
        """Start the bridge server and spawn pi subprocess"""
        self.bridge = PiToolBridge(user_ns=self.user_ns)
        await self.bridge.start()

        cmd = [
            "pi",
            "--mode",
            "rpc",
            "--no-session",
            "--tools",
            "bash",
            "--system-prompt",
            self.system_prompt,
            "--provider",
            self.provider,
            "--model",
            self.model,
        ]

        env = os.environ.copy()
        env["IPYAGENT_SOCK"] = str(self.bridge.socket_path)

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=env,
            cwd=str(Path(__file__).parent.parent),
        )

    async def stop(self):
        """Stop the pi subprocess and cleanup bridge"""
        if self.bridge:
            await self.bridge.stop()
            self.bridge = None

        # Terminate the subprocess
        if self.proc:
            if self.proc.returncode is None:  # Still running
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.proc.kill()  # Force kill if graceful shutdown fails
                    await self.proc.wait()
            self.proc = None


class PiChat:
    """Drop-in replacement for AsyncChat using pi backend"""

    def __init__(
        self, model=None, sp="", ns=None, hist=None, tools=None, provider=None
    ):
        self.model = model
        self.sp = sp
        self.ns = ns or {}
        self.hist = hist or []
        self.tools = tools or []
        self.provider = provider
        self.client = None

    async def __call__(self, prompt, think=None):
        """Send a prompt to pi and return response stream"""
        # Create client if not exists
        if self.client is None:
            model = self.model
            if think and think != "off":
                model = f"{self.model}:{think}"
            self.client = PiClient(
                provider=self.provider,
                model=model,
                system_prompt=self.sp,
                user_ns=self.ns,
            )
            await self.client.start()

        # Wait for extension to connect first, then register tools
        if self.client.bridge:
            await self.client.bridge.wait_ready()
            if self.tools:
                await self.client.bridge.register_tools(self.tools)

        # Build prompt with history XML prepended
        full_prompt = _history_xml(self.hist) + prompt

        # Return stream
        return self._stream(full_prompt)

    async def stop(self):
        """Stop the pi client and cleanup resources"""
        if self.client:
            await self.client.stop()
            self.client = None

    async def _stream(self, prompt):
        """Stream response from pi via JSONL RPC"""

        if self.client is None or self.client.proc is None:
            raise RuntimeError("Client not started")
        if self.client.proc.stdin is None or self.client.proc.stdout is None:
            raise RuntimeError("Client process pipes not available")

        # Send the prompt to pi via stdin
        request = {"type": "prompt", "message": prompt}
        self.client.proc.stdin.write(json.dumps(request).encode() + b"\n")
        await self.client.proc.stdin.drain()

        # Read streaming events from stdout
        tool_args = {}  # cache args by tool_call_id from start events
        async for line in self._iter_jsonl(self.client.proc.stdout):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Prompt command failed
            if (
                event.get("type") == "response"
                and event.get("command") == "prompt"
                and not event.get("success", True)
            ):
                err = event.get("error", "prompt failed")
                yield f"<system>{err}</system>"
                break

            # Check for agent completion
            if event.get("type") == "agent_end":
                break

            # Map assistant deltas to formatter-friendly events
            if event.get("type") == "message_update":
                # Handle nested format: {"type": "message_update", "assistantMessageEvent": {...}}
                if "assistantMessageEvent" in event:
                    delta = event.get("assistantMessageEvent", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        yield {
                            "type": "message_update",
                            "text_delta": delta.get("delta", ""),
                        }
                    elif dtype == "thinking_start":
                        yield {"type": "message_update", "thinking_start": True}
                    elif dtype == "thinking_delta":
                        yield {
                            "type": "message_update",
                            "thinking_delta": delta.get("delta", ""),
                        }
                    elif dtype == "thinking_end":
                        # Some pi builds provide final reasoning text on thinking_end.
                        # Preserve it by emitting a delta before the end marker.
                        end_content = delta.get("content") or ""
                        if end_content:
                            yield {
                                "type": "message_update",
                                "thinking_delta": end_content,
                            }
                        yield {"type": "message_update", "thinking_end": True}
                continue

            # Tool execution events (from pi RPC protocol)
            # See: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md
            etype = event.get("type")
            if etype in (
                "tool_execution_start",
                "tool_execution_update",
                "tool_execution_end",
            ):
                tool_call_id = event.get("toolCallId", "tool")
                tool_name = event.get("toolName", "tool")
                args = event.get("args", {})

                if etype == "tool_execution_start":
                    # Cache args for tool_execution_end (which lacks args per RPC spec)
                    tool_args[tool_call_id] = args
                    # Command starts executing
                    yield {
                        "type": "command_start",
                        "id": tool_call_id,
                        "command": args.get("command", tool_name),
                        "cwd": None,
                    }
                elif etype == "tool_execution_update":
                    # Streaming output
                    partial = event.get("partialResult", {})
                    content = partial.get("content", [])
                    output = ""
                    for item in content:
                        if item.get("type") == "text":
                            output += item.get("text", "")
                    yield {
                        "type": "command_delta",
                        "id": tool_call_id,
                        "delta": output,
                        "command": args.get("command", tool_name),
                        "cwd": None,
                    }
                elif etype == "tool_execution_end":
                    # Command completed - use cached args since end event lacks them
                    args = tool_args.pop(tool_call_id, args)
                    result = event.get("result", {})
                    content = result.get("content", [])
                    output = ""
                    for item in content:
                        if item.get("type") == "text":
                            output += item.get("text", "")
                    is_error = event.get("isError", False)
                    exit_code = 1 if is_error else 0
                    text = _compact_call(tool_name, args, output, exit_code)
                    yield {
                        "type": "command_complete",
                        "id": tool_call_id,
                        "text": text,
                    }
                continue

    async def _iter_jsonl(self, stream, chunk_size=65536):
        """Yield JSONL lines from a stream without readline() size limits."""
        buf = b""
        while True:
            chunk = await stream.read(chunk_size)

            if not chunk:
                if buf.strip():
                    line = buf[:-1] if buf.endswith(b"\r") else buf
                    yield line.decode("utf-8", errors="replace")
                break

            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1 :]
                if not line:
                    continue
                if line.endswith(b"\r"):
                    line = line[:-1]
                yield line.decode("utf-8", errors="replace")


class PiStreamFormatter:
    """Formats pi RPC events into display_text and final_text"""

    def __init__(self):
        self.is_tty = False
        self.final_text = ""
        self.display_text = ""
        self._thinking_text = ""
        self._in_thinking = False
        self._live_commands = {}

    def _live_command_text(self, state):
        """Format live command state for display."""
        cmd = state.get("command", "command")
        text = f"⌛ {cmd}"
        if output := state.get("output"):
            text += "\n\n" + _fenced_block(output, "text")
        return text.rstrip()

    def _update_display(self):
        """Update display_text from internal state"""
        parts = []
        if self._thinking_text:
            parts.append(_blockquote(self._thinking_text))
        if self._live_commands:
            live = "\n\n".join(
                self._live_command_text(o) for o in self._live_commands.values()
            )
            parts.append(live)
        if self.final_text:
            parts.append(self.final_text)
        self.display_text = (
            "\n\n".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        )

    def _format_event(self, event):
        """Format a single pi event into output"""
        if isinstance(event, str):
            self.final_text += event
            self._update_display()
            return event

        if not isinstance(event, dict):
            return ""

        event_type = event.get("type")

        if event_type == "message_update":
            if event.get("thinking_start"):
                self._in_thinking = True
                self._thinking_text = ""
                return ""

            if event.get("thinking_delta"):
                self._thinking_text += event.get("thinking_delta", "")
                self._update_display()
                return ""

            if event.get("thinking_end"):
                self._in_thinking = False
                if self._thinking_text:
                    stored = f"<thinking>\n{self._thinking_text}\n</thinking>\n\n"
                    self._thinking_text = ""
                    self.final_text += stored
                    self._update_display()
                    return "" if self.is_tty else stored
                return ""

            # Regular text delta
            if event.get("text_delta"):
                text = event.get("text_delta", "")
                self.final_text += text
                self._update_display()
                return text

        # Command execution events
        if event_type == "command_start":
            self._live_commands[event.get("id")] = dict(
                command=event.get("command"), cwd=event.get("cwd"), output=""
            )
            self._update_display()
            return ""

        if event_type == "command_delta":
            state = self._live_commands.setdefault(
                event.get("id"),
                dict(command=event.get("command"), cwd=event.get("cwd"), output=""),
            )
            if event.get("command") and not state.get("command"):
                state["command"] = event["command"]
            state["output"] += event.get("delta", "")
            self._update_display()
            return ""

        if event_type == "command_complete":
            self._live_commands.pop(event.get("id"), None)
            text = event.get("text", "")
            if text:
                self.final_text += text
                self._update_display()
            return "" if self.is_tty else text

        return ""

    async def format_stream(self, stream):
        """Format a stream of pi events"""
        async for event in stream:
            yield self._format_event(event)
