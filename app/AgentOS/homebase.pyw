"""HyperDesk Home Base: one window for the local AI setup.

Start it from the "HyperDesk Home Base" shortcut (pythonw, no console). It reads the same data and runs the same
`agent-os` commands as the terminal. It opens no network port. Merge and roll back ask first.
"""
import contextlib
import io
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import ctypes
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent


def crashed(kind, err, tb) -> None:
    """sys.excepthook under pythonw, where an error that stops the app starting would vanish: keep it in logs\\ and
    show it. Needs nothing from agent_os, so a broken agent_os.py or config.toml (file, line, column) shows too."""
    text, app = "".join(traceback.format_exception(kind, err, tb)), Path(sys.argv[0])
    log = HERE / "logs" / f"{app.stem}-crash.txt"
    with contextlib.suppress(OSError):
        log.parent.mkdir(exist_ok=True)
        log.write_text(text, encoding="utf-8")
    messagebox.showerror(f"{app.name} could not start", f"{text[-1500:]}\nSaved in {log}")


if sys.stderr is None:  # pythonw (the shortcuts); Game Mode and Lights import this file, so they get it too
    sys.excepthook = crashed
sys.path.insert(0, str(HERE))
import agent_os as a  # noqa: E402

LOCALAI = a.ROOT.parent
PY = Path(sys.executable).with_name("python.exe")  # console Python for agent-os commands, even when we are pythonw
HIDDEN = subprocess.CREATE_NO_WINDOW
SERVICES, stop_service, schedule_info = a.SERVICES, a.stop_service, a.schedule_info
# the SILVADES palette, as in hyperdesk.html and on silvades.com
VOID, SIDE, RAISE, RAISE2, EDGE, LINE2 = "#050505", "#0a0a0a", "#121212", "#1a1a1a", "#222222", "#333333"
TEXT, MUTED, FAINT, NEON, NEON_DIM = "#e0e0e0", "#9a9a9a", "#777777", "#39ff14", "#2bcc10"
COLORS = {a.PASS: NEON, a.WARN: "#f2b84b", a.FAIL: "#ff6b6b"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TASK_TAGS = {"COMPLETED": (a.PASS,), "AWAITING_APPROVAL": (a.PASS,), "FAILED": (a.FAIL,)}
LEVELS = ["auto", "0 - evidence only, no model", "1 - local model answers", "2 - one agent changes code",
          "3 - plan, then implement"]


def capture(fn, *args) -> str:
    """What one of the ledger reports would print."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def agent_os(*args: str) -> list[str]:
    return [str(PY), str(a.ROOT / "agent_os.py"), *args]


def snapshot(cfg: dict) -> dict:
    """Everything the window shows that is cheap to read. Runs in a worker thread."""
    game = a.game_running(cfg)  # while one runs, no nvidia-smi or schtasks: polling them stutters the game
    code, gpu = (0, "not read while a game runs") if game else a.run(
        ["nvidia-smi", "--query-gpu=name,memory.free,memory.total", "--format=csv,noheader,nounits"])
    s = {"gpu": gpu if code == 0 else "no reading", "game": game, "crash": "",
         "services": {name: a.port_open(port) for name, (port, _, _) in SERVICES.items()}}
    try:
        s["ollama"] = a.get_json(cfg["ollama"]["url"] + "/api/version")["version"]
        s["loaded"] = a.get_json(cfg["ollama"]["url"] + "/api/ps")["models"]
    except (OSError, ValueError, KeyError):
        s["ollama"], s["loaded"] = "not answering", []
    try:
        with contextlib.closing(a.ledger()) as con:
            s["tasks"] = con.execute("select id, created, state, level, agent, tests_before, tests_after, task "
                                     "from tasks order by id desc limit 100").fetchall()
            s["calls"] = con.execute("select count(*) from calls where agent in ('claude', 'codex') "
                                     "and at >= date('now', 'localtime')").fetchone()[0]
            s["usage"] = capture(a.show_usage, con, 14)
    except sqlite3.Error as e:  # a damaged ledger: services and the rest still show, so they can still be started
        s["tasks"], s["calls"], s["usage"] = [], "?", f"Ledger unreadable ({e}). See HEALTH: state-db."
    log = a.ROOT / "logs" / "update.txt"
    s["update_log"] = log.read_text(encoding="utf-8") if log.exists() else "No update has run yet."
    s["schedule"] = "Daily update task: not read while a game runs" if game else schedule_info()
    with contextlib.suppress(OSError):  # hyperdesk.pyw writes it when it cannot start and deletes it after a good start
        text = (a.ROOT / "logs" / "hyperdesk-crash.txt").read_text(encoding="utf-8", errors="replace").strip()
        s["crash"] = (text.splitlines() or ["no detail in the file"])[-1]
    return s


def table(parent, columns: dict, height: int = 8) -> ttk.Treeview:
    """A scrollable table; columns maps heading -> width (0 = stretch)."""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=list(columns), show="headings", height=height, selectmode="browse")
    for name, width in columns.items():
        tree.heading(name, text=name.upper(), anchor="w")
        tree.column(name, width=px(width or 200), stretch=not width, anchor="w")
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    for status, color in COLORS.items():
        tree.tag_configure(status, foreground=color)
    return tree


def fill(tree: ttk.Treeview, rows, tags=lambda row: ()) -> None:
    keep = tree.selection()
    tree.delete(*tree.get_children())
    for row in rows:
        tree.insert("", "end", iid=str(row[0]), values=row, tags=tags(row))
    for iid in keep:
        if tree.exists(iid):
            tree.selection_set(iid)


def text_box(parent, height: int = 10) -> tk.Text:
    """Read-only output; a ttk scrollbar, since the classic one stays light on Windows."""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    box = tk.Text(frame, height=height, wrap="none", state="disabled", **FIELD, font=FONTS["mono"])
    bar = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    box.pack(side="left", fill="both", expand=True)
    return box


FONTS: dict = {}  # filled by theme(): Plex and Oxanium from assets\fonts, else the Windows fonts closest to them
FIELD = dict(bg=SIDE, fg=MUTED, insertbackground=NEON, selectbackground=NEON, selectforeground=VOID, relief="flat",
             highlightthickness=1, highlightbackground=EDGE, highlightcolor=NEON_DIM)  # padding: theme(), once K is known
K = 1.0  # screen pixels per 96-DPI pixel, set by theme(); fonts are in points and scale by themselves


def px(*n: float):
    """Sizes below are written for a 100% display; this makes them the same size on a scaled one."""
    r = [round(x * K) for x in n]
    return r[0] if len(r) == 1 else tuple(r)


def dark_titlebar(win: tk.Misc) -> None:
    """Windows 11 draws this window's title bar dark (this window only; nothing system-wide changes)."""
    win.update_idletasks()
    with contextlib.suppress(AttributeError, OSError):
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)


def theme(root: tk.Tk) -> None:
    global K
    K = root.winfo_fpixels("1i") / 96
    FIELD.update(padx=px(8), pady=px(6))
    have = set(tkfont.families(root))
    pick = lambda *names: next((n for n in names if n in have), names[-1])  # noqa: E731
    sans, mono = pick("IBM Plex Sans", "Segoe UI Variable Text", "Segoe UI"), pick("IBM Plex Mono", "Cascadia Mono", "Consolas")
    disp = pick("Oxanium", "Segoe UI Variable Display", "Segoe UI")
    FONTS.update(sans=(sans, 10), mono=(mono, 9), label=(mono, 8, "bold"), disp=(disp, 15, "bold"), btn=(disp, 9, "bold"))
    root.configure(bg=VOID)
    root.option_add("*Toplevel.background", VOID)
    for k, v in (("background", RAISE), ("foreground", TEXT), ("selectBackground", NEON), ("selectForeground", VOID)):
        root.option_add(f"*TCombobox*Listbox.{k}", v)  # the drop-down list is a classic widget
    s = ttk.Style(root)
    s.theme_use("clam")  # the native "vista" theme ignores colours
    s.configure(".", background=VOID, foreground=TEXT, fieldbackground=RAISE, bordercolor=EDGE, lightcolor=VOID,
                darkcolor=VOID, troughcolor=VOID, focuscolor=NEON_DIM, selectbackground=NEON, selectforeground=VOID,
                insertcolor=NEON, arrowcolor=FAINT, font=FONTS["sans"])
    s.configure("Note.TLabel", foreground=FAINT)
    s.configure("Status.TLabel", background=SIDE, foreground=FAINT, font=FONTS["mono"])
    s.configure("TLabelframe", bordercolor=EDGE)
    s.configure("TLabelframe.Label", foreground=NEON, font=FONTS["label"])
    s.configure("TButton", background=RAISE, bordercolor=LINE2, padding=px(12, 5), font=FONTS["btn"], relief="flat")
    s.map("TButton", background=[("pressed", RAISE2), ("active", RAISE2)], foreground=[("active", NEON)],
          bordercolor=[("active", NEON_DIM), ("focus", NEON_DIM)])
    s.configure("Go.TButton", background=NEON, foreground=VOID, bordercolor=NEON)
    s.map("Go.TButton", background=[("active", NEON_DIM)], foreground=[("active", VOID)], bordercolor=[("active", NEON_DIM)])
    s.configure("TNotebook", borderwidth=0, tabmargins=px(0, 4, 0, 0))
    s.configure("TNotebook.Tab", background=VOID, foreground=FAINT, bordercolor=VOID, padding=px(16, 7), font=FONTS["label"])
    s.map("TNotebook.Tab", background=[("selected", SIDE)], foreground=[("selected", NEON), ("active", TEXT)],
          bordercolor=[("selected", EDGE)], lightcolor=[("selected", NEON)])
    s.configure("Treeview", background=SIDE, fieldbackground=SIDE, foreground=TEXT, rowheight=px(26), borderwidth=0)
    s.map("Treeview", background=[("selected", RAISE2)], foreground=[("selected", NEON)])
    s.configure("Treeview.Heading", background=VOID, foreground=FAINT, bordercolor=EDGE, relief="flat", font=FONTS["label"],
                padding=px(4, 5))
    s.map("Treeview.Heading", background=[("active", VOID)], foreground=[("active", TEXT)])
    s.configure("TScrollbar", background=RAISE2, bordercolor=VOID, lightcolor=RAISE2, darkcolor=RAISE2, gripcount=0,
                arrowsize=px(12))
    s.map("TScrollbar", background=[("active", LINE2)])
    s.configure("Sash", sashthickness=px(6), background=EDGE)
    s.configure("TCombobox", background=RAISE, bordercolor=LINE2, arrowcolor=NEON, arrowsize=px(12), padding=px(4))
    s.map("TCombobox", fieldbackground=[("readonly", RAISE)], selectbackground=[("readonly", RAISE)],
          selectforeground=[("readonly", TEXT)], bordercolor=[("focus", NEON_DIM)])


def missing(script: Path) -> bool:
    """Say so when a button's script is not on this PC: cmd fails in a hidden window, so it would look as if it worked."""
    if not script.is_file():
        messagebox.showerror("Not found", f"{script} is missing, so this button cannot work on this PC.")
    return not script.is_file()


def set_text(box, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


class HomeBase(tk.Tk):
    def __init__(self):
        with contextlib.suppress(AttributeError, OSError):  # sharp text: Windows stops stretching a 96-DPI bitmap
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # this process only; must come before Tk starts
        for ttf in (a.ROOT / "assets" / "fonts").glob("*.ttf"):  # Plex and Oxanium for this process only: no install
            with contextlib.suppress(AttributeError, OSError):
                ctypes.windll.gdi32.AddFontResourceExW(str(ttf), 0x10, 0)  # 0x10 = FR_PRIVATE
        super().__init__()
        theme(self)
        self.title("HyperDesk Home Base")
        with contextlib.suppress(tk.TclError):  # the SILVADES HyperDesk icon in the title bar and taskbar
            self.iconbitmap(default=str(a.ROOT / "assets" / "hyperdesk.ico"))
        self.geometry("%dx%d" % px(1200, 780))
        self.minsize(*px(900, 600))
        self.cfg = a.load_config()
        self.events: queue.Queue = queue.Queue()  # (callback, value) posted by worker threads, run on the Tk thread
        self.action = ""  # title of the agent-os command running now
        self.refreshing = False
        self.signins = self.calls = ""
        self.build()
        dark_titlebar(self)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.pump)
        self.refresh()
        self.check_health()
        self.after(15000, self.tick)

    # --- plumbing: work happens in threads, results come back through the queue

    def background(self, work, done) -> None:
        def go():
            try:
                self.events.put((done, work()))
            except Exception as e:  # shown in the activity log; the window stays up
                self.events.put((self.log, f"error: {e!r}"))
        threading.Thread(target=go, daemon=True).start()

    def pump(self) -> None:
        self.after(100, self.pump)  # first: a callback that raises must not end the pump, and every later result
        while not self.events.empty():
            done, value = self.events.get_nowait()
            done(value)

    def log(self, line: str) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", ANSI.sub("", line) + "\n")  # tools like OmniRoute print terminal colour codes
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def command(self, title: str, cmd: list[str], then=None) -> None:
        """Run one agent-os style command at a time, streaming its output into the activity log."""
        if self.action:
            messagebox.showinfo("Busy", f"Wait for “{self.action}” to finish.")
            return
        self.action = title
        self.status.configure(text=f"Running: {title}")
        self.log(f"\n> {title}")
        out = a.ROOT / "logs" / "homebase-command.txt"

        def go():
            # Output goes to a file the window follows, not a pipe: closing the window must not break a running task.
            out.parent.mkdir(exist_ok=True)
            with open(out, "w", encoding="utf-8") as sink:
                p = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=HIDDEN, cwd=a.ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            with open(out, encoding="utf-8", errors="replace") as src:
                while True:
                    line = src.readline()
                    if line:
                        self.events.put((self.log, line.rstrip()))
                    elif p.poll() is not None:
                        return p.returncode
                    else:
                        time.sleep(0.2)

        def finished(code):
            self.log(f"< {title}: {'done' if code == 0 else f'exit {code}'}")
            self.action = ""
            self.status.configure(text="")
            self.refresh()
            if then:
                then()
        self.background(go, finished)

    # --- layout

    def build(self) -> None:
        top = self.top = ttk.Frame(self, padding=px(12, 10))
        top.pack(fill="x")
        self.crash = ttk.Frame(self, padding=px(12, 0, 12, 8))  # packed under `top` by show() while HyperDesk is down
        shared = "share" in self.cfg  # the shared build: no updates (a newer zip does that), tasks or usage; plain overview
        if not shared:
            ttk.Button(self.crash, text="Undo last update", command=self.undo_last).pack(side="right")  # first: it stays
        self.crash_text = ttk.Label(self.crash, foreground=COLORS[a.FAIL], wraplength=px(860))
        self.crash_text.pack(side="left")
        tk.Label(top, text="HYPERDESK", bg=VOID, fg=NEON, font=FONTS["disp"]).pack(side="left", padx=px(0, 14))
        self.headline = tk.Label(top, text="Checking health…", bg=VOID, fg=MUTED, font=(FONTS["disp"][0], 13))
        self.headline.pack(side="left")
        for text, cmd in (("Check health", self.check_health), *(() if shared else (("Update now", self.update_now),)),
                          ("Unload models", self.unload), ("Stop all AI (before gaming)", self.stop_all)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=px(3))

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=px(12))
        tabs = ttk.Notebook(panes)
        panes.add(tabs, weight=4)
        activity = ttk.LabelFrame(panes, text="ACTIVITY", padding=px(4))
        panes.add(activity, weight=1)
        self.logbox = text_box(activity, height=8)
        self.status = ttk.Label(self, padding=px(12, 4), anchor="w", style="Status.TLabel")
        self.status.pack(fill="x")

        home = ttk.Frame(tabs, padding=px(8))
        tabs.add(home, text="OVERVIEW")
        machine = ttk.LabelFrame(home, text="MACHINE", padding=px(6))
        machine.pack(fill="x")
        self.machine = ttk.Label(machine, text="…")
        self.machine.pack(anchor="w")
        self.agents = ttk.Label(machine, text="")
        self.agents.pack(anchor="w")
        models = ttk.LabelFrame(home, text="OLLAMA: MODELS IN VRAM", padding=px(6))
        models.pack(fill="both", expand=True, pady=px(8, 0))
        self.models = table(models, {"model": 0, "VRAM GB": 90, "unloads at": 220}, height=3)
        services = ttk.LabelFrame(home, text="SERVICES (127.0.0.1 ONLY)", padding=px(6))
        services.pack(fill="both", expand=True, pady=px(8, 0))
        self.services = table(services, {"service": 160, "port": 70, "state": 110, "address": 0}, height=3)
        row = ttk.Frame(services)
        row.pack(fill="x", pady=px(4, 0))
        for text, cmd in (("Start", self.start_service), ("Stop", self.stop_selected), ("Open", self.open_service)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=px(0, 4))
        if not shared:
            ttk.Label(row, text="OmniRoute: never connect a Claude or Codex subscription login in it.",
                      style="Note.TLabel").pack(side="right")

        health = ttk.Frame(tabs, padding=px(8))
        tabs.add(health, text="HEALTH")
        self.health = table(health, {"check": 110, "status": 60, "detail": 0, "fix": 0}, height=14)

        tasks = ttk.Frame(tabs, padding=px(8))
        if not shared:  # built either way, like UPDATES below, so show() still fills it
            tabs.add(tasks, text="TASKS")
        row = ttk.Frame(tasks)
        row.pack(fill="x", pady=px(0, 6))
        for text, cmd in (("New task…", self.new_task), ("Inspect", self.inspect), ("Merge", self.merge),
                          ("Roll back", self.rollback),
                          ("Undo merge", lambda: self.undo(self.selected(self.tasks, "task")))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=px(0, 4))
        ttk.Label(row, text="Claude and Codex run through their own CLIs on your subscriptions.",
                  style="Note.TLabel").pack(side="right")
        self.tasks = table(tasks, {"id": 180, "created": 130, "state": 130, "level": 45, "agent": 80,
                                   "tests before": 105, "after": 50, "task": 0}, height=14)
        self.tasks.bind("<Double-1>", lambda e: self.inspect())

        usage = ttk.Frame(tabs, padding=px(8))
        if not shared:
            tabs.add(usage, text="USAGE")
        self.usage = text_box(usage)

        updates = ttk.Frame(tabs, padding=px(8))
        if not shared:  # built either way, so show() still fills its widgets; only the tab is left out
            tabs.add(updates, text="UPDATES")
        row = ttk.Frame(updates)
        row.pack(fill="x", pady=px(0, 6))
        ttk.Button(row, text="Check for updates", command=lambda: self.command(
            "update check", agent_os("update", "--check"))).pack(side="left", padx=px(0, 4))
        ttk.Button(row, text="Update now", command=self.update_now).pack(side="left")
        self.schedule = ttk.Label(updates, text="", style="Note.TLabel")
        self.schedule.pack(anchor="w", pady=px(0, 6))
        self.update_log = text_box(updates)

    # --- data

    def tick(self) -> None:
        self.refresh()
        self.after(15000, self.tick)

    def refresh(self) -> None:
        if not self.refreshing:
            self.refreshing = True

            def work():
                try:
                    return snapshot(self.cfg)
                finally:  # also when it raises, or one failed snapshot stops every later refresh
                    self.refreshing = False
            self.background(work, self.show)

    def show(self, s: dict) -> None:
        if s["crash"]:  # the recovery console: HyperDesk did not start
            log = a.ROOT / "logs" / "hyperdesk-crash.txt"
            fix = "run Install.cmd from the HyperDesk OS zip again" if "share" in self.cfg else "undo"
            self.crash_text.configure(text=f"HyperDesk could not start: {s['crash']}\n{log}  ({fix}, then open it again)")
            self.crash.pack(fill="x", after=self.top)
        else:
            self.crash.pack_forget()
        try:
            name, free, total = [x.strip() for x in s["gpu"].split(",")]
            gpu = f"{name}: {int(free):,} of {int(total):,} MiB VRAM free"
        except ValueError:
            gpu = f"GPU: {s['gpu']}"
        game = f"   |   GAME RUNNING: {s['game']} (local models are skipped)" if s["game"] else ""
        self.machine.configure(text=f"{gpu}   |   Ollama {s['ollama']}{game}")
        self.calls = f"Agent calls today: {s['calls']} of {self.cfg['agents']['max_calls_per_day']}"
        self.show_agents()
        fill(self.models, [(m["name"], f"{m.get('size_vram', 0) / 2**30:.1f}", m.get("expires_at", "")[:19])
                           for m in s["loaded"]])
        fill(self.services, [(n, port, "running" if s["services"][n] else "stopped", url)
                             for n, (port, _, url) in SERVICES.items()],
             lambda r: (a.PASS,) if r[2] == "running" else ())
        blank = lambda v: "" if v is None else v  # noqa: E731
        fill(self.tasks, [(t[0], t[1][5:16], t[2], blank(t[3]), blank(t[4]), blank(t[5]), blank(t[6]), t[7])
                          for t in s["tasks"]], lambda r: TASK_TAGS.get(r[2], ()))
        set_text(self.usage, s["usage"])
        set_text(self.update_log, s["update_log"])
        self.schedule.configure(text=s["schedule"])

    def check_health(self) -> None:
        self.headline.configure(text="Checking health…", fg=MUTED)
        self.background(lambda: a.doctor_checks(self.cfg), self.show_health)

    def show_health(self, results) -> None:
        fill(self.health, [(n, st, d, f) for n, st, d, f in results], lambda r: (r[1],))
        fails = [n for n, st, _, _ in results if st == a.FAIL]
        warns = [n for n, st, _, _ in results if st == a.WARN]
        if fails:
            self.headline.configure(text=f"Problem: {', '.join(fails)}", fg=COLORS[a.FAIL])
        elif warns:
            self.headline.configure(text=f"Working, with warnings: {', '.join(warns)}", fg=COLORS[a.WARN])
        else:
            self.headline.configure(text="All systems OK", fg=COLORS[a.PASS])
        by = {n: d for n, _, d, _ in results}
        self.signins = f"Claude: {by.get('claude', '?')}   |   Codex: {by.get('codex', '?')}"
        self.show_agents()

    def show_agents(self) -> None:
        self.agents.configure(text="   |   ".join(x for x in (self.signins, self.calls) if x))

    # --- actions

    def selected(self, tree: ttk.Treeview, what: str) -> str:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Nothing selected", f"Select a {what} first.")
            return ""
        return sel[0]

    def start_service(self) -> None:
        name = self.selected(self.services, "service")
        if name:  # a command-line string: `start` only takes its first argument as the title when it is quoted
            # cwd: Open WebUI keeps its signing key in the folder it starts in, which must stay LOCALAI
            subprocess.Popen(f'cmd /c start "{name}" /min "{SERVICES[name][1]}"', cwd=LOCALAI, creationflags=HIDDEN)
            self.log(f"starting {name} in its own minimized window (closing that window stops it)")
            self.after(4000, self.refresh)

    def stop_selected(self) -> None:
        name = self.selected(self.services, "service")
        if name:
            self.log(f"stopping {name}")
            self.background(lambda: stop_service(name), lambda out: (self.log(out), self.refresh()))

    def open_service(self) -> None:
        name = self.selected(self.services, "service")
        if name:  # its own app window, like Start AI.bat does for Open WebUI
            subprocess.Popen(f'cmd /c start "" msedge --app={SERVICES[name][2]}', creationflags=HIDDEN)

    def unload(self) -> None:
        if not missing(LOCALAI / "bin" / "unload-ai.cmd"):
            self.command("unload models", ["cmd", "/c", str(LOCALAI / "bin" / "unload-ai.cmd")])

    def stop_all(self) -> None:  # the one kill switch: Ollama itself, every service and job, and Home Base too
        if not missing(LOCALAI / "Stop AI.bat"):
            subprocess.Popen(f'cmd /c start "Stop AI" "{LOCALAI / "Stop AI.bat"}"', cwd=LOCALAI, creationflags=HIDDEN)

    def update_now(self) -> None:
        self.command("update", agent_os("update"))

    def new_task(self) -> None:
        workspace = LOCALAI / "workspace"
        repos = sorted(p.name for p in workspace.iterdir() if (p / ".git").exists()) if workspace.is_dir() else []
        if not repos:
            messagebox.showinfo("No repos", f"Put a git repo in {workspace} first.")
            return
        win = tk.Toplevel(self)
        win.title("New task")
        win.transient(self)
        win.grab_set()
        body = ttk.Frame(win, padding=px(12))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="What should be done?").grid(row=0, column=0, columnspan=2, sticky="w")
        text = tk.Text(body, width=70, height=5, wrap="word", **{**FIELD, "fg": TEXT}, font=FONTS["sans"])
        text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=px(2, 8))
        fields = {}
        for i, (label, values) in enumerate((("Repo", repos), ("Agent", [
                f"as configured ({self.cfg['agents']['implementer']})", "opencode", "claude", "codex"]), ("Level", LEVELS)), 2):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w", pady=px(2))
            box = ttk.Combobox(body, values=values, state="readonly", width=40)
            box.current(0)
            box.grid(row=i, column=1, sticky="w", pady=px(2))
            fields[label] = box

        def go():
            task = text.get("1.0", "end").strip()
            if not task:
                return
            args = ["run", task, "--repo", str(workspace / fields["Repo"].get())]
            if fields["Agent"].current() > 0:
                args += ["--agent", fields["Agent"].get()]
            if fields["Level"].current() > 0:
                args += ["--level", str(fields["Level"].current() - 1)]
            win.destroy()
            self.command(f"task: {task[:50]}", agent_os(*args))

        row = ttk.Frame(body)
        row.grid(row=5, column=0, columnspan=2, sticky="e", pady=px(10, 0))
        ttk.Button(row, text="Run", command=go, style="Go.TButton").pack(side="right")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=px(4))
        dark_titlebar(win)
        text.focus_set()

    def inspect(self) -> None:
        task_id = self.selected(self.tasks, "task")
        if not task_id:
            return
        with contextlib.closing(a.ledger()) as con:
            report = capture(a.inspect_task, con, task_id)
        win = tk.Toplevel(self)
        win.title(f"Task {task_id}")
        win.geometry("%dx%d" % px(1000, 700))
        set_text(text_box(win), report)
        dark_titlebar(win)

    def merge(self) -> None:
        task_id = self.selected(self.tasks, "task")
        if task_id and messagebox.askyesno("Merge", f"Merge task {task_id} into its repo?\n\nThis fast-forwards the "
                                           "branch you have checked out there (or merges it, if the tests pass), then "
                                           "removes the task's worktree."):
            self.command(f"merge {task_id}", agent_os("merge", task_id))

    def undo(self, task_id: str) -> None:
        if task_id and messagebox.askyesno("Undo", f"Undo merged task {task_id}?\n\nA git revert adds new commits "
                                           "that take its changes back out of its repo. Nothing is deleted."):
            self.command(f"undo {task_id}", agent_os("undo", task_id))

    def undo_last(self) -> None:
        """The recovery button: undo the newest update merged into Agent OS itself (HyperDesk, Home Base)."""
        try:
            with contextlib.closing(a.ledger()) as con:
                task_id = a.last_merged(con, str(a.ROOT))
                # only an update applied since HyperDesk last started can be why it no longer starts
                opened = con.execute("select max(at) from actions where what = 'HyperDesk opened'").fetchone()[0] or ""
                applied = task_id and con.execute("select max(at) from events where task = ? and state = 'COMPLETED'",
                                                  (task_id,)).fetchone()[0] or ""
        except sqlite3.Error as e:
            messagebox.showerror("Undo last update", f"The ledger cannot be read ({e}). See HEALTH: state-db.")
            return
        if not task_id or applied < opened:
            messagebox.showinfo("Undo last update", "No update was applied since HyperDesk last started, so undoing "
                                "one will not fix this. The log named above shows what stopped it.")
        else:
            self.undo(task_id)  # a good start of HyperDesk then deletes the crash file, and this banner goes

    def rollback(self) -> None:
        task_id = self.selected(self.tasks, "task")
        if task_id and messagebox.askyesno("Roll back", f"Discard task {task_id}?\n\nIts worktree and branch are "
                                           "deleted. Your repo itself is not touched."):
            self.command(f"rollback {task_id}", agent_os("rollback", task_id))

    def close(self) -> None:
        if self.action and not messagebox.askyesno(
                "Still running", f"“{self.action}” is still running. It carries on without this window "
                "and records its result in the ledger. Close anyway?"):
            return
        self.destroy()


if __name__ == "__main__":
    a.start_ollama()
    HomeBase().mainloop()
