import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as net from "net";

let socket: net.Socket | null = null;
const pendingRequests = new Map<
  string,
  { resolve: (value: any) => void; reject: (error: Error) => void }
>();

async function connectToSocket(pi: ExtensionAPI, socketPath: string): Promise<void> {
  const MAX_RETRIES = 10;
  const RETRY_DELAY = 500;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      await new Promise<void>((resolve, reject) => {
        socket = net.createConnection(socketPath, () => {
          resolve();
        });

        socket.on("data", (data) => {
          const lines = data.toString().split("\n").filter((l) => l.trim());
          for (const line of lines) {
            try {
              const msg = JSON.parse(line);
              if (msg.method === "tool_result") {
                const pending = pendingRequests.get(msg.request_id);
                if (!pending) continue;
                pendingRequests.delete(msg.request_id);
                msg.success
                  ? pending.resolve(msg.result)
                  : pending.reject(new Error(msg.result || "Tool failed"));
              } else if (msg.method === "register_tools") {
                registerToolsFromMessage(pi, msg);
              }
            } catch {}
          }
        });

        socket.on("error", (err) => {
          reject(err);
        });

        socket.on("close", () => {
          socket = null;
        });
      });
      return;
    } catch {
      if (attempt < MAX_RETRIES) {
        await new Promise((r) => setTimeout(r, RETRY_DELAY));
      }
    }
  }

  throw new Error(`Failed to connect to ${socketPath} after ${MAX_RETRIES} attempts`);
}

async function callPythonTool(name: string, args: any): Promise<any> {
  if (!socket) throw new Error("Not connected to ipycodex");

  const requestId = Math.random().toString(36).slice(2);

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
  }
}

export default function (pi: ExtensionAPI) {
  const socketPath = process.env.IPYCODEX_SOCK;
  if (!socketPath) return;

  pi.on("session_start", async () => {
    await connectToSocket(pi, socketPath);
  });

  pi.on("session_shutdown", async () => {
    if (socket) {
      socket.destroy();
      socket = null;
    }
    for (const [, pending] of pendingRequests) {
      pending.reject(new Error("Session shutdown"));
    }
    pendingRequests.clear();
  });
}
