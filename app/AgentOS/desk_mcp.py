"""Agent OS approvals: the --permission-prompt-tool of the research agent (see ask_researcher in agent_os.py).

When the agent wants a browser action outside its read-only set (click, type, ...), Claude Code calls `approve` here.
This asks Ben in HyperDesk and waits for his answer. HyperDesk closed, no answer within 5 minutes, or any error: the
action is denied and the agent carries on read-only. MCP over stdio (one JSON-RPC message per line), stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
STATE = Path(__file__).resolve().parent / "state" / "hyperdesk.json"
WAIT = 330  # seconds; HyperDesk itself gives up after 300 and answers "deny"
TOOL = {"name": "approve", "description": "Ask the user to approve one tool call the agent wants to make.",
        "inputSchema": {"type": "object", "required": ["tool_name", "input"],
                        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                       "tool_use_id": {"type": "string"}}}}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def refuse_url(url) -> str:
    """Why the agent may not open this address, or '' if it may: http or https to the public internet only.
    This is what stops a page from making the agent 'navigate' to javascript:... (code without approval)."""
    text = str(url or "").strip()
    # Browsers and Node (WebFetch) read "\" as "/" and drop tabs and newlines, so "https://127.0.0.1\@example.com/"
    # is example.com to Python but this PC to them (found by the 2026-10-05 attack run). Refuse anything they could
    # read differently, and any user@ part.
    if "\\" in text or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text):
        return "That address has characters browsers read differently (backslash, spaces or control characters)."
    try:
        parts = urllib.parse.urlsplit(text)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return "That address does not parse."
    if parts.scheme.lower() not in ("http", "https"):
        return f"Only http:// and https:// addresses are allowed (not {parts.scheme or 'a bare name'}:). Use a full https:// address."
    if "@" in parts.netloc:
        return "Addresses with a user name (user@host) are not allowed."
    from agent_os import public_address
    if not public_address(host, port or (443 if parts.scheme.lower() == "https" else 80)):
        return "That address is on this PC or the local network (or does not resolve): off limits."
    return ""


def decide(args: dict) -> dict:
    """Claude Code's permission answer: allow (with the input unchanged) or deny with a reason the agent reads.
    Opening an address is decided here by the address alone; acting on a page asks Ben."""
    tool, given = str(args.get("tool_name", "")), args.get("input") or {}
    if not isinstance(given, dict):
        return {"behavior": "deny", "message": "Malformed request."}
    if tool in ("WebFetch", "mcp__browser__browser_navigate", "mcp__browser__browser_tabs"):
        if tool == "mcp__browser__browser_tabs" and not given.get("url"):  # list, select, close, or a blank tab
            return {"behavior": "allow", "updatedInput": given}
        reason = refuse_url(given.get("url"))
        return {"behavior": "deny", "message": reason} if reason else {"behavior": "allow", "updatedInput": given}
    try:
        desk = json.loads(STATE.read_text(encoding="utf-8"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(desk['port'])}/api/approval",
            data=json.dumps({"task": os.environ.get("AGENT_OS_TASK", ""), "tool": tool, "input": given}).encode(),
            headers={"Content-Type": "application/json", "X-Token": desk["token"]})
        with _NO_PROXY.open(request, timeout=WAIT) as r:
            allow = json.load(r).get("allow") is True
    except (OSError, ValueError, KeyError, TypeError):
        return {"behavior": "deny", "message": "Nobody could approve this: HyperDesk is not open. Carry on read-only."}
    if allow:
        return {"behavior": "allow", "updatedInput": given}
    return {"behavior": "deny", "message": "The user said no (or did not answer in time). Carry on read-only, or "
                                           "answer with what you have."}


def reply(msg: dict) -> dict | None:
    """The JSON-RPC response to one message, or None for a notification."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-os-desk", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call" and (msg.get("params") or {}).get("name") == "approve":
        answer = decide((msg["params"].get("arguments")) or {})
        result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # Claude Code speaks UTF-8; Windows would read the pipe as cp1252
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            out = reply(msg)
        except Exception as e:  # never leave Claude Code waiting: a request that breaks us is answered, and denied
            out = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                   "result": {"content": [{"type": "text", "text": json.dumps(
                       {"behavior": "deny", "message": f"Refused: the request could not be checked ({type(e).__name__})."})}]}}
            if out["id"] is None:
                continue
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
