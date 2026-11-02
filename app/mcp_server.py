"""Khoj.ai as an MCP server (Streamable HTTP transport, stateless).

Users point their own agent (Claude Desktop, Claude Code, Cursor, any MCP
client) at POST /mcp with their Firebase ID token as a Bearer header; the
agent can then search the documents THAT user uploaded - the uid from the
verified token is applied as a server-side filter, never taken from tool
arguments, so one user can never query another's corpus.

Implemented directly as JSON-RPC 2.0 (initialize / ping / tools/list /
tools/call) - for a two-tool surface this stays dependency-free and fully
auditable. Not a general MCP framework.
"""

import json

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "khoj-ai", "version": "0.2.0"}

TOOLS = [
    {
        "name": "search_documents",
        "description": (
            "Search the signed-in user's own uploaded documents in Khoj.ai "
            "and return a summary plus the most relevant passages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "pageSize": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_my_documents",
        "description": "List the documents the signed-in user has uploaded to Khoj.ai.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _format_search(data: dict) -> str:
    lines = []
    if data.get("summary"):
        lines.append(data["summary"])
        lines.append("")
    for r in data.get("results", []):
        lines.append(f"## {r.get('title') or 'Document'}")
        if r.get("link"):
            lines.append(r["link"])
        for s in r.get("extractive", []):
            lines.append(s)
        for s in r.get("snippets", []):
            lines.append(s)
        lines.append("")
    return "\n".join(lines).strip() or "No results."


def handle_rpc(body, user: dict, run_search, list_documents):
    """Handle one JSON-RPC message. Returns a response dict, or None for
    notifications (which get no reply)."""
    if not isinstance(body, dict) or "method" not in body:
        return _error(None, -32600, "Invalid request")
    method = body["method"]
    req_id = body.get("id")
    params = body.get("params") or {}

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Khoj.ai searches the documents you uploaded. Use "
                "search_documents for questions about them."
            ),
        })
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_documents":
                query = (args.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                data = run_search(query, user, int(args.get("pageSize", 10)))
                return _result(req_id, {
                    "content": [{"type": "text", "text": _format_search(data)}],
                    "structuredContent": data,
                    "isError": False,
                })
            if name == "list_my_documents":
                docs = list_documents(user)
                text = "\n".join(f"- {d['title']}" for d in docs) or "No documents uploaded yet."
                return _result(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"documents": docs},
                    "isError": False,
                })
            return _error(req_id, -32602, f"Unknown tool: {name}")
        except Exception as exc:  # tool errors are results, not protocol errors
            return _result(req_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })
    return _error(req_id, -32601, f"Method not found: {method}")


def config_snippet(base_url: str) -> str:
    """Ready-to-paste MCP client config (token placeholder, not a real one)."""
    return json.dumps({
        "mcpServers": {
            "khoj": {
                "url": f"{base_url.rstrip('/')}/mcp",
                "headers": {"Authorization": "Bearer <YOUR_KHOJ_ID_TOKEN>"},
            }
        }
    }, indent=2)
