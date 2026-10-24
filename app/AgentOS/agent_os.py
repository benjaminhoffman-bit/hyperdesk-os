#!/usr/bin/env python3
"""Agent OS: thin controller for HyperDesk.

Stdlib only. Usage: agent-os doctor | install | smoke | context
"""
import sys

if sys.version_info < (3, 12):
    sys.exit("Agent OS needs Python 3.12 or newer.")

import argparse
import collections
import contextlib
import csv
import ctypes
import datetime
import functools
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
# Under pythonw (the home base app, the scheduled update) there is no console, so every console tool we start
# would pop up its own window. Keep them hidden then; with a console they simply share it.
# Ask for the console itself: a pythonw whose stdout is a pipe (improve's test run) has a stdout but no console,
# and the old `sys.stdout is None` test let its git calls flash ~35 Terminal windows (2026-10-11).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" and not ctypes.windll.kernel32.GetConsoleWindow() else 0
# Requests to 127.0.0.1 must never be sent through a system proxy.
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def fresh_path() -> str:
    """PATH as a new terminal would see it. Installers update the registry, not this process."""
    parts = [os.environ.get("PATH", "")]
    if os.name == "nt":
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    parts.append(os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])))
            except OSError:
                pass
    return os.pathsep.join(parts)


def find_tool(name: str) -> str | None:
    """Full path of a tool: PATH first, then uv's tool folder (its shims are not always on PATH)."""
    path = shutil.which(name, path=fresh_path())
    if path or name == "uv":
        return path
    uv = shutil.which("uv", path=fresh_path())
    if not uv:
        return None
    try:
        bin_dir = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, errors="replace",
                                 timeout=20, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None
    return shutil.which(name, path=bin_dir) if bin_dir else None


def kill_tree(pid: int) -> None:
    """End a process and everything it started. Never raises."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


STOP_POLL = 15  # seconds between a long run's stop() checks (each one a tasklist)


def run_tree(args: list[str], timeout: float, input: str | None = None, stop=None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run with captured output that ends the whole tree on a timeout: an npm .cmd shim (opencode, codex)
    starts the real exe as a grandchild that holds the pipes, and subprocess.run kills only cmd.exe, then waits.
    `stop`: asked every 15 s; true ends the run early, as a timeout (a game started under a local model, say)."""
    stdin = subprocess.DEVNULL if input is None else subprocess.PIPE
    end = time.monotonic() + timeout
    with subprocess.Popen(args, stdin=stdin, stdout=subprocess.PIPE, stderr=kw.pop("stderr", subprocess.PIPE),
                          creationflags=NO_WINDOW, **kw) as p:
        try:
            while True:
                try:
                    out, err = p.communicate(input, timeout=max(0.1, min(STOP_POLL, end - time.monotonic())) if stop
                                             else timeout)
                    break
                except subprocess.TimeoutExpired:
                    input = None  # already written: communicate refuses it a second time
                    if not stop or time.monotonic() >= end or stop():
                        raise
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            p.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.communicate(timeout=30)
            raise
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def run(cmd: list[str], timeout: float = 20, cwd: Path | None = None) -> tuple[int, str]:
    """Run a tool without a shell. Returns (exit code, output); 127 if the tool cannot be found."""
    exe = cmd[0] if os.path.isabs(cmd[0]) else find_tool(cmd[0])
    if not exe:
        return 127, ""
    try:
        p = run_tree([exe, *cmd[1:]], timeout, cwd=cwd, text=True, errors="replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        return 124, str(e)
    return p.returncode, (p.stdout.strip() or p.stderr.strip())


def first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def get_json(url: str, timeout: float = 3):
    with _NO_PROXY.open(url, timeout=timeout) as r:
        return json.load(r)


def running_processes() -> list[str]:
    """Lower-case image names of every running process. Uses tasklist, so Windows only."""
    code, out = run(["tasklist", "/fo", "csv", "/nh"])
    return [row[0].lower() for row in csv.reader(io.StringIO(out)) if row] if code == 0 else []


def game_running(cfg: dict, names: list[str] | None = None) -> str:
    """Name of a running game from the guard list, or ''. Pass `names` to reuse one running_processes() reading."""
    names = running_processes() if names is None else names
    return next((g for g in cfg["gpu"]["games"] if any(g.lower() in n for n in names)), "")


# Adapters: one function per tier. Each returns {ok, data, seconds, usage, error} and never raises.

PAID_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


def result(start: float, data=None, usage: dict | None = None, error: str = "", session: str = "") -> dict:
    """session: an agent session a follow-up call can resume ('' if none)."""
    return {"ok": not error, "data": data, "seconds": round(time.monotonic() - start, 1), "usage": usage or {},
            "error": error, "session": session}


def ask_local(cfg: dict, alias: str, prompt: str, schema: dict | None = None, tools: list | None = None,
              keep_alive=None, timeout: float = 120, num_predict: int = 512) -> dict:
    """One chat turn against an Ollama alias. `schema` forces JSON of that shape; `tools` offers tool calls."""
    # num_predict: at temperature 0 a small model can loop forever inside a JSON string (measured on code and fast);
    # the cap turns a 120 s hang into a ~5 s invalid reply. HyperDesk's wrap-up raises it for its longer note.
    body = {"model": alias, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "think": False, "options": {"temperature": 0, "num_predict": num_predict}}
    if schema:
        body["format"] = schema
    if tools:
        body["tools"] = tools
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    start = time.monotonic()
    request = urllib.request.Request(cfg["ollama"]["url"] + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with _NO_PROXY.open(request, timeout=timeout) as r:
            reply = json.load(r)
        message = reply["message"]
        data = message.get("tool_calls") if tools else json.loads(message["content"]) if schema else message["content"]
    except urllib.error.HTTPError as e:
        return result(start, error=f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except (OSError, ValueError, KeyError) as e:
        return result(start, error=repr(e)[:300])
    return result(start, data, {"in": reply.get("prompt_eval_count"), "out": reply.get("eval_count")})


FREE_KEY = ROOT / "state" / "free.key"  # the free tier's API key (OmniRoute's, or a provider's); Ben saves it in HyperDesk


def free_unavailable(cfg: dict) -> str:
    """Why the free cloud tier ([free] in config.toml) cannot take a request now, or ''."""
    free = cfg.get("free") or {}
    if not free.get("url") or not free.get("model"):
        return "no [free] url and model in config.toml"
    if not FREE_KEY.is_file():
        return "no API key saved (Apps & APIs, Free cloud)"
    host = urllib.parse.urlsplit(free["url"])
    if host.hostname in ("127.0.0.1", "localhost") and not port_open(host.port or 80):
        return "OmniRoute is not running"
    return ""


def ask_free(cfg: dict, messages: list[dict], timeout: float = 180, max_tokens: int = 4000) -> dict:
    """One chat completion on the free cloud tier: any OpenAI-compatible endpoint, normally OmniRoute on 127.0.0.1,
    whose combo falls back across free providers (Gemini, Groq, OpenRouter :free, NVIDIA NIM...) on a 429.
    Not counted in the daily cap: it costs nothing. data: the reply text; usage also names the model that answered."""
    free, start = cfg["free"], time.monotonic()
    key = FREE_KEY.read_text(encoding="utf-8").strip()
    body = {"model": free["model"], "messages": messages, "stream": False, "max_tokens": max_tokens}
    request = urllib.request.Request(free["url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with _NO_PROXY.open(request, timeout=timeout) as r:
            reply = json.load(r)
        text = reply["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as e:
        return result(start, error=f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        return result(start, error=repr(e)[:300])
    u = reply.get("usage") or {}
    return result(start, text, {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
                                "model": str(reply.get("model") or free["model"])})


def ask_agent(cmd: list[str], cwd: Path, stdin_text: str = "", timeout: float = 300,
              extra_env: dict | None = None, stop=None) -> tuple[int, str, str]:
    """Run an agent CLI with paid-API keys removed from its environment, so it uses the subscription sign-in.
    `stop()`: a reason to end it early (see run_tree), or ''."""
    exe = find_tool(cmd[0])
    if not exe:
        return 127, "", f"{cmd[0]} is not installed"
    env = {k: v for k, v in os.environ.items() if k not in PAID_KEYS} | (extra_env or {})
    why = []
    try:
        p = run_tree([exe, *cmd[1:]], timeout, stdin_text, stop=stop and (lambda: why.append(stop()) or why[-1]),
                     cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"stopped: {why[-1]}" if why and why[-1] else f"timed out after {timeout:.0f}s"
    except OSError as e:
        return 126, "", str(e)
    return p.returncode, p.stdout, p.stderr


def failure(agent: str, code: int, out: str, err: str) -> str:
    """Full output of a failed agent call goes to logs\\agents\\; the error keeps a short tail and that path."""
    path = ROOT / "logs" / "agents" / f"{time.strftime('%Y%m%d-%H%M%S')}-{agent}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"exit {code}\n--- stderr\n{err}\n--- stdout\n{out}\n", encoding="utf-8")
    return f"exit {code}: {(err or out).strip()[-300:]} (full output: {path})"


def ask_claude(prompt: str, schema: dict, cwd: Path, timeout: float = 300, write: bool = False,
               session: str = "", dirs: list[Path] | tuple = (), deny: list[str] | tuple = ()) -> dict:
    """Headless Claude Code. Read-only, or with write: file edits inside cwd plus the unittest command.
    Anything else that would need a permission prompt is denied. The prompt goes in on stdin (no 32K argv limit).
    Lean: no MCP servers, no skills, only the built-in tools the role needs, and read-only roles run --restricted.
    Measured 2026-10-05: fixed overhead 54.6K -> 7.7K input tokens (write) / 4.9K (read-only).
    A write session is kept, so a repair can --resume it and reuse the cached context.
    `dirs`: more folders its file tools may use (--restricted confines them to cwd plus these); `deny`: permission
    deny rules on top, e.g. for secrets inside those folders."""
    start = time.monotonic()
    # On Windows Claude Code runs commands through its PowerShell tool, elsewhere through Bash: allow the tests in both.
    allowed = ["Read", "Glob", "Grep"] + (["Edit", "Write", "Bash(python -m unittest:*)",
                                           "PowerShell(python -m unittest:*)"] if write else [])
    builtins = "Read,Glob,Grep" + (",Edit,Write,Bash,PowerShell" if write else "")
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--permission-mode", "acceptEdits" if write else "dontAsk", "--permission-prompts", "none",
           "--setting-sources", "user", "--strict-mcp-config", "--disable-slash-commands", "--tools", builtins,
           "--settings", json.dumps({"bashOutputMaxChars": 8000, **({"permissions": {"deny": [*deny]}} if deny else {})}),
           "--allowedTools", *allowed, *(["--add-dir", *map(str, dirs)] if dirs else []), "--max-turns", "40" if write else "10"]
    cmd += (["--resume", session] if session else []) + ([] if write else ["--restricted", "--no-session-persistence"])
    code, out, err = ask_agent(cmd, cwd, stdin_text=prompt, timeout=timeout)
    try:
        reply = json.loads(out)
        data = reply.get("structured_output")
    except (ValueError, AttributeError):
        return result(start, error=failure("claude", code, out, err))
    usage = reply.get("usage") or {}
    tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    # cost_est_usd is Claude Code's own estimate at API prices; on a subscription it is usage, not a charge.
    usage = {"in": tokens_in, "out": usage.get("output_tokens"), "cost_est_usd": reply.get("total_cost_usd")}
    if code != 0 or reply.get("is_error") or not isinstance(data, dict):
        return result(start, data, usage, error=failure("claude", code, out, f"{err}\n{reply.get('result') or ''}"))
    return result(start, data, usage, session=(reply.get("session_id") or "") if write else "")


# Codex features that add tool definitions a headless coding call never uses. Measured 2026-10-05: switching them
# off cut the fixed overhead from 19.9K to 12.4K input tokens. Set with -c, which ignores names a newer Codex
# dropped; --disable would refuse to start on an unknown name.
CODEX_OFF = ("apps", "browser_use", "browser_use_external", "computer_use", "image_generation", "plugins",
             "multi_agent", "goals", "skill_search", "tool_suggest", "in_app_browser")


@functools.lru_cache(maxsize=1)
def codex_mcp_off() -> tuple[str, ...]:
    """-c overrides that switch off every enabled MCP server Codex lets us switch off, for headless calls (Codex has
    no --strict-mcp-config). Each override is tried once, because Codex refuses to start on one that names a server
    missing from config.toml, and its bundled servers (cua_repl, node_repl) are listed but not in that file."""
    try:
        names = [s["name"] for s in json.loads(run(["codex", "mcp", "list", "--json"], timeout=60)[1]) if s.get("enabled")]
    except (ValueError, TypeError, KeyError):
        return ()
    flags = [f"mcp_servers.{n}.enabled=false" for n in names]
    return tuple(x for f in flags if run(["codex", "-c", f, "mcp", "list", "--json"], timeout=60)[0] == 0
                 for x in ("-c", f))


def ask_codex(prompt: str, schema: dict, cwd: Path, timeout: float = 300, write: bool = False,
              session: str = "") -> dict:
    """Headless Codex in its read-only sandbox, or workspace-write (writes inside cwd only, no network).
    The prompt goes in on stdin, the answer comes back in a file. A write session is kept for a repair to resume."""
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as d:
        schema_file, answer_file = Path(d) / "schema.json", Path(d) / "answer.json"
        schema_file.write_text(json.dumps(schema), encoding="utf-8")
        # sandbox_mode via -c: `exec resume` has no --sandbox flag
        cmd = ["codex", "exec", *(["resume", session] if session else []), "--json", "--output-schema",
               str(schema_file), "-o", str(answer_file), "--skip-git-repo-check",
               "-c", f'sandbox_mode="{"workspace-write" if write else "read-only"}"',
               "-c", "tool_output_token_limit=4000", *[x for f in CODEX_OFF for x in ("-c", f"features.{f}=false")],
               *codex_mcp_off()]
        cmd += [] if write and not session else ["--ephemeral"]
        if os.name == "nt":
            cmd += ["-c", "windows.sandbox=unelevated"]  # never the admin-level sandbox setup
        code, out, err = ask_agent([*cmd, "-"], cwd, stdin_text=prompt, timeout=timeout)
        try:
            data = json.loads(answer_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    usage, thread = {}, ""
    for line in out.splitlines():
        with contextlib.suppress(ValueError, AttributeError):
            event = json.loads(line)
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            elif event.get("type") == "thread.started":
                thread = event.get("thread_id") or ""
    usage = {"in": usage.get("input_tokens"), "out": usage.get("output_tokens")}
    if code != 0 or not isinstance(data, dict):
        return result(start, data, usage, error=failure("codex", code, out, err))
    return result(start, data, usage, session=thread if write and not session else session)


OPENCODE_CONFIG = ROOT / "opencode.json"  # models (OmniRoute's free combo, Ollama) and permissions: edits, unittest, git
# run_task's step between the free tier and Claude: opencode on this PC's qwen-agent (opencode.json: ~11 GB VRAM)
# ponytail: one VRAM need for every ollama/ opencode model; per model if another one is added
LOCAL_AGENT, LOCAL_AGENT_MIB = "local:qwen-agent", 11500


def last_json(text: str) -> dict | None:
    """The last JSON object in a reply that also holds prose and code: tried from the last '{' backwards."""
    end = text.rfind("}")
    for i in reversed([m.start() for m in re.finditer(r"\{", text[:end])]):
        with contextlib.suppress(ValueError):
            if isinstance(data := json.loads(text[i:end + 1]), dict):
                return data
    return None


def ollama_unload(cfg: dict, name: str) -> None:
    """Free the VRAM one Ollama model holds (keep_alive 0, nothing generated). Only a loaded one: a request for a
    model that is not in memory would load it first (11.5 GB for qwen-agent, maybe as a game starts). Never raises."""
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        if not {name, f"{name}:latest"} & {m.get("name") for m in get_json(cfg["ollama"]["url"] + "/api/ps")["models"]}:
            return
        request = urllib.request.Request(cfg["ollama"]["url"] + "/api/generate", data=json.dumps(
            {"model": name, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"})
        with _NO_PROXY.open(request, timeout=60) as r:
            r.read()


def ask_opencode(cfg: dict, prompt: str, cwd: Path, timeout: float = 1200, session: str = "", model: str = "") -> dict:
    """Headless opencode as implementer, on the free cloud tier ([agents] opencode_model, normally omniroute/free)
    or `model` (ollama/... runs on this PC): no cap, no subscription. Its permissions come from opencode.json (edits
    allowed; shell only the unittest and git status/diff commands; no web). The prompt goes in on stdin. It has no
    schema flag, so it is asked to end with the JSON and parsed leniently: the controller judges the change by
    running the tests itself anyway."""
    start = time.monotonic()
    model = model or cfg["agents"].get("opencode_model", "omniroute/free")
    if model.startswith("omniroute/") and (why := free_unavailable(cfg)):
        return result(start, error=f"opencode: free cloud tier unavailable ({why})")
    if model.startswith("ollama/") and (why := local_unavailable(cfg, LOCAL_AGENT_MIB)):
        return result(start, error=f"opencode: local tier unavailable ({why})")
    env = {"OPENCODE_CONFIG": str(OPENCODE_CONFIG), "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    if FREE_KEY.is_file():
        env["AGENT_OS_FREE_KEY"] = FREE_KEY.read_text(encoding="utf-8").strip()  # opencode.json: {env:AGENT_OS_FREE_KEY}
    ask = (prompt + "\n\nWhen you are done, end your reply with one JSON object with these keys: " + ", ".join(
        AGENT_SCHEMA["required"]) + ' (verdict "done" or "blocked").') if not session else prompt
    # --title: with a title set, opencode never asks a small model to name the session (an unguarded local load)
    code, out, err = ask_agent(["opencode", "run", "--format", "json", "-m", model, "--title", "agent-os",
                                *(["--session", session] if session else [])], cwd, ask, timeout, env,
                               stop=(lambda: (g := game_running(cfg)) and f"game running: {g}")
                               if model.startswith("ollama/") else None)
    if model.startswith("ollama/"):
        ollama_unload(cfg, model.removeprefix("ollama/"))
    texts, tokens, sid = [], {"in": 0, "out": 0}, ""
    for line in out.splitlines():
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            event = json.loads(line)
            sid = event.get("sessionID") or sid
            part = event.get("part") or {}
            if event.get("type") == "text":
                texts.append(part.get("text", ""))
            elif event.get("type") == "step_finish":
                tokens["in"] += (part.get("tokens") or {}).get("input") or 0
                tokens["out"] += (part.get("tokens") or {}).get("output") or 0
            elif event.get("type") == "error":
                err += f"\n{json.dumps(event.get('error') or event)[:500]}"
    text = "\n".join(texts)
    found = last_json(text) or {}
    # no final JSON at all: the model ended its turn before it was done (run_task gives it the repair round)
    data = {"summary": text.strip()[-1000:], "findings": [], "plan": [], "risks": [], "files_changed": [],
            "verdict": "done" if isinstance(found.get("verdict"), str) else "unfinished", "confidence": 0.5}
    # (a quoted {"in": 0} mid-plan is not the final object: only one with a verdict ends the turn)
    data |= {k: v for k, v in found.items() if k in data and type(v) is type(data[k])}  # a free model's types vary
    if code != 0 or not texts or "error" in err.lower() and not tokens["out"]:
        return result(start, data, tokens, error=failure("opencode", code, out, err))
    return result(start, data, tokens, session=sid)


# Research agent: Claude Code with web search and fetch, plus a Playwright-driven Edge in a fresh in-memory profile
# (no cookies, no sign-ins). Reading is automatic; anything that acts on a page goes to Ben through desk_mcp.py and
# HyperDesk (Approve / Deny), and is denied when nobody answers. Everything else is switched off.
# Two walls keep it on the public internet, both found necessary by the 2026-10-05 review:
# - desk_mcp.py checks every address WebFetch, browser_navigate and browser_tabs are given (http/https to a public
#   host only), so a page cannot make the agent "navigate" to javascript:... and run code without approval;
# - every request the browser itself makes (pages, redirects, images, websockets) goes through GuardProxy, which
#   refuses anything that resolves to this PC, the LAN or another non-public address. Playwright's --blocked-origins
#   is not a wall (its own help says so; redirects and *.localhost pass), so it is not used.
BROWSER_READ = ("browser_navigate_back", "browser_snapshot", "browser_find", "browser_wait_for", "browser_close")
BROWSER_BY_ADDRESS = ("browser_navigate", "browser_tabs")  # desk_mcp.py allows a public http(s) address, no Ben needed
BROWSER_ASK = ("browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
               "browser_hover", "browser_drag", "browser_handle_dialog")
BROWSER_NEVER = ("browser_evaluate", "browser_run_code_unsafe", "browser_file_upload", "browser_drop",
                 "browser_network_request", "browser_network_requests", "browser_console_messages",
                 "browser_emulate_media", "browser_resize", "browser_take_screenshot")


def public_address(host: str, port: int):
    """Where to connect for host:port if every address the host resolves to is on the public internet, else None.
    Connecting to exactly the address that was checked means a second DNS answer cannot point it at this PC."""
    host = (host or "").strip("[]").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if found and all(ipaddress.ip_address(a[4][0].split("%")[0]).is_global for a in found):
            return found[0][4][:2]
    except (OSError, ValueError, UnicodeError):
        pass
    return None


class GuardProxy(socketserver.ThreadingTCPServer):
    """The research browser's only way out: a forward proxy on 127.0.0.1 that reaches public addresses and nothing
    else. Playwright sends loopback traffic through a configured proxy too (it adds <-loopback> to the bypass list)."""
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Guard)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Guard(socketserver.StreamRequestHandler):
    rbufsize = 0  # unbuffered: nothing the browser sent after the headers may be stranded in a buffer
    timeout = 300  # an idle connection closes after five minutes

    def handle(self) -> None:
        try:
            method, target, version = self.rfile.readline(8192).decode("latin-1").split()
        except ValueError:
            return
        headers = []
        while (line := self.rfile.readline(8192)) not in (b"\r\n", b"\n", b""):
            headers.append(line)
        url = urllib.parse.urlsplit(target)
        if method == "CONNECT":  # https and websockets: a tunnel to host:port
            host, _, port = target.rpartition(":")
        else:  # plain http: GET http://host/path HTTP/1.1
            host, port = (url.hostname or "", url.port or 80) if url.scheme == "http" else ("", 0)
        address = public_address(host, int(port)) if str(port).isdigit() else None
        if not address:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        with socket.create_connection(address, timeout=30) as upstream:
            upstream.settimeout(self.timeout)
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:  # one request per connection, path-only request line, no proxy headers
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(kept)
                                 + b"Connection: close\r\n\r\n")
            back = threading.Thread(target=_pump, args=(upstream, self.connection), daemon=True)
            back.start()
            _pump(self.connection, upstream)
            back.join()


def _pump(src: socket.socket, dst: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while data := src.recv(65536):
            dst.sendall(data)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


RESEARCH_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": ["answer", "sources", "confidence", "follow_ups"],
                   "properties": {"answer": {"type": "string"},
                                  "sources": {"type": "array", "items": {
                                      "type": "object", "additionalProperties": False, "required": ["title", "url"],
                                      "properties": {"title": {"type": "string"}, "url": {"type": "string"}}}},
                                  "confidence": {"type": "number"},
                                  "follow_ups": {"type": "array", "items": {"type": "string"}}}}


def ask_researcher(prompt: str, cwd: Path, task_id: str, show_browser: bool = False, timeout: float = 1200) -> dict:
    """Headless Claude Code as a read-only web researcher. This is the one call that uses --permission-prompts host:
    its prompts go to desk_mcp.py, which asks Ben and denies on no answer, so it can never hang on a prompt."""
    start = time.monotonic()
    node = ROOT.parent / "tools" / "node"
    playwright = node / "node_modules" / "@playwright" / "mcp" / "cli.js"
    # This variable would make Playwright stop sending loopback traffic through the proxy; it must not reach the browser.
    os.environ.pop("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", None)
    guard = GuardProxy()
    servers = {"desk": {"command": str(Path(sys.executable).with_name("python.exe")), "args": [str(ROOT / "desk_mcp.py")],
                        "env": {"AGENT_OS_TASK": task_id, "PYTHONIOENCODING": "utf-8"}}}
    if playwright.is_file():  # without it the agent still has web search and fetch
        servers["browser"] = {"command": str(node / "node.exe"), "args": [
            str(playwright), "--browser", "msedge", "--isolated", "--no-webmcp", "--image-responses", "omit",
            "--output-dir", str(cwd), "--proxy-server", guard.url, *([] if show_browser else ["--headless"])]}
    browser = lambda names: [f"mcp__browser__{t}" for t in names]  # noqa: E731
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(RESEARCH_SCHEMA),
           "--permission-mode", "manual", "--permission-prompts", "host", "--permission-prompt-tool", "mcp__desk__approve",
           "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers}),
           "--disable-slash-commands", "--tools", "WebSearch,WebFetch", "--restricted", "--no-session-persistence",
           "--settings", json.dumps({"bashOutputMaxChars": 8000}),
           "--allowedTools", "WebSearch", *browser(BROWSER_READ),  # WebFetch and navigation: desk_mcp checks the address
           "--disallowedTools", *browser(BROWSER_NEVER), "--max-turns", "40"]
    try:
        code, out, err = ask_agent(cmd, cwd, stdin_text=prompt, timeout=timeout)
    finally:
        guard.shutdown()
        guard.server_close()
    try:
        reply = json.loads(out)
        data = reply.get("structured_output")
    except (ValueError, AttributeError):
        return result(start, error=failure("claude", code, out, err))
    usage = reply.get("usage") or {}
    usage = {"in": sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
             "out": usage.get("output_tokens"), "cost_est_usd": reply.get("total_cost_usd")}
    if code != 0 or reply.get("is_error") or not isinstance(data, dict):
        return result(start, data, usage, error=failure("claude", code, out, f"{err}\n{reply.get('result') or ''}"))
    return result(start, data, usage)


# Context builder: deterministic evidence for a task, highest signal first, packed into a token budget. No model.

STOPWORDS = frozenset(
    "find this that with from into what when where which should would could make sure please there their have does "
    "work works project code repo repository file files function method class issue problem error errors broken "
    "wrong fixed".split())


def search_terms(task: str) -> list[str]:
    """Identifier-like words of the task, plus a 4-letter stem of long ones ('authentication' also gives 'auth')."""
    words = [w for w in re.findall(r"[a-z_][a-z0-9_]{3,}", task.lower()) if w not in STOPWORDS]
    # ponytail: prefix stemming; have the local model suggest terms if real tasks show this is too blunt.
    return list(dict.fromkeys(words + [w[:4] for w in words if len(w) >= 7]))[:12]


def test_command(repo: Path) -> list[str]:
    # ponytail: Python unittest only; add a per-repo override once a non-Python repo is a target.
    # -t . only for a tests package: a plain tests folder (Agent OS's own) is "not importable" under it
    cmd = [sys.executable, "-m", "unittest", "discover", *(["-t", "."] if (repo / "tests" / "__init__.py").is_file() else [])]
    return cmd + ["-s", "tests"] if (repo / "tests").is_dir() else cmd


def run_tests(repo: Path, cmd: list[str], timeout: float = 600) -> tuple[int, str]:
    """The repo's own tests: no paid keys, no .pyc litter. Returns (exit code, stdout and stderr interleaved)."""
    env = {k: v for k, v in os.environ.items() if k not in PAID_KEYS} | {"PYTHONDONTWRITEBYTECODE": "1"}
    try:  # run_tree: a hung test's own children die with it, so the timeout holds
        p = run_tree(cmd, timeout, cwd=repo, env=env, stderr=subprocess.STDOUT, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"tests timed out after {timeout:.0f}s"
    except OSError as e:
        return 127, str(e)
    return p.returncode, p.stdout.strip()


def graph_facts(repo: Path, terms: list[str], budget: int) -> tuple[str, str]:
    """Graphify's code graph around the terms, EXTRACTED edges only. Returns (facts, provenance note)."""
    exe = find_tool("graphify")
    if not exe:
        return "", "Graphify not installed, skipped"
    # graphify-out/ is written into the repo; hide it from git without touching the repo's own .gitignore.
    code, path = run(["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"])
    if code == 0:
        exclude = repo / path
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if "graphify-out/" not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "graphify-out/\n",
                               encoding="utf-8")
    code, out = run([exe, "update", "."], timeout=300, cwd=repo)  # code only: AST parse, no LLM
    if code != 0:
        return "", f"graphify update failed: {first_line(out)}"
    _, out = run([exe, "query", " ".join(terms), "--budget", str(budget)], timeout=60, cwd=repo)
    lines = out.splitlines()
    facts = [l for l in lines if l.startswith("NODE ") or (l.startswith("EDGE ") and "[EXTRACTED" in l)]
    dropped = sum(l.startswith("EDGE ") for l in lines) - sum(l.startswith("EDGE ") for l in facts)
    return "\n".join(facts), f"graphify query {' '.join(terms)!r}, EXTRACTED edges only ({dropped} others dropped)"


def build_context(repo: Path, task: str, test_cmd: list[str], budget: int = 4000) -> tuple[str, int]:
    """Evidence for an agent prompt, each section citing its source. Returns (text, exit code of the tests)."""
    terms = search_terms(task)

    def git(*args: str) -> tuple[int, str]:
        return run(["git", "-C", str(repo), *args], timeout=60)

    tests_code, tests_out = run_tests(repo, test_cmd)
    # Graph first: it hides graphify-out/ from git, so git status below stays clean.
    facts, graph_note = graph_facts(repo, terms, budget // 3) if terms else ("", "no search terms")
    state = []
    for args in (["status", "--short", "--branch"], ["log", "--oneline", "-5"], ["diff", "HEAD"]):
        code, out = git(*args)
        if code == 0 and out:
            state.append(f"$ git {' '.join(args)}\n{out}")
    files = git("ls-files")[1].splitlines()
    likely = [f for f in files if any(t in f.lower() for t in terms)]
    code, out = git("grep", "-n", "-I", "-i", "-F", *[x for t in terms for x in ("-e", t)]) if terms else (1, "")
    hits = out.splitlines() if code == 0 else []
    per_file = collections.Counter(h.split(":", 1)[0] for h in hits)
    hits.sort(key=lambda h: -per_file[h.split(":", 1)[0]])  # stable: line order kept within a file
    sections = [  # (title, source, text, keep the end instead of the start when cut)
        ("Tests", f"{' '.join(test_cmd[1:])}, exit {tests_code}", tests_out if tests_code else "all pass", True),
        ("Git state", "git status, log, diff", "\n\n".join(state), False),
        ("Likely files", "git ls-files matching the search terms", "\n".join(likely), False),
        ("Code graph", graph_note, facts, False),
        ("Text search", "git grep -i, files with most hits first", "\n".join(h[:200] for h in hits), False),
    ]
    # ponytail: 4 chars per token, close enough for a budget; each section gets at most a third of it.
    room, cap, parts = budget * 4, budget * 4 // 3, []
    for title, source, text, tail in sections:
        n = min(len(text), cap, max(room, 0))
        cut = text if n == len(text) else ("[...]\n" + text[len(text) - n:] if tail else text[:n] + "\n[...]")
        parts.append(f"## {title}\nsource: {source}\n{cut or '(none)'}")
        room -= n
    return f"# Context for: {task}\nsearch terms: {', '.join(terms) or '(none)'}\n\n" + "\n\n".join(parts), tests_code


# Each check returns (status, detail, fix). WARN = a tier is missing or degraded. FAIL = broken or unsafe.

def check_python():
    return PASS, sys.version.split()[0], ""


def check_git():
    code, out = run(["git", "--version"])
    return (PASS, first_line(out), "") if code == 0 else (FAIL, "not found", "Install Git for Windows.")


def check_ollama(cfg: dict):
    url = cfg["ollama"]["url"]
    try:
        version = get_json(f"{url}/api/version")["version"]
        have = {m["name"].removesuffix(":latest") for m in get_json(f"{url}/api/tags")["models"]}
    except (OSError, ValueError, KeyError) as e:
        return WARN, f"not answering at {url} ({e})", "Start Ollama from the Start menu. The local tier is skipped until then."
    missing = [a for a in cfg["ollama"]["aliases"] if a not in have]
    if missing and "share" in cfg:  # the shared build has no modelfiles folder: its installer makes the aliases
        return WARN, f"{version}, missing aliases: {', '.join(missing)}", "Run Install.cmd from the HyperDesk OS zip again: it makes them."
    if missing:  # an alias is built from its Modelfile, whose base model is local: `ollama pull` cannot bring it back
        files = ROOT.parent / "modelfiles"
        return (WARN, f"{version}, missing aliases: {', '.join(missing)}", "  then  ".join(
            f"ollama create {x} -f {files / f'{x}.Modelfile'}" if (files / f"{x}.Modelfile").is_file()
            else f"no {x}.Modelfile in {files}: write one, or take {x} out of config.toml [ollama] aliases" for x in missing))
    return PASS, f"{version}, aliases ok: {', '.join(cfg['ollama']['aliases'])}", ""


def check_ollama_bind(cfg: dict):
    return check_bind(urllib.parse.urlsplit(cfg["ollama"]["url"]).port,
                      "Set the user env var OLLAMA_HOST=127.0.0.1:11434, then restart Ollama.")


def check_bind(port: int, fix: str):
    """FAIL if the port answers on any non-loopback IPv4 address of this machine."""
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        ips = set()
    exposed = []
    for ip in sorted(ip for ip in ips if not ip.startswith("127.")):
        try:
            socket.create_connection((ip, port), timeout=0.5).close()
            exposed.append(ip)
        except OSError:
            pass
    if exposed:
        return FAIL, f"port {port} answers on {', '.join(exposed)}", fix
    return PASS, f"port {port} is not reachable on LAN addresses", ""


def check_gpu(cfg: dict):
    code, out = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    try:
        name, total, used = [x.strip() for x in first_line(out).split(",")]
        detail = f"{name}, {int(total) - int(used)} of {int(total)} MiB VRAM free"
    except ValueError:
        if "share" in cfg:  # a friend's PC may have no NVIDIA card: Ollama runs anyway, as local_unavailable allows
            return PASS, "no NVIDIA reading: local models run on the CPU or another GPU (slower)", ""
        return WARN, "nvidia-smi gave no reading", "The local tier needs the NVIDIA driver."
    game = game_running(cfg)
    if game:
        return WARN, f"{detail}; game running: {game}", "The local tier is skipped while a game runs (measured: VRAM overflow)."
    return PASS, detail, ""


def check_game_guard(cfg: dict):
    """bin\\_guard.cmd (Ben's) is the only game check the ai-*.cmd launchers have: it must name every config.toml game."""
    guard = ROOT.parent / "bin" / "_guard.cmd"
    if not guard.is_file():
        return WARN, "bin\\_guard.cmd not found", "The ai-*.cmd launchers would load a model during a game."
    line = next((x.lower() for x in guard.read_text(errors="replace").splitlines() if "findstr" in x.lower()), "")
    if miss := [g for g in cfg["gpu"]["games"] if g.lower() not in line]:  # its findstr line, not a comment
        return (WARN, f"bin\\_guard.cmd does not warn for {', '.join(miss)}",
                f"Add {' '.join(miss)} to the findstr list in {guard} (bin is yours; Agent OS leaves it alone).")
    return PASS, "bin\\_guard.cmd warns for the same games as config.toml", ""


def check_disk():
    free_gb = shutil.disk_usage(ROOT).free / 2**30
    if free_gb < 20:
        return WARN, f"{free_gb:.0f} GB free on {ROOT.anchor}", "Free up space: worktrees and logs live here."
    return PASS, f"{free_gb:.0f} GB free on {ROOT.anchor}", ""


def check_claude():
    code, out = run(["claude", "--version"])
    if code != 0:
        return WARN, "CLI not installed", "agent-os install   then run `claude` once and sign in"
    version = first_line(out)
    code, out = run(["claude", "auth", "status"])
    try:
        method = json.loads(out).get("authMethod", "unknown")
    except (ValueError, AttributeError):
        method = "unknown"
    if code != 0:
        return WARN, f"{version}, not signed in", "Run `claude` once and sign in with your Claude account."
    if method not in ("claude.ai", "oauth_token"):
        return WARN, f"{version}, signed in via {method}", "Agent calls would not use your subscription. Run `claude auth login`."
    return PASS, f"{version}, subscription sign-in", ""


def check_codex():
    code, out = run(["codex", "--version"])
    if code != 0:
        return WARN, "CLI not installed", "agent-os install   then run `codex.cmd login`"
    version = first_line(out)
    code, out = run(["codex", "login", "status"])
    if code != 0 or "not logged in" in out.lower():
        return WARN, f"{version}, not signed in", "Run `codex.cmd login` and sign in with ChatGPT."
    if "chatgpt" not in out.lower():
        return WARN, f"{version}, signed in with an API key", "Agent calls would bill the API. Run `codex.cmd login` and choose ChatGPT."
    return PASS, f"{version}, ChatGPT sign-in", ""


def check_api_keys():
    found = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY") if os.environ.get(k)]
    if found:
        return (WARN, f"set: {', '.join(found)} (values hidden)",
                "With these set, the agent CLIs can bill the paid API instead of your subscription.")
    return PASS, "no paid-API keys in the environment", ""


def check_graphify():
    code, out = run(["graphify", "--version"])
    return (PASS, first_line(out), "") if code == 0 else (WARN, "not installed (optional)", "agent-os install")


def check_browser():
    package = ROOT.parent / "tools" / "node" / "node_modules" / "@playwright" / "mcp" / "package.json"
    if not package.is_file():
        return WARN, "Playwright MCP not installed (research falls back to web search and fetch)", "agent-os install"
    edge = Path(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"))
    version = json.loads(package.read_text(encoding="utf-8"))["version"]
    if not edge.is_file():
        return WARN, f"Playwright MCP {version}, but Edge was not found", "The research browser uses the installed Edge."
    return PASS, f"Playwright MCP {version} on Edge, fresh profile each run", ""


def voice_python() -> str:
    """Open WebUI's own Python, which already ships faster-whisper: local speech-to-text without another install."""
    code, out = run(["uv", "tool", "dir"], 30)
    exe = Path(out.strip()) / "open-webui" / "Scripts" / "python.exe" if code == 0 and out else None
    return str(exe) if exe and exe.is_file() else ""


def check_voice():
    python = voice_python()
    if not python:
        return WARN, "Open WebUI's environment not found", "Voice input uses the faster-whisper that Open WebUI brings."
    code, out = run([python, "-c", "import faster_whisper; print(faster_whisper.__version__)"], 60)
    if code != 0:
        return WARN, "faster-whisper not importable", "Reinstall Open WebUI: uv tool install open-webui --force"
    return PASS, f"faster-whisper {first_line(out)} (on this PC)", ""


def check_state_db(db: Path | None = None):
    db = db or ROOT / "state" / "agent_os.db"
    if not db.exists():
        return PASS, "not created yet", ""
    try:
        with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as e:
        result = str(e)
    if result != "ok":
        return FAIL, f"integrity_check: {result}", f"Restore {db.name} from a copy, or move it aside to start a fresh ledger."
    return PASS, "integrity ok", ""


def doctor_checks(cfg: dict) -> list[tuple[str, str, str, str]]:
    """Every check as (name, status, detail, fix). Shared by `doctor` and the home base app."""
    checks = [
        ("python", check_python),
        ("git", check_git),
        ("ollama", lambda: check_ollama(cfg)),
        ("ollama-bind", lambda: check_ollama_bind(cfg)),
        # Plain `omniroute` binds 0.0.0.0 with the public secrets from its package .env.
        ("omni-bind", lambda: check_bind(20128, "Stop OmniRoute. Start it only with omniroute-local.cmd (127.0.0.1).")),
        ("gpu", lambda: check_gpu(cfg)),
        ("game-guard", lambda: check_game_guard(cfg)),
        ("disk", check_disk),
        ("claude", check_claude),
        ("codex", check_codex),
        ("api-keys", check_api_keys),
        ("graphify", check_graphify),
        ("browser", check_browser),
        ("voice", check_voice),
        ("free", lambda: (WARN, why, "Optional: config.toml [free], then Apps & APIs in HyperDesk.") if (
            why := free_unavailable(cfg)) else (PASS, f"{cfg['free']['model']} at {cfg['free']['url']}", "")),
        ("state-db", check_state_db),
        ("desk-bind", lambda: check_bind(8765, "Close HyperDesk and send logs\\doctor.txt to Claude: it must bind 127.0.0.1.")),
    ]
    if "share" in cfg:  # a friend's PC has no git, cloud agents, OmniRoute, Ben's bin or Open WebUI: these only alarm
        checks = [c for c in checks if c[0] not in {"git", "omni-bind", "game-guard", "claude", "codex", "api-keys",
                                                   "graphify", "browser", "voice", "free"}]
    results = []
    for name, check in checks:
        try:
            results.append((name, *check()))
        except Exception as e:  # a doctor that crashes on one check hides the other ten
            results.append((name, FAIL, f"check crashed: {e!r}", "Send logs\\doctor.txt to Claude."))
    return results


def doctor(cfg: dict) -> int:
    lines = [f"Agent OS doctor  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    failed = False
    for name, status, detail, fix in doctor_checks(cfg):
        failed = failed or status == FAIL
        lines.append(f"{status}  {name:<12} {detail}")
        if fix:
            lines.append(f"      fix: {fix}")
    report = "\n".join(lines)
    print(report)
    log = ROOT / "logs" / "doctor.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(report + "\n", encoding="utf-8")
    return 1 if failed else 0


def npm_cmd() -> str:
    npm = ROOT.parent / "tools" / "node" / "npm.cmd"  # full path: bare `npm` breaks from other folders (RUNBOOK)
    return str(npm) if npm.is_file() else "npm"


def install_steps(cutoff: str) -> list[dict]:
    npm = npm_cmd()
    return [
        {"tool": "claude", "component": "Claude Code CLI", "source": "winget Anthropic.ClaudeCode",
         "cmd": ["winget", "install", "--id", "Anthropic.ClaudeCode", "-e", "--scope", "user", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
         "uninstall": "winget uninstall Anthropic.ClaudeCode", "reason": "cloud agent",
         "if_failed": "No-admin alternative from Anthropic, in PowerShell: irm https://claude.ai/install.ps1 | iex"},
        {"tool": "codex", "component": "Codex CLI", "source": "npm @openai/codex",
         "cmd": [npm, "install", "-g", "@openai/codex", "--before", cutoff],
         "uninstall": f"{npm} uninstall -g @openai/codex", "reason": "cloud agent and independent reviewer"},
        {"tool": "graphify", "component": "Graphify", "source": "PyPI graphifyy via uv",
         "cmd": ["uv", "tool", "install", "graphifyy", "--exclude-newer", cutoff],
         "uninstall": "uv tool uninstall graphifyy", "reason": "code-only structural graph for context"},
        # Added 2026-10-05 at Ben's request. Start it only through bin\omniroute-local.cmd (loopback, auth on), and
        # never connect a Claude or Codex subscription login to it.
        {"tool": "omniroute", "component": "OmniRoute", "source": "npm omniroute",
         "cmd": [npm, "install", "-g", "omniroute", "--before", cutoff],
         "uninstall": f"{npm} uninstall -g omniroute", "reason": "API gateway in front of Ollama (Ben's request)"},
        # Added 2026-10-05 at Ben's request: the research agent's browser. Drives the installed Edge in a fresh,
        # empty profile (--isolated), so it never sees Ben's sign-ins; no browser download.
        {"tool": "playwright-mcp", "component": "Playwright MCP", "source": "npm @playwright/mcp",
         "cmd": [npm, "install", "-g", "@playwright/mcp", "--before", cutoff],
         "uninstall": f"{npm} uninstall -g @playwright/mcp", "reason": "browser for the research agent (Ben's request)"},
        # Added 2026-10-06 at Ben's request: a coding agent that runs on the free cloud tier (OmniRoute) or local
        # models, so fixes need not spend the Claude/Codex cap.
        {"tool": "opencode", "component": "opencode", "source": "npm opencode-ai",
         "cmd": [npm, "install", "-g", "opencode-ai", "--before", cutoff],
         "uninstall": f"{npm} uninstall -g opencode-ai", "reason": "free-tier coding agent (Ben's request)"},
    ]


def install() -> int:
    """Install the approved tools that are missing. User scope only; records each one in install-manifest.json."""
    # ponytail: fixed 7-day cooldown on npm/PyPI releases (supply-chain hygiene); move to config.toml if it ever needs tuning.
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    manifest_path = ROOT / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    env = dict(os.environ)
    if (ROOT.parent / ".uv-cache").is_dir():
        env["UV_CACHE_DIR"] = str(ROOT.parent / ".uv-cache")  # keep downloads off C:, same as the September setup
    lines: list[str] = []
    failed = False

    def say(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    say(f"Agent OS install  {time.strftime('%Y-%m-%d %H:%M:%S')}  (npm/PyPI releases up to {cutoff})")
    for step in install_steps(cutoff):
        name = step["component"]
        have = find_tool(step["tool"])
        if have:
            say(f"SKIP  {name}: already installed at {have}")
            continue
        # If our lookup misses (winget is a Windows app alias), hand the bare name to the OS to resolve.
        installer = step["cmd"][0] if os.path.isabs(step["cmd"][0]) else (find_tool(step["cmd"][0]) or step["cmd"][0])
        say(f"RUN   {' '.join(step['cmd'])}")
        try:
            code = subprocess.run([installer, *step["cmd"][1:]], env=env, timeout=1800, stdin=subprocess.DEVNULL).returncode
        except (subprocess.TimeoutExpired, OSError) as e:
            code = 127 if isinstance(e, FileNotFoundError) else 124
            say(f"      {e}")
        path = find_tool(step["tool"])
        if code != 0 or not path:
            say(f"FAIL  {name}: exit code {code}" + ("" if path else f", `{step['tool']}` not found afterwards"))
            if "if_failed" in step:
                say(f"      {step['if_failed']}")
            failed = True
            continue
        version = first_line(run([step["tool"], "--version"])[1]) or "unknown"
        manifest.append({"component": name, "version": version, "source": step["source"], "location": path,
                         "command": " ".join(step["cmd"]), "reason": step["reason"],
                         "date": time.strftime("%Y-%m-%d"), "uninstall": step["uninstall"]})
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        say(f"OK    {name}: {version} at {path}")
    say("")
    say("Next, in a NEW terminal (sign-in is yours to do; nothing here touches credentials):")
    say("  1. claude         sign in through the browser prompt, then type /exit")
    # codex.cmd, not codex: PowerShell would pick npm's codex.ps1 shim, which the execution policy blocks.
    say("  2. codex.cmd login   choose ChatGPT. If Codex offers a sandbox setup that needs administrator rights, decline it")
    say("  3. agent-os doctor")
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "install.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


SMOKE_PROMPT = "Reply with JSON only. Put the single word READY in the answer field."
SMOKE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                "additionalProperties": False}
SMOKE_TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "Get the current time in a timezone",
    "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}, "required": ["zone"]}}}]


def smoke(cfg: dict) -> int:
    """One tiny request per tier, checked against a schema. Proves each tier works end to end."""
    lines = [f"Agent OS smoke  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    failed = False

    def record(name: str, r: dict, good) -> None:
        nonlocal failed
        try:
            ok = r["ok"] and bool(good(r["data"]))
        except (KeyError, IndexError, TypeError, AttributeError):
            ok = False
        missing = r["error"].endswith("is not installed")
        failed = failed or not (ok or missing)
        status = PASS if ok else WARN if missing else FAIL
        detail = json.dumps(r["data"])[:120] if ok else (r["error"] or f"unexpected reply: {json.dumps(r['data'])[:200]}")
        lines.append(f"{status}  {name:<17} {r['seconds']:>5}s  {json.dumps(r['usage'])}  {detail}")
        print(lines[-1], flush=True)

    def ready(data) -> bool:
        return "READY" in data["answer"].upper()

    print(lines[0], flush=True)
    game = game_running(cfg)
    for alias in cfg["ollama"]["local_tier"]:
        if game:
            lines.append(f"WARN  local:{alias:<11} skipped, game running: {game}")
            print(lines[-1], flush=True)
            continue
        record(f"local:{alias} json", ask_local(cfg, alias, SMOKE_PROMPT, schema=SMOKE_SCHEMA), ready)
        record(f"local:{alias} tools", ask_local(cfg, alias, "What time is it in UTC? Call the tool.",
                                                 tools=SMOKE_TOOLS, keep_alive=0),  # 0 = unload right after
               lambda calls: calls[0]["function"]["name"] == "get_time")
    workspace = ROOT.parent / "workspace"
    cwd = workspace if workspace.is_dir() else ROOT
    record("claude", ask_claude(SMOKE_PROMPT, SMOKE_SCHEMA, cwd), ready)
    record("codex", ask_codex(SMOKE_PROMPT, SMOKE_SCHEMA, cwd), ready)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "smoke.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


# Updates: each tool moves to its newest release that is at least 7 days old (same supply-chain rule as install),
# is checked, and goes back to the version it had if the check fails. Ollama updates itself; Python, Node, Git,
# ComfyUI and the models are left to Ben.

# component (as in install-manifest.json), installer, package, command that must still run afterwards, and "verify":
# a doctor check that must still PASS if it passed before (zero tokens: the sign-in status, not a model call).
UPDATES = [
    {"name": "Claude Code CLI", "kind": "winget", "pkg": "Anthropic.ClaudeCode", "tool": "claude",
     "dates": "@anthropic-ai/claude-code", "verify": check_claude},  # winget has no publish dates; npm has
    {"name": "Codex CLI", "kind": "npm", "pkg": "@openai/codex", "tool": "codex", "verify": check_codex},
    {"name": "Qwen Code", "kind": "npm", "pkg": "@qwen-code/qwen-code", "tool": "qwen"},
    {"name": "OmniRoute", "kind": "npm", "pkg": "omniroute", "tool": "omniroute", "port": 20128},
    {"name": "Playwright MCP", "kind": "npm", "pkg": "@playwright/mcp", "tool": "playwright-mcp"},
    {"name": "opencode", "kind": "npm", "pkg": "opencode-ai", "tool": "opencode"},
    {"name": "Graphify", "kind": "uv", "pkg": "graphifyy", "tool": "graphify"},
    {"name": "Open WebUI", "kind": "uv", "pkg": "open-webui", "tool": None, "port": 8080,
     "verify": check_voice},  # voice input and Studio's transcription run in its environment
]
BUSY_STATES = ("RECEIVED", "CLASSIFIED", "CONTEXT_BUILDING", "PLANNING", "EXECUTING", "VALIDATING", "REVIEWING")
STALE_HOURS = 6  # a task busy for longer has nothing running it any more (its agent calls time out long before)


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def installed_version(u: dict) -> str:
    if u["kind"] == "npm":
        try:
            return json.loads(run([npm_cmd(), "ls", "-g", u["pkg"], "--depth", "0", "--json"], 60)[1])[
                "dependencies"][u["pkg"]]["version"]
        except (ValueError, KeyError, TypeError):
            return ""
    if u["kind"] == "uv":
        m = re.search(rf"^{re.escape(u['pkg'])} v(\S+)", run(["uv", "tool", "list"], 60)[1], re.M)
        return m.group(1) if m else ""
    m = re.search(r"\d+(\.\d+)+", run([u["tool"], "--version"], 60)[1])  # winget: ask the tool itself
    return m.group(0) if m else ""


def newest_release(u: dict, cutoff: str) -> str:
    """Newest stable release published before the cutoff date, or '' if that cannot be found out."""
    try:
        if u["kind"] == "uv":
            with urllib.request.urlopen(f"https://pypi.org/pypi/{u['pkg']}/json", timeout=30) as r:
                releases = json.load(r)["releases"]
            ok = [v for v, files in releases.items() if files and re.fullmatch(r"\d+(\.\d+)*", v)
                  and not any(f.get("yanked") for f in files) and min(f["upload_time_iso_8601"] for f in files) < cutoff]
        else:  # publish dates from npm. Not `npm view --before`: it ignores the date and reports today's latest
            times = json.loads(run([npm_cmd(), "view", u.get("dates", u["pkg"]), "time", "--json"], 60)[1])
            ok = [v for v, t in times.items() if re.fullmatch(r"\d+\.\d+\.\d+", v) and t < cutoff]
            if u["kind"] == "winget":  # only versions winget actually offers
                listed = run(["winget", "show", "--id", u["pkg"], "-e", "--versions", "--disable-interactivity"], 120)[1]
                ok = [v for v in ok if re.search(rf"^{re.escape(v)}\s*$", listed, re.M)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    return max(ok, key=version_key, default="")


def uv_receipt(pkg: str) -> dict:
    """The [tool] table uv keeps for an installed tool: requirements (with extras), python, entrypoints."""
    try:
        receipt = Path(run(["uv", "tool", "dir"], 60)[1].strip()) / pkg / "uv-receipt.toml"
        return tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]
    except (OSError, ValueError, KeyError):
        return {}


def set_version(u: dict, version: str, cutoff: str) -> tuple[int, str]:
    """Install exactly this version (used both to update and to roll back)."""
    if u["kind"] == "npm":
        # --before holds its dependencies (caret ranges, no lockfile in a global install) to the 7-day rule too
        return run([npm_cmd(), "install", "-g", f"{u['pkg']}@{version}", "--before", cutoff], 1800)
    if u["kind"] == "uv":  # keep the Python (Open WebUI is pinned to 3.11) and the extras (graphifyy[mcp]) it has
        tool = uv_receipt(u["pkg"])
        python = tool.get("python")
        extras = next((r.get("extras") or [] for r in tool.get("requirements", []) if r.get("name") == u["pkg"]), [])
        spec = f"{u['pkg']}[{','.join(extras)}]" if extras else u["pkg"]
        return run(["uv", "tool", "install", f"{spec}=={version}", "--force", "--exclude-newer", cutoff,
                    *(["--python", python] if python else [])], 1800)
    return run(["winget", "install", "--id", u["pkg"], "-e", "--version", version, "--scope", "user", "--force",
                "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"], 1800)


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def start_ollama() -> None:
    """Ollama no longer starts at sign-in (Ben, 2026-10-07): opening HyperDesk or Home Base starts it."""
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    if not port_open(11434) and app.exists():
        subprocess.Popen([str(app)], cwd=app.parent)  # not ours: it would hold the install folder open, so uninstall fails


def present(services: dict) -> dict:
    return {name: s for name, s in services.items() if s[1].exists()}  # only what this PC has: the shared build ships none


# Local services the desktop apps (HyperDesk, Home Base) start, stop and open. Each binds 127.0.0.1 only.
SERVICES = present({  # name: (port, script that starts it in its own minimized window, address)
    "Open WebUI": (8080, ROOT.parent / "Start AI.bat", "http://127.0.0.1:8080"),
    "ComfyUI": (8188, ROOT.parent / "Start Images.bat", "http://127.0.0.1:8188"),
    "OmniRoute": (20128, ROOT.parent / "bin" / "omniroute-local.cmd", "http://127.0.0.1:20128"),
})
UPDATE_TASK = "AgentOS daily update"


def listening_pids(port: int) -> list[int]:
    rows = (line.split() for line in run(["netstat", "-ano", "-p", "TCP"], 20)[1].splitlines())
    return sorted({int(r[-1]) for r in rows if len(r) >= 5 and r[1].endswith(f":{port}") and r[3] == "LISTENING"})


def stop_service(name: str) -> str:
    if name == "OmniRoute":  # its own stop ends the supervisor first; killing the server alone gets it restarted
        p = subprocess.run([str(ROOT.parent / "tools" / "node" / "omniroute.cmd"), "stop"], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
                           env={**os.environ, "DATA_DIR": str(ROOT.parent / "omniroute-data")})
        return (p.stdout + p.stderr).strip()
    pids = listening_pids(SERVICES[name][0])
    if not pids:
        return f"{name} is not running"
    return run(["taskkill", "/T", "/F", *[x for pid in pids for x in ("/PID", str(pid))]], 30)[1]


def schedule_info() -> str:
    code, out = run(["schtasks", "/Query", "/TN", UPDATE_TASK, "/FO", "LIST", "/V"], 20)
    if code != 0:
        return "Daily update task: not set up"
    info = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    pick = lambda k: info.get(k, "?").strip()  # noqa: E731
    return (f"Daily update task: next run {pick('Next Run Time')}  |  last run {pick('Last Run Time')}  "
            f"(result {pick('Last Result')})")


def update(cfg: dict, check_only: bool = False) -> int | None:
    """Bring each tool in UPDATES to its newest release at least 7 days old. Writes logs\\update.txt."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    if (ROOT.parent / ".uv-cache").is_dir():
        os.environ["UV_CACHE_DIR"] = str(ROOT.parent / ".uv-cache")  # keep downloads off C:, as install does
    lines = []

    def say(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    say(f"Agent OS update  {now()}  (releases published before {cutoff}){'  CHECK ONLY' if check_only else ''}")
    game = game_running(cfg)
    with contextlib.closing(ledger()) as con:
        task_busy = con.execute(f"select count(*) from tasks where state in ({','.join('?' * len(BUSY_STATES))}) "
                                "and created >= datetime('now', 'localtime', ?)",
                                (*BUSY_STATES, f"-{STALE_HOURS} hours")).fetchone()[0]
    if not check_only:  # one update at a time: Home Base, HyperDesk and the 11:00 task each guard only themselves
        import msvcrt  # Windows only, like winreg in fresh_path
        lock = open(ROOT / "state" / "update.lock", "a")  # held until update() returns; Windows frees it if we die
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            say("SKIP  another agent-os update is running")
            return None  # its update.txt and history stay that run's, and it runs the self-check itself
    failed = False
    changed = []
    for u in UPDATES:
        have, want = installed_version(u), newest_release(u, cutoff)
        label = f"{u['name']:<16} {have or '?':>9} -> {want or '?':<9}"
        if not have or not want:
            say(f"WARN  {label} could not read the {'installed' if not have else 'newest'} version")
            continue
        if version_key(want) <= version_key(have):
            say(f"OK    {label} up to date")
            continue
        busy = ""
        if game:
            busy = f"game running: {game}"
        elif task_busy and u["tool"] in ("claude", "codex"):
            busy = "an agent task is running"
        elif u["tool"] == "opencode" and (task_busy or "opencode.exe" in running_processes()):  # npm: EBUSY mid-task
            busy = "opencode is running (an agent task, or one of yours)"
        elif u.get("port") and port_open(u["port"]):  # Windows locks the files of a running program
            busy = f"it is running (port {u['port']})"
        elif u["kind"] == "uv" and (exe := next((Path(e["install-path"]).name for e in uv_receipt(u["pkg"]).get(
                "entrypoints", []) if Path(e.get("install-path", "")).name.lower() in running_processes()), "")):
            busy = (f"{exe} is running (an open Codex or Claude Code session's MCP server: quit those apps, then "
                    "System, Update now)")  # 2026-10-06: graphify-mcp locked it
        if check_only or busy:
            say(f"{'SKIP' if busy else 'NEW '}  {label} {busy and 'skipped: ' + busy}".rstrip())
            continue
        before = bool(u.get("verify")) and u["verify"]()[0] == PASS  # a check already failing must not undo every update
        code, out = set_version(u, want, cutoff)
        works = installed_version(u) == want and (not u["tool"] or run([u["tool"], "--version"], 60)[0] == 0)
        if code == 0 and works and before and (check := u["verify"]())[0] != PASS:
            works, out = False, f"{check[0]} after the update: {check[1]}"
        if code == 0 and works:
            say(f"DONE  {label} updated{'; check PASS' if before else ''}")
            changed.append((u, have, want))
            continue
        failed = True
        back = set_version(u, have, cutoff)[0] == 0 and installed_version(u) == have
        say(f"FAIL  {label} exit {code}: {first_line(out)[:150]}; {'rolled back to ' + have if back else 'ROLLBACK FAILED'}")

    # opencode must still answer headless (a release could change the `run --format json` events ask_opencode reads).
    # Claude and Codex were checked by their sign-in status above: no paid call here. This is one free-tier or local
    # call, in an empty folder since opencode may edit where it runs.
    for u, old, new in changed:
        if u["tool"] == "opencode":
            with tempfile.TemporaryDirectory() as d:
                r = ask_opencode(cfg, SMOKE_PROMPT, Path(d), 300)
                if "tier unavailable" in (free := r["error"]):  # refused before running: try this PC's model
                    r = ask_opencode(cfg, SMOKE_PROMPT, Path(d), 300, model="ollama/" + LOCAL_AGENT.removeprefix("local:"))
            if re.search("tier unavailable|stopped: game running", r["error"]) or LIMIT_ERROR.search(r["error"]):
                # nothing could run it (or a game started under it): not the release's fault
                say(f"WARN  {u['name']} not checked headless (kept: --version works): "
                    + "; ".join(dict.fromkeys((free[:120], r["error"][:120]))))
            elif not (r["ok"] and "READY" in str(r["data"]).upper()):
                failed = True
                back = set_version(u, old, cutoff)[0] == 0 and installed_version(u) == old
                say(f"FAIL  {u['name']} no longer answers headless: {r['error'][:150]}; "
                    f"{'rolled back to ' + old if back else 'ROLLBACK FAILED'}")
                continue
            else:
                say(f"OK    {u['name']} answers headless after the update")
        record = ROOT / "install-manifest.json"  # keep the recorded version true
        manifest = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
        if not (entry := next((m for m in manifest if m["component"] == u["name"]), None)):  # Open WebUI, Qwen Code
            manifest.append(entry := {"component": u["name"], "source": f"{u['kind']} {u['pkg']}"})
        entry.update(version=new, date=time.strftime("%Y-%m-%d"))  # the version checked above
        record.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (ROOT / "logs").mkdir(exist_ok=True)
    if failed:
        notify("Update problem", "; ".join(x for x in lines if x.startswith("FAIL"))[:300])
    if not check_only:  # a check streams to its console; it must not erase the last real run's FAIL lines
        (ROOT / "logs" / "update.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(ROOT / "logs" / "update-history.txt", "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)  # the next update may start
        lock.close()
    return 1 if failed else 0


# Task runner: one task = one ledger row, one worktree, one implementer call, at most one repair round,
# and a review by the other vendor when the change is large or security-sensitive. Merging stays human.

STATES = ("RECEIVED", "CLASSIFIED", "CONTEXT_BUILDING", "PLANNING", "AWAITING_APPROVAL", "EXECUTING",
          "VALIDATING", "REVIEWING", "COMPLETED", "FAILED", "ROLLED_BACK")
PROMPTS = Path(__file__).resolve().parent / "prompts"
CONTEXT_BUDGET = 6000  # tokens; well under the 32K local ceiling, and keeps cloud prompts lean
REVIEW_OVER_LINES = 40
RISKY = re.compile(r"auth|passw|token|session|crypt|secret|permission|login", re.I)
LEVEL_RULES = [  # first match wins, so the more demanding level takes precedence; no match: the local model decides
    (3, re.compile(r"\b(design|architect\w*|migrat\w*|redesign\w*|restructur\w*|rewrite)\b")),
    (2, re.compile(r"\b(fix\w*|implement\w*|add(s|ed|ing)?|remov\w*|refactor\w*|renam\w*|chang\w*|updat\w*|"
                   r"creat\w*|write|delet\w*|repair\w*|debug\w*)\b")),
    (1, re.compile(r"\b(explain\w*|summar\w*|describ\w*|why|how|what|where|which)\b")),
    (0, re.compile(r"\b(run|search|grep|list|show|count)\b")),
]
CLASSIFY_PROMPT = ("Classify this coding task. 0: needs only search or running tests. 1: a read-only question a small "
                   "model can answer from evidence. 2: a code change one agent can make. 3: needs a design before code."
                   " Give the reason in one short sentence.\n\nTask: {task}")
ANSWER_PROMPT = ("Answer in at most three sentences, using only the evidence below and citing file:line. If it is not "
                 "enough, say so.\n\nQuestion: {task}\n\n{context}")
LEVEL_SCHEMA = {"type": "object", "properties": {"level": {"type": "integer", "enum": [0, 1, 2, 3]},
                                                 "reason": {"type": "string"}},
                "required": ["level", "reason"], "additionalProperties": False}
AGENT_SCHEMA = {"type": "object", "additionalProperties": False,  # every field required: Codex's strict mode needs it
                "required": ["summary", "findings", "plan", "risks", "files_changed", "verdict", "confidence"],
                "properties": {"summary": {"type": "string"},
                               "findings": {"type": "array", "items": {"type": "string"}},
                               "plan": {"type": "array", "items": {"type": "string"}},
                               "risks": {"type": "array", "items": {"type": "string"}},
                               "files_changed": {"type": "array", "items": {"type": "string"}},
                               "verdict": {"type": "string", "enum": ["done", "blocked", "approve", "request_changes"]},
                               "confidence": {"type": "number"}}}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ledger() -> sqlite3.Connection:
    db = ROOT / "state" / "agent_os.db"
    db.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
        create table if not exists tasks(id text primary key, created text, task text, repo text, level int,
            reason text, agent text, state text, worktree text, branch text, base text, tests_before int,
            tests_after int, diff_stat text, review text, context text, error text, error_class text);
        create table if not exists events(task text, at text, state text, detail text);
        create table if not exists calls(task text, at text, role text, agent text, ok int, seconds real,
            tokens_in int, tokens_out int, cost_est_usd real, prompt_version text, error text, data text);
        create table if not exists actions(at text, what text, detail text);
        create table if not exists jobs(id text primary key, created text, input text, kind text, route text,
            why text, state text, output text, error text, task text, files text, seconds real);
        create table if not exists summaries(thread text, upto int, summary text, primary key (thread, upto));""")
    # HyperDesk conversations; said = the whole input typed, attach = files Ben attached (added after the table shipped)
    for column in ("thread", "batch", "said", "attach"):
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute(f"alter table jobs add column {column} text")
    return con


def update_task(con: sqlite3.Connection, task_id: str, **fields) -> None:
    con.execute(f"update tasks set {', '.join(f'{k} = ?' for k in fields)} where id = ?", [*fields.values(), task_id])
    con.commit()


def set_state(con: sqlite3.Connection, task_id: str, state: str, detail: str = "", **fields) -> None:
    """Committed at once, so after a crash the ledger shows the last state reached and its evidence.
    A task that now needs Ben (waiting for his merge, or failed) also raises a Windows notification."""
    assert state in STATES, state
    con.execute("insert into events values (?, ?, ?, ?)", (task_id, now(), state, detail))
    update_task(con, task_id, state=state, **fields)
    print(f"[{task_id}] {state:<17} {detail}", flush=True)
    if state in ("AWAITING_APPROVAL", "FAILED"):
        task = con.execute("select task from tasks where id = ?", (task_id,)).fetchone()[0]
        notify("Task ready for your approval" if state == "AWAITING_APPROVAL" else "Task failed",
               f"{task}\n{detail}")


# Windows notifications. The AppUserModelID is set on the HyperDesk Start-menu shortcut (make-shortcuts.ps1), so
# Windows shows HyperDesk's name and logo (and a click should launch that shortcut).
TOAST_APP = "SILVADES.HyperDesk"
NOTIFY = True  # the tests switch this off


def notify(title: str, body: str) -> None:
    """Fire and forget: a missing notification must never stop a task."""
    if not NOTIFY or os.name != "nt":
        return
    import html
    logo = (ROOT / "assets" / "hyperdesk-256.png").as_uri()
    xml = (f'<toast><visual><binding template="ToastGeneric"><text>{html.escape(title[:120])}</text>'
           f'<text>{html.escape(body[:300])}</text><image placement="appLogoOverride" src="{logo}"/>'
           '</binding></visual></toast>')
    script = ("[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime];"
              "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime];"
              "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:AGENT_OS_TOAST);"
              f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_APP}')"
              ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    with contextlib.suppress(OSError):  # the XML goes in through the environment: no quoting to get wrong
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         env={**os.environ, "AGENT_OS_TOAST": xml}, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def record_call(con: sqlite3.Connection, task_id: str, role: str, agent: str, r: dict, version: str) -> None:
    u = r["usage"]
    con.execute("insert into calls values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, now(), role, agent, int(r["ok"]), r["seconds"], u.get("in"), u.get("out"),
                 u.get("cost_est_usd"), version, r["error"], json.dumps(r["data"])))
    con.commit()


def load_prompt(name: str) -> tuple[str, str]:
    """A role prompt and its version (content hash), which goes into the ledger with every call."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return text, hashlib.sha1(text.encode()).hexdigest()[:8]


def cap_reached(con: sqlite3.Connection, cfg: dict) -> str:
    """The refusal if today's claude -p plus codex exec calls have reached the cap, else ''."""
    cap = cfg["agents"]["max_calls_per_day"]
    used = con.execute("select count(*) from calls where agent in ('claude', 'codex') "
                       "and at >= date('now', 'localtime')").fetchone()[0]
    return f"daily cap of {cap} agent calls reached (config.toml [agents])" if used >= cap else ""


def call_agent(con: sqlite3.Connection, cfg: dict, task_id: str, role: str, agent: str, prompt: str,
               version: str, cwd: Path, write: bool = False, session: str = "") -> dict:
    """One claude -p or codex exec call (refused past the daily cap) or opencode on the free tier or this PC
    (LOCAL_AGENT; neither capped). Recorded in the ledger."""
    if agent in ("opencode", LOCAL_AGENT):  # implementer only: no read-only mode or schema, so it never reviews or plans
        r = ask_opencode(cfg, prompt, cwd, session=session,
                         model="ollama/" + LOCAL_AGENT.removeprefix("local:") if agent == LOCAL_AGENT else "")
    elif refusal := cap_reached(con, cfg):
        return result(time.monotonic(), error=refusal)
    else:
        r = (ask_claude if agent == "claude" else ask_codex)(prompt, AGENT_SCHEMA, cwd,
                                                             timeout=1200 if write else 600, write=write, session=session)
    record_call(con, task_id, role, agent, r, version)
    return r


def error_class(error: str) -> str:
    if "daily cap" in error:
        return "USER_ACTION_REQUIRED"
    if "timed out" in error:
        return "TRANSIENT"
    if "tier unavailable" in error or "stopped: game running" in error:  # OmniRoute off, a game, little VRAM: this PC
        return "ENVIRONMENT"
    return "TOOL" if "not installed" in error else "MODEL"


def vram_free_mib(cfg: dict) -> int | None:
    """VRAM a local model can use now, or None without a reading. VRAM Ollama already holds counts as free:
    it keeps one model loaded and swaps it on demand."""
    code, out = run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    if code != 0 or not first_line(out).strip().isdigit():
        return None
    try:
        held = sum(m.get("size_vram", 0) for m in get_json(cfg["ollama"]["url"] + "/api/ps")["models"]) // 2**20
    except (OSError, ValueError, KeyError):
        held = 0
    return int(first_line(out)) + held


def local_unavailable(cfg: dict, need_mib: int = 0) -> str:
    """Why the local tier must be skipped right now, or ''. need_mib: a model's VRAM need above the usual floor."""
    game = game_running(cfg)
    if game:
        return f"game running: {game}"
    try:  # before nvidia-smi: a down or empty Ollama needs no VRAM reading
        if not get_json(cfg["ollama"]["url"] + "/api/tags")["models"]:
            return "Ollama lists no models (wrong OLLAMA_MODELS folder?): quit Ollama from the tray and start it again"
    except (OSError, ValueError, KeyError, TypeError):
        return f"Ollama is not answering at {cfg['ollama']['url']}"
    free = vram_free_mib(cfg)
    # ponytail: one floor for all local models (config.toml); per-alias numbers if the tier ever mixes very different sizes.
    floor = max(need_mib, cfg["gpu"]["min_free_vram_mib"])
    return f"only {free} MiB VRAM free, {floor} needed" if free is not None and free < floor else ""


def classify(cfg: dict, task: str) -> tuple[int, str]:
    """Rule table first. Only when no rule matches does the local model decide; failing that, level 2."""
    for level, rule in LEVEL_RULES:
        if m := rule.search(task.lower()):
            return level, f"rule '{m.group(0)}'"
    why = local_unavailable(cfg)
    if not why:
        alias = cfg["ollama"]["classify_model"]
        r = ask_local(cfg, alias, CLASSIFY_PROMPT.format(task=task), schema=LEVEL_SCHEMA)
        if r["ok"] and r["data"].get("level") in (0, 1, 2, 3):
            return r["data"]["level"], f"local:{alias}: {r['data'].get('reason', '')[:200]}"
        why = r["error"] or "unexpected reply"
    return 2, f"no rule matched, local model unavailable ({why}); default"


def run_task(cfg: dict, task: str, repo: Path, level: int | None = None, no_paid: bool = False) -> int:
    """no_paid: the free tier and this PC only (opencode, then the local reviewer); no Claude or Codex call."""
    con = ledger()
    task_id = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() // 1_000_000 % 1000:03d}"
    con.execute("insert into tasks (id, created, task, repo) values (?, ?, ?, ?)", (task_id, now(), task, str(repo)))
    set_state(con, task_id, "RECEIVED", task)

    def git(where: Path, *args: str) -> tuple[int, str]:
        return run(["git", "-C", str(where), *args], timeout=120)

    def fail(error: str, cls: str, **fields) -> int:
        set_state(con, task_id, "FAILED", f"{cls}: {error}", error=error, error_class=cls, **fields)
        return 1

    code, top = git(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        return fail(f"not a git repository: {repo}", "ENVIRONMENT")
    repo, workspace = Path(top).resolve(), (ROOT.parent / "workspace").resolve()
    if not (repo.is_relative_to(workspace) or repo == ROOT):  # Agent OS itself: its worktree lives in workspace too
        return fail(f"{repo} is outside {workspace}, the approved agent boundary", "PERMISSION")
    level, reason = (level, "--level") if level is not None else classify(cfg, task)
    if no_paid and level == 3:  # the architect is a paid call
        level, reason = 2, f"{reason}; level 3 runs as 2 without paid calls"
    set_state(con, task_id, "CLASSIFIED", f"level {level}, {reason}", level=level, reason=reason, repo=str(repo))
    if level >= 2 and git(repo, "status", "--porcelain")[1]:
        return fail("the repo has uncommitted changes; commit or stash them (the worktree starts from HEAD)",
                    "USER_ACTION_REQUIRED")

    set_state(con, task_id, "CONTEXT_BUILDING")
    test_cmd = test_command(repo)
    context, tests_before = build_context(repo, task, test_cmd, CONTEXT_BUDGET)
    update_task(con, task_id, context=context, tests_before=tests_before)
    if level == 0:
        print(context)
        set_state(con, task_id, "COMPLETED", "deterministic evidence only, no model")
        return 0
    if level == 1:
        if why := local_unavailable(cfg):
            return fail(f"local tier unavailable ({why}); run again later, or with --level 2", "ENVIRONMENT")
        alias = cfg["ollama"]["answer_model"]
        r = ask_local(cfg, alias, ANSWER_PROMPT.format(task=task, context=context), schema=SMOKE_SCHEMA)  # {answer}
        record_call(con, task_id, "answer", f"local:{alias}", r, "")
        if not r["ok"]:
            return fail(r["error"], "MODEL")
        print(r["data"]["answer"])
        set_state(con, task_id, "COMPLETED", f"answered by local:{alias}", agent=f"local:{alias}")
        return 0

    implementer = "opencode" if no_paid else cfg["agents"]["implementer"]
    other = "codex" if implementer == "claude" else "claude"  # opencode's fallback and reviewer: Claude
    # no_paid (self-repair): opencode on the free tier, then on this PC. Otherwise the free tier, then the other vendor:
    # this PC's 11.5 GB qwen-agent runs only where HyperDesk holds its GPU lane for it (self-changes).
    on_free = implementer == "opencode" and cfg["agents"].get("opencode_model", "omniroute/free").startswith("omniroute/")
    chain = [implementer, *([LOCAL_AGENT] if on_free and no_paid else []), *([] if no_paid else [other])]
    plan = ""
    if level == 3:
        set_state(con, task_id, "PLANNING", f"architect: {other}")
        text, version = load_prompt("architect")
        r = call_agent(con, cfg, task_id, "architect", other, f"{text}\n\nTask: {task}\n\n{context}", version, repo)
        if not r["ok"]:
            return fail(f"{other}: {r['error']}", error_class(r["error"]))
        plan = "\n".join(f"{i}. {step}" for i, step in enumerate(r["data"]["plan"], 1))

    base = git(repo, "rev-parse", "HEAD")[1]
    branch, wt = f"agent-os/{task_id}", workspace / "worktrees" / f"{repo.name}-{task_id}"
    code, out = git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base)
    if code != 0:
        return fail(f"git worktree add: {out}", "TOOL")
    update_task(con, task_id, worktree=str(wt), branch=branch, base=base)
    text, version = load_prompt("implementer")
    prompt = (f"{text}\n\nTask: {task}\nTest command: python {' '.join(test_cmd[1:])}\n"
              + (f"\nPlan from the architect:\n{plan}\n" if plan else "") + f"\n{context}")

    def fail_clean(error: str, cls: str) -> int:  # an untouched worktree is no evidence: remove it with its branch
        if not git(wt, "status", "--porcelain")[1] and git(wt, "rev-parse", "HEAD")[1] == base:
            drop_worktree(str(repo), str(wt), branch, merged=False)
            return fail(error, cls, worktree=None, branch=None)
        return fail(error, cls)

    kept = None  # the last free or local reply that ended fine but changed nothing
    for i, agent in enumerate(chain):  # each fallback starts from a clean worktree
        if i and agent == LOCAL_AGENT and local_unavailable(cfg, LOCAL_AGENT_MIB):
            agent = chain[i - 1]  # refused before it ran: the attempt before keeps its edits and its error
            continue
        if i:
            git(wt, "reset", "-q", "--hard", base)
            git(wt, "clean", "-fdq")
        set_state(con, task_id, "EXECUTING", f"implementer: {agent}, worktree {wt}", agent=agent)
        r = call_agent(con, cfg, task_id, "implementer", agent, prompt, version, wt, write=True)
        if error_class(r["error"]) == "USER_ACTION_REQUIRED" and not kept or r["ok"] and (
                agent not in ("opencode", LOCAL_AGENT) or git(wt, "status", "--porcelain")[1]):
            break  # a free or local reply that changed no file moves on; a paid agent gets its one attempt
        kept = (agent, r) if r["ok"] else kept
    else:  # no later step could run (local tier off, say) and left nothing behind: the empty reply gets the repair
        if kept and not r["ok"] and not git(wt, "status", "--porcelain")[1]:
            agent, r = kept
            update_task(con, task_id, agent=agent)
    if not r["ok"]:
        return fail_clean(f"{agent}: {r['error']}", error_class(r["error"]))

    def validate() -> tuple[list[str], int, int, str]:
        """Commit whatever the agent left, then the controller runs the tests itself."""
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=Agent OS", "-c", "user.email=agent-os@localhost", "commit", "-q", "-m",
            f"agent-os {task_id}: {task[:72]}")
        rows = [line.split("\t") for line in git(wt, "diff", "--numstat", base)[1].splitlines()]
        lines = sum(int(row[0]) + int(row[1]) for row in rows if row[0].isdigit() and row[1].isdigit())
        set_state(con, task_id, "VALIDATING", f"{len(rows)} files, {lines} lines changed")
        return [row[-1] for row in rows], lines, *run_tests(wt, test_cmd)

    files, lines, tests_after, test_out = validate()
    unfinished = not files and (r["data"] or {}).get("verdict") == "unfinished"  # ended its turn before its final JSON
    if files and tests_after != 0 or unfinished:
        fix = ("You stopped before changing any file. Make the change now, then run the test command." if unfinished
               else f"Your change is committed, but the tests fail:\n{test_out[-6000:]}\n\nFix that.")
        # Resuming the implementer's session reuses its cached context: only the failure is sent again.
        set_state(con, task_id, "EXECUTING", f"repair round by {agent}" + (" (resumed)" if r["session"] else ""))
        call_agent(con, cfg, task_id, "repair", agent, fix if r["session"] else f"{prompt}\n\n{fix}", version, wt,
                   write=True, session=r["session"])
        files, lines, tests_after, test_out = validate()
    stat = git(wt, "diff", "--stat", base)[1]
    if not files:
        return fail_clean(f"{agent} changed nothing: {(r['data'] or {}).get('summary', '')[:300]}", "MODEL")
    if tests_after != 0:
        return fail(f"tests still fail after one repair round (exit {tests_after})", "VALIDATION",
                    tests_after=tests_after, diff_stat=stat)

    review, findings = "skipped: small change, nothing security-sensitive", []
    if lines > REVIEW_OVER_LINES or RISKY.search(task + " " + " ".join(files)):
        local = "local:code"  # this PC's coding model (32K context: the ~65K-char prompt below fits)
        reviewer = local if no_paid else other if agent in (implementer, LOCAL_AGENT) else \
            implementer if implementer != "opencode" else "codex"
        set_state(con, task_id, "REVIEWING", f"reviewer: {reviewer} (sees task, diff, tests; not the implementer's notes)")
        text, rversion = load_prompt("reviewer")
        ask = (f"{text}\n\nTask: {task}\n\nDiff:\n{git(wt, 'diff', base)[1][:60000]}\n\n"
               f"Test output (exit 0):\n{test_out[-4000:]}")
        r = None if no_paid else call_agent(con, cfg, task_id, "reviewer", reviewer, ask, rversion, wt)
        if r is None:  # --no-paid: this PC reviews (HyperDesk holds the GPU lane for self-changes; a workspace task has
            if why := local_unavailable(cfg):  # none, so a failed paid review there stays "failed", not a local load)
                mine = result(time.monotonic(), error=f"local tier unavailable ({why})")
            else:
                mine = ask_local(cfg, local.removeprefix("local:"), ask, schema=AGENT_SCHEMA, keep_alive=0,
                                 timeout=600, num_predict=1500)
                record_call(con, task_id, "reviewer", local, mine, rversion)
            reviewer, r = local, mine
        review = f"{reviewer}: {r['data']['verdict']}" if r["ok"] else f"{reviewer} failed: {r['error'][:200]}"
        findings = r["data"]["findings"] + r["data"]["risks"] if r["ok"] else []
    set_state(con, task_id, "AWAITING_APPROVAL", f"tests pass; review {review}", tests_after=0, diff_stat=stat,
              review=json.dumps({"verdict": review, "findings": findings}))
    print(f"\n{stat}\ntests: exit {tests_before} before, 0 after\nreview: {review}")
    for f in findings:
        print(f"  - {f}")
    print(f"branch {branch} in {wt}\n  merge:   agent-os merge {task_id}\n  discard: agent-os rollback {task_id}")
    return 0


def research(cfg: dict, question: str, show_browser: bool = False) -> int:
    """One web question: a ledger task (repo 'web'), one researcher call, the answer with its sources."""
    con = ledger()
    task_id = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() // 1_000_000 % 1000:03d}"
    con.execute("insert into tasks (id, created, task, repo) values (?, ?, ?, 'web')", (task_id, now(), question))
    set_state(con, task_id, "RECEIVED", question)
    if refusal := cap_reached(con, cfg):
        set_state(con, task_id, "FAILED", f"USER_ACTION_REQUIRED: {refusal}", error=refusal,
                  error_class="USER_ACTION_REQUIRED")
        return 1
    work = ROOT / "state" / "research" / task_id  # the browser's downloads and snapshots, if any
    work.mkdir(parents=True, exist_ok=True)
    set_state(con, task_id, "EXECUTING", f"researcher: claude, browser {'shown' if show_browser else 'hidden'}",
              agent="claude")
    text, version = load_prompt("researcher")
    r = ask_researcher(f"{text}\n\nQuestion: {question}", work, task_id, show_browser)
    record_call(con, task_id, "researcher", "claude", r, version)
    if not r["ok"]:
        set_state(con, task_id, "FAILED", f"{error_class(r['error'])}: {r['error'][:300]}", error=r["error"],
                  error_class=error_class(r["error"]))
        return 1
    print(f"\n{r['data']['answer']}\n")
    for s in r["data"]["sources"]:
        print(f"  - {s['title']}: {s['url']}")
    set_state(con, task_id, "COMPLETED", f"answered with {len(r['data']['sources'])} sources")
    notify("Research done", question)
    return 0


# Ledger commands. merge and rollback are Ben's to run; the controller never calls them itself.

def find_task(con: sqlite3.Connection, ref: str) -> str:
    """Task id from an exact id, any unique part of one, or 'last'. '' (and a message) if none or ambiguous."""
    sql = "select id from tasks order by id desc limit 1" if ref == "last" else "select id from tasks where id like ?"
    ids = [i for (i,) in con.execute(sql, () if ref == "last" else (f"%{ref}%",))]
    if len(ids) != 1:
        print(f"no task matches {ref!r}" if not ids else f"{ref!r} matches {len(ids)} tasks: {', '.join(ids[:5])}")
        return ""
    return ids[0]


def print_table(con: sqlite3.Connection, sql: str, args: tuple = ()) -> None:
    cur = con.execute(sql, args)
    rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
    heads = [d[0] for d in cur.description]
    widths = [max(map(len, col)) for col in zip(heads, *rows)]
    for row in (heads, *rows):
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())


def drop_worktree(repo: str, wt: str, branch: str, merged: bool) -> str:
    """Remove a task's worktree and branch. Returns '' or the error. -d (safe) after a merge, -D for a rollback."""
    run(["git", "-C", repo, "worktree", "remove", "--force", wt], timeout=120)
    run(["git", "-C", repo, "worktree", "prune"])
    code, out = run(["git", "-C", repo, "branch", "-d" if merged else "-D", branch])
    still_there = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])[0] == 0
    return out if code != 0 and still_there else ""


GIT_ID = ["-c", "user.name=Agent OS", "-c", "user.email=agent-os@localhost"]


def clean_tree(repo: str) -> str:
    """'' when the repo's tree is clean, else why not. Agent OS itself first saves Ben's edits and the daily
    update's install-manifest.json as a recoverable version (self_edit's checkpoint): Ben does not use git."""
    if Path(repo).resolve() == ROOT:
        import self_edit  # lazy: it imports this module
        try:
            self_edit.checkpoint(ROOT)
        except ValueError as e:
            return str(e)
        return ""
    return f"{repo} has uncommitted changes; commit or stash them first" if run(
        ["git", "-C", repo, "status", "--porcelain"])[1] else ""


def merge_task(con: sqlite3.Connection, task_id: str) -> int:
    state, repo, wt, branch = con.execute("select state, repo, worktree, branch from tasks where id = ?",
                                          (task_id,)).fetchone()
    if state != "AWAITING_APPROVAL":
        print(f"{task_id} is {state}; only a task AWAITING_APPROVAL can be merged")
        return 1
    if why := clean_tree(repo):
        print(why)
        return 1
    how = "fast-forwarded"
    code, out = run(["git", "-C", repo, "merge", "--ff-only", "-q", branch], timeout=120)
    if code != 0:  # the repo moved on since the task began: merge without committing, test, and keep it only if green
        how = "merged"
        code, out = run(["git", "-C", repo, *GIT_ID, "merge", "--no-ff", "--no-commit", "-q", branch], timeout=120)
        if code == 0:
            code, out = run_tests(Path(repo), test_command(Path(repo)))
        if code == 0:
            code, out = run(["git", "-C", repo, *GIT_ID, "commit", "-q", "--no-edit"], timeout=120)
        if code != 0:
            run(["git", "-C", repo, "merge", "--abort"], timeout=120)  # the tree is as it was before the merge
            print("This change clashes with changes made after it was prepared, so nothing was applied. "
                  f"Discard it and ask again.\n{out[-1500:]}")
            return 1
    target = run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"])[1]
    error = drop_worktree(repo, wt, branch, merged=True)
    set_state(con, task_id, "COMPLETED", f"{how} {target}" + (f"; cleanup: {error}" if error else ""))
    return 0


def undo_task(con: sqlite3.Connection, task_id: str) -> int:
    """Undo an applied task: new commits revert its own (found by their subject), so nothing is deleted."""
    state, repo = con.execute("select state, repo from tasks where id = ?", (task_id,)).fetchone()
    if state != "COMPLETED":
        print(f"{task_id} is {state}; only an applied (COMPLETED) task can be undone")
        return 1
    prefix = f"agent-os {task_id}:"
    code, out = run(["git", "-C", repo, "log", "--format=%H %s", "--fixed-strings", f"--grep={prefix}"], timeout=120)
    shas = [sha for sha, _, subject in (line.partition(" ") for line in out.splitlines())  # newest first
            if subject.startswith(prefix)] if code == 0 else []
    if not shas:
        print(f"{task_id} has no commits in {repo}: nothing to undo")
        return 1
    if why := clean_tree(repo):
        print(why)
        return 1
    code, out = run(["git", "-C", repo, *GIT_ID, "revert", "--no-edit", *shas], timeout=120)
    if code != 0:
        run(["git", "-C", repo, "revert", "--abort"], timeout=120)
        print(f"Later changes touch the same lines, so {task_id} cannot be undone by itself. Your files are "
              f"unchanged.\n{first_line(out)}")
        return 1
    set_state(con, task_id, "ROLLED_BACK", f"undone: reverted {len(shas)} commit(s)")
    return 0


def last_merged(con: sqlite3.Connection, repo: str = "") -> str:
    """The newest applied task (in `repo` when given), or ''."""
    row = con.execute("select t.id from tasks t join events e on e.task = t.id where e.state = 'COMPLETED' and "
                      "t.state = 'COMPLETED' and "  # an undone task keeps its event: skip it, so Undo reaches the one before
                      "(e.detail like 'fast-forwarded%' or e.detail like 'merged%') and (? = '' or t.repo = ?) "
                      "order by e.rowid desc limit 1", (repo, repo)).fetchone()  # applied last, not created last
    return row[0] if row else ""


def rollback_task(con: sqlite3.Connection, task_id: str) -> int:
    state, repo, wt, branch, created = con.execute("select state, repo, worktree, branch, created from tasks "
                                                   "where id = ?", (task_id,)).fetchone()
    if state == "COMPLETED":
        print(f"{task_id} is COMPLETED. If it was merged, undo it with: agent-os undo {task_id}")
        return 1
    if state == "ROLLED_BACK":
        print(f"{task_id} is already rolled back")
        return 0
    stale = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - STALE_HOURS * 3600))
    if state in BUSY_STATES and created > stale:  # discarding under a running agent would leave it half-written
        print(f"{task_id} is {state} and may still be running; it can be discarded after {STALE_HOURS} hours")
        return 1
    error = drop_worktree(repo, wt, branch, merged=False) if branch else ""
    if error:
        print(f"could not delete branch {branch}: {error}")
        return 1
    set_state(con, task_id, "ROLLED_BACK", f"removed worktree and branch {branch}" if branch else "nothing to remove")
    return 0


def inspect_task(con: sqlite3.Connection, task_id: str) -> None:
    cur = con.execute("select * from tasks where id = ?", (task_id,))
    task = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    context = task.pop("context") or ""
    for k, v in task.items():
        print(f"{k:<12} {v}")
    print()
    print_table(con, "select at, state, detail from events where task = ? order by rowid", (task_id,))
    print()
    print_table(con, "select at, role, agent, ok, seconds, tokens_in, tokens_out, cost_est_usd, prompt_version, error "
                     "from calls where task = ? order by rowid", (task_id,))
    for role, agent, data in con.execute("select role, agent, data from calls where task = ? order by rowid", (task_id,)):
        reply = json.loads(data) if data else None
        if isinstance(reply, dict):
            print(f"\n{role} ({agent}): {reply.get('verdict') or ''} {reply.get('summary') or reply.get('answer', '')}")
            for line in (x for k in ("findings", "risks", "plan") if isinstance(reply.get(k), list) for x in reply[k]):
                print(f"  - {line}")  # isinstance: older free-model replies stored a string here
    print(f"\ncontext given to the agents ({len(context)} chars):\n{context}")


def show_status(con: sqlite3.Connection, cfg: dict) -> None:
    print_table(con, "select id, state, level, agent, substr(task, 1, 60) task from tasks where state not in "
                     "('COMPLETED', 'ROLLED_BACK') and not (state = 'FAILED' and worktree is null) order by id")
    used = con.execute("select count(*) from calls where agent in ('claude', 'codex') "
                       "and at >= date('now', 'localtime')").fetchone()[0]
    print(f"\nagent calls today: {used} of {cfg['agents']['max_calls_per_day']}")


def show_usage(con: sqlite3.Connection, days: int) -> None:
    print_table(con, "select date(at) day, case when agent like 'local:%' then 'local' when agent in ('free', 'opencode') "
                     "then 'free' when agent = 'gemini' then 'API' else 'subscription' end kind, "
                     "agent, count(*) calls, sum(ok) ok, round(sum(seconds), 1) seconds, sum(tokens_in) tokens_in, "
                     "sum(tokens_out) tokens_out, round(sum(cost_est_usd), 2) api_rate_usd from calls "
                     "where at >= date('now', 'localtime', ?) group by day, agent order by day, agent",
                (f"-{days - 1} days",))
    print("\napi_rate_usd is Claude Code's own estimate at API prices; on the subscription it is usage, not a charge."
          "\nGemini API billing is controlled by your Google project; Gemini cost is not estimated here.")


# Local-tier benchmark: the two jobs the local model really does, with the production prompts.
BENCH_CLASSIFY = [  # (expected level, task); none matches the rule table, since only rule misses reach the model
    (0, "Find every TODO comment"), (0, "Locate all calls to check_password"), (0, "Do the unit tests pass?"),
    (1, "Give an overview of the auth module"), (1, "Is the password hashing salted?"),
    (1, "Does logout invalidate the token?"),
    (2, "Session tokens never expire; make them expire after an hour"), (2, "Users can log in with any password"),
    (2, "Lower the PBKDF2 iterations to 50,000"), (2, "Expired sessions are still accepted"),
    (3, "Move session storage from memory to SQLite"),
    (3, "Split the auth module into a package with separate session and user services"),
    (3, "Introduce OAuth login next to passwords"),
]
BENCH_ANSWER = [  # (question about workspace\authdemo, strings the answer must contain, any case)
    ("Which function decides whether a session is still valid, and in which file?", ["is_session_valid", "auth.py"]),
    ("How many seconds does a session last?", ["3600"]),
    ("Which key-derivation function protects stored passwords?", ["pbkdf2"]),
    ("Which tests fail right now?", ["test_expired_session_is_rejected", "test_fresh_session_identifies_user"]),
]


def bench(cfg: dict) -> int:
    """Each local-tier candidate on classification tie-breaks and evidence answers. Writes logs\\bench.txt."""
    if why := local_unavailable(cfg):
        print(f"local tier unavailable: {why}")
        return 1
    repo = ROOT.parent / "workspace" / "authdemo"
    if not repo.is_dir():
        print(f"the answer cases need the fixture repo {repo}")
        return 1
    assert not any(rule.search(t.lower()) for _, t in BENCH_CLASSIFY for _, rule in LEVEL_RULES)
    contexts = [build_context(repo, q, test_command(repo), CONTEXT_BUDGET)[0] for q, _ in BENCH_ANSWER]
    lines = [f"Agent OS local bench  {now()}  ({len(BENCH_CLASSIFY)} classify, {len(BENCH_ANSWER)} answer cases)"]
    for alias in cfg["ollama"]["local_tier"]:
        ask_local(cfg, alias, "Reply OK.")  # load first, so no case is timed cold
        seconds, misses, invalid, right = [], [], 0, {"classify": 0, "answer": 0}
        cases = [("classify", CLASSIFY_PROMPT.format(task=t), LEVEL_SCHEMA, level, t) for level, t in BENCH_CLASSIFY]
        cases += [("answer", ANSWER_PROMPT.format(task=q, context=c), SMOKE_SCHEMA, want, q)
                  for (q, want), c in zip(BENCH_ANSWER, contexts)]
        for kind, prompt, schema, want, label in cases:
            r = ask_local(cfg, alias, prompt, schema=schema)
            seconds.append(r["seconds"])
            got = r["data"].get("level" if kind == "classify" else "answer") if isinstance(r["data"], dict) else None
            if got is None:
                invalid += 1
                misses.append(f"{kind}: {label} -> invalid reply {r['error'][:80]}")
            elif (got == want) if kind == "classify" else all(w.lower() in str(got).lower() for w in want):
                right[kind] += 1
            else:
                misses.append(f"{kind}: {label} -> {str(got)[:120]!r}")
        ask_local(cfg, alias, "Reply OK.", keep_alive=0)  # unload: the GPU is shared with games
        lines.append(f"{alias:<6} classify {right['classify']}/{len(BENCH_CLASSIFY)}  answer {right['answer']}/"
                     f"{len(BENCH_ANSWER)}  invalid JSON {invalid}  median {sorted(seconds)[len(seconds) // 2]}s  "
                     f"max {max(seconds)}s")
        lines += [f"         miss  {m}" for m in misses]
        print("\n".join(lines[-1 - len(misses):]), flush=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "bench.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


IMPROVE_DAYS = 7
LIMIT_ERROR = re.compile(r"session limit|usage limit|rate limit|at capacity|quota|\b429\b", re.I)
# Ben's to act on, not a bug (his uncommitted work, a guard that refused, a tier that is off): shown, no Fix task
NOT_CODE = re.compile(r"USER_ACTION_REQUIRED|tier unavailable|not installed|not answering|not on this PC|game running|"
                      r"not running|cancelled or cleared|timed out|being prepared|needs? review|daily cap", re.I)
SELF_FIX = "In Agent OS (this repo: agent_os.py, hyperdesk.pyw, hyperdesk.html), "


def improve(cfg: dict) -> int:
    """Self-check with no model calls: what failed, was asked again, or cost cloud tokens in the last week; where code
    can fix it, with a ready task Ben can run on Agent OS itself (HyperDesk: System, Improve; `run --repo` this folder,
    ends at his merge). The daily update runs it, so the list is fresh each morning. Writes logs\\improve.md,
    state\\improve.json."""
    found, since = [], f"-{IMPROVE_DAYS} days"

    def add(what: str, evidence: str, task: str = "") -> None:
        found.append({"what": what, "evidence": evidence.strip()[:700], "task": task and SELF_FIX + task})

    code, out = (0, "") if "share" in cfg else run_tests(ROOT, test_command(ROOT))  # the shared zip ships no tests
    if code:  # the names go in the task too: after a flake, run_task's own test run passes and shows nothing
        names = "\n".join(re.findall(r"^(?:FAIL|ERROR): .*", out, re.M)) or out[-300:]
        if run_tests(ROOT, test_command(ROOT))[0] == 0:  # ponytail: one re-run tells a flake from a break
            add("Agent OS tests are flaky", f"{names}\n{out[-400:]}", f"these unit tests failed once and passed on a "
                f"re-run:\n{names}\nFind the race or order dependence in them and make them deterministic without "
                "weakening them.")
        else:
            add("Agent OS tests fail", f"{names}\n{out[-400:]}",
                f"make the failing unit tests pass without weakening them:\n{names}")
    for name, status, detail, fix in doctor_checks(cfg):
        if status == FAIL:
            add(f"doctor: {name} fails", f"{detail}. {fix}")  # this PC's setup: Ben's to fix, not a code task
    log = ROOT / "logs" / "update.txt"
    if bad := [x for x in (log.read_text(encoding="utf-8").splitlines() if log.is_file() else []) if x.startswith("FAIL")]:
        add("the last update failed", "\n".join(bad), "the last `agent-os update` (logs\\update.txt) reported:\n"
            + "\n".join(bad) + "\nFind why in update() / set_version() and fix it, with a unit test. Do not run the "
            "update itself: it changes installed tools.")
    hist = ROOT / "logs" / "update-history.txt"
    runs = [b for b in (hist.read_text(encoding="utf-8", errors="replace").split("\n\n") if hist.is_file() else [])
            if b.startswith("Agent OS update")][-3:]
    for u in UPDATES:  # one skip is the lock-safety rule working; three in a row and that tool never updates
        if len(runs) == 3 and len(skips := [x for b in runs for x in b.splitlines()
                                            if x.startswith(f"SKIP  {u['name']} ")]) == 3:
            add(f"{u['name']} was skipped by the last 3 updates", f"{skips[-1]}\nClose what it names, then System, "
                "Update now.")  # Ben's setup, not code
    with contextlib.closing(ledger()) as con:
        q = lambda sql: con.execute(sql, (since,)).fetchall()  # noqa: E731
        # Fixes Ben merged (and did not undo): a failure their task quotes is not offered again unless it recurs.
        merged = [(t, at) for t, at in con.execute(
            "select t.task, max(e.at) from tasks t join events e on e.task = t.id where t.state = 'COMPLETED' and "
            "e.state = 'COMPLETED' and (e.detail like 'fast-forwarded%' or e.detail like 'merged%') group by t.id")
            if (t or "").startswith(SELF_FIX)]
        fixed = lambda text, last: any(repr(text) in t and at > last for t, at in merged)  # noqa: E731
        jobs, seen = collections.Counter(), {}
        for error, sample, at in q("select error, input, created from jobs where state = 'failed' "
                                   "and created >= datetime('now', 'localtime', ?) order by created"):
            error = re.sub(r"^\[[^\]]*\]\s+FAILED\s+", "", error or "")[:160]  # a task id prefix makes each one unique
            jobs[error] += 1
            seen[error] = (sample or "", at)  # the newest
        for error, n in jobs.most_common(5):
            if not fixed(error, (last := seen[error])[1]):
                add(f"{n} HyperDesk job(s) failed", f"{error}\ne.g. {last[0][:160]!r}", "" if NOT_CODE.search(error)
                    else f"HyperDesk jobs failed {n} time(s) this week with: {error!r} (e.g. on {last[0][:160]!r}). "
                    "Find the cause and fix it, with a unit test that reproduces it.")
        errlog, cut = ROOT / "logs" / "hyperdesk-errors.txt", con.execute(
            "select datetime('now', 'localtime', ?)", (since,)).fetchone()[0]
        crashes, newest = collections.Counter(), {}
        for entry in re.split(r"^=== ", errlog.read_text(encoding="utf-8", errors="replace")
                              if errlog.is_file() else "", flags=re.M)[1:]:  # "=== <at> <where>\n<traceback>"
            head, _, trace = entry.partition("\n")
            if head[:19] >= cut and (trace := trace.strip()):
                crash = trace.splitlines()[-1][:200]  # ponytail: grouped by the exception line; one with ids never groups
                crashes[crash] += 1
                newest[crash] = (head[:19], head[20:].strip(), trace[-500:])
        for crash, n in crashes.most_common(3):
            last, place, trace = newest[crash]
            if n > 1 and not fixed(crash, last):  # once can be a fluke; again is a bug
                add(f"HyperDesk's server failed {n} times ({place})", trace,
                    f"HyperDesk's server (hyperdesk.pyw) logged this error {n} times this week, last in {place}: "
                    f"{crash!r}. Traceback:\n{trace}\nFind the cause and fix it, with a unit test that reproduces it.")
        if (crashlog := ROOT / "logs" / "hyperdesk-crash.txt").is_file():  # deleted by the next start that works
            trace = crashlog.read_text(encoding="utf-8", errors="replace").strip()[-700:]
            add("HyperDesk could not start", trace + "\nHome Base can undo the last applied update.",
                f"HyperDesk (hyperdesk.pyw) fails to start with:\n{trace}\nFind the cause and fix it, with a unit test "
                "that reproduces it.")
        failed, limits, last_call = collections.Counter(), collections.Counter(), {}
        for agent, error, at in q("select agent, error, at from calls where ok = 0 and "
                                  "at >= datetime('now', 'localtime', ?) order by at"):
            if "daily cap" in error:
                continue
            if m := LIMIT_ERROR.search(error):
                limits[(agent, m.group(0).lower())] += 1
            else:  # one log file per call: group without it
                key = (agent, re.sub(r"\s*\(full output: [^)]*\)", "", error)[-200:].strip())
                failed[key] += 1
                last_call[key] = at  # the newest
        if limits:  # the vendor's quota, not a bug: nothing to fix in code
            add(f"{sum(limits.values())} cloud call(s) hit a vendor limit", "\n".join(
                f"- {agent} x{n}: {e[:160]}" for (agent, e), n in limits.most_common(4)) + "\nThe free cloud tier "
                "([free]) takes answers when Claude is out; tasks wait for the reset.")
        for (agent, error), n in failed.most_common(4):
            if fixed(error, last_call[(agent, error)]):
                continue
            add(f"{n} failed {agent} call(s)", error, "" if NOT_CODE.search(error) else
                f"{agent} calls failed {n} time(s) this week with: {error!r}. Find the cause and fix it, with a "
                "unit test that reproduces it. The ledger's calls table has the full error, which names the full "
                "output in logs\\agents: read that first.")
        again = q("select input, group_concat(route, ' then ') from (select input, route, coalesce(thread, id) t from "
                  "jobs where kind = 'answer' and created >= datetime('now', 'localtime', ?) order by created) "
                  "group by t, input having count(*) > 1 limit 12")
        if again:
            lines = "\n".join(f"- {text[:120]!r}: {routes}" for text, routes in again)
            add(f"{len(again)} answer(s) asked again on another model", lines,
                "these questions were asked again on a different model, so answer_route sent them to the wrong one "
                f"first:\n{lines}\nAdjust the word lists or thresholds in hyperdesk.pyw so similar questions go where "
                "they were answered the second time, and add them to the routing test.")
        if slow := q("select substr(route, 14), count(*), round(avg(seconds)) from jobs where route like 'answer:local:%' "
                     "and seconds > 90 and created >= datetime('now', 'localtime', ?) group by 1 order by 2 desc"):
            add(f"{sum(n for _, n, _ in slow)} local answer(s) took over 90 s", "\n".join(
                f"- {model} x{n}, avg {s:.0f} s" for model, n, s in slow) + "\nA model larger than the free VRAM runs "
                "partly on the CPU; otherwise model swaps (26-32 s each) or long replies. The Models page shows sizes.")
        spent = q("select count(*), coalesce(sum(tokens_in), 0) from calls where agent = 'claude' and role = 'answer' "
                  "and at >= datetime('now', 'localtime', ?)")[0]
        if spent[0] and (why := free_unavailable(cfg)):
            add(f"{spent[0]} Claude answers ({spent[1] // 1000}K input tokens) could have been free",
                f"The free cloud tier is off ({why}). " + ("Start OmniRoute (Services) before asking." if "not running"
                in why else "Set it up: config.toml [free], then Apps & APIs in HyperDesk."))
        stale = con.execute(f"select id, state from tasks where (state in ({','.join('?' * len(BUSY_STATES))}) or "
                            "state = 'FAILED' and worktree is not null) and created < datetime('now', 'localtime', ?) "
                            "order by id", (*BUSY_STATES, f"-{STALE_HOURS} hours")).fetchall()
        if stale:  # Ben's to discard: nothing runs them any more, and a failed one keeps its worktree for a look
            add(f"{len(stale)} stale task(s)", "\n".join(f"agent-os rollback {i}   ({s})" for i, s in stale[:10]) +
                "\nEach removes that task's worktree and branch; the repo is untouched.")
    lines = [f"# Agent OS: improve  {now()}  (last {IMPROVE_DAYS} days, no model calls)", ""]
    for f in found or [{"what": "Nothing to improve: tests pass, no failures or re-asks this week.", "evidence": "",
                        "task": ""}]:
        lines += [f"## {f['what']}", f["evidence"], *([f"Task: {f['task']}"] if f["task"] else []), ""]
    print("\n".join(lines))
    for path, text in ((ROOT / "logs" / "improve.md", "\n".join(lines)),
                       (ROOT / "state" / "improve.json", json.dumps({"at": now(), "found": found}))):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-os", description="Thin controller for local and cloud coding agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check every tool the controller depends on; writes logs\\doctor.txt")
    sub.add_parser("install", help="install the missing agent CLIs and Graphify (user scope); writes install-manifest.json")
    sub.add_parser("smoke", help="one tiny schema-checked request per tier (local, claude, codex); writes logs\\smoke.txt")
    c = sub.add_parser("context", help="print the evidence an agent would get for a task (runs the repo's tests)")
    c.add_argument("task")
    c.add_argument("--repo", type=Path, default=Path.cwd())
    c.add_argument("--budget", type=int, default=4000, help="tokens")
    r = sub.add_parser("run", help="run a task: classify, build context, implement in a worktree, test, review")
    r.add_argument("task")
    r.add_argument("--repo", type=Path, default=Path.cwd(), help="a git repo inside the workspace folder")
    r.add_argument("--agent", choices=["claude", "codex", "opencode"],
                   help="implementer for this task (default: config.toml); opencode runs on the free tier, uncapped")
    r.add_argument("--level", type=int, choices=range(4), help="skip classification: 0 no model, 1 local, "
                   "2 one agent, 3 plan then implement")
    r.add_argument("--no-paid", action="store_true", help="free tier and this PC only, no Claude or Codex call: "
                   "opencode implements, the local code model reviews, level 3 runs as 2")
    q = sub.add_parser("research", help="answer a question from the web: search, fetch, read-only browser; clicks "
                       "and typing ask you in HyperDesk")
    q.add_argument("question")
    q.add_argument("--show-browser", action="store_true", help="show the browser window instead of running it hidden")
    for name, text in (("merge", "apply a task AWAITING_APPROVAL to the repo (fast-forward, else a tested merge), "
                                 "then remove its worktree"),
                       ("rollback", "discard a task: remove its worktree and branch; the repo is untouched"),
                       ("undo", "undo an applied task with new commits that revert its own; 'last': the newest applied"),
                       ("inspect", "everything recorded for one task, including the context its agents got")):
        sub.add_parser(name, help=text).add_argument("task", help="task id, any unique part of it, or 'last'")
    sub.add_parser("update", help="update the agent tools to their newest release at least 7 days old, check, roll "
                   "back on failure; writes logs\\update.txt").add_argument("--check", action="store_true",
                                                                             help="only show what would change")
    sub.add_parser("improve", help="self-check, no model calls: failures, re-asks and cloud spend this week, each with a "
                   "task to run on Agent OS itself; writes logs\\improve.md")
    sub.add_parser("bench", help="local-tier candidates on classification and evidence answers; writes logs\\bench.txt")
    sub.add_parser("status", help="open tasks and today's agent calls")
    sub.add_parser("history", help="recent tasks").add_argument("-n", type=int, default=20)
    sub.add_parser("usage", help="model calls per day: local vs subscription").add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    if args.cmd == "install":
        return install()
    if args.cmd == "context":
        code, top = run(["git", "-C", str(args.repo), "rev-parse", "--show-toplevel"])
        if code != 0:
            print(f"not a git repository: {args.repo}")
            return 2
        repo = Path(top)
        print(build_context(repo, args.task, test_command(repo), args.budget)[0])
        return 0
    if args.cmd == "update":
        if "share" in load_config():  # update() is Ben's toolchain policy (npm, uv, his agents): not on a friend's PC
            print("Updates are off in the shared build: install a newer HyperDesk OS zip.")
            return 0
        code = update(load_config(), args.check)
        if code is None:  # another update holds the lock and runs the self-check after itself
            return 0
        if not args.check:  # the daily task: a fresh self-check each morning, no model calls
            improve(load_config())
        return code
    if args.cmd == "improve":
        return improve(load_config())
    if args.cmd == "research":
        return research(load_config(), args.question, args.show_browser)
    if args.cmd == "run":
        cfg = load_config()
        cfg["agents"]["implementer"] = args.agent or cfg["agents"]["implementer"]
        return run_task(cfg, args.task, args.repo, args.level, args.no_paid)
    if args.cmd in ("merge", "rollback", "undo", "inspect", "status", "history", "usage"):
        with contextlib.closing(ledger()) as con:
            if args.cmd == "status":
                show_status(con, load_config())
            elif args.cmd == "history":
                print_table(con, "select id, created, state, level, agent, tests_before before, tests_after after, "
                                 "substr(task, 1, 60) task from tasks order by id desc limit ?", (args.n,))
            elif args.cmd == "usage":
                show_usage(con, args.days)
            elif args.cmd == "undo" and args.task == "last" and not last_merged(con):
                print("no applied task to undo")
                return 1
            elif not (task_id := last_merged(con) if args.cmd == "undo" and args.task == "last"
                      else find_task(con, args.task)):
                return 1
            elif args.cmd == "inspect":
                inspect_task(con, task_id)
            else:
                return {"merge": merge_task, "rollback": rollback_task, "undo": undo_task}[args.cmd](con, task_id)
        return 0
    return {"doctor": doctor, "smoke": smoke, "bench": bench}[args.cmd](load_config())


if __name__ == "__main__":
    if sys.stdout:  # None under pythonw (scheduled update): print() then writes nothing, reports still go to logs\
        sys.stdout.reconfigure(errors="replace")  # an odd character in a tool's output must not crash the report
    sys.exit(main())
