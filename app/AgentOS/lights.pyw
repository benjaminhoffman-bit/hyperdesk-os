"""HyperDesk Lights: one color and brightness for every light OpenRGB can reach, from a small window.

Needs OpenRGB (openrgb.org) with its SDK server on 127.0.0.1:6742; speaks the SDK's protocol 0, its oldest and simplest
layout (OpenRGBSDK.md). Without [share] in config.toml it starts the portable OpenRGB in ..\\workspace\\openrgb hidden
(its settings in its config folder) unless a server already answers, and stops it when the window closes; the shared
build only connects. Needs no admin and changes no Windows setting. A board, fans or GPU OpenRGB does not support yet
stay with their vendor's app and show up here by themselves once it does.
"""
import contextlib
import ctypes
import socket
import struct
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import homebase as hb  # noqa: E402  (the HyperDesk palette, fonts and theme)

ASSETS = hb.a.ROOT / "assets"
OPENRGB_DIR = hb.LOCALAI / "workspace" / "openrgb"
OPENRGB = OPENRGB_DIR / "OpenRGB Windows 64-bit" / "OpenRGB.exe"
HOST, PORT = "127.0.0.1", 6742
COUNT, DATA, CLIENT_NAME, UPDATE_LEDS, CUSTOM_MODE = 0, 1, 50, 1050, 1100  # SDK packet ids
GAMMA = 2.2  # LEDs look brighter than their value; 2.2 makes the slider feel even. 1.0 = linear
SWATCHES = ("#ff0000", "#ff7a00", "#ffd000", "#39ff14", "#00e5ff", "#0040ff", "#8a2be2", "#ff00b4", "#ffffff")


def packet(dev: int, pid: int, data: bytes = b"") -> bytes:
    """One SDK packet: 'ORGB', device index, packet id, data size (little-endian), then the data."""
    return b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data


def controller(blob: bytes) -> tuple[str, int]:
    """(name, LED count) from a protocol-0 controller data block. Strings are a u16 length (counting the NUL), bytes."""
    pos = 8  # data_size, type

    def u16() -> int:
        nonlocal pos
        pos += 2
        return struct.unpack_from("<H", blob, pos - 2)[0]

    def text() -> str:
        nonlocal pos
        n = u16()
        pos += n
        return blob[pos - n:pos].rstrip(b"\0").decode("utf-8", "replace")

    name = text()
    for _ in range(4):  # description, version, serial, location
        text()
    modes = u16()
    pos += 4  # active mode
    for _ in range(modes):
        text()
        pos += 36  # value, flags, speed min/max, colors min/max, speed, direction, color mode
        n = u16()  # never `pos += 4 * u16()`: += reads pos before u16() moves it
        pos += 4 * n  # the mode's colors
    for _ in range(u16()):  # zones
        text()
        pos += 16  # type, LEDs min/max/count
        n = u16()
        pos += n  # matrix map (height, width, cells); 0 when the zone has none
    return name, u16()


def start_server() -> subprocess.Popen | None:
    """OpenRGB's server, hidden, on 127.0.0.1 only. None when one already answers (that one is not ours to stop) or in
    the shared build, which never launches an exe: it connects to the OpenRGB the user runs."""
    if "share" in hb.a.load_config():
        return None
    with contextlib.suppress(OSError):
        socket.create_connection((HOST, PORT), timeout=0.5).close()
        return None
    config = OPENRGB_DIR / "config"
    config.mkdir(exist_ok=True)
    return subprocess.Popen([str(OPENRGB), "--server", "--server-host", HOST, "--server-port", str(PORT),
                             "--noautoconnect", "--config", str(config)], cwd=OPENRGB.parent, creationflags=hb.HIDDEN)


class Client:
    """One connection to the OpenRGB server."""

    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=3)
        self.sock.sendall(packet(0, CLIENT_NAME, b"HyperDesk Lights\0"))
        self.custom: set = set()  # devices already switched to their per-LED (Direct) mode

    def read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("OpenRGB closed the connection")
            buf += chunk
        return buf

    def ask(self, dev: int, pid: int) -> bytes:
        self.sock.sendall(packet(dev, pid))
        while True:  # the server also sends DEVICE_LIST_UPDATED whenever it likes: skip what was not asked for
            magic, _, got, size = struct.unpack("<4sIII", self.read(16))
            if magic != b"ORGB":
                raise OSError("not an OpenRGB server")
            data = self.read(size)
            if got == pid:
                return data

    def count(self) -> int:
        return struct.unpack("<I", self.ask(0, COUNT))[0]

    def devices(self) -> list[tuple[str, int]]:
        return [controller(self.ask(i, DATA)) for i in range(self.count())]

    def paint(self, dev: int, leds: int, rgb: tuple) -> None:
        if dev not in self.custom:  # once: setting a mode makes some devices blink
            self.sock.sendall(packet(dev, CUSTOM_MODE))
            self.custom.add(dev)
        body = struct.pack("<H", leds) + bytes((*rgb, 0)) * leds  # RGBColor bytes: R, G, B, 0
        self.sock.sendall(packet(dev, UPDATE_LEDS, struct.pack("<I", 4 + len(body)) + body))


class Lights(tk.Tk):
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
        s.configure("Horizontal.TScale", background=hb.NEON, troughcolor=hb.RAISE2, bordercolor=hb.LINE2,
                    lightcolor=hb.NEON, darkcolor=hb.NEON_DIM)
        self.title("HyperDesk Lights")
        with contextlib.suppress(tk.TclError):
            self.iconbitmap(default=str(ASSETS / "hyperdesk.ico"))
        self.configure(padx=hb.px(18), pady=hb.px(14))
        self.minsize(hb.px(460), 0)
        self.color, self.client, self.rows, self.proc = hb.NEON, None, [], None
        tk.Label(self, text="LIGHTS", bg=hb.VOID, fg=hb.NEON, font=(hb.FONTS["disp"][0], 22, "bold")).pack(anchor="w")
        ttk.Label(self, text="Ticked lights follow the color. Needs OpenRGB (openrgb.org) with its SDK server on.",
                  style="Note.TLabel", wraplength=hb.px(440)).pack(anchor="w", pady=hb.px(6, 4))
        self.list = ttk.Frame(self)
        self.list.pack(fill="x", pady=hb.px(4, 10))
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.swatch = tk.Label(row, width=6, bg=self.color, cursor="hand2")
        self.swatch.pack(side="left", fill="y", padx=hb.px(0, 12))
        self.swatch.bind("<Button-1>", lambda _: self.pick())
        for c in SWATCHES:
            chip = tk.Label(row, width=2, bg=c, cursor="hand2")
            chip.pack(side="left", padx=hb.px(2))
            chip.bind("<Button-1>", lambda _, c=c: self.set_color(c))
        row = ttk.Frame(self)
        row.pack(fill="x", pady=hb.px(12, 0))
        self.bright = tk.DoubleVar(value=100)
        ttk.Scale(row, from_=0, to=100, variable=self.bright, command=self.apply).pack(side="left", fill="x",
                                                                                      expand=True)
        self.level = ttk.Label(row, text="100%", width=5, anchor="e")
        self.level.pack(side="left", padx=hb.px(8, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=hb.px(14, 0))
        self.status = ttk.Label(bar, text="Starting OpenRGB…", style="Note.TLabel", wraplength=hb.px(300))
        self.status.pack(side="left")
        ttk.Button(bar, text="Off", command=lambda: (self.bright.set(0), self.apply())).pack(side="right")
        ttk.Button(bar, text="Rescan", command=self.rescan).pack(side="right", padx=hb.px(8))
        hb.dark_titlebar(self)
        self.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.proc = start_server()
        except OSError:
            self.status.configure(text=f"OpenRGB is not at {OPENRGB}")
            return
        self.rescan()

    def rescan(self) -> None:
        with contextlib.suppress(OSError, AttributeError):
            self.client.sock.close()
        self.client, self.deadline = None, time.monotonic() + 20
        self.status.configure(text="Looking for lights…")
        self.after(500, self.wait)

    def wait(self, last: int = -1) -> None:
        """Poll until the server answers and its device count holds still for a second (detection takes ~6 s)."""
        try:
            self.client = self.client or Client()
            n = self.client.count()
        except OSError:
            self.client, n = None, -1
        # ponytail: "still for 1 s" can miss a device that detects later; Rescan picks it up
        if (n > 0 and n == last) or time.monotonic() > self.deadline:
            self.load()
        else:
            self.after(1000, self.wait, n)

    def load(self) -> None:
        for child in self.list.winfo_children():
            child.destroy()
        self.rows = []
        try:
            devices = self.client.devices()
        except (OSError, AttributeError, struct.error):
            self.status.configure(text="Open OpenRGB, keep its SDK server on 127.0.0.1 port 6742, start it, "
                                  "then Rescan.")
            return
        for dev, (name, leds) in enumerate(devices):
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.list, text=f"{name}   ({leds} LEDs)", variable=var, command=self.apply).pack(
                anchor="w", pady=hb.px(1))
            self.rows.append((var, dev, leds))
        self.status.configure(text=f"{len(devices)} lights" if devices else "OpenRGB found no lights.")

    def pick(self) -> None:
        _, color = colorchooser.askcolor(self.color, parent=self, title="Light color")
        if color:
            self.set_color(color)

    def set_color(self, color: str) -> None:
        self.color = color
        self.swatch.configure(bg=color)
        if self.bright.get() < 1:  # a color after Off means on again
            self.bright.set(100)
        self.apply()

    def apply(self, *_) -> None:
        """Paint every ticked light; unticked ones keep what they show."""
        b = self.bright.get()
        self.level.configure(text=f"{b:.0f}%")
        rgb = tuple(round(int(self.color[i:i + 2], 16) * (b / 100) ** GAMMA) for i in (1, 3, 5))
        if not self.client:  # mid-Rescan: no connection yet
            return
        try:
            for var, dev, leds in self.rows:
                if var.get() and leds:
                    self.client.paint(dev, leds, rgb)
        except OSError:
            self.status.configure(text="Lost OpenRGB (Rescan to reconnect).")

    def close(self) -> None:
        with contextlib.suppress(OSError, AttributeError):
            self.client.sock.close()
        if self.proc:  # only the server we started; lights keep their last color
            self.proc.terminate()
        self.destroy()


if __name__ == "__main__":
    Lights().mainloop()
