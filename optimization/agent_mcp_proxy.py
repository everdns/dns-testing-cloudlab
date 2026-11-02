#!/usr/bin/env python3
"""Stdio MCP server that exposes agent_search.py's tools to Claude Code.

Claude Code starts this process (agent_runner.ClaudeCodeRunner passes it in
--mcp-config). It holds no search state. Every tools/list and tools/call is
forwarded over the Unix socket given as the only argument to the ToolRelay in
the agent_search.py process, which does the work.

Only the part of MCP that Claude Code needs for tools is implemented:
newline-delimited JSON-RPC 2.0 on stdin/stdout with initialize, ping,
tools/list and tools/call. Standard library only.

    python3 agent_mcp_proxy.py /path/to/tools.sock
"""
import json
import socket
import sys
import threading

DEFAULT_PROTOCOL = "2025-06-18"

_write_lock = threading.Lock()


def send(message):
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def relay(socket_path, request):
    """Send one request to the ToolRelay and return its JSON reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        sock.sendall((json.dumps(request) + "\n").encode())
        with sock.makefile("r") as reader:
            return json.loads(reader.readline())


def handle(socket_path, message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "dns-config-search", "version": "1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = relay(socket_path, {"op": "list"})["tools"]
        return {"tools": [{"name": t["name"], "description": t["description"],
                           "inputSchema": t["input_schema"]} for t in tools]}
    if method == "tools/call":
        reply = relay(socket_path, {"op": "call", "name": params.get("name"),
                                    "arguments": params.get("arguments") or {}})
        return {"content": [{"type": "text", "text": reply["text"]}],
                "isError": bool(reply["is_error"])}
    raise LookupError(method)


def serve(socket_path, message):
    msg_id = message.get("id")
    try:
        result = handle(socket_path, message)
    except LookupError:
        if msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"method not found: "
                                                       f"{message.get('method')}"}})
        return
    except (OSError, ValueError, KeyError) as e:
        if msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32603, "message": f"tool relay failed: {e}"}})
        return
    if msg_id is not None:
        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def main():
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    socket_path = sys.argv[1]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "parse error"}})
            continue
        if "method" not in message:
            continue  # a response to a request we never send
        # A measurement takes minutes; answer pings and other calls meanwhile.
        threading.Thread(target=serve, args=(socket_path, message),
                         daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
