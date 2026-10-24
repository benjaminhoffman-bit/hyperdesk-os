"""HyperDesk Game Mode: one click before you play, for maximum performance.

Scans every app running in your session and ticks all of them except the keep list (Discord, music apps, game
launchers, games, anything playing sound) and what the PC needs (Windows itself, anti-cheat, mouse, sound, mic and VR
drivers' apps). GO force-kills the ticked ones, twice so nothing they respawn survives (unsaved work in them is lost).
The shared build ([share] in config.toml) also keeps accessibility, password managers, sync, VPN and vendor tools,
touchpad, sound and hotkey helpers,
and closes the ticked ones once instead, as if X was clicked (an app may ask to save; one without a window keeps
running). Then Stop AI.bat, so no model holds VRAM. Needs no admin and changes no Windows setting.
"""
import contextlib
import ctypes
import ctypes.wintypes as w
import json
import os
import re
import subprocess
import sys
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import homebase as hb  # noqa: E402  (the HyperDesk palette, fonts and theme; agent_os comes with it as hb.a)

a = hb.a
ASSETS = a.ROOT / "assets"
# Never killed: matched, lower case, against each process's exe path. A kept app keeps every process of its exe and
# everything it started (the game a launcher ran).
KEEP = ("discord", "teamspeak", "mumble", "spotify", "tidal", "applemusic", "apple music", "itunes", "youtube music",
        "youtube-music", "amazon music", "deezer", "obs-studio", "tiktok live studio", "steam", "battle.net",
        "electronic arts", "ubisoft", "gog galaxy", "xbox", "games\\", "nvidia")
# Matched against window titles too, so music and chat web apps keep their browser.
TITLES = ("discord", "spotify", "tidal", "apple music", "youtube music", "amazon music", "deezer")
# Never killed either: what games, input, sound, the mic and fan curves need. Matched like KEEP.
PROTECT = ("anticheat", "battleye", "vanguard", "faceit", "gameinput", "\\oculus\\", "\\lghub\\", "logioptions", "razer",
           "corsair", "steelseries", "\\creative\\", "nahimic", "realtek", "voicemeeter", "vb-audio", "voicemod", "krisp",
           "wacom", "\\intel\\", "\\amd\\", "afterburner", "rivatuner", "ds4windows", "elgato", "nzxt", "fancontrol")
# The shared build never closes these either: what a stranger's PC may need. Matched like KEEP. "\\omen", not "omen"
# (phenomenon); the sync exes, not "onedrive" (Desktop may live in the synced folder).
STRANGER = ("nvda", "freedom scientific", "nuance", "tobii", "powertoys", "autohotkey", "1password", "bitwarden",
            "keepass", "\\onedrive.exe", "\\dropbox.exe", "googledrivefs", "vpn", "armoury", "\\asus\\", "\\msi\\",
            "msicenter", "lenovo", "\\omen", "omencommandcenter", "alienware", "streamlabs", "overwolf",
            # touchpad, sound and hotkey helpers that live in Program Files, and more sync, VPN and password apps
            "synaptics", "\\elantech\\", "\\alps", "\\waves\\", "maxxaudio", "dolby", "conexant", "\\hp\\", "\\dell\\",
            "\\acer\\", "gigabyte", "tabtip", "sogou", "tailscale", "wireguard", "cloudflare warp", "zerotier",
            "proton pass", "nordpass", "enpass", "\\box\\", "icloud", "megasync", "nextcloud", "syncthing")
# Cut from exe paths before KEEP words: a profile named c:\users\asus is no vendor app.
HOME = os.environ.get("USERPROFILE", "").lower() + "\\"
DROP = ("\\wallpaper_engine\\",)  # killed even though a KEEP word (steam) matches: it holds VRAM while paused
AI = re.compile(re.escape(str(hb.LOCALAI).lower() + "\\") + r"(agentos|comfyui|open-webui|start-)|\\programs\\ollama\\"
                r"|omniroute|opencode")  # = Stop AI.bat; LOCALAI is %LOCALAPPDATA%\HyperDesk in a shared install
SYSTEM = os.environ.get("SystemRoot", r"C:\Windows").lower() + "\\"
user32 = ctypes.windll.user32
user32.MonitorFromWindow.restype = w.HMONITOR
user32.GetMonitorInfoW.argtypes = (w.HMONITOR, ctypes.c_void_p)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def processes() -> list[dict]:
    """Every process with parent, session, exe, command line and RAM: one CIM call, no admin (as Stop AI.bat does)."""
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Select-Object ProcessId,"
          "ParentProcessId,SessionId,ExecutablePath,CommandLine,WorkingSetSize | ConvertTo-Json -Compress")
    with contextlib.suppress(OSError, subprocess.TimeoutExpired, ValueError):
        return json.loads(subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                         encoding="utf-8", errors="replace", timeout=60, creationflags=hb.HIDDEN).stdout)
    return []


def windows() -> tuple[dict, set]:
    """Window titles by pid, and the pids with a window the size of its monitor (the game, even when alt-tabbed)."""
    titles, full = {}, set()

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def each(hwnd, _):
        h, pid, r, mon = w.HWND(hwnd), w.DWORD(), (ctypes.c_long * 11)(44), (ctypes.c_long * 10)(40)
        n = user32.GetWindowTextLengthW(h)
        if n and user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            titles[pid.value] = f"{titles.get(pid.value, '')} {buf.value}"
            if user32.IsIconic(h):  # minimized (an alt-tabbed full-screen game): its restored rect, WINDOWPLACEMENT
                user32.GetWindowPlacement(h, r)
                r = r[7:11]
            else:
                user32.GetWindowRect(h, r)
                r = r[0:4]
            user32.GetMonitorInfoW(user32.MonitorFromWindow(h, 2), mon)  # MONITORINFO; 2 = nearest monitor
            if (r[2] - r[0], r[3] - r[1]) == (mon[3] - mon[1], mon[4] - mon[2]):
                full.add(pid.value)
        return True

    user32.EnumWindows(each, 0)
    return titles, full


def playing() -> set:
    """Pids with sound playing right now: music in a background browser tab, a game. Core Audio through ctypes."""
    pids, ref = set(), ctypes.addressof

    def com(obj, i, *args):  # call method i of a COM object's vtable; raises OSError on a failed HRESULT
        fn = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[i]
        ctypes.WINFUNCTYPE(ctypes.HRESULT, *[ctypes.c_void_p] * (len(args) + 1))(fn)(obj, *args)

    iid = lambda s: ctypes.create_string_buffer(uuid.UUID(s).bytes_le)  # noqa: E731
    mgr2, ctl2 = iid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), iid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    enum, devices, n = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_uint()
    # ponytail: COM references are never Released; the app lives seconds and Windows frees them at exit
    with contextlib.suppress(OSError):
        ctypes.windll.ole32.CoInitialize(None)
        if ctypes.windll.ole32.CoCreateInstance(iid("BCDE0395-E52F-467C-8E3D-C4579291692E"), None, 23,  # MMDeviceEnumerator
                                                iid("A95664D2-9614-4F35-A746-DE8DB63617E6"), ctypes.byref(enum)):
            return pids
        com(enum, 3, 0, 1, ref(devices))  # EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)
        com(devices, 3, ref(n))  # GetCount
    for d in range(n.value):  # a device or session that fails (a headset mid-reconnect) skips only itself
        dev, mgr, sessions, count = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_int()
        with contextlib.suppress(OSError):
            com(devices, 4, d, ref(dev))  # Item
            com(dev, 3, ref(mgr2), 23, None, ref(mgr))  # Activate(IAudioSessionManager2)
            com(mgr, 5, ref(sessions))  # GetSessionEnumerator
            com(sessions, 3, ref(count))
        for s in range(count.value):
            ctl, ctl_2, state, pid = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_int(), w.DWORD()
            with contextlib.suppress(OSError):
                com(sessions, 4, s, ref(ctl))  # GetSession
                com(ctl, 0, ref(ctl2), ref(ctl_2))  # QueryInterface(IAudioSessionControl2)
                com(ctl_2, 3, ref(state))  # GetState
                com(ctl_2, 14, ref(pid))  # GetProcessId
                if state.value == 1:  # AudioSessionStateActive
                    pids.add(pid.value)
    return pids


def plan(procs: list[dict], me: int, titles: dict, full: set, words: tuple, keep_ai: bool,
         keep: tuple = KEEP + PROTECT) -> tuple[dict, list]:
    """({exe: [process]} to kill, names kept). Candidates: this session's processes outside Windows. A seed (an exe with
    a keep word, a music or chat window, full screen or playing sound) keeps every process of its exe and all it started;
    so do this app and Windows' own UI. `keep`: keep words, matched below HOME; `words`: config games and unticked rows,
    matched on the whole path. keep_ai: the AI (and its jobs) too."""
    session = next((p["SessionId"] for p in procs if p["ProcessId"] == me), None)
    exe = {p["ProcessId"]: (p["ExecutablePath"] or "").removeprefix("\\\\?\\").lower() for p in procs}
    mine = {p["ProcessId"]: p for p in procs
            if p["SessionId"] == session and exe[p["ProcessId"]] and not exe[p["ProcessId"]].startswith(SYSTEM)}
    drop = {pid for pid in mine if any(d in exe[pid] for d in DROP)}
    seeds = {pid for pid in mine if pid in full or any(k in exe[pid].removeprefix(HOME) for k in keep)
             or any(k in exe[pid] for k in words) or any(k in titles.get(pid, "").lower() for k in TITLES)} - drop
    kept = {pid for pid in mine if exe[pid] in {exe[s] for s in seeds}} | {me}  # not all pythonw because of us
    if keep_ai:
        kept |= {pid for pid, p in mine.items() if AI.search(f"{exe[pid]} {(p['CommandLine'] or '').lower()}")}
    children: dict = {}
    for p in procs:
        children.setdefault(p["ParentProcessId"], []).append(p["ProcessId"])
    todo = list(kept | {pid for pid, path in exe.items() if path.startswith(SYSTEM + "systemapps\\")})  # Start, Search
    while todo:  # everything a kept process or Windows' own UI started (a launcher's game, Search's web view)
        for child in children.get(todo.pop(), []):
            if child not in kept and child not in drop:
                kept.add(child)
                todo.append(child)
    kill: dict = {}
    for pid, p in mine.items():
        if pid not in kept and "\\microsoft.windowsterminal_" not in exe[pid]:  # its tabs are judged one by one
            kill.setdefault(exe[pid], []).append(p)
    return kill, sorted({Path(mine[pid]["ExecutablePath"]).stem for pid in kept if pid in mine}, key=str.lower)


def terminate(pid: int) -> bool:
    """Force-kill one process. True when it is gone or going."""
    h = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not h:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such process any more
    ok = k32.TerminateProcess(h, 1) or ctypes.get_last_error() == 5  # ACCESS_DENIED here: it is already exiting
    k32.CloseHandle(h)
    return bool(ok)


class GameMode(tk.Tk):
    def __init__(self):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # as Home Base: sharp text, real pixel sizes
        for ttf in (ASSETS / "fonts").glob("*.ttf"):
            with contextlib.suppress(AttributeError, OSError):
                ctypes.windll.gdi32.AddFontResourceExW(str(ttf), 0x10, 0)  # FR_PRIVATE: this process only
        super().__init__()
        hb.theme(self)
        s = ttk.Style(self)
        s.configure("TCheckbutton", indicatorbackground=hb.RAISE, indicatorforeground=hb.NEON,
                    upperbordercolor=hb.LINE2, lowerbordercolor=hb.LINE2)
        s.map("TCheckbutton", background=[("active", hb.VOID)], foreground=[("active", hb.NEON)],
              indicatorbackground=[("active", hb.RAISE2)])
        self.title("HyperDesk Game Mode")
        with contextlib.suppress(tk.TclError):
            self.iconbitmap(default=str(ASSETS / "gamemode.ico"))
        self.configure(padx=hb.px(18), pady=hb.px(14))
        self.minsize(hb.px(720), 0)
        cfg = a.load_config()
        self.games = tuple(g.lower() for g in cfg["gpu"]["games"])
        self.shared = "share" in cfg  # a stranger's PC: close politely, never force-kill
        self.rows: list = []
        self.spared: set = set()  # exes unticked so far: they stay unticked across Refresh
        self.logo = tk.PhotoImage(file=str(ASSETS / "gamemode-256.png")).subsample(4)
        top = ttk.Frame(self)
        top.pack(fill="x")
        tk.Label(top, image=self.logo, bg=hb.VOID).pack(side="left")
        tk.Label(top, text="GAME MODE", bg=hb.VOID, fg=hb.NEON, font=(hb.FONTS["disp"][0], 22, "bold")).pack(
            side="left", padx=hb.px(14))
        ttk.Label(self, text=("Ticked apps are closed, as if you clicked X: an app may ask to save first."
                              if self.shared else "Ticked apps are force-killed: anything unsaved in them is lost.")
                  + " Untick what you want.", style="Note.TLabel").pack(anchor="w", pady=hb.px(12, 6))
        self.list = ttk.Frame(self)
        bar = ttk.Frame(self)  # packed from the bottom up before the list: GO stays on screen however many apps run
        bar.pack(side="bottom", fill="x", pady=hb.px(12, 0))
        self.status = ttk.Label(bar, text="Scanning…", style="Note.TLabel")
        self.status.pack(side="left")
        self.go_button = ttk.Button(bar, text="GO", style="Go.TButton", command=self.go)
        self.go_button.pack(side="right")
        ttk.Button(bar, text="Refresh", command=self.scan).pack(side="right", padx=hb.px(8))
        self.ai = tk.BooleanVar(value=True)
        back = "opening HyperDesk" if self.shared else "Start AI.bat"
        ttk.Checkbutton(self, text=f"Stop local AI too (frees VRAM; {back} brings it back)",
                        variable=self.ai, command=self.scan).pack(side="bottom", anchor="w")
        self.kept = ttk.Label(self, style="Note.TLabel", wraplength=hb.px(700))
        self.kept.pack(side="bottom", anchor="w", pady=hb.px(10, 4))
        self.list.pack(fill="both", expand=True)
        hb.dark_titlebar(self)
        self.after(50, self.scan)  # the window first, then the ~2 s process scan

    def plan(self) -> tuple[dict, list]:
        """A fresh scan. Unticked rows count as kept, so what they started is spared too."""
        self.spared = (self.spared | {exe for var, exe in self.rows if not var.get()}) - {
            exe for var, exe in self.rows if var.get()}
        titles, full = windows()
        return plan(processes(), os.getpid(), titles, full | playing(), self.games + tuple(self.spared),
                    keep_ai=not self.ai.get(), keep=KEEP + PROTECT + (STRANGER if self.shared else ()))

    def scan(self) -> None:
        kill, kept = self.plan()
        for child in self.list.winfo_children():
            child.destroy()
        ram = lambda ps: sum(p["WorkingSetSize"] or 0 for p in ps)  # noqa: E731
        groups = sorted(kill.items(), key=lambda kv: -ram(kv[1]))
        # ponytail: two columns, more when the screen is too short for them (every ticked row must be on screen);
        # a scrolling list if one day even that does not fit the width
        fits = max(5, (self.winfo_screenheight() - hb.px(420)) // hb.px(30))
        self.rows, half = [], max(1, min((len(groups) + 1) // 2, fits))
        for i, (exe, ps) in enumerate(groups):  # biggest first, down each column
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.list, variable=var, text=f"{Path(ps[0]['ExecutablePath']).stem}  ×{len(ps)}   "
                            f"{ram(ps) / 2**20:,.0f} MB").grid(row=i % half, column=i // half, sticky="w",
                                                               padx=hb.px(0, 24), pady=hb.px(1))
            self.rows.append((var, exe))
        if not groups:
            ttk.Label(self.list, text="Nothing to close." if self.shared else "Nothing to kill.").grid(sticky="w")
        self.kept.configure(text="Keeping: " + (", ".join(kept) or "nothing"))
        total = sum(ram(ps) for ps in kill.values()) / 2**30
        upto = "up to " if self.shared else ""  # a polite close leaves windowless and save-asking apps running
        self.status.configure(text=f"{len(groups)} apps, {upto}{total:.1f} GB RAM" if kill or kept else
                              "Could not read the process list (Refresh to try again).")

    def go(self) -> None:
        ticked = {exe for var, exe in self.rows if var.get()}  # only what was on screen, ticked, is ever killed
        self.go_button.state(["disabled"])
        self.status.configure(text="Closing..." if self.shared else "Killing…")
        self.update_idletasks()
        if self.shared:  # once, taskkill without /f: WM_CLOSE, like clicking X (a fresh scan, as below)
            kill, _ = self.plan()
            pids = [f for exe, ps in kill.items() if exe in ticked for p in ps for f in ("/pid", str(p["ProcessId"]))]
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                if pids:  # fails quietly for a process with no window or an admin one: those keep running
                    subprocess.run(["taskkill", *pids], capture_output=True, timeout=60, creationflags=hb.HIDDEN)
        else:
            for _ in range(2):  # twice, as Stop AI.bat: also what a dying parent was about to respawn
                kill, _ = self.plan()  # a fresh scan each pass: a PID seen minutes ago may be another app now
                failed = sorted({Path(exe).name for exe, ps in kill.items() if exe in ticked
                                 for p in ps if not terminate(p["ProcessId"])})
            if failed:
                messagebox.showwarning("Game Mode", "Could not kill these (they run as admin):\n\n" + "\n".join(failed))
        if self.ai.get() and not hb.missing(hb.LOCALAI / "Stop AI.bat"):  # Home Base's kill switch: ports, Ollama, us
            subprocess.Popen(f'cmd /c start "Stop AI" "{hb.LOCALAI / "Stop AI.bat"}"', cwd=hb.LOCALAI,
                             creationflags=hb.HIDDEN)
        self.destroy()


if __name__ == "__main__":
    GameMode().mainloop()
