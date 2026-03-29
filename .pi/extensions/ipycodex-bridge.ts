import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as net from "net";
import * as fs from "fs";

const DEBUG = process.env.IPYCODEX_DEBUG === "1";

const LOG_FILE = process.env.IPYCODEX_LOG || `/tmp/ipycodex-bridge-${process.pid}.log`;

function log(message: string) {
  const timestamp = new Date().toISOString();
  fs.appendFileSync(LOG_FILE, `[${timestamp}] ${message}\n`);
}

let socket: net.Socket | null = null;
const pendingRequests = new Map<
  string,
  { resolve: (value: any) => void; reject: (error: Error) => void }
>();

function connectToSocket(pi: ExtensionAPI, socketPath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    socket = net.createConnection(socketPath, () => {
      log(`Connected to ${socketPath}`);
      resolve();
    });

    socket.on("data", (data) => {
      const lines = data.toString().split("\n").filter((l) => l.trim());
      for (const line of lines) {
        try {
          const msg = JSON.parse(line);
          if (msg.method === "tool_result") {
            log(`Tool result received: ${msg.request_id} (success: ${msg.success})`);
            const pending = pendingRequests.get(msg.request_id);
            if (!pending) continue;
            pendingRequests.delete(msg.request_id);
            msg.success
              ? pending.resolve(msg.result)
              : pending.reject(new Error(msg.result || "Tool failed"));
          } else if (msg.method === "register_tools") {
            const toolCount = msg.tools?.length || 0;
            log(`Registering ${toolCount} tools from message`);
            registerToolsFromMessage(pi, msg);
          }
        } catch {
          log(`Bad JSON: ${line}`);
        }
      }
    });

    socket.on("error", (err) => {
      log(`Socket error: ${err.message}`);
    });

    socket.on("close", () => {
      log("Socket closed");
      socket = null;
    });
  });
}

async function callPythonTool(name: string, args: any): Promise<any> {
  if (!socket) throw new Error("Not connected to ipycodex");

  const requestId = Math.random().toString(36).slice(2);
  log(`Tool call: ${name} (request_id: ${requestId})`);

  return new Promise((resolve, reject) => {
    pendingRequests.set(requestId, { resolve, reject });

    socket!.write(
      JSON.stringify({
        method: "tool_call",
        request_id: requestId,
        name,
        args: args || {},
      }) + "\n"
    );

    setTimeout(() => {
      if (!pendingRequests.has(requestId)) return;
      pendingRequests.delete(requestId);
      reject(new Error("Tool execution timeout"));
    }, 30000);
  });
}

function registerToolsFromMessage(pi: ExtensionAPI, msg: any) {
  const tools = msg.tools || [];
  for (const tool of tools) {
    // Expect Codex-style: {"type": "function", "function": {...}}
    const fn = tool.function;
    if (!fn?.name) continue;

    pi.registerTool({
      name: fn.name,
      label: fn.name,
      description: fn.description || `Call ${fn.name}`,
      parameters: Type.Unsafe(fn.parameters || { type: "object" }),
      async execute(_toolCallId, params) {
        const result = await callPythonTool(fn.name, params);
        return {
          content: [{ type: "text", text: String(result) }],
          details: { result },
        };
      },
    });

    log(`Registered: ${fn.name}`);
  }
}

export default function (pi: ExtensionAPI) {
  const socketPath = process.env.IPYCODEX_SOCK;
  if (!socketPath) {
    log("Error: IPYCODEX_SOCK not set");
    return;
  }

  pi.on("session_start", async () => {
    log("Session started, connecting to socket...");
    await connectToSocket(pi, socketPath);
  });

  pi.on("session_shutdown", async () => {
    log("Session shutdown, cleaning up...");
    if (socket) {
      socket.destroy();
      socket = null;
    }
    // Clean up pending requests
    for (const [id, pending] of pendingRequests) {
      pending.reject(new Error("Session shutdown"));
    }
    pendingRequests.clear();
    log("Cleanup complete");
  });
}
