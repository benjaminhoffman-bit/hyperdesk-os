"""HyperDesk: the local AI desktop. One app window over everything on this PC: agents, models, chat, services and
APIs, browsers, projects, tasks, usage, health and updates.

Start it from the "HyperDesk" shortcut (pythonw, no console). It serves one page (hyperdesk.html) to an Edge app
window from 127.0.0.1 only. Every API call needs this launch's random token and a loopback Host header, so no web
page can drive it. It stops a few minutes after its last window closes: no service, no daemon. Commands it starts
write to logs\\hyperdesk-command.txt and keep running without it, as in Home Base.
"""
import base64
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # the shared build has no updates to undo (Home Base decides the same way); raw text: agent_os may be what broke
    SHARED = "\n[share]" in (Path(__file__).resolve().parent / "config.toml").read_text(encoding="utf-8")
except OSError:
    SHARED = False
REPAIR = "running Install.cmd from the HyperDesk OS zip again repairs it" if SHARED else "it can undo the last update"


def crashed(kind, err, tb) -> None:
    """pythonw has no stderr: an error that stops HyperDesk starting (say a broken agent_os.py) would vanish. Keep it
    where Home Base shows it, and say so in a box: nothing from agent_os, which may be what broke."""
    text = "".join(traceback.format_exception(kind, err, tb))
    log = Path(__file__).resolve().parent / "logs" / "hyperdesk-crash.txt"
    with contextlib.suppress(OSError):
        log.parent.mkdir(exist_ok=True)
        log.write_text(text, encoding="utf-8")
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, f"{text[-1500:]}\n\nOpen Home Base: it shows this, and {REPAIR}.",
                                     "HyperDesk could not start", 0x10)  # MB_ICONERROR


if sys.stderr is None:  # the shortcut runs pythonw
    sys.excepthook = crashed
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_os as a  # noqa: E402
import video_analytics  # noqa: E402
import assistant_modes  # noqa: E402
import self_edit  # noqa: E402
import studio  # noqa: E402
import local_web  # noqa: E402
import file_tools  # noqa: E402
import quick_edit  # noqa: E402
import post_trends  # noqa: E402

LOCALAI, HOME = a.ROOT.parent, Path.home()
WORKSPACE = LOCALAI / "workspace"
PORT = 8765  # a free port is used instead if this one is taken
IDLE_EXIT = 240  # seconds without a request before the server stops; a hidden window still polls once a minute
PY = Path(sys.executable).with_name("python.exe")  # console Python for agent-os commands, even when we are pythonw
HIDDEN = subprocess.CREATE_NO_WINDOW
STATE = a.ROOT / "state" / "hyperdesk.json"  # port and token of the running server, so a second click reuses it
CMD_LOG = a.ROOT / "logs" / "hyperdesk-command.txt"
ERRORS = a.ROOT / "logs" / "hyperdesk-errors.txt"  # pythonw has no stderr: tracebacks land here for `agent-os improve`
CRASH = a.ROOT / "logs" / "hyperdesk-crash.txt"  # why the last start failed; deleted once a start works
PAGE = a.ROOT / "hyperdesk.html"
ENV = os.path.expandvars
BROWSERS = {  # name: places Windows installs it
    "Edge": [ENV(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
             ENV(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")],
    "Chrome": [ENV(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
               ENV(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")],
    "Firefox": [ENV(r"%ProgramFiles%\Mozilla Firefox\firefox.exe")],
    "Brave": [ENV(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe")],
}
# Ben enabled opt-in public web reading for these models; code/shell access stays separate.
CHAT_ONLY = re.compile(r"abliterat|uncensor|heretic|^free", re.I)
# Interactive sessions a project can open in a new Windows Terminal tab. Qwen Code gets `daily`, the agent model.
SESSIONS = {"terminal": [], "claude": ["claude"], "codex": ["codex"],
            "qwen": ["ollama", "launch", "qwen", "--model", "daily", "--", "--approval-mode", "default"]}
COMFY, COMFY_DIR = "http://127.0.0.1:8188", LOCALAI / "ComfyUI"
# Video page: an ai-video-editor checkout with its own venv edits one video with AI; without it, quick_edit.py (no AI)
VIDEO = LOCALAI / "ai-video-editor"
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".m4v"}
QUICK = not (VIDEO / ".venv" / "Scripts" / "python.exe").exists()  # no ai-video-editor (a shared install): quick_edit.py
# Analytics page: workspace\silvades-analytics (sda.py), its read-only daily sync and the Task Scheduler entry that runs it
ANALYTICS = WORKSPACE / "silvades-analytics"
ANALYTICS_TASK = "SilvaDes analytics daily"
# Website monitor page: workspace\site-monitor (sitemon.py), each watched website's uptime, traffic, search and leads
SITES = WORKSPACE / "site-monitor"
SITES_TASK = "Website monitor"
SITE_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")  # a sites.toml section: it names the dashboard file, so no path characters
FOLDERS = {"workspace": WORKSPACE, "artifacts": LOCALAI / "artifacts", "logs": a.ROOT / "logs", "AgentOS": a.ROOT,
           "LocalAI": LOCALAI, "images": COMFY_DIR / "output", "knowledge": a.ROOT / "knowledge",
           "videos": quick_edit.OUT if QUICK else VIDEO / "projects", "analytics": ANALYTICS, "sites": SITES,
           "files": file_tools.OUT}
ACTIVE_DAYS = 14  # a project with agent activity this recent counts as current
TASK_ID = re.compile(r"\d{8}-\d{6}-\d{3}")
ASSETS = a.ROOT / "assets"  # logo, icon, fonts: served to the page without the token (nothing secret)
KINDS = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".js": "text/javascript", ".css": "text/css"}
SIZES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "wide": (1344, 768)}  # SDXL
VOICE_DIR = a.ROOT / "state" / "whisper"
MAX_BODY = 16_000_000  # a voice clip or a dropped file (base64) is the largest thing the page sends
KNOW = a.ROOT / "knowledge"  # what every model starts with: context.md, skills\*.md, handoffs\*.md (Knowledge page)
UPLOADS = a.ROOT / "state" / "uploads"  # dropped and pasted files; picked files are read where they are


def user_name(cfg: dict) -> str:
    """Whose desk this is: Ben, or the shared build's [share] name ("" when left blank)."""
    return str((cfg.get("share") or {}).get("name", "")).strip() if "share" in cfg else "Ben"


try:  # read once for the model-facing prompts; the page gets it live from status
    NAME = user_name(a.load_config())
except (OSError, ValueError):
    NAME = "Ben"


def who(blank: str = "the user") -> str:
    """The user in a model-facing prompt: their name, or `blank` ("The user", "User") when the share left it blank."""
    return NAME or blank


def log_action(what: str, detail: str = "") -> None:
    """One line in the ledger's actions table: the OS's own part of the activity timeline."""
    with contextlib.closing(a.ledger()) as con:
        con.execute("insert into actions values (?, ?, ?)", (a.now(), what, detail))
        con.commit()


def log_error(where: str, text: str) -> None:  # ponytail: never rotated; trim it if it ever passes a few MB
    with contextlib.suppress(OSError):
        ERRORS.parent.mkdir(exist_ok=True)
        with open(ERRORS, "a", encoding="utf-8") as f:
            f.write(f"=== {a.now()} {where}\n{text.rstrip()}\n")


def code_stamp() -> str:
    """The top-level modules' text, hashed: an aborted merge or undo rewrites files with new times but the same code,
    which must not say "update applied". Line endings ignored (git's autocrlf)."""
    # ponytail: reads ~0.5 MB per status poll; key it on mtimes first if that ever shows up
    return hashlib.sha1(b"".join(p.read_bytes().replace(b"\r\n", b"\n") for p in sorted(a.ROOT.glob("*.py*")))).hexdigest()


STAMP = code_stamp()  # the code this server runs: the page and assets are read from disk on every load, Python is not


def capture(fn, *args) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def browser(name: str) -> str:
    return next((p for p in BROWSERS[name] if os.path.isfile(p)), "")


def open_app(url: str) -> None:
    """An address in its own Edge app window (no tabs, no address bar), as Start AI.bat does for Open WebUI."""
    edge = browser("Edge")
    if edge:
        subprocess.Popen([edge, f"--app={url}"])
    else:
        webbrowser.open(url)


def cached(seconds: float):
    """Remember a function's result per arguments for a while; refresh=True skips the memory."""
    def wrap(fn):
        memo, lock = {}, threading.Lock()

        def get(*args, refresh: bool = False):
            with lock:
                at, value = memo.get(args, (-1e9, None))
                if refresh or time.monotonic() - at > seconds:
                    value = fn(*args)
                    memo[args] = (time.monotonic(), value)
                return value
        return get
    return wrap


# --- what the page shows

def status(cfg: dict) -> dict:
    """The cheap, live part: polled every few seconds."""
    procs = a.running_processes()
    code, out = a.run(["nvidia-smi", "--query-gpu=name,memory.free,memory.total,utilization.gpu",
                       "--format=csv,noheader,nounits"])
    try:
        name, free, total, util = [x.strip() for x in a.first_line(out).split(",")]
        gpu = {"name": name, "free": int(free), "total": int(total), "util": int(util)}
    except ValueError:
        gpu = None
    try:
        ollama = a.get_json(cfg["ollama"]["url"] + "/api/version")["version"]
        loaded = [{"name": m["name"].removesuffix(":latest"), "vram": m.get("size_vram", 0),
                   "until": m.get("expires_at", "")[:19]} for m in a.get_json(cfg["ollama"]["url"] + "/api/ps")["models"]]
    except (OSError, ValueError, KeyError):
        ollama, loaded = "", []
    with contextlib.closing(a.ledger()) as con:
        calls = con.execute("select count(*) from calls where agent in ('claude', 'codex') "
                            "and at >= date('now', 'localtime')").fetchone()[0]
        awaiting = con.execute("select count(*) from tasks where state = 'AWAITING_APPROVAL'").fetchone()[0]
    return {"gpu": gpu, "game": a.game_running(cfg, procs), "ollama": ollama, "loaded": loaded,
            "services": {n: a.port_open(port) for n, (port, _, _) in a.SERVICES.items()},
            "browsers": {n: {"installed": bool(browser(n)), "running": Path(browser(n)).name.lower() in procs}
                         for n in BROWSERS},
            "calls": calls, "cap": cfg["agents"]["max_calls_per_day"], "awaiting": awaiting,
            "implementer": cfg["agents"]["implementer"], "checkpoints": checkpoints(),
            "approvals": [{k: v for k, v in x.items() if k != "event"} for x in list(APPROVALS.values())],
            "jobs": {s: sum(j.state == s for j in list(JOBS.values())) for s in ("running", "queued")},
            "command": RUNNER.state(), "health": HEALTH, "stale": code_stamp() != STAMP,  # an applied update waits for a reopen
            "free": {"why": a.free_unavailable(cfg), "url": (cfg.get("free") or {}).get("url", ""),
                     "model": (cfg.get("free") or {}).get("model", "")},
            "ads": bool((cfg.get("share") or {}).get("ads")), "name": user_name(cfg), "hide": hidden(cfg), "share": "share" in cfg,
            "folders": [n for n, p in FOLDERS.items() if p.exists()], "workspace": str(WORKSPACE)}


def hidden(cfg: dict) -> list[str]:
    """What the page leaves out: parts this PC lacks (a shared install has none of Ben's extras) and, in the shared
    build, updates (the daily update, git and the 7-day rule are Ben's) and the power-user pages (the lite build: its
    Video page is Quick edit, shown even without FFmpeg so it can say how to get it, and Analytics is Post trends)."""
    lite = "share" in cfg
    lacks = {"analytics": not lite and not (ANALYTICS / "sda.py").exists(), "images": "ComfyUI" not in a.SERVICES,
             "sites": lite or not (SITES / "sitemon.py").exists(),
             "studio": lite or not shutil.which("ffmpeg"), "improve": not (a.ROOT / ".git").exists(), "updates": lite,
             "projects": lite, "tasks": lite, "activity": lite,
             "voice": "share" in cfg and not a.voice_python(),  # "share" first: Ben's 4 s poll never runs uv
             "claude": "share" in cfg and not a.find_tool("claude")}  # Ask Claude would only fail there
    return sorted(k for k, v in lacks.items() if v)


def session_cwd(path: Path) -> str:
    """The working folder a Claude Code session recorded (it is on most lines; the first 64 KB is plenty)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', f.read(65536))
    return json.loads(f'"{m.group(1)}"') if m else ""


def git_info(path: str) -> dict | None:
    code, out = a.run(["git", "-C", path, "status", "--porcelain=v2", "--branch"])
    if code != 0:
        return None
    info = {"branch": "", "ahead": 0, "behind": 0, "changed": 0, "commit": "", "commit_at": 0}
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            info["branch"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            ahead, behind = line.split()[2:4]
            info["ahead"], info["behind"] = int(ahead), -int(behind)
        elif not line.startswith("#"):
            info["changed"] += 1
    code, out = a.run(["git", "-C", path, "log", "-1", "--format=%ct%x09%s"])
    if code == 0 and "\t" in out:
        when, info["commit"] = out.split("\t", 1)
        info["commit_at"] = int(when)
    return info


@cached(30)
def projects() -> list[dict]:
    """Every folder an agent has worked in, from each agent's own records, newest activity first."""
    found: dict[str, dict] = {}

    def add(path: str, agent: str, when: float, n: int = 1, claude_dir: str = "") -> None:
        p = Path(path)
        e = found.setdefault(str(p).lower(), {"path": str(p), "name": p.name or str(p), "agents": {}, "last": 0,
                                              "claude_dir": ""})
        if n:
            e["agents"][agent] = e["agents"].get(agent, 0) + n
        e["last"] = max(e["last"], when)
        e["claude_dir"] = e["claude_dir"] or claude_dir

    for d in (HOME / ".claude" / "projects").glob("*"):  # one folder per project, one .jsonl per session
        sessions = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if sessions and (cwd := session_cwd(sessions[-1])):
            add(cwd, "Claude Code", sessions[-1].stat().st_mtime, len(sessions), d.name)
    for rt in (HOME / ".qwen" / "projects").glob("*/chats/*.runtime.json"):  # names the folder Qwen Code ran in
        with contextlib.suppress(OSError, ValueError, KeyError):
            chat = rt.with_name(rt.name.removesuffix(".runtime.json") + ".jsonl")
            add(json.loads(rt.read_text(encoding="utf-8"))["work_dir"], "Qwen Code",
                (chat if chat.exists() else rt).stat().st_mtime)
    # Codex: only the first line of each session file, its session_meta (where it ran). Ben asked for this
    # (2026-10-05). Nothing else under ~\.codex is opened: auth.json there holds the sign-in.
    for f in (Path(os.environ.get("CODEX_HOME") or HOME / ".codex") / "sessions").glob("*/*/*/rollout-*.jsonl"):
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            with open(f, encoding="utf-8", errors="replace") as h:
                meta = json.loads(h.readline())
            if meta.get("type") == "session_meta":
                add(meta["payload"]["cwd"], "Codex", f.stat().st_mtime)
    with contextlib.closing(a.ledger()) as con:
        for repo, n, last in con.execute("select repo, count(*), max(created) from tasks group by repo"):
            if repo and Path(repo).is_absolute():  # research tasks have repo 'web'
                add(repo, "Agent OS", time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S")), n)
    for git in WORKSPACE.glob("*/.git"):  # workspace repos count even before an agent touches them
        reflog = git / "logs" / "HEAD"  # moves on commit or checkout; .git itself changes on every `git status`
        add(str(git.parent), "", reflog.stat().st_mtime if reflog.exists() else 0, 0)
    skip = [Path(tempfile.gettempdir(), "claude").resolve(), (WORKSPACE / "worktrees").resolve(),  # scratchpads, tasks
            Path(os.environ.get("APPDATA", HOME), "Claude", "scratch-workspaces").resolve()]  # Claude desktop scratch
    out = []
    for e in found.values():
        p = Path(e["path"])
        if any(p.is_relative_to(s) for s in skip):
            continue
        e["exists"] = p.is_dir()
        e["git"] = git_info(e["path"]) if e["exists"] else None
        e["active"] = time.time() - e["last"] < ACTIVE_DAYS * 86400
        e["in_workspace"] = p.parent == WORKSPACE and (p / ".git").exists()
        out.append(e)
    return sorted(out, key=lambda e: -e["last"])


def models(cfg: dict) -> list[dict]:
    url = cfg["ollama"]["url"]
    tags = a.get_json(url + "/api/tags", timeout=10)["models"]
    ps = {m["name"]: m for m in a.get_json(url + "/api/ps")["models"]}
    jobs = {cfg["ollama"]["classify_model"]: "classifies tasks", cfg["ollama"]["answer_model"]: "answers questions"}
    rows = []
    for m in sorted(tags, key=lambda m: m["name"]):
        name, d, live = m["name"].removesuffix(":latest"), m.get("details") or {}, ps.get(m["name"])
        role = "chat only" if CHAT_ONLY.search(name) else "agents" if name in cfg["ollama"]["aliases"] else "chat"
        rows.append({"name": name, "size": m.get("size", 0), "family": d.get("family", ""),
                     "params": d.get("parameter_size", ""), "quant": d.get("quantization_level", ""), "role": role,
                     "job": jobs.get(name, ""), "vram": live.get("size_vram", 0) if live else 0,
                     "until": live.get("expires_at", "")[:19] if live else ""})
    return rows


def tasks() -> list[dict]:
    """undoable: applied (Undo update reverts it); stale: busy far longer than any run, so nothing runs it (Discard)."""
    with contextlib.closing(a.ledger()) as con:
        cur = con.execute("select id, created, state, level, agent, tests_before, tests_after, repo, task, review, "
                          "state = 'COMPLETED' and exists(select 1 from events e where e.task = t.id and e.state = "
                          "'COMPLETED' and (e.detail like 'fast-forwarded%' or e.detail like 'merged%')) as undoable, "
                          f"state in ({','.join('?' * len(a.BUSY_STATES))}) and created < datetime('now', 'localtime', ?) "
                          "as stale from tasks t order by id desc limit 200", (*a.BUSY_STATES, f"-{a.STALE_HOURS} hours"))
        heads = [d[0] for d in cur.description]
        return [{**dict(zip(heads, row)), "undoable": bool(row[-2]), "stale": bool(row[-1])} for row in cur.fetchall()]


def timeline(limit: int = 250) -> list[tuple]:
    """Everything that happened, newest first: task states, agent calls, the OS's own actions, updates."""
    with contextlib.closing(a.ledger()) as con:
        rows = con.execute("""
            select at, 'task', task || '  ' || state || coalesce(': ' || substr(detail, 1, 240), '') from events
            union all select at, 'agent', agent || ' ' || role || case when ok then ' ok, ' else ' FAILED, ' end
                || seconds || ' s, ' || coalesce(tokens_in, 0) || ' tokens in' from calls
            union all select at, 'desk', what || coalesce(': ' || nullif(substr(detail, 1, 240), ''), '') from actions
            order by 1 desc limit ?""", (limit,)).fetchall()
    log = a.ROOT / "logs" / "update-history.txt"
    for block in (log.read_text(encoding="utf-8", errors="replace").split("\n\n") if log.exists() else []):
        lines = block.strip().splitlines()
        if lines and (m := re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", lines[0])):
            done = [re.sub(r"\s+", " ", x) for x in lines[1:] if x.startswith(("DONE", "FAIL", "SKIP", "WARN"))]
            rows.append((m.group(1), "update", "; ".join(done) or "everything up to date"))
    return sorted(rows, reverse=True)[:limit]


@cached(600)
def ccusage(kind: str, report: str) -> list[dict]:
    """ccusage's own reading of the local Claude Code / Codex logs (all sessions, not only Agent OS)."""
    since = time.strftime("%Y%m%d", time.localtime(time.time() - 13 * 86400))
    code, out = a.run(["ccusage", kind, report, "--offline", "--json", *(["--since", since] if report == "daily" else [])],
                      180)
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data.get({"daily": "daily", "session": "sessions", "blocks": "blocks"}[report]) or []


def usage() -> dict:
    with contextlib.closing(a.ledger()) as con:
        ledger = con.execute("select date(at), case when agent like 'local:%' then 'os_local' when agent in ('free', "
                             "'opencode') then 'os_free' else 'os_cloud' end, "
                             "count(*) from calls where at >= date('now', 'localtime', '-13 days') group by 1, 2"
                             ).fetchall()
    days: dict[str, dict] = {}
    for day, kind, n in ledger:
        days.setdefault(day, {})[kind] = n
    parts = ("outputTokens", "inputTokens", "cacheCreationTokens", "cacheReadTokens")  # the page stacks them in this order
    models: dict[str, list] = {}  # name -> [tool, total tokens, output tokens, API-rate USD]
    for kind in ("claude", "codex"):
        for d in ccusage(kind, "daily"):
            day = days.setdefault(d["date"], {})
            day[kind] = d.get("totalTokens", 0)
            day[f"{kind}_usd"] = round(d.get("totalCost", d.get("costUSD", 0)) or 0, 2)
            day[f"{kind}_parts"] = [d.get(p, 0) or 0 for p in parts]
            # Claude lists modelBreakdowns with a cost each; Codex a models dict without one
            for m in d.get("modelBreakdowns") or [dict(v, modelName=k) for k, v in (d.get("models") or {}).items()]:
                row = models.setdefault(m["modelName"], [kind, 0, 0, 0.0])
                row[1] += m.get("totalTokens") or sum(m.get(p, 0) or 0 for p in parts)
                row[2] += m.get("outputTokens", 0) or 0
                row[3] += m.get("cost", 0) or 0
    by_dir = {e["claude_dir"]: e["name"] for e in projects() if e["claude_dir"]}
    per_project: dict[str, list] = {}
    for s in ccusage("claude", "session"):
        folder = re.split(r"[\\/]", s.get("projectPath") or "?")[0]  # subagent sessions add a session folder
        name = by_dir.get(folder, folder)
        row = per_project.setdefault(name, [name, 0, 0.0, ""])
        row[1] += s.get("totalTokens", 0)
        row[2] += s.get("totalCost", 0) or 0
        row[3] = max(row[3], s.get("lastActivity", ""))
    return {"days": sorted(days.items(), reverse=True),
            "models": sorted(([n, k, t, o, round(c, 2)] for n, (k, t, o, c) in models.items()), key=lambda r: -r[2]),
            "projects": sorted(([n, t, round(c, 2), last[:10]] for n, t, c, last in per_project.values()),
                               key=lambda r: -r[1])}


def digest(cfg: dict) -> dict:
    """The Home summary: what needs Ben, what happened since his last visit (or in the last 24 hours if he was here
    in the last 6), and usage against the caps. Lines are (kind, text, view to open)."""
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731
    with contextlib.closing(a.ledger()) as con:
        opens = [at for (at,) in con.execute("select at from actions where what = 'HyperDesk opened' "
                                            "order by at desc limit 2")]
        prev = opens[1] if len(opens) > 1 else ""
        since = prev if prev and prev < stamp(time.time() - 6 * 3600) else stamp(time.time() - 86400)
        changed = con.execute("select id, state, task, repo from tasks where id in (select task from events where "
                              "at >= ?) order by id desc", (since,)).fetchall()
        awaiting = con.execute("select task from tasks where state = 'AWAITING_APPROVAL'").fetchall()
        calls = con.execute("select count(*) from calls where agent in ('claude', 'codex') and at >= ?",
                            (since,)).fetchone()[0]
        today = con.execute("select count(*) from calls where agent in ('claude', 'codex') "
                            "and at >= date('now', 'localtime')").fetchone()[0]
        kept = dict(con.execute("select case when agent like 'local:%' then 'local' else 'free' end, count(*) from calls "
                                "where (agent like 'local:%' or agent in ('free', 'opencode')) and role in ('answer', "
                                "'implementer', 'repair') and at >= date('now', 'localtime') group by 1").fetchall())
        wrapped = {d.split(":")[0] for (d,) in con.execute("select detail from actions where what = 'wrapped up'")}
        # long conversations that went quiet (an hour or more) since then, never wrapped up: knowledge not yet kept
        quiet = [(t, title) for t, title in con.execute(
            "select coalesce(thread, id), min(coalesce(said, input)) from jobs group by 1 having count(distinct batch) "
            ">= 3 and max(created) >= ? and max(created) < ? order by max(created) desc",
            (since, stamp(time.time() - 3600))) if t not in wrapped]
    lines = []
    cut = lambda t: t if len(t) <= 60 else t[:58].rstrip() + "…"  # noqa: E731
    names = lambda rows, n=3: "; ".join(cut(r) for r in rows[:n]) + (f" and {len(rows) - n} more" if len(rows) > n else "")  # noqa: E731
    if awaiting:
        lines.append(("warn", f"{len(awaiting)} task{'s' * (len(awaiting) > 1)} waiting for your merge: "
                              f"{names([t for (t,) in awaiting])}", "tasks"))
    failed = [t for _, s, t, _ in changed if s == "FAILED"]
    if failed:
        lines.append(("bad", f"{len(failed)} failed: {names(failed)}", "tasks"))
    fixes = [f["what"] for f in improvements()["found"] if f["task"]]
    if fixes:
        lines.append(("warn", f"Self-check: {len(fixes)} fix{'es' * (len(fixes) > 1)} ready: {names(fixes, 2)}", "system"))
    for t, title in quiet[:2]:
        lines.append(("ok", f"Wrap up “{cut(title)}” to keep what it found", f"thread:{t}"))
    issues = [r[0] for r in HEALTH["results"] if r[1] != a.PASS]
    if issues:
        lines.append(("warn", f"Health: {', '.join(issues)} to look at", "system"))
    research = [t for _, s, t, repo in changed if repo == "web" and s == "COMPLETED"]
    if research:
        lines.append(("ok", f"Research answered: {names(research)}", "research"))
    done = [t for _, s, t, repo in changed if repo != "web" and s == "COMPLETED"]
    if done:
        lines.append(("ok", f"{len(done)} task{'s' * (len(done) > 1)} completed: {names(done)}", "tasks"))
    for at, kind, text in timeline(400):
        if kind == "update" and at >= since:
            # skipped or failed: a warn line, so Home (which hides info lines) shows it
            lines.append(("warn" if re.search(r"\b(SKIP|FAIL|WARN) ", text) else "info", f"Update {at[5:16]}: {text[:200]}",
                          "system"))
    since_t = time.mktime(time.strptime(since, "%Y-%m-%d %H:%M:%S"))
    active = [e for e in projects() if e["last"] >= since_t]
    if active:
        worked = list(dict.fromkeys(e["name"] for e in active))  # Codex Desktop makes one dated folder per task
        lines.append(("info", f"Worked on: {', '.join(worked[:6])}" + (f" and {len(worked) - 6} more" if len(worked) > 6
                                                                         else ""), "projects"))
    images = [p for p in image_paths(500) if p.stat().st_mtime >= since_t]
    if images:
        lines.append(("info", f"{len(images)} new image{'s' * (len(images) > 1)}", "images"))
    with contextlib.suppress(OSError, ValueError, KeyError):
        new = [m["name"].removesuffix(":latest") for m in a.get_json(cfg["ollama"]["url"] + "/api/tags", 10)["models"]
               if m.get("modified_at", "")[:19].replace("T", " ") >= since]
        if new:
            lines.append(("info", f"New or changed models: {', '.join(new)}", "models"))
    cap = cfg["agents"]["max_calls_per_day"]
    if "share" not in cfg or a.find_tool("claude"):  # a shared install without Claude Code has no cloud calls to count
        lines.append(("warn" if today >= cap * 0.8 else "ok", f"Agent OS cloud calls today: {today} of {cap}"
                      + (f" ({calls} since then)" if calls else ""), "activity"))
    if kept:
        lines.append(("ok", "Kept off Claude today: " + ", ".join(f"{n} {'on this PC' if k == 'local' else 'on free cloud'}"
                                                            for k, n in sorted(kept.items(), reverse=True)), "activity"))
    blocks = [b for b in ccusage("claude", "blocks") if not b.get("isGap")]
    live = next((b for b in blocks if b.get("isActive")), None)
    peak = max((b.get("totalTokens", 0) for b in blocks if not b.get("isActive")), default=0)
    if live:
        ends = datetime.datetime.fromisoformat(live["endTime"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        ratio = live["totalTokens"] / peak if peak else 0
        share = ", your busiest window yet" if ratio > 1 else f", {ratio:.0%} of your busiest one" if peak else ""
        lines.append(("warn" if peak and live["totalTokens"] > peak * 0.8 else "info",
                      f"Claude 5-hour window: {live['totalTokens'] / 1e6:.1f}M tokens{share}; resets {ends}", "activity"))
    return {"label": "Since you were last here" if since == prev else "Last 24 hours", "since": since,
            "lines": [{"kind": k, "text": t, "view": v} for k, t, v in lines]}


# --- images: ComfyUI's own API, the SDXL checkpoint it already has

def comfy(path: str, body=None, timeout: float = 10):
    request = urllib.request.Request(COMFY + path, headers={"Content-Type": "application/json"},
                                     data=None if body is None else json.dumps(body).encode())
    with a._NO_PROXY.open(request, timeout=timeout) as r:
        return json.load(r)


def checkpoints() -> list[str]:
    folder = COMFY_DIR / "models" / "checkpoints"
    return sorted(p.name for p in folder.iterdir() if p.suffix in (".safetensors", ".ckpt")) if folder.is_dir() else []


def ollama_keep(cfg: dict, name: str, keep) -> None:
    """Load (keep = how long) or unload (keep = 0) one Ollama model. No prompt: nothing is generated."""
    request = urllib.request.Request(cfg["ollama"]["url"] + "/api/generate", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": name, "keep_alive": keep}).encode())
    with a._NO_PROXY.open(request, timeout=180) as r:
        r.read()


def generate(cfg: dict, body: dict) -> dict:
    """Queue one SDXL text-to-image job in ComfyUI. Returns its prompt id; image_status() follows it."""
    prompt = str(body.get("prompt", "")).strip()
    if not 0 < len(prompt) <= 2000:
        raise ValueError("describe the image (up to 2000 characters)")
    width, height = SIZES.get(str(body.get("size")), SIZES["square"])
    steps = min(max(int(body.get("steps") or 28), 8), 60)
    scale = min(max(float(body.get("cfg") or 6.5), 1.0), 15.0)
    count = min(max(int(body.get("count") or 1), 1), 4)
    seed = str(body.get("seed", "")).strip()
    seed = int(seed) if seed.isdigit() else secrets.randbelow(2**48)
    ckpt = str(body.get("checkpoint") or (checkpoints() or [""])[0])
    if ckpt not in checkpoints():
        raise ValueError(f"no checkpoint {ckpt!r} in {COMFY_DIR / 'models' / 'checkpoints'}")
    if game := a.game_running(cfg):
        raise ValueError(f"game running ({game}): no image jobs now")
    if not a.port_open(8188):
        raise ValueError("ComfyUI is not running: press Start ComfyUI first")
    if body.get("free_vram", True):  # SDXL wants ~9 GB; a chat model in VRAM would push it onto the CPU
        with contextlib.suppress(OSError, ValueError, KeyError):
            for m in a.get_json(cfg["ollama"]["url"] + "/api/ps")["models"]:
                ollama_keep(cfg, m["name"], 0)
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": str(body.get("negative", ""))[:1000], "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": count}},
        "5": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": scale, "sampler_name": "dpmpp_2m",
                                                   "scheduler": "karras", "denoise": 1.0, "model": ["1", 0],
                                                   "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "hyperdesk", "images": ["6", 0]}},
    }
    queued = comfy("/prompt", {"prompt": graph, "client_id": "hyperdesk"}, timeout=30)
    log_action("image", f"{prompt[:160]} ({width}x{height}, {steps} steps, seed {seed})")
    return {"id": queued["prompt_id"], "seed": seed}


def image_status(pid: str) -> dict:
    history = comfy(f"/history/{pid}")
    if pid in history:
        h = history[pid]
        files = [f"{i.get('subfolder', '')}/{i['filename']}".lstrip("/") for node in h.get("outputs", {}).values()
                 for i in node.get("images", []) if i.get("type") == "output"]
        errors = [m[1].get("exception_message", "") for m in h.get("status", {}).get("messages", [])
                  if m[0] == "execution_error"]
        return {"done": True, "files": files, "error": "; ".join(filter(None, errors))[:400]}
    queue = comfy("/queue")
    if any(job[1] == pid for job in queue.get("queue_running", [])):
        return {"done": False, "state": "rendering"}
    pending = [job[1] for job in queue.get("queue_pending", [])]
    return {"done": False, "state": f"queued, {pending.index(pid) + 1} ahead" if pid in pending else "starting"}


def png_prompt(path: Path) -> str:
    """The positive prompt ComfyUI stored in its PNG (the 'prompt' text chunk holds the graph it ran). Best effort:
    any graph it does not understand (no KSampler, odd JSON) just gives no prompt, never a broken gallery."""
    with contextlib.suppress(Exception):
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return ""
            while True:  # text chunks come before the image data
                n, kind = struct.unpack(">I4s", f.read(8))
                if kind in (b"IDAT", b"IEND"):
                    return ""
                data = f.read(n)
                f.seek(4, 1)
                key, _, value = data.partition(b"\0")
                if kind == b"tEXt" and key == b"prompt":
                    graph = json.loads(value)
                    sampler = next((n for n in graph.values() if str(n.get("class_type", "")).startswith("KSampler")), None)
                    return str(graph[sampler["inputs"]["positive"][0]]["inputs"]["text"])[:600] if sampler else ""
    return ""


def image_paths(limit: int) -> list[Path]:
    out = COMFY_DIR / "output"
    return sorted((p for p in out.rglob("*") if p.suffix.lower() in KINDS and p.suffix != ".svg"),
                  key=lambda p: -p.stat().st_mtime)[:limit] if out.is_dir() else []


def gallery(limit: int = 60) -> list[dict]:
    out = COMFY_DIR / "output"
    return [{"name": p.relative_to(out).as_posix(), "at": p.stat().st_mtime, "prompt": png_prompt(p)}
            for p in image_paths(limit)]


def image_file(name: str) -> tuple[bytes, str]:
    """One picture from ComfyUI's output folder, by its path inside that folder; nothing outside it."""
    out = (COMFY_DIR / "output").resolve()
    path = (out / urllib.parse.unquote(name)).resolve()
    if not path.is_relative_to(out) or path.suffix.lower() not in KINDS or path.suffix == ".svg" or not path.is_file():
        raise ValueError("no such image")
    return path.read_bytes(), KINDS[path.suffix.lower()]


# --- the research agent's approvals (desk_mcp.py holds the agent's request open until Ben answers)

APPROVALS: dict[str, dict] = {}
APPROVAL_WAIT = 300  # seconds, then it counts as no


def describe(tool: str, given: dict) -> str:
    """What the research agent wants to do, in words Ben can say yes or no to."""
    name = tool.removeprefix("mcp__browser__").removeprefix("browser_")
    el = given.get("element") or given.get("ref") or "an element"
    fields = [f for f in given.get("fields") or [] if isinstance(f, dict)]
    return {"click": f"Click “{el}”" + (" twice" if given.get("doubleClick") else ""),
            "type": f"Type “{given.get('text', '')}” into “{el}”" + (" and press Enter" if given.get("submit") else ""),
            "fill_form": "Fill in " + "; ".join(f"{f.get('name', '?')}: “{f.get('value', '')}”" for f in fields),
            "select_option": f"Choose {', '.join(map(str, given.get('values') or []))} in “{el}”",
            "press_key": f"Press the {given.get('key', '?')} key",
            "hover": f"Point at “{el}”",
            "drag": f"Drag “{given.get('startElement', '?')}” onto “{given.get('endElement', '?')}”",
            "handle_dialog": ("Accept" if given.get("accept") else "Dismiss") + " a page dialog",
            }.get(name) or f"{tool}: {json.dumps(given)[:200]}"


def ask_ben(task_id: str, tool: str, given: dict) -> bool:
    """Hold the agent's request until Ben answers in HyperDesk (a Windows notification tells him), or say no."""
    with contextlib.closing(a.ledger()) as con:
        row = con.execute("select task from tasks where id = ?", (task_id,)).fetchone()
    item = {"id": secrets.token_hex(6), "task": row[0] if row else "", "tool": tool, "what": describe(tool, given),
            "input": given, "at": a.now(), "event": threading.Event(), "allow": False, "task_id": task_id}
    APPROVALS[item["id"]] = item
    a.notify("The research agent asks first", item["what"])
    try:
        item["event"].wait(APPROVAL_WAIT)
    finally:
        APPROVALS.pop(item["id"], None)
    log_action("approved" if item["allow"] else "refused", f"{item['what']} (research: {item['task'][:80]})")
    return item["allow"]


def research_list() -> list[dict]:
    with contextlib.closing(a.ledger()) as con:
        rows = con.execute("select t.id, t.created, t.state, t.task, t.error, (select data from calls where task = t.id "
                           "and role = 'researcher' order by rowid desc limit 1) from tasks t where t.repo = 'web' "
                           "order by t.id desc limit 50").fetchall()
    out = []
    for task_id, created, state, question, error, data in rows:
        reply = json.loads(data) if data else None
        out.append({"id": task_id, "created": created, "state": state, "question": question, "error": error or "",
                    **(reply if isinstance(reply, dict) else {})})
    return out


# --- voice: one resident faster-whisper worker (voice.py --serve) under Open WebUI's Python

class Voice:
    """Started when recording starts, so its ~8 s warm-up overlaps the speaking; a clip then takes ~1.5 s."""

    def __init__(self):
        self.proc, self.lock = None, threading.Lock()

    def ensure(self, cfg: dict) -> None:
        if self.proc and self.proc.poll() is None:
            return
        python = a.voice_python()
        if not python:
            raise ValueError("Voice input is not set up on this PC (it uses Open WebUI's faster-whisper).")
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [python, str(a.ROOT / "voice.py"), "--serve", cfg.get("voice", {}).get("model", "small.en"), str(VOICE_DIR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=HIDDEN)
        if not self.proc.stdout.readline():  # {"ready": true} once the model is loaded
            self.proc = None
            raise ValueError("the speech model did not start")

    def warm(self, cfg: dict) -> None:
        with self.lock:
            with contextlib.suppress(ValueError, OSError):
                self.ensure(cfg)

    def transcribe(self, cfg: dict, audio: bytes) -> dict:
        with self.lock, tempfile.TemporaryDirectory() as d:
            self.ensure(cfg)
            clip = Path(d) / "clip.webm"  # the decoder reads the real format from the bytes
            clip.write_bytes(audio)
            # ponytail: no read timeout on the worker's pipe; add one if a clip ever hangs it
            self.proc.stdin.write(json.dumps({"file": str(clip)}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            self.proc = None
            raise ValueError("the speech worker stopped; try again")
        reply = json.loads(line)
        if reply.get("error"):
            raise ValueError(f"could not transcribe: {reply['error']}")
        return reply

    def stop(self) -> None:
        if self.proc:
            self.proc.kill()


VOICE = Voice()


# --- attachments: files and folders anywhere on this PC, read where they are (a dropped or pasted file is kept in
# state\uploads). Never secrets, never Windows' own folders.

OFF_LIMITS = [HOME / ".codex", HOME / ".ollama", HOME / ".ssh", HOME / ".claude", HOME / ".qwen", LOCALAI / "open-webui",
              LOCALAI / "omniroute-data", *(Path(p) for p in map(os.environ.get, ("SystemRoot", "ProgramFiles",
                                                                                 "ProgramFiles(x86)", "ProgramData")) if p)]
SECRET_NAME = re.compile(r"\.env(\..*)?|\.webui_secret_key|\.credentials\.json|.*\.(pem|key|pfx|p12)|id_(rsa|ed25519|ecdsa)\b.*",
                         re.I)
# The same walls for a Claude answer: --restricted keeps its file tools inside the folders attached, these deny the
# secrets inside them. The // anchor is needed: without it Claude Code reads a pattern relative to its working folder,
# and `**/.env` let an attached folder's .env through (tested 2026-10-05; `//**/.env` blocked it).
CLAUDE_DENY = [f"Read(//**/{n})" for n in (".env", ".env.*", ".webui_secret_key", ".credentials.json", "*.pem", "*.key",
                                          "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*")] + \
              [f"Read({d})" for d in ("~/.codex/**", "~/.ollama/**", "~/.ssh/**", "~/.claude/**", "~/.qwen/**",
                                      *(f"//{LOCALAI.drive[:1].lower()}{LOCALAI.as_posix()[2:]}/{n}/**"  # //<drive>/... form
                                        for n in ("open-webui", "omniroute-data")))]
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
BINARY_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".zip", ".7z", ".rar", ".exe", ".dll",
              ".msi", ".iso", ".mp3", ".mp4", ".mov", ".wav", ".psd", ".ai", ".db", ".sqlite", ".gguf", ".safetensors"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "graphify-out"}  # and every hidden folder
TEXT_MAX = 300_000  # bytes read from one file
CHARS_PER_TOKEN = 3  # ponytail: one rough rate for prose and code; the model's own tokenizer if routing misjudges
REPLY_TOKENS = 2048  # kept free in a local model's context for its answer


def off_limits(p: Path) -> bool:
    """Secrets (keys, sign-ins, .env files) and Windows' own folders: no model reads them, no session opens there."""
    p = p.resolve()
    return bool(SECRET_NAME.fullmatch(p.name)) or any(p.is_relative_to(d) for d in OFF_LIMITS)


def attached(given) -> list[Path]:
    """The paths the page sent: absolute, there, not off limits; at most 20."""
    if not isinstance(given, list) or len(given) > 20:
        raise ValueError("attach up to 20 files or folders")
    out = []
    for g in map(str, given):
        p = Path(g)
        if not p.is_absolute() or not p.exists():
            raise ValueError(f"not found: {g}")
        if off_limits(p):
            raise ValueError(f"{p.name or p} is off limits (secrets or Windows' own files)")
        out.append(p.resolve())
    return list(dict.fromkeys(out))


def file_text(p: Path) -> str | None:
    """A file's text (its first TEXT_MAX bytes), or None when it is not text. PDF and Office files: their text
    (file_tools reads them without running anything in them); pictures and other binaries: None."""
    if p.suffix.lower() in file_tools.READABLE:
        return file_tools.text_of(p)
    if p.suffix.lower() in BINARY_EXT | IMAGE_EXT:
        return None
    try:
        with open(p, "rb") as f:
            data = f.read(TEXT_MAX)
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:  # cut inside a character at TEXT_MAX, or an older Windows file
        cut = len(data) == TEXT_MAX and e.start >= len(data) - 3
        return data.decode("utf-8" if cut else "cp1252", "replace")


def size_of(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def folder_files(root: Path, limit: int = 2000) -> list[Path]:
    """Files under a folder, without hidden and build folders or anything off limits; at most `limit`."""
    out = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not off_limits(Path(here, d)))
        out += [Path(here, f) for f in sorted(files) if not SECRET_NAME.fullmatch(f)]
        if len(out) >= limit:
            return out[:limit]
    return out


def pack(paths: tuple[str, ...], room: int) -> dict:
    """What a local model is given, in `room` characters: each file's text; for a folder its file list, then its
    smallest text files first (so most of them fit); pictures as base64, at most 4. `left`: what did not fit or is
    not text, so the model can say what it did not see."""
    texts, images, left = [], [], []

    def put(block: str) -> bool:
        nonlocal room
        if len(block) > room:
            return False
        texts.append(block)
        room -= len(block)
        return True

    for p in map(Path, paths):
        if p.is_dir():
            files = folder_files(p)
            put(f'<folder path="{p}">\n' + "\n".join(str(f.relative_to(p)) for f in files[:300])
                + ("\n…" if len(files) > 300 else "") + "\n</folder>")
            for f in sorted(files, key=size_of):  # a folder's pictures and documents are only listed
                if (body := file_text(f)) is not None and not put(f'<file path="{f}">\n{body}\n</file>'):
                    left.append(str(f))
        elif p.suffix.lower() in IMAGE_EXT and len(images) < 4 and size_of(p) < 20_000_000:
            images.append(base64.b64encode(p.read_bytes()).decode())
        elif (body := file_text(p)) is None or not put(f'<file path="{p}">\n{body}\n</file>'):
            left.append(str(p))
    return {"text": "\n\n".join(texts), "images": images, "left": left}


@cached(30)
def measure(paths: tuple[str, ...]) -> tuple[int, bool, bool]:
    """(characters of text, has pictures, has something only Claude can take) of attachments, for routing: PDF and
    Office files, or more text than even a big room holds."""
    got = pack(paths, 400_000)
    return len(got["text"]), bool(got["images"]), bool(got["left"])


def ollama_open(request: urllib.request.Request, timeout: float):
    """Ollama's own error (404: "model 'x' not found") instead of urllib's bare "HTTP Error 404: Not Found"."""
    try:
        return a._NO_PROXY.open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read()).get("error") or e.reason
        except (OSError, ValueError, AttributeError):
            said = e.reason
        raise ValueError(f"Ollama HTTP {e.code}: {said}" + (": it is not on this PC now; pick another model or pull it"
                                                            if e.code == 404 else "")) from None
    except urllib.error.URLError as e:  # quit from its tray, or crashed, since HyperDesk started it
        if not isinstance(e.reason, ConnectionRefusedError):
            raise
        a.start_ollama()  # started once here, never retried: Ben sends it again
        raise ValueError("Ollama was not running: starting it now. Send this again in a few seconds.") from None


@cached(600)
def model_info(url: str, name: str) -> tuple[int, bool]:
    """(context window in tokens, sees pictures) of an Ollama model, from its Modelfile. Ollama not running (it is
    started) or a model Ollama does not have: ValueError; other network trouble: OSError."""
    request = urllib.request.Request(url + "/api/show", data=json.dumps({"model": name}).encode(),
                                     headers={"Content-Type": "application/json"})
    with ollama_open(request, timeout=10) as r:
        info = json.load(r)
    m = re.search(r"num_ctx\s+(\d+)", info.get("parameters") or "")
    return int(m.group(1)) if m else 4096, "vision" in (info.get("capabilities") or [])


def thread_files(tid: str) -> list[Path]:
    """Everything attached in a conversation that is still there: a follow-up reads it again, as in a chat."""
    found = [Path(p) for j in thread_jobs(tid) for p in j["attach"]]
    return [p for p in dict.fromkeys(found) if p.exists() and not off_limits(p)]


PICKER = r"""
import ctypes, json, sys, tkinter as tk
from tkinter import filedialog
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp on a scaled display
except Exception:
    pass
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
if sys.argv[1] == "folder":
    got = filedialog.askdirectory(parent=root, title="Attach a folder", mustexist=True)
    print(json.dumps([got] if got else []))
else:
    print(json.dumps(list(filedialog.askopenfilenames(parent=root, title="Attach files"))))
"""
PICKING = threading.Lock()


def pick(kind: str) -> dict:
    """Windows' own file or folder dialog, on this PC: an attachment is the real file where it is, so nothing is
    copied and any size works. In its own process: Tk wants a main thread of its own."""
    if kind not in ("files", "folder"):
        raise ValueError("pick files or a folder")
    if not PICKING.acquire(blocking=False):
        raise ValueError("a file dialog is already open (it may be behind this window)")
    try:
        out = subprocess.run([str(PY), "-c", PICKER, kind], capture_output=True, text=True, timeout=1800,
                             creationflags=HIDDEN).stdout
    finally:
        PICKING.release()
    paths = [Path(p) for p in json.loads(out or "[]")]
    return {"paths": [str(p) for p in paths if not off_limits(p)], "refused": [p.name for p in paths if off_limits(p)]}


# --- knowledge: what every model starts with, so nothing is learned again each time. knowledge\context.md (Ben's
# standing context: every answer), skills\*.md and handoffs\*.md (the ones that fit a request). Ben adds to it; a
# model only drafts (wrap-up), Ben saves.

STOP = set("about after again also because been before being could does doing done each from have here into just like "
           "make more most much need only other over please same should some such than that their them then there "
           "these they this those very want were what when where which while will with would your".split())
NOTE_NAME = re.compile(r"\w[\w .-]{0,79}")


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", text.lower())) - STOP


def notes(kind: str) -> list[Path]:
    """Skills or handoffs, newest first."""
    return sorted((KNOW / kind).glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)


def note_path(kind: str, name: str = "") -> Path:
    if kind == "context":
        return KNOW / "context.md"
    if kind not in ("skills", "handoffs") or not NOTE_NAME.fullmatch(name) or name.endswith((" ", ".")):
        raise ValueError("a note's name is letters, digits, spaces, '-' and '.' (up to 80)")
    return KNOW / kind / f"{name}.md"


def relevant(kind: str, text: str, n: int) -> list[tuple[str, str]]:
    """The `n` skills or handoffs sharing the most words with a request (two at least, or its name): (name, text).
    ponytail: word overlap on the name and opening; an Ollama embedding model if it starts picking the wrong ones."""
    want, low, found = words(text), text.lower(), []
    for f in notes(kind):
        body = f.read_text(encoding="utf-8", errors="replace")
        score = len(want & words(f.stem.replace("-", " ") + " " + body[:1500]))
        if score >= 2 or (len(f.stem) >= 4 and f.stem.lower() in low):
            found.append((score, f.stem, body))
    found.sort(key=lambda x: -x[0])  # stable: the newest first among equals
    return [(name, body) for _, name, body in found[:n]]


def brief(text: str) -> str:
    """What an answer starts with: Ben's standing context, then the skills and the handoff that fit the request."""
    ctx, parts = KNOW / "context.md", []
    if ctx.is_file() and (body := ctx.read_text(encoding="utf-8", errors="replace").strip()):
        parts.append(f"What {who()} wants every assistant to know (their standing context):\n" + body[-6000:])
    parts += [f"{who('The user')}'s skill '{n}' (how they want this kind of thing done):\n{b[:3000]}"
              for n, b in relevant("skills", text, 2)]
    parts += [f"Handoff '{n}' (notes from earlier work):\n{b[:3000]}" for n, b in relevant("handoffs", text, 1)]
    return "\n\n".join(parts)


def remember(facts: list[str]) -> None:
    """Facts Ben chose to keep: one line each, at the end of his context."""
    ctx = KNOW / "context.md"
    ctx.parent.mkdir(exist_ok=True)
    old = ctx.read_text(encoding="utf-8") if ctx.is_file() else ""
    with open(ctx, "a", encoding="utf-8") as f:
        f.write(("\n" if old and not old.endswith("\n") else "") + "".join(f"- {x}\n" for x in facts))


def knowledge() -> dict:
    read = lambda f: f.read_text(encoding="utf-8", errors="replace")  # noqa: E731
    ctx = KNOW / "context.md"
    return {"folder": str(KNOW), "context": read(ctx) if ctx.is_file() else "",
            **{kind: [{"name": f.stem, "text": read(f)} for f in notes(kind)] for kind in ("skills", "handoffs")}}


def session_brief() -> Path:
    """Ben's context and an index of his skills and handoffs, for a Claude Code session opened from HyperDesk
    (--append-system-prompt-file). Written fresh at each opening, so it is never stale."""
    first = lambda f: next((ln.strip("# ").strip() for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()  # noqa: E731
                            if ln.strip()), "")[:100]
    lines = [brief("") or f"{who('The user')} has no standing context yet.", "",
             f"{who('The user')} keeps skills (how they want things done) and handoffs (notes from earlier work) in {KNOW}. "
             "Before you start, read the ones that fit the task:"]
    for kind in ("skills", "handoffs"):
        lines += [f"- {kind}\\{f.name}: {first(f)}" for f in notes(kind)[:40]]
    path = a.ROOT / "state" / "session-brief.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@contextlib.contextmanager
def gpu_now():
    """The GPU lane for a click Ben waits on (Tidy, Wrap up): taken at once or refused, so `fast` never loads next to
    a picture, a Studio edit or another local job."""
    if not LANES["gpu"].acquire(blocking=False):
        raise ValueError("Your local AI is busy. Try again when the current job finishes.")
    try:
        yield
    finally:
        LANES["gpu"].release()


def gpu_until(done) -> None:
    """Keep the GPU lane, already taken, until `done()` returns: work that runs outside a job (an Images-page render,
    a video edit in the console) holds it as a job would, so no local model loads beside it."""
    def wait():
        try:
            done()
        finally:
            LANES["gpu"].release()
    threading.Thread(target=wait, daemon=True).start()


def gpu_runner(title: str, cmd: list[str], before=None) -> str:
    """RUNNER.start for a console command that loads local models (smoke, bench, the video AI): it takes the GPU lane
    first (refused while a job holds it), runs `before` under it (an unload), and keeps it until the process ends."""
    if not LANES["gpu"].acquire(blocking=False):
        raise ValueError("Your local AI is busy. Try again when the current job finishes.")
    try:
        if before:
            before()
        busy = RUNNER.start(title, cmd)
    except BaseException:
        LANES["gpu"].release()
        raise
    proc = None if busy else RUNNER.proc
    gpu_until(proc.wait) if proc and proc.poll() is None else LANES["gpu"].release()  # at once if it never started
    return busy


def comfy_wait(pid: str) -> None:
    """Until ComfyUI has finished `pid`, or has not known it (or not answered) for about 30 s."""
    missing = 0
    while missing <= 20:
        time.sleep(1.5)
        try:
            st = image_status(pid)
        except (OSError, ValueError, KeyError):
            st = {"done": False, "state": "starting"}
        if st["done"]:
            return
        missing = missing + 1 if st["state"] == "starting" else 0


TIDY_PROMPT = """Below is {who}'s standing context: facts every AI assistant gets with each request. Rewrite it shorter: merge duplicates and lines that say the same thing, keep the newer line when two disagree (newer lines are lower down), group related lines under a few short '## ' headings, '- ' one fact per line. Keep every other fact, number, name and file path exactly. Add nothing. Reply with the new text only.

{context}"""


def tidy(cfg: dict) -> dict:
    """A shorter context.md drafted by the local model (answers see only its newest 6,000 characters, so a long one
    loses its oldest facts). Local only: Ben's context never goes to a cloud model for this. Nothing is saved here:
    the page puts the draft in the editor and Ben saves it (knowledge.save)."""
    ctx = KNOW / "context.md"
    have = ctx.read_text(encoding="utf-8", errors="replace").strip() if ctx.is_file() else ""
    if have.count("\n") < 3:
        raise ValueError("your context is short already")
    if why := a.local_unavailable(cfg):
        raise ValueError(f"local models are off ({why}); try again later")
    with gpu_now():
        r = a.ask_local(cfg, "fast", TIDY_PROMPT.format(context=have[-24000:], who=who()), timeout=300,
                        num_predict=3000)
    with contextlib.closing(a.ledger()) as con:
        a.record_call(con, "knowledge", "tidy", "local:fast", r, "")
    if not r["ok"] or len(str(r["data"]).strip()) < 20:
        raise ValueError(f"fast could not draft it: {(r['error'] or 'empty reply')[:300]}")
    return {"text": r["data"].strip(), "before": len(have), "after": len(r["data"].strip())}


WRAP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "handoff", "facts"],
               "properties": {"title": {"type": "string"}, "handoff": {"type": "string"},
                              "facts": {"type": "array", "items": {"type": "string"}}}}
WRAP_PROMPT = """Close this conversation between {who} and their AI assistants. Reply in JSON with:
- title: 3 to 6 words naming what it was about.
- handoff: a Markdown note for whoever picks this up later: what {who} wanted, what was found or decided (keep facts, numbers, names and file paths exact), and what is still open. Under 200 words, no small talk.
- facts: lasting facts about {who}, their business, their PC or how they like things done, that every assistant should know from now on. One short sentence each; only what {who} said or clearly confirmed; nothing already in {who}'s context below. Usually none or one.

{Who}'s context now:
{context}

The conversation:
{conversation}"""


def wrapup(cfg: dict, tid: str) -> dict:
    """A handoff and facts worth keeping, drafted from a conversation by the local model (Claude, within the cap, when
    the GPU is off limits). Nothing is saved here: Ben edits, picks and saves in the page (knowledge.wrap)."""
    turns = thread_context(tid, chars=24000)
    if not turns:
        raise ValueError("nothing to wrap up yet")
    ctx = KNOW / "context.md"
    have = ctx.read_text(encoding="utf-8", errors="replace").strip() if ctx.is_file() else ""
    prompt = WRAP_PROMPT.format(who=who(), Who=who("The user"), context=have or "(empty)", conversation="\n\n".join(
        f"{who('User') if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in turns))
    with contextlib.closing(a.ledger()) as con:
        if why := a.local_unavailable(cfg):
            if not cfg.get("jobs", {}).get("auto_cloud", True):  # the shared build: the conversation stays on this PC
                raise ValueError(f"local models are off ({why}); try again later")
            if refusal := a.cap_reached(con, cfg):
                raise ValueError(f"local models are off ({why}) and {refusal}")
            model, scratch = "Claude", a.ROOT / "state" / "answers"
            scratch.mkdir(parents=True, exist_ok=True)
            r = a.ask_claude(prompt, WRAP_SCHEMA, scratch, timeout=300)
            a.record_call(con, tid, "wrapup", "claude", r, "")
        else:
            with gpu_now():
                model, r = "fast", a.ask_local(cfg, "fast", prompt, WRAP_SCHEMA, timeout=300, num_predict=1500)
            a.record_call(con, tid, "wrapup", "local:fast", r, "")
    if not r["ok"] or not isinstance(r["data"], dict):
        raise ValueError(f"{model} could not draft it: {(r['error'] or 'unexpected reply')[:300]}")
    d, plain = r["data"], lambda s: re.sub(r"\W+", " ", s).strip().lower()  # noqa: E731
    known = plain(have)  # the prompt says "nothing already in his context"; fast repeated one anyway (2026-10-05)
    return {"model": model, "title": str(d.get("title", ""))[:60], "handoff": str(d.get("handoff", "")),
            "facts": [str(f).strip() for f in d.get("facts") or [] if plain(str(f)) and plain(str(f)) not in known][:10]}


# --- one box: whatever Ben types becomes background jobs, each sent to the cheapest place that can do it, running
# side by side in lanes: one GPU job at a time (local models and SDXL share the card's memory), a few cloud jobs, desk actions free.

IMAGE_WORDS = re.compile(
    r"^(image|draw|paint|sketch|picture|photo)\b|\b(draw|paint|sketch|illustrate)\b|\b(image|picture|photo|illustration)"
    r" of\b|\b(make|create|generate|design|render)\s+(me\s+)?an?\s+(\w+\s+)?(image|picture|photo|logo|wallpaper|poster|"
    r"icon|illustration|render|artwork)\b", re.I)
WEB_WORDS = re.compile(
    r"https?://|\b(latest|newest|news|today|tonight|yesterday|this (week|month|year)|right now|currently|price|prices|"
    r"release notes|released|look up|search (the )?(web|internet|online)|research|google|find out|what'?s new|weather|"
    r"forecast|stock|scores?)\b", re.I)
CODE_WORDS = re.compile(r"\b(fix\w*|implement\w*|add|refactor\w*|rename\w*|debug\w*|tests?|bug|failing)\b", re.I)
OPEN_WORDS = re.compile(r"^(open|launch|start|stop)\s+(.+?)(?:\s+in\s+(claude|codex|qwen|terminal|folder|explorer))?$", re.I)
REMEMBER = re.compile(r"^remember(?:\s+that|\s*:)\s*(.+)", re.I | re.S)  # "remember: …" / "remember that …"
DEEP_WORDS = re.compile(r"\b(in[- ]depth|step by step|thorough\w*|detailed|architecture|strategy|business plan|review "
                        r"(this|my)|analy[sz]\w*|critique|contract|legal|proposal)\b", re.I)
MEDIUM_WORDS = re.compile(r"\b(explain\w*|why|pros and cons|how (do|does|to|can|should)|compare|difference|summari[sz]e|write|draft|code|"
                          r"function|script|regex|sql|translate|rewrite|plan)\b", re.I)
# Split "research X and draw Y" before a new verb; kept only when the parts are different kinds of work.
SPLIT = re.compile(r"\s*(?:\n+|;\s*|,?\s+(?:and then|then|and also|also|and|plus)\s+(?=(?:draw|paint|sketch|make|create|"
                   r"generate|research|look up|find out|search|fix|open|start|launch|stop|explain|write|summari[sz]e|"
                   r"tell me|compare)\b))", re.I)
# "how do I start and stop ComfyUI" is a question, not a desk action: plan() keeps it whole. Start only: a trailing "?"
# already makes open_target miss, and "start comfyui and tell me why it crashed?" is a real start.
QUESTION = re.compile(r"^(how|what|why|when|where|which|who|should|can|could|would|is|are|do|does|did)\b", re.I)
DAILY_VRAM_MIB = 13000  # gpt-oss 20B at its context; below this the quick model answers instead (measured 2026-10-04)
LANE_OF = {"answer:local": "gpu", "image": "gpu", "answer:cloud": "cloud", "research": "cloud", "code": "cloud",
           "open": "desk", "answer:free": "free", "answer:team": "gpu", "answer:gemini": "free", "studio": "gpu",
           "file": "gpu"}


def open_target(text: str) -> dict | None:
    """'open myapp in codex', 'start comfyui', 'stop open webui', 'open edge': the desk action, or None."""
    m = OPEN_WORDS.match(text.strip().rstrip("."))
    if not m:
        return None
    verb, name, tool = m.group(1).lower(), m.group(2).strip().lower(), (m.group(3) or "").lower()
    where = Path(m.group(2).strip().strip('"'))  # "open D:\Docs\taxes in claude": any folder, a session that asks first
    if verb != "stop" and re.match(r"[a-z]:[\\/]", str(where), re.I) and where.is_dir() and not off_limits(where):
        return {"do": "project.open", "path": str(where.resolve()), "tool": {"folder": "explorer"}.get(tool, tool or "claude")}
    for service in a.SERVICES:
        if name in (service.lower(), service.lower().replace(" ", "")):
            on = a.port_open(a.SERVICES[service][0])
            do = "service.stop" if verb == "stop" else "service.open" if on and verb == "open" else "service.start"
            return {"do": do, "name": service}
    if verb == "stop":
        return None
    for browser_name in BROWSERS:
        if name == browser_name.lower() and browser(browser_name):
            return {"do": "browser.open", "name": browser_name}
    if name in FOLDERS and FOLDERS[name].exists():  # a missing one ("sites" on a shared PC) leaves the name to a project
        return {"do": "folder.open", "name": name}
    project = next((e for e in projects() if e["exists"] and e["name"].lower() == name), None)
    if project:
        return {"do": "project.open", "path": project["path"], "tool": {"folder": "explorer"}.get(tool, tool or "claude")}
    return None


def answer_route(cfg: dict, text: str, prefer: str = "auto", files: list[Path] | tuple = (),
                 had_files: bool = False) -> tuple[str, str, str]:
    """(route, model, why) for a question: the quick local model, the reasoning local model, or Claude.
    Attachments scale it: the smallest local model whose context window holds them (and that sees pictures, when
    there are any), Claude past that or for files only Claude reads (PDF, Office). Each model keeps its Modelfile's
    context size: asking Ollama for another reloads the model (26-32 s) and can spill out of VRAM.
    `had_files`: the conversation had attachments, also ones since moved (gone from `files`): run_answer refuses free
    cloud and Gemini for it, so routing must not pick them."""
    if prefer in ("team", "gemini"):
        if files or had_files and prefer == "gemini":
            raise ValueError('Use This PC or Claude for attachments; this mode accepts conversation text only.')
        if prefer == 'gemini':
            return 'answer:gemini', assistant_modes.gemini_model(), 'your Gemini API connection; one request'
        if why := a.local_unavailable(cfg):
            raise ValueError('Local team unavailable: ' + why)
        return 'answer:team', 'local team', 'draft, review, final answer; three local turns'
    deep = len(text) > 1500 or bool(DEEP_WORDS.search(text))
    medium = len(text) > 160 or bool(MEDIUM_WORDS.search(text))
    local_no = a.local_unavailable(cfg)
    with contextlib.closing(a.ledger()) as con:
        cloud_no = a.cap_reached(con, cfg)
    cloud_no = cloud_no or ("" if a.find_tool("claude") else "Claude Code is not installed")
    auto_cloud = cfg.get("jobs", {}).get("auto_cloud", True)
    free = None if local_no else a.vram_free_mib(cfg)
    local = ["fast", "daily"] if free is None or free >= DAILY_VRAM_MIB else ["fast"]  # daily needs ~13 GB free
    pictures, big, fallback = False, "", "fast"  # big: why only Claude takes all of it
    if files and not local_no:
        chars, pictures, only_claude = measure(tuple(map(str, files)))
        need = (len(text) + chars + len(brief(text)) + 6000) // CHARS_PER_TOKEN + REPLY_TOKENS  # 6000: the talk so far
        fallback = "daily" if not pictures and "daily" in local else "fast"  # when nothing holds all of it
        info = {m: model_info(cfg["ollama"]["url"], m) for m in local}
        local = [m for m in local if need <= info[m][0] and (info[m][1] or not pictures)]
        big = "PDF or Office files: Claude reads those" if only_claude else "" if local else \
            f"about {need // 1000}K tokens, more than a local model holds"
    # the free cloud tier takes what would go to Claude, unless files are attached (free tiers may train on input)
    free_no = "files are attached" if files or had_files else a.free_unavailable(cfg)
    free_auto = not free_no and cfg.get("free", {}).get("auto", True)
    free_model = cfg.get("free", {}).get("model", "")
    if prefer == "free":
        if free_no:
            raise ValueError(f"free cloud unavailable: {free_no}")
        return "answer:free", free_model, "you asked for free cloud"
    want = prefer if prefer in ("local", "cloud") else "cloud" if (deep or big) and auto_cloud else "local"
    if want == "local" and local_no and prefer != "local" and free_auto:
        return "answer:free", free_model, f"local models are off ({local_no}): free cloud"
    if want == "local" and local_no and prefer != "local" and auto_cloud and not cloud_no:  # off: nothing goes unasked
        return "answer:cloud", "claude", f"local models are off ({local_no})"
    if want == "cloud" and prefer != "cloud" and free_auto:
        return "answer:free", free_model, "a deep question: free cloud (Claude if you pick it)"
    if want == "cloud" and cloud_no and not local_no:
        want = "local"
        reason = f"Claude unavailable ({cloud_no})"
    elif want == "cloud":
        return "answer:cloud", "claude", "you asked for the cloud" if prefer == "cloud" else \
            f"{big}: Claude" if big else "a deep question: Claude"
    else:
        reason = "you asked for local" if prefer == "local" else ""
    if local_no:
        raise ValueError(f"nowhere to run it: {local_no}" + (f"; {cloud_no}" if cloud_no else ""))
    if not local:
        return "answer:local", fallback, reason or f"{big}: {fallback} gets what fits"
    if medium and "daily" in local:
        return "answer:local", "daily", reason or "needs some reasoning: daily (on this PC)"
    if "fast" in local:
        return "answer:local", "fast", reason or ("a picture: fast (sees images, on this PC)" if pictures else
                                                  "a quick question: fast (on this PC)" if not medium else
                                                  f"daily does not fit in VRAM now ({free} MiB): fast")
    return "answer:local", "daily", reason or "too big for fast: daily (on this PC)"


def file_item(cfg: dict, text: str, files: list[Path] | tuple = ()) -> dict | None:
    """A request to have a file written ("convert this to word", "edit the attached letter"), or None. Always on this
    PC: attachments never leave it."""
    if fmt := file_tools.wants(text, files):
        _, model, _ = answer_route(cfg, text, "local", files)
        return {"kind": "file", "route": "file", "model": model, "target": {"format": fmt},
                "why": f"a {file_tools.LABEL[fmt]} file: {model} writes it on this PC, saved in workspace\\files"}
    return None


def route_one(cfg: dict, text: str, prefer: str = "auto", files: list[Path] | tuple = (), had_files: bool = False) -> dict:
    if m := REMEMBER.match(text.strip()):
        return {"kind": "open", "route": "open", "model": "", "why": "kept in your context (Knowledge)",
                "target": {"do": "context.add", "text": m.group(1).strip()}}
    if target := open_target(text):
        return {"kind": "open", "route": "open", "model": "", "why": "a desk action", "target": target}
    repo = next((r for r in workspace_repos() if re.search(rf"\b{re.escape(r)}\b", text, re.I)), "")
    if repo and CODE_WORDS.search(text) and "share" not in cfg:  # the lite build has no Tasks: it stays a question
        return {"kind": "code", "route": "code", "model": "agent-os", "target": {"repo": repo},
                "why": f"a change in {repo}: Agent OS picks the level and the agent, ends at your merge"}
    if item := file_item(cfg, text, files):
        return item
    if IMAGE_WORDS.search(text):
        return {"kind": "image", "route": "image", "model": "sdxl", "why": "a picture: SDXL on this PC's GPU"}
    if WEB_WORDS.search(text):
        # local web search when it is on and a local model can run now; Auto otherwise falls through to research
        if prefer in ('auto', 'local') and local_web.allowed('fast') and not files and not a.local_unavailable(cfg):
            return {'kind': 'answer', 'route': 'answer:local', 'model': 'fast',
                    'why': 'local model with free public web search enabled'}
        # the pick wins (This PC raises when it cannot run); neither searches the web, nor does a PC without Claude Code,
        # nor Auto with auto_cloud off ("questions stay local unless you pick Cloud": the shared build)
        if prefer in ('local', 'free') or not a.find_tool('claude') or \
                (prefer == 'auto' and not cfg.get('jobs', {}).get('auto_cloud', True)):
            route, model, why = answer_route(cfg, text, prefer, files, had_files)
            return {'kind': 'answer', 'route': route, 'model': model,
                    'why': f'{why}; no web search, so recent facts may be missing'}
        return {"kind": "research", "route": "research", "model": "claude+web",
                "why": "needs current information: the research agent (web, read-only browser)"}
    route, model, why = answer_route(cfg, text, prefer, files, had_files)
    return {"kind": "answer", "route": route, "model": model, "why": why}


def plan(cfg: dict, text: str, prefer: str = "auto", files: list[Path] | tuple = (), had_files: bool = False) -> list[dict]:
    """The jobs one input asks for. Split only where the parts are different kinds of work (or on new lines).
    `files`: the conversation's attachments; an answer reads them."""
    text = text.strip()
    if m := re.match(r'^(?:ask )?(gemini|local team)\s*:\s*(.+)$', text, re.I | re.S):
        prefer, text = ('gemini' if m[1].lower() == 'gemini' else 'team'), m[2]
    if prefer == 'improve' or (prefer not in ('team', 'gemini') and re.match(r'^(?:improve|change|redesign|fix) hyperdesk\b', text, re.I)
                               and 'improve' not in hidden(cfg)):  # no git here: typed, it is just a question
        if files:
            raise ValueError('Describe the HyperDesk change without attachments.')
        return [{'text': text, 'kind': 'code', 'route': 'code', 'model': 'agent-os',
                 'why': SELF_WHY,
                 'target': {'repo': a.ROOT.name, 'path': str(a.ROOT), 'level': '2'}}]
    if prefer in ('team', 'gemini'):
        route, model, why = answer_route(cfg, text, prefer, files, had_files)
        return [{'text': text, 'kind': 'answer', 'route': route, 'model': model, 'why': why}]
    parts = [p.strip(" ,.") for p in SPLIT.split(text) if p and p.strip(" ,.")]
    jobs = [{"text": p, **route_one(cfg, p, prefer, files, had_files)} for p in parts]
    explicit = "\n" in text or ";" in text
    if len(jobs) > 1 and not explicit and (len({j["kind"] for j in jobs}) == 1 and jobs[0]["kind"] in ("answer", "code")
                                           or QUESTION.match(text) and any(j["kind"] == "open" for j in jobs)):
        jobs = [{"text": text, **route_one(cfg, text, prefer, files, had_files)}]  # "explain X and write Y": one request after all
    return jobs[:6]


# A self-change's stored `why` is all the ledger keeps of its target (Agent OS itself): Try again reads it back
SELF_WHY = "save your current version, build and test the change, then you click Apply update"
FIX_WHY = "a self-check finding: Agent OS on itself"


class Job:
    def __init__(self, text: str, item: dict, thread: str = "", batch: str = "", context: list | None = None,
                 said: str = "", attach: list | None = None, read: list | None = None):
        self.id, self.created, self.text, self.said = secrets.token_hex(5), a.now(), text, said or text
        self.kind, self.route, self.model, self.why = item["kind"], item["route"], item["model"], item["why"]
        self.target = item.get("target") or {}
        self.thread, self.batch = thread or self.id, batch or self.id  # conversation, and the input it came from
        self.context = context or []  # earlier turns of the conversation, for answers
        self.attach = [str(p) for p in attach or []]  # what Ben attached to this input (shown on it)
        self.read = [Path(p) for p in read or []]  # what an answer reads: those and the conversation's earlier ones
        self.state, self.output, self.error, self.task, self.files = "queued", "", "", "", []
        self.started = self.seconds = 0.0
        self.cancelled, self.proc, self.stream = False, None, None

    def view(self) -> dict:
        return {"id": self.id, "created": self.created, "text": self.text, "kind": self.kind, "route": self.route,
                "model": self.model, "why": self.why, "state": self.state, "output": self.output[-20000:],
                "error": self.error, "task": self.task, "files": self.files, "thread": self.thread, "batch": self.batch,
                "said": self.said, "attach": self.attach,
                "seconds": round(time.monotonic() - self.started if self.state == "running" else self.seconds, 1)}

    def cancel(self) -> None:
        self.cancelled = True
        if self.state == "queued":
            self.state = "cancelled"
        if self.route == 'studio':
            with contextlib.suppress(OSError, ValueError):
                (studio.folder(self.target['recording']) / 'cancel-requested').write_text('stop', encoding='utf-8')
            return  # let the worker stop its active FFmpeg/Whisper child as well
        with contextlib.suppress(OSError, AttributeError):  # an agent_os.py run: run_agent_os ends its whole tree
            if self.stream:
                self.stream.close()
        # a picture: run_image stops its own ComfyUI prompt (it knows the id), never another render


JOBS: dict[str, Job] = {}
LANES = {"gpu": threading.Semaphore(1), "cloud": threading.Semaphore(2), "desk": threading.Semaphore(4),
         "free": threading.Semaphore(3)}  # main() sizes cloud; free: limited by provider rates, not the cap


def submit(cfg: dict, text: str, item: dict, thread: str = "", batch: str = "", context: list | None = None,
           said: str = "", attach: list | None = None, read: list | None = None) -> Job:
    job = Job(text, item, thread, batch, context, said, attach, read)
    JOBS[job.id] = job
    with contextlib.closing(a.ledger()) as con:
        con.execute("insert into jobs (id, created, input, kind, route, why, state, thread, batch, said, attach) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job.id, job.created, text, job.kind, f"{job.route}:{job.model}", job.why, job.state, job.thread,
                     job.batch, job.said, json.dumps(job.attach) if job.attach else ""))
        con.commit()
    threading.Thread(target=run_job, args=(cfg, job), daemon=True).start()
    return job


def run_job(cfg: dict, job: Job) -> None:
    with LANES[LANE_OF[job.route]]:  # queued here until the lane is free
        if not job.cancelled:
            job.state, job.started = "running", time.monotonic()
            try:
                RUNNERS[job.kind](cfg, job)
            except Exception as e:  # one failed job must not take the server thread with it
                job.error = job.error or f"{type(e).__name__}: {e}"[:600]
            job.seconds = time.monotonic() - job.started
        job.state = "cancelled" if job.cancelled else "failed" if job.error else "done"
    with contextlib.closing(a.ledger()) as con:
        con.execute("update jobs set state = ?, output = ?, error = ?, task = ?, files = ?, seconds = ? where id = ?",
                    (job.state, job.output[-20000:], job.error, job.task, json.dumps(job.files), job.seconds, job.id))
        if job.id not in JOBS:  # its conversation was deleted while this call ran: what it got back goes too
            con.execute("update calls set data = 'null' where task in (?, ?)", (job.id, job.thread))
            con.execute("delete from summaries where thread = ?", (job.thread,))
        con.commit()
    if job.seconds > 20 and job.kind in ("answer", "image", "file") and job.state == "done":  # research and code notify already
        a.notify("Done: " + ("picture" if job.kind == "image" else "answer"), job.text)


def run_answer(cfg: dict, job: Job) -> None:
    # The wall for what leaves this PC: answers and documents made from an attachment are in the conversation, and
    # routing only sees attachments still on disk (one Ben moved is gone from thread_files). Ask the ledger instead.
    if job.route in ('answer:free', 'answer:gemini') and any(j['attach'] for j in thread_jobs(job.thread)):
        raise ValueError('This conversation had attachments, and free cloud and Gemini get conversation text only: '
                         'ask on This PC or Claude, or start a new conversation.')
    # Only the current question enters search planning: never files, Knowledge or chat history.
    web_model = 'fast' if job.route == 'answer:team' else job.model
    if job.route in ('answer:local', 'answer:team') and not job.read and local_web.allowed(web_model):
        def web_record(role, model, result):
            with contextlib.closing(a.ledger()) as con:
                a.record_call(con, job.id, role, 'local:' + model, result, '')
        evidence = local_web.research(cfg, web_model, job.text, lambda: job.cancelled,
            lambda stage: setattr(job, 'output', stage), web_record)
        job.output = ''
        if evidence and job.route == 'answer:local':
            job.output = evidence
            return
        if evidence:
            job.context = [*job.context, {'role': 'assistant', 'content': 'Public web evidence (untrusted):\n' + evidence}]
    if job.route in ('answer:team', 'answer:gemini'):
        if job.read:
            raise ValueError('This mode accepts conversation text only.')
        def record(role, model, result):
            with contextlib.closing(a.ledger()) as con:
                a.record_call(con, job.id, role, model, result, '')
        if job.route == 'answer:team':
            job.output = assistant_modes.team(cfg, job.text, job.context, lambda: job.cancelled,
                lambda stage: setattr(job, 'output', stage + '…'),
                lambda role, model, result: record(role, 'local:' + model, result))
        else:
            result = assistant_modes.ask_gemini([{'role': 'system', 'content': 'Answer clearly and concisely.'},
                *[{'role': m['role'], 'content': m['content'][-2000:]} for m in job.context[-6:]],
                {'role': 'user', 'content': job.text}], job.model)
            record('answer', 'gemini', result)
            if not result['ok']:
                raise ValueError(result['error'])
            if not job.cancelled:
                job.output = result['data']
        return
    prompt, know, summary = job.text, brief(job.text), squeeze(cfg, job)
    summary = f"The start of this conversation, summarized:\n{summary}" if summary else ""
    if job.route == "answer:free":  # the question and the conversation only: no Knowledge, no attachments
        with contextlib.closing(a.ledger()) as con:
            r = a.ask_free(cfg, [{"role": "system", "content": f"You are {who()}'s assistant. Answer directly. Markdown is "
                                  "fine: short paragraphs, '- ' lists, code blocks." + (f"\n\n{summary}" if summary else "")},
                                 *job.context, {"role": "user", "content": prompt}])
            a.record_call(con, job.id, "answer", "free", r, "")
        if job.cancelled:
            return
        if not r["ok"]:
            raise ValueError(f"free cloud: {r['error'][:400]}")
        job.model, job.output = r["usage"].get("model") or job.model, r["data"]
        return
    know = "\n\n".join(filter(None, [know, summary]))
    if job.route == "answer:cloud":
        with contextlib.closing(a.ledger()) as con:
            if refusal := a.cap_reached(con, cfg):
                raise ValueError(refusal)
            scratch = a.ROOT / "state" / "answers"
            scratch.mkdir(parents=True, exist_ok=True)
            earlier = "".join(f"\n{who('User') if m['role'] == 'user' else 'You'}: {m['content']}" for m in job.context)
            files = "".join(f"\n- {p}{' (a folder)' if p.is_dir() else ''}" for p in job.read)
            # read-only (--restricted): its file tools reach only these folders, minus the secrets in CLAUDE_DENY
            dirs = list(dict.fromkeys([*(p if p.is_dir() else p.parent for p in job.read), *([KNOW] if KNOW.is_dir() else [])]))
            r = a.ask_claude(f"Answer {who()}'s question directly. Markdown is fine: short paragraphs, '- ' lists, code blocks."
                             + (f"\n\n{know}" if know else "")
                             + (f"\n\n{who('The user')}'s other skills and handoffs are in {KNOW}; the ones that fit are "
                                "above. Read another only if the question needs it." if KNOW.is_dir() else "")
                             + (f"\n\n{who('The user')} attached these. Read what the question needs with your tools:{files}"
                                if files else "")
                             + (f"\n\nThe conversation so far:{earlier}" if earlier else "")
                             + f"\n\nQuestion: {prompt}", a.SMOKE_SCHEMA, scratch, timeout=600, dirs=dirs, deny=CLAUDE_DENY)
            a.record_call(con, job.id, "answer", "claude", r, "")
        if job.cancelled:
            return
        if not r["ok"]:
            raise ValueError(r["error"][:400])
        job.output = r["data"]["answer"]
        return
    if game := a.game_running(cfg):
        raise ValueError(f"game running ({game}): no local model loads now")
    # attachments fill what the model's context window has left after the question, knowledge, talk so far and reply
    window, sees = model_info(cfg["ollama"]["url"], job.model)
    room = (window - REPLY_TOKENS) * CHARS_PER_TOKEN - len(prompt) - len(know) - sum(len(m["content"]) for m in job.context)
    got = pack(tuple(map(str, job.read)), max(room, 0)) if job.read else {"text": "", "images": [], "left": []}
    unseen = [Path(p).name for p in got["left"]] + ([f"{len(got['images'])} picture(s): {job.model} cannot see pictures"]
                                                   if got["images"] and not sees else [])
    user = {"role": "user", "content": (got["text"] + "\n\n" if got["text"] else "") + prompt
            + '\n\nThis answer has no live web evidence. Do not claim you searched or verified current information.'
            + (f"\n\n(Attached but not given to you, too big or not text: {', '.join(unseen[:15])}"
               f"{' …' if len(unseen) > 15 else ''}. Say so if it matters.)" if unseen else ""),
            **({"images": got["images"]} if got["images"] and sees else {})}
    request = urllib.request.Request(cfg["ollama"]["url"] + "/api/chat", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": job.model, "stream": True, "messages": [
                                         *([{"role": "system", "content": know}] if know else []), *job.context,
                                         user]}).encode())
    with ollama_open(request, timeout=600) as r:
        job.stream = r
        for line in r:
            if job.cancelled:
                return
            part = json.loads(line)
            if part.get("error"):
                raise ValueError(part["error"])
            job.output += (part.get("message") or {}).get("content", "")


def run_agent_os(job: Job, args: list[str]) -> None:
    """research or run as an Agent OS subprocess; its output streams into the job, its task id is kept."""
    if job.cancelled:  # Stop pressed before the run started (e.g. while saving the app version)
        return
    log = a.ROOT / "logs" / "jobs" / f"{job.id}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as sink:
        job.proc = subprocess.Popen(agent_os(*args), stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=HIDDEN, cwd=a.ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    killed = False
    while True:
        done = job.proc.poll() is not None
        if job.cancelled and not done and not killed:  # here, not on the HTTP thread; while the parent lives /T
            a.kill_tree(job.proc.pid)  # finds its claude/opencode/tests children, which Popen.kill would orphan
            for x in list(APPROVALS.values()):  # its asker died with the tree: refuse now, not after 5 minutes
                if job.task and x.get("task_id") == job.task:
                    x["event"].set()
            if job.target.get("path") == str(a.ROOT):  # only a self-change (--no-paid) can have loaded qwen-agent
                with contextlib.suppress(Exception):  # the killed run never reached ask_opencode's unload of it
                    a.ollama_unload(Desk.cfg, a.LOCAL_AGENT.removeprefix("local:"))
            killed = True
        job.output = log.read_text(encoding="utf-8", errors="replace")[-20000:]
        if not job.task and (m := re.search(r"^\[(\d{8}-\d{6}-\d{3})\]", job.output, re.M)):
            job.task = m.group(1)
        if done:
            break
        time.sleep(0.5)
    if job.proc.returncode and not job.cancelled:
        job.error = next((line for line in reversed(job.output.splitlines()) if "FAILED" in line), "") \
            or f"exit {job.proc.returncode}"
    if job.cancelled and job.task:  # close its ledger row, so Discard appears and update() stops counting it busy
        with contextlib.closing(a.ledger()) as con:
            row = con.execute("select state from tasks where id = ?", (job.task,)).fetchone()
            if row and row[0] in a.BUSY_STATES:  # never over AWAITING_APPROVAL: merge_task needs it
                a.set_state(con, job.task, "FAILED", "USER_ACTION_REQUIRED: stopped in HyperDesk",
                            error="stopped in HyperDesk", error_class="USER_ACTION_REQUIRED")


def run_research(cfg: dict, job: Job) -> None:
    run_agent_os(job, ["research", *(["--show-browser"] if job.target.get("show") else []), "--", job.text])
    if job.task:  # the answer and its sources, not the log
        found = next((r for r in research_list() if r["id"] == job.task), {})
        if found.get("answer"):
            job.output = found["answer"] + "".join(f"\n[{i}] {s.get('title', '')}: {s.get('url', '')}"
                                                   for i, s in enumerate(found.get("sources") or [], 1))


def wait_for(lock, job: Job) -> bool:
    """Queue on a lock or lane like any job, but give up when Ben presses Stop."""
    while not lock.acquire(timeout=2):
        if job.cancelled:
            return False
    return True


def run_code(cfg: dict, job: Job) -> None:
    t = job.target
    if t.get('path') == str(a.ROOT):  # Agent OS on itself: free or this PC only, never a paid call
        job.output = 'Waiting for the other HyperDesk change to finish…'
        # ponytail: a waiting change holds one cloud slot; give self-changes their own lane if that starves research
        if not wait_for(self_edit.LOCK, job):
            return
        with contextlib.ExitStack() as held:
            held.callback(self_edit.LOCK.release)
            if job.cancelled:  # Stop pressed just as the lock came free
                return
            omni = str((cfg.get('agents') or {}).get('opencode_model', '')).startswith('omniroute/')
            if omni and a.free_unavailable(cfg) == 'OmniRoute is not running':  # start it, as run_image does ComfyUI
                job.output = 'Starting the free cloud (OmniRoute)…'
                act(cfg, {'do': 'service.start', 'name': 'OmniRoute'})
                for _ in range(30):
                    if a.port_open(a.SERVICES['OmniRoute'][0]) or job.cancelled:
                        break
                    time.sleep(2)
                time.sleep(3)  # it answers a moment after the port opens
            # Always the GPU lane: qwen-agent (after a free reply that fails or edits nothing) and the local reviewer
            # may load during the run, so no SDXL, studio edit or local answer beside them.
            # ponytail: a self-change on the free tier then queues local jobs for its whole run; fine while they are rare
            job.output = 'Waiting for the GPU (another local job is running)…'
            if not wait_for(LANES['gpu'], job):
                return
            held.callback(LANES['gpu'].release)
            if job.cancelled:  # Stop pressed while OmniRoute started or the lane came free: nothing saved
                return
            job.output = 'Saving your current app version…'
            version = self_edit.checkpoint()
            log_action('saved app version', version)
            job.output = 'Version saved. Preparing your change in a separate copy…'
            run_agent_os(job, ['run', '--repo', str(a.ROOT), '--level', '2', '--no-paid', '--agent', 'opencode',
                               '--', job.text])
        return
    run_agent_os(job, ["run", "--repo", t.get("path") or str(WORKSPACE / t["repo"]), *(["--agent", t["agent"]] if t.get("agent") else []),
                       *(["--level", t["level"]] if t.get("level") else []), "--", job.text])


def run_image(cfg: dict, job: Job) -> None:
    if "ComfyUI" not in a.SERVICES:  # a shared install: SERVICES lists only start scripts that exist
        raise ValueError("Pictures need ComfyUI, which is not set up on this PC")
    if not a.port_open(8188):  # automation: start ComfyUI and wait for it
        act(cfg, {"do": "service.start", "name": "ComfyUI"})
        for _ in range(90):
            if a.port_open(8188) or job.cancelled:
                break
            time.sleep(2)
        time.sleep(3)  # its API answers a moment after the port opens
    if job.cancelled:
        return
    queued = generate(cfg, {"prompt": job.text, **job.target})
    job.output, missing = f"seed {queued['seed']}", 0
    while True:
        if job.cancelled:  # only this prompt: rendering, it is interrupted; waiting, it leaves the queue
            with contextlib.suppress(OSError, ValueError):  # both answer with an empty body, which json.load refuses
                comfy("/interrupt", {"prompt_id": queued["id"]})
            with contextlib.suppress(OSError, ValueError):
                comfy("/queue", {"delete": [queued["id"]]})
            return
        time.sleep(1.5)
        st = image_status(queued["id"])
        if st["done"]:
            if st["error"]:
                raise ValueError(st["error"])
            job.files = st["files"]
            return
        # in neither its history nor its queue for ~30 s: cancelled or cleared in ComfyUI's own page (as the Images page)
        missing = missing + 1 if st["state"] == "starting" else 0
        if missing > 20:
            raise ValueError("ComfyUI no longer has this picture job (it was cancelled or cleared in ComfyUI)")
        job.output = f"seed {queued['seed']} · {st['state']}"


def run_open(cfg: dict, job: Job) -> None:
    job.output = act(cfg, job.target)


def run_studio(cfg: dict, job: Job) -> None:
    rid = job.target['recording']
    logfile = a.ROOT / 'logs' / f'studio-{rid}.txt'
    # Whisper cannot evict Ollama's idle resident models (this job holds the GPU lane, so none is in use): unload them
    with contextlib.suppress(OSError, ValueError, KeyError):
        for m in a.get_json(cfg['ollama']['url'] + '/api/ps')['models']:
            ollama_keep(cfg, m['name'], 0)
    with logfile.open('w', encoding='utf-8') as sink:
        job.proc = subprocess.Popen([str(PY), str(a.ROOT / 'studio_edit.py'), rid], cwd=a.ROOT,
                                    stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=HIDDEN)
        job.proc.wait()
    value = studio.read(rid)
    if job.cancelled or (job.proc.returncode and value['state'] != 'failed'):
        value.update(state='failed', message='Editing stopped. Your original is saved. You can retry the edit.')
        studio.write(rid, value)
    job.output = value['message']
    if value['state'] == 'failed':
        job.error = value['message']


def run_file(cfg: dict, job: Job) -> None:
    """A local model writes the content; file_tools turns it into the file in workspace/files. An edit reads the
    attachment and saves a new copy: the original is never written to. No cloud, no tools, no shell."""
    fmt = job.target["format"]
    if why := a.local_unavailable(cfg):
        raise ValueError(f"files are made on this PC and local models are off: {why}")
    if game := a.game_running(cfg):
        raise ValueError(f"game running ({game}): no local model loads now")
    window, _ = model_info(cfg["ollama"]["url"], job.model)
    # an earlier document's head (its "Saved …" line and opening) matters most for "now make it a pdf": keep that end
    earlier = "".join(f"\n{who('User') if m['role'] == 'user' else 'You'}: "
                      f"{m['content'][:3200] if m['role'] == 'assistant' else m['content'][-2000:]}" for m in job.context[-4:])
    room = (window - 8192) * CHARS_PER_TOKEN - len(job.text) - len(earlier) - 2000
    got = pack(tuple(map(str, job.read)), max(room, 0)) if job.read else {"text": "", "left": []}
    editing = job.read if file_tools.EDIT_THIS.search(job.text) else ()
    # an edit, or a file attached to this request, needs its text; "make a pdf about X" in a talk that only had a
    # screenshot does not
    if job.read and not got["text"] and (editing or job.attach):
        raise ValueError("none of the attached files has text this PC can read (a scanned PDF or a picture?)")
    job.output = f"Writing the {file_tools.LABEL[fmt]}…"
    r = a.ask_local(cfg, job.model, file_tools.prompt(fmt, job.text, got["text"], earlier), timeout=900,
                    num_predict=8192, keep_alive=0)
    with contextlib.closing(a.ledger()) as con:
        a.record_call(con, job.id, "file", "local:" + job.model, r, "")
    if job.cancelled:
        return
    if not r["ok"]:
        raise ValueError(r["error"][:400])
    path = file_tools.save(fmt, job.text, r["data"], editing)
    log_action("made file", str(path))
    skipped = f"\n\nNot read (too big or no text): {', '.join(Path(p).name for p in got['left'][:10])}" \
        if got["left"] else ""
    job.output = f"Saved **{path.name}** in `{path.parent}`.{skipped}\n\n---\n\n{file_tools.clean(r['data'])[:3000]}"


RUNNERS = {"answer": run_answer, "research": run_research, "code": run_code, "image": run_image, "open": run_open,
           "studio": run_studio, "file": run_file}


# --- conversations: each input is a batch of jobs; inputs in one conversation (thread) share context, like a chat

JOB_COLUMNS = "id, created, input, kind, route, why, state, output, error, task, files, seconds, thread, batch, said, attach"
THREAD_ID = re.compile(r"[0-9a-f]{10,12}")


def mark_interrupted() -> None:
    """Jobs that were running when HyperDesk last stopped will never finish: say so."""
    with contextlib.closing(a.ledger()) as con:
        con.execute("update jobs set state = 'interrupted' where state in ('queued', 'running')")
        con.commit()


def row_view(row: tuple) -> dict:
    """A finished job from the ledger, in the shape Job.view() gives; a live one comes from memory instead."""
    jid, created, text, kind, route, why, state, output, error, task, files, seconds, thread, batch, said, attach = row
    if jid in JOBS:
        return JOBS[jid].view()
    # stored as "<route>:<model>"; answer routes have a colon of their own and model names may too (gpt-oss:20b)
    head = 2 if (route or "").startswith("answer:") else 1
    parts = (route or "").split(":", head)
    route, model = ":".join(parts[:head]), parts[head] if len(parts) > head else ""
    return {"id": jid, "created": created, "text": text, "kind": kind, "route": route or kind, "model": model,
            "why": why or "", "state": state, "output": output or "", "error": error or "", "task": task or "",
            "files": json.loads(files) if files else [], "seconds": round(seconds or 0, 1),
            "thread": thread or jid, "batch": batch or jid, "said": said or text, "attach": json.loads(attach) if attach else []}


def threads() -> list[dict]:
    """Conversations for the sidebar, newest first: the first input is the title."""
    with contextlib.closing(a.ledger()) as con:
        rows = con.execute("select coalesce(thread, id), created, coalesce(said, input) from jobs order by created, rowid"
                           ).fetchall()
    found: dict[str, dict] = {}
    for tid, created, text in rows:
        t = found.setdefault(tid, {"id": tid, "title": text[:90], "started": created, "updated": created})
        t["updated"] = created
    busy = {j.thread for j in list(JOBS.values()) if j.state in ("queued", "running")}
    return [dict(t, busy=t["id"] in busy) for t in sorted(found.values(), key=lambda t: t["updated"], reverse=True)][:100]


def thread_jobs(tid: str) -> list[dict]:
    """A conversation's jobs in order, each with its Agent OS task's state now (a merge changes what a card offers)."""
    with contextlib.closing(a.ledger()) as con:
        rows = con.execute(f"select {JOB_COLUMNS} from jobs where coalesce(thread, id) = ? order by created, rowid",
                           (tid,)).fetchall()
        states = dict(con.execute("select id, state from tasks where id in (select task from jobs where "
                                  "coalesce(thread, id) = ? and task != '')", (tid,)).fetchall())
    return [dict(v, task_state=states.get(v["task"], "")) for v in map(row_view, rows)]


def thread_context(tid: str, chars: int = 12000, before: str = "") -> list[dict]:
    """The conversation so far as chat messages (each input, then the answers it got), newest kept within `chars`.
    `before`: a batch id; only the turns before it count (asking a question again must not show it its own answer).
    chars=0: all of it (an answer squeezes it later, summarizing what does not fit)."""
    turns: list[tuple[str, list, list]] = []
    for j in thread_jobs(tid):
        if before and j["batch"] == before:
            break
        if not turns or turns[-1][0] != j["batch"]:
            turns.append((j["batch"], [j["said"]], []))
        # a file job's document too, so "now make it a pdf" has its source (the free tier never sees a conversation
        # with attachments: answer_route refuses it then)
        if j["kind"] in ("answer", "research", "file") and j["state"] == "done" and j["output"]:
            turns[-1][2].append(j["output"])
    messages = []
    for _, asked, answers in turns:
        messages.append({"role": "user", "content": "\n".join(asked)})
        if answers:
            messages.append({"role": "assistant", "content": "\n\n".join(answers)})
    return messages[overflow(messages, chars):] if chars else messages


def overflow(messages: list[dict], chars: int) -> int:
    """How many of the oldest messages must go for the rest to fit in `chars` (the newest one always stays)."""
    n, total = 0, sum(len(m["content"]) for m in messages)
    while len(messages) - n > 1 and total > chars:
        total -= len(messages[n]["content"])
        n += 1
    return n


SUMMARY_PROMPT = """Summarize the start of a conversation between {who} and their AI assistants for the assistant who continues it. Keep what {who} asked for, what was answered or decided, and every fact, number, name and file path exactly. Under 200 words, no preamble.

{text}"""


def squeeze(cfg: dict, job: Job, chars: int = 12000) -> str:
    """Trims job.context to the newest `chars` and returns a summary of what was cut ('' if nothing was), so a long
    conversation keeps what it said without resending all of it. Rolling and cached per conversation (ledger table
    `summaries`): each summary is written once, from the previous one plus the turns cut since. Written by the job's
    own local model (already loaded: no swap) or `fast` for a cloud answer when the GPU lane is free; with no local model
    the turns are dropped."""
    n = overflow(job.context, chars)
    older, job.context = job.context[:n], job.context[n:]
    if not n:
        return ""
    with contextlib.closing(a.ledger()) as con:
        row = con.execute("select upto, summary from summaries where thread = ? and upto <= ? order by upto desc limit 1",
                          (job.thread, n)).fetchone()
        if row and row[0] == n:
            return row[1]
        start, prior = row or (0, "")
        model = "" if a.local_unavailable(cfg) else job.model if job.route == "answer:local" else "fast"
        borrow = model == "fast" and job.route != "answer:local"  # a cloud or free answer borrows the GPU lane
        if borrow and not LANES["gpu"].acquire(blocking=False):
            model, borrow = "", False  # GPU busy: no stall and no model swap; an older summary, the next answer tries again
        if not model and job.route == "answer:free" and not a.free_unavailable(cfg):
            model = "free"  # that conversation goes there anyway
        if not model:
            return prior
        text = (f"Summary of the part before:\n{prior}\n\nThen:\n" if prior else "") + "\n\n".join(
            f"{who('User') if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in older[start:])
        ask = SUMMARY_PROMPT.format(text=text[-30000:], who=who())
        try:
            r = a.ask_free(cfg, [{"role": "user", "content": ask}], max_tokens=800) if model == "free" else \
                a.ask_local(cfg, model, ask, timeout=180, num_predict=800)
        finally:
            if borrow:
                LANES["gpu"].release()
        a.record_call(con, job.thread, "summary", "free" if model == "free" else f"local:{model}", r, "")
        if not r["ok"] or not str(r["data"]).strip():
            return prior  # an older summary beats none; the next answer tries again
        con.execute("insert or replace into summaries values (?, ?, ?)", (job.thread, n, r["data"].strip()))
        con.commit()
        return r["data"].strip()


def pinned_item(cfg: dict, prefer: str) -> dict | None:
    """'model:<name>' from the composer's picker: one answer on exactly that model, no routing."""
    if not prefer.startswith("model:"):
        return None
    name = prefer.removeprefix("model:")
    if name == "claude":
        return {"kind": "answer", "route": "answer:cloud", "model": "claude", "why": "you picked Claude"}
    if name not in model_names(cfg):
        raise ValueError(f"unknown model {name!r}")
    return {"kind": "answer", "route": "answer:local", "model": name, "why": f"you picked {name}"}


# --- doing things

class Runner:
    """One agent-os style command at a time; its output goes to a file the page follows."""

    def __init__(self):
        self.title, self.proc, self.lock = "", None, threading.Lock()

    def start(self, title: str, cmd: list[str]) -> str:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return f"Busy: wait for “{self.title}” to finish."
            CMD_LOG.parent.mkdir(exist_ok=True)
            with open(CMD_LOG, "w", encoding="utf-8") as sink:  # a file, not a pipe: the command outlives the window
                self.proc = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=HIDDEN, cwd=a.ROOT,
                                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.title = title
        log_action("command", title)
        proc = self.proc
        threading.Thread(target=lambda: log_action("command finished", f"{title}: exit {proc.wait()}"),
                         daemon=True).start()
        return ""

    def busy(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def state(self) -> dict:
        return {"title": self.title, "running": self.busy(), "exit": self.proc.poll() if self.proc else None}


RUNNER = Runner()
HEALTH: dict = {"at": "", "running": False, "results": []}


def check_health(cfg: dict) -> None:
    if HEALTH["running"]:
        return
    HEALTH["running"] = True

    def go():
        try:
            HEALTH["results"] = [list(r) for r in a.doctor_checks(cfg)]
            HEALTH["at"] = a.now()
        finally:
            HEALTH["running"] = False
    threading.Thread(target=go, daemon=True).start()


def ffmpeg_ready() -> bool:
    """Quick edit can run: ffmpeg and ffprobe on PATH or in winget's folder (found even if HyperDesk started first)."""
    return all(shutil.which(t, path=quick_edit.ffmpeg_path()) for t in ("ffmpeg", "ffprobe"))


def video_projects() -> list[dict]:
    """The Video page: each edited video's folder (ai-video-editor\\projects, or Quick edit's), newest first."""
    root, out = FOLDERS["videos"], []
    for d in sorted((p for p in root.iterdir() if p.is_dir()) if root.is_dir() else [],
                    key=lambda p: p.stat().st_mtime, reverse=True):
        summary, clean = {}, {}
        with contextlib.suppress(OSError, ValueError, KeyError, StopIteration):
            summary = json.loads(next(d.glob("scene_analysis_*.json")).read_text(encoding="utf-8"))["summary"]
        with contextlib.suppress(OSError, ValueError, KeyError):  # the clean-up cut's numbers
            plan = json.loads((d / "edit_plan.json").read_text(encoding="utf-8"))
            clean = {k: plan[k] for k in ("cuts", "before", "after")}
        vertical = next(d.glob("*_vertical.mp4"), None)
        youtube = (d / "timeline_youtube.fcpxml").exists() or next(d.glob("*_youtube.mp4"), None) is not None
        out.append({"name": d.name.rsplit("-", 1)[0] if QUICK else d.name,  # Quick edit's folders end in a path hash
                    "path": str(d), "when": d.stat().st_mtime, "summary": summary, "clean": clean,
                    "youtube": youtube, "vertical": vertical.name if vertical else ""})
    return out


def task_info(name: str) -> dict:
    """A Task Scheduler entry: there or not, on or off, next and last run."""
    code, out = a.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"], 20)
    info = dict(line.split(":", 1) for line in out.splitlines() if ":" in line) if code == 0 else {}
    pick = lambda k: info.get(k, "").strip()  # noqa: E731

    def iso(text: str) -> str:  # schtasks prints US-style local times; "N/A" or 1999 (never ran) become ""
        with contextlib.suppress(ValueError):
            when = datetime.datetime.strptime(text, "%m/%d/%Y %I:%M:%S %p")
            return when.isoformat() if when.year > 2000 else ""
        return ""
    return {"exists": code == 0, "enabled": pick("Scheduled Task State") == "Enabled",
            "next": pick("Next Run Time"), "last": pick("Last Run Time"), "result": pick("Last Result"),
            "next_iso": iso(pick("Next Run Time")), "last_iso": iso(pick("Last Run Time"))}


def task_toggle(name: str, on: bool) -> None:
    """On or off: the task stays registered, Task Scheduler just skips it."""
    code, out = a.run(["schtasks", "/Change", "/TN", name, "/ENABLE" if on else "/DISABLE"], 20)
    if code != 0:
        raise ValueError(f"Task Scheduler refused: {out[:200] or 'is the task registered?'}")


def site_monitor(site: str = "", with_page: bool = False) -> dict:
    """The Website monitor page: its sites (sites.toml), the hourly task, the chosen site's last run and dashboard."""
    import tomllib
    found: dict = {}
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):  # no sites.toml yet, or one half-edited by hand
        found = tomllib.loads((SITES / "sites.toml").read_text(encoding="utf-8"))
    sites = [{"id": k, "name": str(v.get("name") or k), "url": str(v.get("url", "")), "theme": str(v.get("theme", ""))}
             for k, v in found.items() if isinstance(v, dict) and SITE_ID.fullmatch(k)]
    ids = [s["id"] for s in sites]
    pick = site if site in ids else (ids[0] if ids else "")
    runs, started = [], ""
    with contextlib.suppress(Exception):  # no database yet, or one being written
        import sqlite3
        db = sqlite3.connect(f"file:{SITES / 'state' / 'monitor.db'}?mode=ro", uri=True, timeout=2)
        with contextlib.closing(db):
            started = db.execute("SELECT max(started) FROM runs WHERE site=?", (pick,)).fetchone()[0] or ""
            runs = [{"source": s, "ok": bool(ok), "message": m} for s, ok, m in
                    db.execute("SELECT source, ok, message FROM runs WHERE site=? AND started=?", (pick, started))]
    page = SITES / "dashboards" / f"{pick}.html"
    has_page = bool(pick) and page.exists()
    return {"sites": sites, "site": pick, "task": task_info(SITES_TASK), "runs": runs, "synced": started,
            "ready": (SITES / "sitemon.py").exists(), "dashboard": page.stat().st_mtime if has_page else 0,
            "command": RUNNER.state(), **({"html": page.read_text(encoding="utf-8")} if with_page and has_page else {})}


def analytics(with_page: bool = False) -> dict:
    """The Analytics page: the daily task (on/off, next and last run), the last sync per source, the dashboard."""
    task = task_info(ANALYTICS_TASK)
    runs, started = [], ""
    with contextlib.suppress(Exception):  # no database yet, or one being written: the page just shows no runs
        import sqlite3
        db = sqlite3.connect(f"file:{ANALYTICS / 'state' / 'analytics.db'}?mode=ro", uri=True, timeout=2)
        with contextlib.closing(db):
            started = db.execute("SELECT max(started) FROM runs").fetchone()[0] or ""
            runs = [{"source": s, "ok": bool(ok), "message": m}
                    for s, ok, m in db.execute("SELECT source, ok, message FROM runs WHERE started=?", (started,))]
    page = ANALYTICS / "dashboard.html"
    return {"task": task, "runs": runs, "synced": started, "ready": (ANALYTICS / "sda.py").exists(),
            "dashboard": page.stat().st_mtime if page.exists() else 0, "command": RUNNER.state(),
            **({"html": page.read_text(encoding="utf-8")} if with_page and page.exists() else {})}


def improvements() -> dict:
    """The last self-check (`agent-os improve`, also run by the daily update)."""
    path = a.ROOT / "state" / "improve.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": "", "found": []}
    # a failed start the next start cleared (Undo last update, say) is no longer a fix to offer; page, digest and Fix
    # all read this list, so their indexes stay in step
    crash = a.ROOT / "logs" / "hyperdesk-crash.txt"  # CRASH, read where the self-check reads it
    data["found"] = [f for f in data.get("found", []) if f.get("what") != "HyperDesk could not start" or crash.exists()]
    return data


def agent_os(*args: str) -> list[str]:
    return [str(PY), str(a.ROOT / "agent_os.py"), *args]


def workspace_repos() -> list[str]:
    return sorted(p.name for p in WORKSPACE.iterdir() if (p / ".git").exists()) if WORKSPACE.is_dir() else []


def model_names(cfg: dict) -> set[str]:
    return {m["name"].removesuffix(":latest") for m in a.get_json(cfg["ollama"]["url"] + "/api/tags", 10)["models"]}


def needed(script: Path) -> Path:
    """A script a button runs, or a plain refusal when this install lacks it (rather than a success that did nothing)."""
    if not script.exists():
        raise ValueError(f"{script.name} is missing from {script.parent}, so nothing was done")
    return script


def act(cfg: dict, body: dict) -> str:
    """Every button on the page ends up here. Inputs are checked against what exists; nothing goes through a shell
    except `start` for the service scripts, whose names come from SERVICES. Returns a message for the page."""
    do = body.get("do", "")
    if do == 'localweb.save':
        value = local_web.save(body.get('settings'))
        log_action('local model internet access', json.dumps(value))
        return 'Internet access settings saved.'
    name = str(body.get("name", ""))
    if do in ("service.start", "service.stop", "service.open"):
        if name not in a.SERVICES:
            raise ValueError(f"unknown service {name!r}")
        port, script, url = a.SERVICES[name]
        if do == "service.open":
            open_app(url)
            return f"Opening {name}"
        if do == "service.stop":
            out = a.stop_service(name)
            log_action("stopped service", name)
            return out or f"{name} stopped"
        if a.port_open(port):
            return f"{name} is already running"
        # cwd: Open WebUI keeps its signing key in the folder it starts in, which must stay LOCALAI
        subprocess.Popen(f'cmd /c start "{name}" /min "{script}"', cwd=LOCALAI, creationflags=HIDDEN)
        log_action("started service", name)
        return f"Starting {name} in its own minimized window (closing that window stops it)"
    if do == "browser.open":
        exe, url = browser(name) if name in BROWSERS else "", str(body.get("url", ""))
        if not exe:
            raise ValueError(f"{name} is not installed")
        if url and not re.match(r"https?://", url):
            raise ValueError("only http and https addresses")
        subprocess.Popen([exe, *([url] if url else [])])
        return f"Opening {name}"
    if do == "folder.open":
        if name not in FOLDERS:
            raise ValueError(f"unknown folder {name!r}")
        if not FOLDERS[name].exists():
            raise ValueError(f"{name} is not on this PC")
        subprocess.Popen(["explorer", str(FOLDERS[name])])
        return f"Opening {FOLDERS[name]}"
    if do == "project.open":  # a project, or any folder Ben names: the session asks him before each change
        path, tool = str(body.get("path", "")), body.get("tool", "")
        known = {e["path"] for e in projects() if e["exists"]} | {str(WORKSPACE)}
        if path not in known and not (Path(path).is_absolute() and Path(path).is_dir() and not off_limits(Path(path))):
            raise ValueError("not a folder on this PC (or off limits: secrets, Windows' own folders)")
        if tool == "explorer":
            subprocess.Popen(["explorer", path])
            return f"Opening {path}"
        if tool not in SESSIONS:
            raise ValueError(f"unknown tool {tool!r}")
        if tool == "qwen" and (game := a.game_running(cfg)):
            raise ValueError(f"game running ({game}): no local model loads now")
        wt = a.find_tool("wt")
        title = f"{tool} - {Path(path).name}"
        command = [*SESSIONS[tool], *(["--add-dir", str(KNOW), "--append-system-prompt-file", str(session_brief())]
                                      if tool == "claude" else [])]  # Claude starts knowing Ben's knowledge folder
        session = ["cmd", "/k", *command] if command else []
        subprocess.Popen([wt, "-d", path, "--title", title, *session] if wt else
                         ["cmd", "/c", "start", title, "/D", path, "cmd", "/k", *command])
        log_action("opened session", f"{tool} in {path}")
        return f"Opened {tool} in {path}"
    if do in ("model.load", "model.unload"):
        if name not in model_names(cfg):
            raise ValueError(f"unknown model {name!r}")
        if do == "model.load" and (game := a.game_running(cfg)):
            raise ValueError(f"game running ({game}): loading a model now overflows VRAM")
        with gpu_now() if do == "model.load" else contextlib.nullcontext():  # not beside a job's model or picture
            ollama_keep(cfg, name, "15m" if do == "model.load" else 0)
        log_action("loaded model" if do == "model.load" else "unloaded model", name)
        return f"{name} {'loaded for 15 minutes' if do == 'model.load' else 'unloaded'}"
    if do == "models.unload":
        out = a.run(["cmd", "/c", str(needed(LOCALAI / "bin" / "unload-ai.cmd"))], 60)[1] or "models unloaded"
        log_action("unloaded all models")
        return out
    if do == "stop_all":  # the one kill switch: Ollama itself, every service and job, and HyperDesk too
        script = needed(LOCALAI / "Stop AI.bat")
        log_action("stopped all AI")
        subprocess.Popen(f'cmd /c start "Stop AI" "{script}"', cwd=LOCALAI, creationflags=HIDDEN)
        return "Stopping all AI. HyperDesk closes too; nothing comes back until you open it or Start AI."
    if do == "health":
        check_health(cfg)
        return "Checking health"
    if do == "approval":
        item = APPROVALS.get(str(body.get("id", "")))
        if not item:
            raise ValueError("that request is no longer waiting (answered, or timed out)")
        item["allow"] = body.get("allow") is True
        item["event"].set()
        return "Allowed: the agent carries on" if item["allow"] else "Refused: the agent carries on read-only"
    if do == "voice.warm":
        threading.Thread(target=VOICE.warm, args=(cfg,), daemon=True).start()
        return "warming up"
    if do == "research":
        question = str(body.get("question", "")).strip()
        if not question or len(question) > 2000:
            raise ValueError("ask a question (up to 2000 characters)")
        submit(cfg, question, {"kind": "research", "route": "research", "model": "claude+web", "why": "asked in Research",
                               "target": {"show": body.get("show") is True}})
        return "Researching in the background (Jobs)"
    if do == "job.cancel":
        job = JOBS.get(str(body.get("id", "")))
        if not job:
            raise ValueError("that job is not running")
        job.cancel()
        return "Cancelling"
    if do == "job.again":  # the same input again in its conversation, e.g. on Claude instead of a local model
        found = next((j for j in thread_jobs(str(body.get("thread", ""))) if j["id"] == body.get("id")), None) \
            if THREAD_ID.fullmatch(str(body.get("thread", ""))) else None
        if not found:
            raise ValueError("no such job")
        prefer = str(body.get("prefer", "auto"))
        files = sorted(thread_files(found["thread"]), key=lambda p: str(p) not in found["attach"]) \
            if found["kind"] in ("answer", "file") else []  # its own attachments first, as when it was asked
        if found["kind"] == "answer":  # an answer is asked again as an answer, never re-routed into a task or action
            had = any(j["attach"] for j in thread_jobs(found["thread"]))
            item = pinned_item(cfg, prefer) or dict(zip(("route", "model", "why"),
                                                        answer_route(cfg, found["text"], prefer, files, had)), kind="answer")
        elif found["kind"] == "code" and found["why"] in (SELF_WHY, FIX_WHY):  # a HyperDesk change: Agent OS itself again
            item = plan(cfg, found["text"], "improve")[0]
        elif found["kind"] == "studio":  # its recording is not in the text
            raise ValueError("retry this edit from the Studio page (Create edit)")
        elif found["kind"] in ("code", "open", "file"):  # their target (repo, action, format) comes back from the text
            item = file_item(cfg, found["text"], files) if found["kind"] == "file" else route_one(cfg, found["text"])
            if not item or item["kind"] != found["kind"]:
                raise ValueError("this can no longer be done from its text alone: start it again from the composer")
        else:  # research, picture: the text is the whole request
            item = {"kind": found["kind"], "route": found["route"], "model": found["model"], "why": "again"}
        context = thread_context(found["thread"], 0, found["batch"]) if item["kind"] in ("answer", "file") else None
        submit(cfg, found["text"], item, found["thread"], secrets.token_hex(5), context, found["said"],
               attach=found["attach"] if item["kind"] == "file" else None, read=files)
        return "Started again"
    if do == "thread.delete":
        tid = str(body.get("id", ""))
        if not THREAD_ID.fullmatch(tid):
            raise ValueError("bad conversation id")
        for j in [j for j in list(JOBS.values()) if j.thread == tid]:
            j.cancel()
            JOBS.pop(j.id, None)
        copies = {Path(p).parent for j in thread_jobs(tid) for p in j["attach"] if Path(p).parent.parent == UPLOADS}
        with contextlib.closing(a.ledger()) as con:  # the calls' content goes too; their counts stay for cap and usage
            con.execute("update calls set data = 'null' where task = ? or task in (select id from jobs where "
                        "coalesce(thread, id) = ?)", (tid, tid))
            con.execute("delete from jobs where coalesce(thread, id) = ?", (tid,))
            con.execute("delete from summaries where thread = ?", (tid,))
            con.commit()
        for folder in copies:  # the copies of files dropped into it; Ben's own files stay where they are
            shutil.rmtree(folder, ignore_errors=True)
        log_action("deleted conversation", tid)
        return "Conversation deleted"
    if do == 'gemini.save':
        assistant_modes.save_gemini(str(body.get('model', '')).strip(), str(body.get('key', '')).strip())
        return 'Gemini connected. Choose Gemini beside the message box to use it.'
    if do == "free.key":  # the free tier's API key: kept in state\free.key (git-ignored), never sent back to the page
        key = str(body.get("key", "")).strip()
        if not re.fullmatch(r"[\x21-\x7e]{8,500}", key):
            raise ValueError("paste the API key (8 to 500 characters, no spaces)")
        a.FREE_KEY.parent.mkdir(exist_ok=True)
        a.FREE_KEY.write_text(key, encoding="utf-8")
        log_action("free cloud key saved")
        return "Key saved"
    if do == "free.test":  # one tiny request: which model answered, and how fast
        if why := a.free_unavailable(cfg):
            raise ValueError(why)
        # room to think: free combos often land on a reasoning model, which spends its first tokens on thinking
        r = a.ask_free(cfg, [{"role": "user", "content": "Reply with the single word READY."}], timeout=60, max_tokens=400)
        with contextlib.closing(a.ledger()) as con:
            a.record_call(con, "free-test", "smoke", "free", r, "")
        if not r["ok"]:
            raise ValueError(r["error"][:300])
        return f"Free cloud answered in {r['seconds']} s ({r['usage'].get('model')}): {r['data'].strip()[:40]}"
    if do == "context.add":  # "remember: …" in the box
        fact = " ".join(str(body.get("text", "")).split())
        if not fact or len(fact) > 2000:
            raise ValueError("say what to remember (up to 2000 characters)")
        remember([fact])
        log_action("remembered", fact[:160])
        return f"Remembered: {fact}"
    if do == "knowledge.save":  # a note written or edited on the Knowledge page; kind "context" is the context itself
        kind, name, old = str(body.get("kind", "")), " ".join(str(body.get("name", "")).split()), str(body.get("old", ""))
        text = str(body.get("text", "")).replace("\r\n", "\n")
        path = note_path(kind, name)
        if len(text) > 200_000:
            raise ValueError("a note holds up to 200,000 characters")
        if kind != "context" and old.lower() != name.lower() and path.exists():  # new or renamed onto another note
            raise ValueError(f"there is already a {kind[:-1]} named {path.stem}: pick another name")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if kind != "context" and old and old.lower() != name.lower():  # renamed (Windows names ignore case)
            note_path(kind, old).unlink(missing_ok=True)
        log_action("saved note", f"{kind} {name}".strip())
        return "Saved"
    if do == "knowledge.delete":
        kind, name = str(body.get("kind", "")), str(body.get("name", ""))
        if kind == "context":
            raise ValueError("empty the context and save instead")
        note_path(kind, name).unlink(missing_ok=True)
        log_action("deleted note", f"{kind} {name}")
        return f"Deleted {name}"
    if do == "knowledge.wrap":  # what Ben kept from a wrap-up: the handoff (as he edited it) and the facts he ticked
        title, text = " ".join(str(body.get("title", "")).split())[:60], str(body.get("text", "")).strip()
        facts = [" ".join(str(f).split())[:500] for f in body.get("facts") or [] if str(f).strip()][:20]
        saved = []
        if text:
            base = f"{time.strftime('%Y-%m-%d')} {re.sub(r'[^\w .-]', '', title).strip(' .') or 'handoff'}"
            name = next(n for n in (base, *(f"{base} {i}" for i in range(2, 100))) if not note_path("handoffs", n).exists())
            path = note_path("handoffs", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title or 'Handoff'}\n\n{text}\n", encoding="utf-8")
            saved.append(f"handoff “{name}”")
        if facts:
            remember(facts)
            saved.append(f"{len(facts)} fact{'s' if len(facts) > 1 else ''} in your context")
        tid = str(body.get("thread", ""))  # recorded, so the Home digest stops suggesting this one
        log_action("wrapped up", (f"{tid}: " if THREAD_ID.fullmatch(tid) else "") + ", ".join(saved))
        return "Saved " + " and ".join(saved) if saved else "Nothing to save"
    commands = {"update": ("update", agent_os("update")), "update_check": ("update check", agent_os("update", "--check")),
                "smoke": ("smoke test", agent_os("smoke")), "bench": ("local bench", agent_os("bench")),
                "improve": ("self-check", agent_os("improve"))}
    if do == "improve.fix":  # one finding of the last self-check, as an Agent OS task on Agent OS itself: Ben merges
        imp = improvements()
        found = imp["found"]
        if str(body.get("at", "")) != imp["at"]:  # the 11:00 update rewrites the list: an index alone could start another
            raise ValueError("The self-check ran again since this page loaded: open System again and look at the list.")
        i = int(str(body.get("i", ""))) if str(body.get("i", "")).isdigit() else -1  # the page sends data-* strings
        if not 0 <= i < len(found) or not found[i]["task"]:
            raise ValueError("no such finding: run the self-check again")
        task = found[i]["task"]
        with contextlib.closing(a.ledger()) as con:  # a fix exists (a stopped or failed one may be tried again)
            done = con.execute("select 1 from tasks where task = ? and state in ('AWAITING_APPROVAL', 'COMPLETED') "
                               "and created >= datetime('now', 'localtime', '-7 days')", (task,)).fetchone()
        if done or any(j.text == task and j.state in ("queued", "running") for j in list(JOBS.values())):
            raise ValueError("Already fixed, or being fixed: see Tasks. It leaves this list once its failures age out.")
        item = {"kind": "code", "route": "code", "model": "agent-os", "why": FIX_WHY,  # run_code: free or this PC only
                "target": {"repo": a.ROOT.name, "path": str(a.ROOT)}}
        job = submit(cfg, task, item, said=f"Improve: {found[i]['what']}")
        return f"Started: {found[i]['what']} (conversation {job.thread})"
    if do in commands:  # smoke and bench load each local model in turn: they hold the GPU lane like a job
        return (gpu_runner if do in ("smoke", "bench") else RUNNER.start)(*commands[do]) or f"Started {commands[do][0]}"
    if do == "video.edit":  # one video through ai-video-editor (a clean-up cut, or the AI's highlights) or Quick edit
        if any(j.route == 'studio' and j.state in ('running', 'queued') for j in list(JOBS.values())):
            raise ValueError('Wait for the recording studio edit to finish first.')
        video, frame = Path(str(body.get("path", ""))), str(body.get("frame", "fit"))
        target = str(body.get("for", "youtube" if QUICK else "both"))
        seconds, py = str(body.get("seconds", "59")), VIDEO / ".venv" / "Scripts" / "python.exe"
        cut, directions = str(body.get("cut", "clean")), str(body.get("directions", "")).strip()
        if not (video.is_absolute() and video.is_file() and video.suffix.lower() in VIDEO_TYPES) or off_limits(video):
            raise ValueError("pick a video file (" + ", ".join(sorted(VIDEO_TYPES)) + ")")
        if target not in ("youtube", "social", "both") or frame not in ("fit", "crop") or \
                cut not in ("clean", "highlights") or len(directions) > 2000 or \
                not (seconds.isdigit() and 10 <= int(seconds) <= 180):
            raise ValueError("make youtube, social or both; cut clean or highlights; frame fit or crop; "
                             "10 to 180 seconds; directions under 2000 characters")
        if QUICK:  # no AI and no model: the pause cut plus typed commands, each line checked before anything starts
            if cut != "clean":
                raise ValueError("Quick edit has no AI highlights: pick Clean up.")
            if bad := quick_edit.check(directions):  # the page lists every command beside the box
                raise ValueError("Not a command: " + " | ".join(bad) + ". The commands are listed on this page.")
            if not ffmpeg_ready():
                raise ValueError("Quick edit needs FFmpeg: run  winget install Gyan.FFmpeg  then open HyperDesk again.")
            cmd = [str(PY), str(a.ROOT / "quick_edit.py"), str(video), "--for", target, "--frame", frame,
                   *([f"--commands={directions}"] if directions else []),  # = so a leading "-" isn't read as a flag
                   *(["--fresh"] if body.get("fresh") is True else [])]
            return RUNNER.start(f"video {video.name}", cmd) or f"Editing {video.name}: follow it in the console"
        if not py.exists():
            raise ValueError(f"ai-video-editor is not installed in {VIDEO}")
        ai = cut == "highlights" or directions  # the vision model (about 9 GB of VRAM) or the directions model
        if ai and (game := a.game_running(cfg)):
            raise ValueError(f"game running ({game}): the video AI needs about 9 GB of VRAM")
        if RUNNER.busy():  # checked before any model is unloaded
            return f"Busy: wait for “{RUNNER.title}” to finish."

        def unload():  # a chat model in VRAM would starve the AI
            with contextlib.suppress(OSError, ValueError, KeyError):
                for m in a.get_json(cfg["ollama"]["url"] + "/api/ps")["models"]:
                    ollama_keep(cfg, m["name"], 0)
        cmd = [str(py), str(VIDEO / "silvades_edit.py"), str(video), "--for", target, "--seconds", seconds,
               "--frame", frame, *(["--highlights"] if cut == "highlights" else []),
               *([f"--directions={directions}"] if directions else []),  # = so a leading "-" isn't read as a flag
               *(["--fresh"] if body.get("fresh") is True else [])]
        title = f"video {video.name}"
        return (gpu_runner(title, cmd, unload) if ai else RUNNER.start(title, cmd)) or \
            f"Editing {video.name}: follow it in the console"
    if do == "analytics.toggle":  # the hourly sync on or off: the task stays registered, Task Scheduler just skips it
        on = str(body.get("on", "")) in ("1", "true", "True")
        task_toggle(ANALYTICS_TASK, on)
        log_action("analytics automatic sync", "on" if on else "off")
        return "Automatic analytics sync on" if on else "Automatic analytics sync off"
    if do == "sites.toggle":
        on = str(body.get("on", "")) in ("1", "true", "True")
        task_toggle(SITES_TASK, on)
        log_action("website monitor automatic checks", "on" if on else "off")
        return "Automatic website checks on" if on else "Automatic website checks off"
    if do in ("sites.check", "sites.sync"):  # Ben is waiting on these: full speed (the scheduled run passes --background)
        if not (SITES / "sitemon.py").exists():
            raise ValueError(f"site-monitor is not in {WORKSPACE}")
        kind = do.split(".")[1]
        return RUNNER.start(f"sites {kind}", [str(PY), str(SITES / "sitemon.py"), kind]) or \
            ("Checking every page: follow it in the console" if kind == "check" else "Syncing every source: follow it in the console")
    if do == "sites.open":  # one site's dashboard in Ben's default browser
        site = str(body.get("site", ""))
        page = SITES / "dashboards" / f"{site}.html"
        if not SITE_ID.fullmatch(site) or not page.exists():
            raise ValueError("no dashboard yet: press Check now")
        os.startfile(page)
        return "Opening the dashboard"
    if do == "sites.add":  # sitemon.py writes the sites.toml section; nothing is fetched until Check now
        url, name = str(body.get("url", "")).strip(), " ".join(str(body.get("name", "")).split())
        if len(url) > 200 or not re.fullmatch(r"(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{1,5})?(/[^\s]*)?", url):
            raise ValueError("Enter a website address, like example.com")
        if len(name) > 60:
            raise ValueError("Keep the name under 60 characters")
        if not (SITES / "sitemon.py").exists():
            raise ValueError(f"site-monitor is not in {WORKSPACE}")
        code, out = a.run([str(PY), str(SITES / "sitemon.py"), "add", url, *([name] if name else [])], 30)
        if code != 0:
            raise ValueError(out.strip()[-300:] or "the site could not be added")
        log_action("website monitor", f"added {url}")
        return f"Added {name or url}: press Check now"
    if do == "analytics.sync":  # Ben is waiting on this one: normal priority (the scheduled run passes --background)
        if not (ANALYTICS / "sda.py").exists():
            raise ValueError(f"silvades-analytics is not in {WORKSPACE}")
        return RUNNER.start("analytics sync", [str(PY), str(ANALYTICS / "sda.py"), "daily"]) or "Syncing: follow it in the console"
    if do == "analytics.open":  # the dashboard in Ben's default browser
        page = ANALYTICS / "dashboard.html"
        if not page.exists():
            raise ValueError("no dashboard yet: press Sync now")
        os.startfile(page)
        return "Opening the dashboard"
    if do in ("merge", "rollback", "undo"):  # undo: git revert of an applied task's own commits
        task_id = str(body.get("id", ""))
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("bad task id")
        return RUNNER.start(f"{do} {task_id}", agent_os(do, task_id)) or f"Started {do} {task_id}"
    if do == "run":
        task, repo = str(body.get("task", "")).strip(), str(body.get("repo", ""))
        agent, level = str(body.get("agent", "")), str(body.get("level", ""))
        if not task or len(task) > 4000:
            raise ValueError("describe the task (up to 4000 characters)")
        if repo not in workspace_repos():
            raise ValueError(f"pick a git repo in {WORKSPACE}")
        target = {"repo": repo, "agent": agent if agent in ("claude", "codex", "opencode") else "",
                  "level": level if level in ("0", "1", "2", "3") else ""}
        submit(cfg, task, {"kind": "code", "route": "code", "model": "agent-os", "why": f"a task in {repo}",
                           "target": target})  # run_code passes the task after "--": a leading "-" stays text
        return "Task started in the background (Jobs)"
    raise ValueError(f"unknown action {do!r}")


# --- the server

class Desk(BaseHTTPRequestHandler):
    token, cfg, last = "", {}, time.monotonic()

    def log_message(self, *args):  # pythonw has no console to log to
        pass

    def reply(self, code: int, body, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, api: bool, token: str | None = None) -> bool:
        """Loopback Host (blocks DNS rebinding); for the API also the token and no foreign Origin (blocks other pages).
        `token` is for <img> requests, which cannot send the header: they carry it in the address instead."""
        port = self.server.server_address[1]
        here = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in here:
            return False
        if not api:
            return True
        origin = self.headers.get("Origin")
        given = self.headers.get("X-Token", "") if token is None else token
        return (origin is None or origin.removeprefix("http://") in here) and secrets.compare_digest(given, Desk.token)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/" or path == "/favicon.ico" or path.startswith("/assets/"):  # the page and its files: no secrets
            if not self.allowed(False):
                return self.reply(403, {"error": "forbidden"})
            if path == "/":
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            name = "hyperdesk.ico" if path == "/favicon.ico" else path.removeprefix("/assets/")
            file = (ASSETS / name).resolve()
            if not re.fullmatch(r"[\w.-]+(/[\w.-]+)?", name) or not file.is_relative_to(ASSETS.resolve()) \
                    or file.suffix not in KINDS or not file.is_file():
                return self.reply(404, {"error": "not found"})
            return self.reply(200, file.read_bytes(), KINDS[file.suffix])
        if not self.allowed(True, params.get("t", "") if path in ("/api/image/file", "/api/studio/file") else None):
            return self.reply(403, {"error": "forbidden"})
        Desk.last = time.monotonic()
        refresh = params.get("refresh") == "1"
        cfg = Desk.cfg
        try:
            if path == '/api/studio':
                items = studio.listing()
                for item in items:
                    active = next((j for j in list(JOBS.values()) if j.target.get('recording') == item['id'] and j.state in ('running', 'queued')), None)
                    if active:
                        item['job'] = active.id if not active.cancelled else ''
                        if active.cancelled:
                            item['message'] = 'Stopping. An active AI request may need to finish first.'
                    if item['state'] in ('editing', 'queued') and not any(
                            j.target.get('recording') == item['id'] and j.state in ('running', 'queued') for j in list(JOBS.values())):
                        item.update(state='failed', message='Editing was interrupted. Your original is saved; retry when ready.')
                return self.reply(200, {'recordings': items, 'tools': studio.tools() if params.get('tools') == '1' else {}})
            if path == '/api/studio/file':
                file = studio.media(params.get('id', ''), urllib.parse.unquote(params.get('name', '')))
                size, start, end = file.stat().st_size, 0, file.stat().st_size - 1
                partial = self.headers.get('Range')
                if partial:
                    match = re.fullmatch(r'bytes=(\d+)-(\d*)', partial)
                    if not match:
                        return self.reply(416, {'error': 'Unsupported range'})
                    start, end = int(match[1]), min(end, int(match[2])) if match[2] else end
                    if start > end:
                        return self.reply(416, {'error': 'Range outside recording'})
                self.send_response(206 if partial else 200)
                self.send_header('Content-Type', {'.webm': 'video/webm', '.vtt': 'text/vtt; charset=utf-8', '.jpg': 'image/jpeg'}.get(file.suffix, 'video/mp4'))
                self.send_header('Content-Length', str(end-start+1))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                if partial:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                with file.open('rb') as source:
                    source.seek(start)
                    left = end-start+1
                    while left:
                        data = source.read(min(left, 1024 * 1024))
                        if not data:
                            break
                        self.wfile.write(data)
                        left -= len(data)
                return
            if path == "/api/image/file":
                return self.reply(200, *image_file(params.get("name", "")))
            if path == "/api/images":
                return self.reply(200, {"images": gallery(), "checkpoints": checkpoints(), "running": a.port_open(8188),
                                        "sizes": SIZES})
            if path == "/api/image/status":
                pid = params.get("id", "")
                if not re.fullmatch(r"[0-9a-f-]{36}", pid):
                    return self.reply(400, {"error": "bad image job id"})
                return self.reply(200, image_status(pid))
            if path == "/api/digest":
                return self.reply(200, digest(cfg))
            if path == "/api/threads":
                return self.reply(200, threads())
            if path == "/api/knowledge":
                return self.reply(200, knowledge())
            if path == "/api/thread":
                tid = params.get("id", "")
                if not THREAD_ID.fullmatch(tid):
                    return self.reply(400, {"error": "bad conversation id"})
                return self.reply(200, thread_jobs(tid))
            if path == "/api/ping":  # only running_desk calls this: a fresh click on the shortcut
                if code_stamp() != STAMP and not busy() and own_state():  # updated and idle: step aside for new code
                    STATE.unlink(missing_ok=True)  # before the new launch writes its own record
                    self.reply(409, {"error": "HyperDesk was updated: restarting"})  # running_desk: '', so main starts
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                return self.reply(200, {"ok": True})
            if path == "/api/status":
                return self.reply(200, status(cfg))
            if path == "/api/projects":
                return self.reply(200, projects(refresh=refresh))
            if path == "/api/models":
                return self.reply(200, models(cfg))
            if path == "/api/tasks":
                return self.reply(200, {"tasks": tasks(), "repos": workspace_repos()})
            if path == "/api/task":
                task_id = params.get("id", "")
                if not TASK_ID.fullmatch(task_id):
                    return self.reply(400, {"error": "bad task id"})
                with contextlib.closing(a.ledger()) as con:
                    return self.reply(200, {"report": capture(a.inspect_task, con, task_id)})
            if path == "/api/timeline":
                return self.reply(200, timeline())
            if path == "/api/usage":
                if refresh:
                    for kind, report in (("claude", "daily"), ("codex", "daily"), ("claude", "session")):
                        ccusage(kind, report, refresh=True)
                return self.reply(200, usage())
            if path == "/api/system":
                log, man = a.ROOT / "logs" / "update.txt", a.ROOT / "install-manifest.json"  # the share zip has no manifest
                return self.reply(200, {"schedule": a.schedule_info(), "update_log": log.read_text(
                    encoding="utf-8", errors="replace") if log.exists() else "No update has run yet.",
                    "manifest": json.loads(man.read_text(encoding="utf-8")) if man.exists() else [],
                    "improve": improvements()})
            if path == "/api/video":
                return self.reply(200, {"projects": video_projects(), "command": RUNNER.state(), "quick": QUICK,
                                        "ready": ffmpeg_ready() if QUICK else (VIDEO / ".venv" / "Scripts" / "python.exe").exists(),
                                        "forms": quick_edit.FORMS if QUICK else ""})
            if path == "/api/analytics":
                return self.reply(200, analytics(with_page=params.get("page") == "1"))
            if path == "/api/sites":
                return self.reply(200, site_monitor(params.get("site", ""), with_page=params.get("page") == "1"))
            if path == '/api/assistant-modes':
                model = assistant_modes.settings().get('gemini_model', '')
                return self.reply(200, {'model': model, 'gemini': bool(model and assistant_modes.GEMINI_KEY.is_file())})
            if path == '/api/local-web':
                return self.reply(200, local_web.settings())
            if path == "/api/video/analytics":
                window = params.get("days", "28")
                if window not in ("7", "28", "90"):
                    return self.reply(400, {"error": "days must be 7, 28 or 90"})
                return self.reply(200, video_analytics.build_report(ANALYTICS / "state" / "analytics.db", int(window)))
            if path == "/api/log":
                start = int(params.get("since", "0"))
                data = b""
                if CMD_LOG.exists():
                    with open(CMD_LOG, "rb") as f:
                        f.seek(start)
                        data = f.read(262144)
                return self.reply(200, {"text": data.decode("utf-8", "replace"), "next": start + len(data),
                                        "command": RUNNER.state()})
        except (OSError, ValueError, KeyError) as e:
            return self.reply(502, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # a bug, not a bad request: keep its trace for the self-check, and still answer
            log_error(f"GET {path}", traceback.format_exc())
            return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.reply(413, {"error": "too large"})
        # read before any reply: Windows resets a socket closed with unread data, and the client never sees the 403
        raw = self.rfile.read(length)
        if not self.allowed(True) or self.headers.get("Content-Type") != "application/json":
            return self.reply(403, {"error": "forbidden"})
        Desk.last = time.monotonic()
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if self.path.startswith('/api/studio/'):
                rid = str(body.get('id', ''))
                if self.path == '/api/studio/start':
                    return self.reply(200, studio.create(body.get('brief') or {}, str(body.get('mime', ''))))
                if self.path == '/api/studio/chunk':
                    return self.reply(200, studio.chunk(rid, body.get('number'), body.get('data')))
                if self.path == '/api/studio/finish':
                    return self.reply(200, studio.finish(rid, body.get('count')))
                if self.path == '/api/studio/questions':
                    if not LANES['gpu'].acquire(blocking=False):
                        raise ValueError('Your local AI is busy. Fill in the brief or try again shortly.')
                    try:
                        return self.reply(200, studio.questions(Desk.cfg, body))
                    finally:
                        LANES['gpu'].release()
                if self.path == '/api/studio/mark':
                    with studio.LOCK:
                        value = studio.read(rid)
                        moment = float(body.get('seconds', -1))
                        if value['state'] != 'recording' or not 0 <= moment <= 7200:
                            raise ValueError('Mark a moment while recording.')
                        value.setdefault('marks', []).append(moment)
                        value['marks'] = value['marks'][:30]
                        studio.write(rid, value)
                    return self.reply(200, {'message': 'Moment marked'})
                if self.path == '/api/studio/edit':
                    with studio.LOCK:
                        if RUNNER.busy():
                            raise ValueError('Wait for the current video or maintenance command to finish, then try again.')
                        value = studio.read(rid)
                        if any(j.target.get('recording') == rid and j.state in ('queued', 'running') for j in list(JOBS.values())):
                            raise ValueError('This recording is already being edited.')
                        if not value['bytes'] or value['state'] == 'recording':
                            raise ValueError('Finish saving this recording first.')
                        if body.get('brief'):
                            value['brief'] = studio.brief(body['brief'])
                        (studio.folder(rid) / 'cancel-requested').unlink(missing_ok=True)
                        value.update(state='queued', message='Waiting for your local AI…')
                        studio.write(rid, value)
                        job = submit(Desk.cfg, 'Edit recording: ' + value['brief']['goal'],
                            {'kind': 'studio', 'route': 'studio', 'model': 'local editor', 'why': 'Your recording brief',
                             'target': {'recording': rid}})
                    return self.reply(200, {'job': job.id})
                raise ValueError('Unknown Studio action.')
            if self.path == "/api/trends":  # Post trends: the CSV the page read from the user's pick, back as a report page
                rows, info = post_trends.load(str(body.get("csv", "")))
                return self.reply(200, {"html": post_trends.html(post_trends.report(rows, info))})
            if self.path == "/api/action":
                message = act(Desk.cfg, body)
                return self.reply(409 if message.startswith("Busy") else 200, {"message": message})
            if self.path in ("/api/do", "/api/plan"):  # the one box; /api/plan only says where it would go
                text, prefer = str(body.get("text", "")).strip(), str(body.get("prefer", "auto"))
                if not text or len(text) > 8000:
                    raise ValueError("type what you want done (up to 8000 characters)")
                tid = str(body.get("thread") or "")
                if tid and not THREAD_ID.fullmatch(tid):
                    raise ValueError("bad conversation id")
                new = attached(body.get("files") or [])  # attached to this input: then the whole input is one job
                # this input's attachments first: a file job takes its format, name and source from the first one
                files = list(dict.fromkeys([*new, *(thread_files(tid) if tid else [])]))
                if prefer == 'improve' and files:
                    raise ValueError('Describe the HyperDesk change in a conversation without attachments.')
                had = bool(new) or bool(tid) and any(j["attach"] for j in thread_jobs(tid))  # moved files count too
                if pinned := pinned_item(Desk.cfg, prefer):
                    items = [dict(pinned, text=text)]
                elif new:  # never the open, code or picture checks: a file job ("convert this to word") or an answer
                    items = [{"text": text, **(file_item(Desk.cfg, text, files) or dict(zip(
                        ("route", "model", "why"), answer_route(Desk.cfg, text, prefer, files, had)), kind="answer"))}]
                else:
                    items = plan(Desk.cfg, text, prefer, files, had)
                if self.path == "/api/plan":
                    return self.reply(200, [{k: i[k] for k in ("kind", "route", "model", "why", "text")} for i in items])
                context = thread_context(tid, 0) if tid else []  # whole: run_answer squeezes it
                tid, batch = tid or secrets.token_hex(6), secrets.token_hex(5)
                reads = ("answer", "file")  # what reads the talk and the attachments (file jobs: always on this PC)
                jobs = [submit(Desk.cfg, i["text"], i, tid, batch, context if i["kind"] in reads else None, text,
                               attach=new, read=files if i["kind"] in reads else None) for i in items]
                log_action("do", f"{text[:160]} -> " + ", ".join(f"{j.kind}:{j.model or j.route}" for j in jobs))
                return self.reply(200, {"thread": tid, "batch": batch, "jobs": [j.view() for j in jobs]})
            if self.path == "/api/upload":  # a dropped or pasted file: kept in state\uploads, attached from there
                name = re.sub(r"[^\w .-]", "_", Path(str(body.get("name") or "")).name).strip(" .")[:100] or "pasted.png"
                data = base64.b64decode(str(body.get("data", "")), validate=True)
                if not data:
                    raise ValueError("that file is empty")
                folder = UPLOADS / secrets.token_hex(5)
                folder.mkdir(parents=True)
                (folder / name).write_bytes(data)
                return self.reply(200, {"path": str(folder / name)})
            if self.path == "/api/pick":  # held open while Ben is in the dialog
                return self.reply(200, pick(str(body.get("kind", ""))))
            if self.path == "/api/tidy":
                return self.reply(200, tidy(Desk.cfg))
            if self.path == "/api/wrapup":
                tid = str(body.get("thread", ""))
                if not THREAD_ID.fullmatch(tid):
                    raise ValueError("bad conversation id")
                return self.reply(200, wrapup(Desk.cfg, tid))
            if self.path == "/api/image":  # the Images page: the GPU lane for the whole render, as a picture job has it
                if not LANES["gpu"].acquire(blocking=False):  # generate() unloads models: never one a job is using
                    raise ValueError("Your local AI is busy. Try again when the current job finishes.")
                try:
                    queued = generate(Desk.cfg, body)
                except BaseException:
                    LANES["gpu"].release()
                    raise
                gpu_until(lambda: comfy_wait(queued["id"]))
                return self.reply(200, queued)
            if self.path == "/api/transcribe":
                audio = base64.b64decode(str(body.get("audio", "")), validate=True)
                if not audio:
                    raise ValueError("no audio")
                return self.reply(200, VOICE.transcribe(Desk.cfg, audio))
            if self.path == "/api/approval":  # from desk_mcp.py; held open until Ben answers
                given = body.get("input")
                if not isinstance(given, dict):
                    raise ValueError("input must be an object")
                return self.reply(200, {"allow": ask_ben(str(body.get("task", "")), str(body.get("tool", "")), given)})
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
            return self.reply(400, {"error": str(e)})
        except Exception as e:  # a bug: as in do_GET
            log_error(f"POST {self.path.partition('?')[0]}", traceback.format_exc())
            return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
        self.reply(404, {"error": "not found"})


def running_desk() -> str:
    """Address of a HyperDesk already serving, or ''."""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        request = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/ping", headers={"X-Token": st["token"]})
        with a._NO_PROXY.open(request, timeout=2) as r:
            r.read()
        return f"http://127.0.0.1:{st['port']}/#{st['token']}"
    except (OSError, ValueError, KeyError):
        return ""


def busy() -> bool:
    """Work this server must finish before it may stop: a command, an approval, a job."""
    return RUNNER.busy() or bool(APPROVALS) or any(j.state in ("queued", "running") for j in list(JOBS.values()))


def own_state() -> bool:
    """state\\hyperdesk.json is this server's record (another HyperDesk may have written its own)."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("token") == Desk.token
    except (OSError, ValueError, AttributeError):
        return False


class Server(ThreadingHTTPServer):
    # Windows lets a second SO_REUSEADDR socket bind a port that is already listening; without it a taken port fails
    # and the fallback below picks a free one.
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address):  # what escaped Desk's handlers; pythonw has no stderr for it
        if not isinstance(sys.exception(), ConnectionError):  # a closed window or a video seek is not a bug
            log_error("request", traceback.format_exc())


def serve(cfg: dict, port: int = PORT) -> ThreadingHTTPServer:
    Desk.token, Desk.cfg, Desk.last = secrets.token_urlsafe(24), cfg, time.monotonic()
    try:
        return Server(("127.0.0.1", port), Desk)
    except OSError:
        return Server(("127.0.0.1", 0), Desk)


def main() -> None:
    if url := running_desk():  # one HyperDesk at a time: a second click opens another window on it
        open_app(url)
        return
    server = serve(a.load_config())
    port = server.server_address[1]
    STATE.parent.mkdir(exist_ok=True)
    (a.ROOT / "logs").mkdir(exist_ok=True)  # a fresh install has none, and Studio's log opens without a mkdir
    for kind in ("skills", "handoffs"):  # there to find in Explorer from the first start
        (KNOW / kind).mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"port": port, "token": Desk.token, "pid": os.getpid()}), encoding="utf-8")

    def idle_watch():
        while time.monotonic() - Desk.last < IDLE_EXIT or busy():
            slept = time.monotonic()
            time.sleep(15)
            if time.monotonic() - slept > 60:  # the PC slept (monotonic counts it here): give the window time to poll
                Desk.last = time.monotonic()
        server.shutdown()
    threading.excepthook = lambda x: log_error(f"thread {x.thread.name if x.thread else ''}",
                                               "".join(traceback.format_exception(x.exc_value)))
    LANES["cloud"] = threading.Semaphore(Desk.cfg.get("jobs", {}).get("cloud_parallel", 2))
    mark_interrupted()
    threading.Thread(target=idle_watch, daemon=True).start()
    a.start_ollama()
    check_health(Desk.cfg)
    log_action("HyperDesk opened")
    CRASH.unlink(missing_ok=True)  # this start worked
    open_app(f"http://127.0.0.1:{port}/#{Desk.token}")
    try:
        server.serve_forever()
    finally:
        VOICE.stop()
        server.server_close()  # the port is free for the launch that replaces this one
        if own_state():  # only our own record: another HyperDesk may have written its own
            STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # pythonw shows nothing: say why where Ben and the self-check can find it
        with contextlib.suppress(OSError):
            CRASH.parent.mkdir(exist_ok=True)
            CRASH.write_text(traceback.format_exc(), encoding="utf-8")
        a.notify("HyperDesk could not start", f"Open Home Base: it shows why, and {REPAIR}.")
        raise  # under pythonw `crashed` then shows the box
