"""Quick edit: one video in, a cleaned-up cut out, with no AI. Everything lands in workspace\\videos\\<name>-<id>\\
(id = a short hash of the full path, so two videos with one name never share a folder); the video's own folder is not
written to. Needs FFmpeg (ffmpeg and ffprobe).

By default the whole video stays. Only the stale parts go (a frozen picture with no sound, like a pause in a screen
recording), every cut gets a short transition, and out comes <name>_youtube.mp4 (landscape, source size) and/or, with
--for social or both, <name>_vertical.mp4 (1080x1920, 30 fps; fit = the whole frame over a blurred fill).

--commands takes typed editing commands, one per line (or split by , or ;): see FORMS. They are read strictly: any
line that is not one of the forms refuses the whole edit before anything runs. The plan is saved next to the result
as edit_plan.json; the pause analysis as stale.json, keyed to the source's path, size and time (--fresh redoes it).

    python quick_edit.py recording.mp4 --for both --commands "trim start 5, cut 2:10-2:40, transition none"
"""
import argparse
import contextlib
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from itertools import zip_longest
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "workspace" / "videos"
FRAMES = {  # any source shape to 1080x1920
    "fit": "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];"
           "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
    "crop": "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
}
GRID = 30  # cut points snap to 1/30 s: whole frames at 30 and 60 fps, whole samples at 48 kHz, so parts join exactly
PAUSE = 2.0  # cut stale spans (frozen picture, no sound) at least this long...
PAD = 0.4  # ...but leave this much at each end, so a cut never clips a word (commands can change both)
MIN_KEEP = 0.5  # a kept sliver shorter than this between two cuts is cut too
FREEZE_NOISE = 0.0005  # freezedetect tolerance (5 fps, 480 px): a wandering cursor is frozen, a tree selection isn't
SILENCE_DB = -35  # quieter than this is silence...
CLICK = 0.4  # ...and a sound shorter than this (a mouse click, a key) doesn't end it
TRANSITIONS = ["fade", "dissolve", "fadeblack", "wipeleft", "slideleft", "smoothleft", "circleopen", "zoomin", "none"]
TRANSITION_S = 0.4
NVENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-profile:v", "high"]
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]  # any CPU: quick, no thread cap
TIME = r"\d+(?::[0-5]\d){0,2}(?:\.\d+)?s?"  # 90, 90s, 75:00, 1:30, 1:02:03.5 (not 1:75)
NUM = r"\d+(?:\.\d+)?s?"
STEM = 40  # of a video's name in folder and file names: a long download name would pass Windows' 260-character paths
FORMS = f"""  trim start SECONDS         remove the first SECONDS (trim end SECONDS: the last)
  cut M:SS-M:SS              remove a span
  keep M:SS-M:SS             never remove this span (wins over pauses and cuts)
  speed M:SS-M:SS FACTOR     play a span faster (2) or slower (0.5); 0.5 to 8
  pauses off                 leave all pauses in (pauses SECONDS: only cut pauses this long, 0.5 to 60, now {PAUSE:g})
  pad SECONDS                how much of each cut pause stays at both ends, 0 to 3 (now {PAD:g})
  transition NAME [SECONDS]  NAME: {", ".join(TRANSITIONS)} (none = hard cuts); SECONDS 0.1 to 1.5"""


def ffmpeg_path() -> str:
    """PATH, plus winget's FFmpeg folder when ffmpeg or ffprobe is not on it (winget's FFmpeg is on PATH only for
    programs started after it was installed)."""
    path = os.environ.get("PATH", "")
    if not (shutil.which("ffmpeg", path=path) and shutil.which("ffprobe", path=path)):
        found = sorted(glob.glob(os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe")))
        if found:
            path = str(Path(found[-1]).parent) + os.pathsep + path
    return path


def environment() -> None:
    """Puts FFmpeg on this process's PATH, which every ffmpeg run inherits; exits with one line when ffmpeg or ffprobe
    is missing."""
    path = ffmpeg_path()
    if missing := [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t, path=path)]:
        sys.exit(f"{' and '.join(missing)} not found. Install FFmpeg (winget install Gyan.FFmpeg), then try again.")
    os.environ["PATH"] = path


def ffmpeg(args: list) -> None:
    if subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *map(str, args)]).returncode:
        sys.exit("ffmpeg failed (see above)")


def encoder(video: Path, vf: str) -> list:
    """NVENC when this PC's ffmpeg and GPU can encode this output (2 frames of the video through vf: NVENC refuses
    frames over 4096 px, like a 5K or super-ultrawide source), else libx264 at a quick preset."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        if not subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-an", "-frames:v", "2",
                               "-vf", vf + ",format=yuv420p", *NVENC, "-f", "null", "-"], capture_output=True,
                              timeout=60).returncode:
            return NVENC
    return X264


def clock(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def secs(v) -> float:
    """'1:02:03', '2:05', '90', '90s' or a number, in seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(str(v).strip().rstrip("s").split(":"))))


def probe(video: Path) -> tuple[float, float, bool]:
    """Length in seconds, frame rate, and whether there is sound; exits with one line when there is no video or no
    length (not a video file, or a broken one). ffprobe's own reason goes to the console as it is."""
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,"
                                      "avg_frame_rate", "-of", "json", str(video)],
                                     stdout=subprocess.PIPE, text=True).stdout or "{}")
    streams = info.get("streams", [])
    try:
        length = float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        length = 0.0
    if not (0 < length < float("inf")) or not any(s.get("codec_type") == "video" for s in streams):
        sys.exit(f"{video.name}: no video with a length found (is it a video file?)")
    num, _, den = next((s["avg_frame_rate"] for s in streams if s["codec_type"] == "video"), "30/1").partition("/")
    fps = float(num) / float(den) if float(den or 0) else 30.0
    return length, fps, any(s.get("codec_type") == "audio" for s in streams)


def both(frozen: list, quiet: list) -> list[list[float]]:
    """Where a frozen span and a silent one overlap, touching overlaps merged: the stale parts."""
    out, overlaps = [], ([max(f[0], q[0]), min(f[1], q[1])] for f in frozen for q in quiet)
    for a, b in sorted(o for o in overlaps if o[1] > o[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(b, out[-1][1])
        else:
            out.append([a, b])
    return out


def stale_spans(video: Path, length: float, audio: bool) -> list[list[float]]:
    """Every [start, end] (0.5 s or longer) where the picture is frozen and there is no sound, in one pass; exits
    when ffmpeg fails, so a failed analysis is never saved as 'no pauses'."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(video), "-map", "0:v:0",
           "-vf", f"fps=5,scale=480:-2,freezedetect=n={FREEZE_NOISE}:d=0.5"]
    if audio:
        cmd += ["-map", "0:a:0", "-af", f"silencedetect=n={SILENCE_DB}dB:d=0.5"]
    r = subprocess.run(cmd + ["-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        sys.exit("finding pauses failed: " + (r.stderr.strip().splitlines() or ["ffmpeg error"])[-1])
    log = r.stderr

    def spans(kind: str, bridge: float = 0.0) -> list[list[float]]:  # one still open at the end runs to the end
        starts, ends = (re.findall(rf"{kind}_{e}: (-?[\d.]+)", log) for e in ("start", "end"))
        out = []
        for a, b in zip_longest(starts, ends, fillvalue=length):
            if a == length:
                continue
            if out and float(a) - out[-1][1] < bridge:
                out[-1][1] = float(b)
            else:
                out.append([max(0.0, float(a)), float(b)])
        return out

    frozen = spans("freeze")  # never bridged: a gap between two still stretches is a change on screen
    return [[round(a, 2), round(b, 2)] for a, b in (both(frozen, spans("silence", CLICK)) if audio else frozen)]


def source_key(video: Path) -> dict:
    """What a saved pause analysis must match: this exact file, unchanged."""
    st = video.stat()
    return {"source": str(video), "size": st.st_size, "mtime": st.st_mtime}


def cached(file: Path, key: dict) -> list | None:
    """The stale spans saved for exactly this source (key), or None: missing, unreadable, or another file or version."""
    with contextlib.suppress(OSError, ValueError, AttributeError, KeyError):
        saved = json.loads(file.read_text(encoding="utf-8"))
        if {k: saved.get(k) for k in key} == key:
            return saved["spans"]
    return None


def project_dir(video: Path) -> Path:
    """workspace\\videos\\<stem>-<8 hex of the full path>: same-named videos from two folders never collide."""
    return OUT / f"{video.stem[:STEM]}-{hashlib.sha256(os.path.normcase(str(video)).encode()).hexdigest()[:8]}"


def checked(raw: dict, length: float) -> dict:
    """A plan with every value made safe: times inside the video, known transitions, sane numbers."""
    def num(key, default, low, high):
        try:
            return min(high, max(low, float(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    def spans(key, factor=False):
        out = []
        for item in raw.get(key) or []:
            try:
                a, b = sorted(min(length, max(0.0, secs(item[k]))) for k in ("start", "end"))
                f = min(8.0, max(0.5, float(item.get("factor", 1)))) if factor else None
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if b - a >= 0.1:
                out.append([a, b, f] if factor else [a, b])
        return out

    return {"remove_pauses": raw.get("remove_pauses") is not False, "pause_seconds": num("pause_seconds", PAUSE, 0.5, 60),
            "pad_seconds": num("pad_seconds", PAD, 0.0, 3.0),
            "transition": raw.get("transition") if raw.get("transition") in TRANSITIONS else TRANSITIONS[0],
            "transition_seconds": num("transition_seconds", TRANSITION_S, 0.1, 1.5),
            "cut": spans("cut"), "keep": spans("keep"), "speed": spans("speed", factor=True)}


def lines(text: str) -> list[str]:
    """Typed commands, one per line, comma or semicolon."""
    return [s.strip() for s in re.split(r"[\n;,]", text or "") if s.strip()]


def parse(commands: list, length: float) -> tuple[dict, list[str]]:
    """Commands (FORMS) to a checked plan, plus every line that is not one or, when length is known (not 0), whose
    span misses the video. Strict on purpose: a line it can't read is refused, never guessed at."""
    raw: dict = {"cut": [], "keep": [], "speed": []}
    odd = []
    fits = lambda v, low, high: bool(re.fullmatch(NUM, v)) and low <= secs(v) <= high  # noqa: E731  (FORMS' ranges)
    for line in map(str, commands):
        w = re.sub(r"\s*(?:[-\u2013\u2014]|\bto\b)\s*", "-", line.strip(" .").lower()).split() or [""]
        span, key = len(w) > 1 and re.fullmatch(f"({TIME})-({TIME})", w[1]), None
        if span and (len(w) == 2 and w[0] in ("cut", "keep") or len(w) == 3 and w[0] == "speed" and
                     re.fullmatch(r"\d+(?:\.\d+)?x?", w[2]) and 0.5 <= float(w[2].rstrip("x")) <= 8):
            key, a, b, f = w[0], secs(span[1]), secs(span[2]), float(w[2].rstrip("x")) if len(w) == 3 else 1.0
        elif len(w) == 3 and w[0] == "trim" and w[1] in ("start", "end") and re.fullmatch(TIME, w[2]):
            n = secs(w[2])  # the end is counted here: length - n
            key, a, b, f = "cut", *((0.0, n) if w[1] == "start" else (length - n, length)), 1.0
        elif len(w) == 2 and w[0] == "pauses" and w[1] in ("off", "none", "in"):
            raw["remove_pauses"] = False
        elif len(w) == 2 and (w[0] == "pauses" and fits(w[1], 0.5, 60) or w[0] == "pad" and fits(w[1], 0, 3)):
            raw["pause_seconds" if w[0] == "pauses" else "pad_seconds"] = secs(w[1])
        elif len(w) in (2, 3) and w[0] == "transition" and w[1] in TRANSITIONS and (len(w) == 2 or fits(w[2], 0.1, 1.5)):
            raw.update(transition=w[1], transition_seconds=secs(w[2]) if len(w) == 3 else TRANSITION_S)
        else:
            odd.append(line)
        if key:
            a, b = sorted((a, b))
            if length and min(b, length) - max(a, 0.0) < 0.1:
                odd.append(line)
            else:
                raw[key].append({"start": a, "end": b, "factor": f})
    return checked(raw, length), odd


def check(commands_text: str, length: float = 0) -> list[str]:
    """The lines HyperDesk must refuse before starting (length 0 = unknown: syntax only)."""
    return parse(lines(commands_text), length)[1]


def refused(bad: list[str], length: float = 0) -> str:
    where = f" (or outside this {clock(length)} video)" if length else ""
    return f"Not a command{where}: " + " | ".join(bad) + "\nUse these, one per line:\n" + FORMS


def timeline(length: float, plan: dict, stale: list) -> list[tuple[int, int, float]]:
    """The cut as runs (start, end, speed), in GRID frames of the source, in play order."""
    n = round(length * GRID)
    at = lambda s: min(n, max(0, round(s * GRID)))  # noqa: E731
    keep, speed = [True] * n, [1.0] * n
    pad = plan["pad_seconds"]
    pauses = [(a + pad, b - pad) for a, b in stale if b - a >= plan["pause_seconds"]] if plan["remove_pauses"] else []
    for a, b in pauses + [tuple(c) for c in plan["cut"]]:
        keep[at(a):at(b)] = [False] * (at(b) - at(a))  # an empty or backwards span changes nothing
    for a, b in plan["keep"]:
        keep[at(a):at(b)] = [True] * (at(b) - at(a))
    for a, b, f in plan["speed"]:
        speed[at(a):at(b)] = [f] * (at(b) - at(a))
    i = 0
    while i < n:  # slivers left between two cuts
        j = i
        while j < n and keep[j] == keep[i]:
            j += 1
        if keep[i] and j - i < MIN_KEEP * GRID:
            keep[i:j] = [False] * (j - i)
        i = j
    runs = []
    for i in range(n):
        if keep[i]:
            if runs and runs[-1][1] == i and runs[-1][2] == speed[i]:
                runs[-1][1] = i + 1
            else:
                runs.append([i, i + 1, speed[i]])
    return [(s, e, f) for s, e, f in runs if round((e - s) / f)]


def joins(runs: list, t: int) -> list[bool]:
    """Which joins get a transition: real cuts (a gap in the source) between two runs that can each give it `t`
    frames from both ends. Joins where only the speed changes, and runs too short, stay hard cuts."""
    lens = [round((e - s) / f) for s, e, f in runs]
    return [bool(t) and runs[k][1] != runs[k + 1][0] and min(lens[k], lens[k + 1]) >= 2 * t + 2
            for k in range(len(runs) - 1)]


def render(video: Path, runs: list, fade: tuple[str, int], vf: str, fps: int, audio: bool, out: Path, enc: list):
    """Encode each run and each transition (fade = xfade name, GRID frames) on its own with `enc`, then join them.
    Parts carry PCM sound, so the one AAC encode at the end leaves no gaps at the joins."""
    name, t = fade
    soft = joins(runs, t)
    parts = out.parent / "parts"  # one name, short paths: outputs render one at a time
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir()

    def chain(i: int, f: float) -> str:  # input i played at speed f, framed by vf, as [v<i>] (and [a<i>])
        g = f"[{i}:v:0]setpts=PTS/{f}," + vf.replace("[", f"[p{i}") + f",setsar=1,fps={fps},format=yuv420p[v{i}]"
        return g + (f";[{i}:a:0]atempo={f},aresample=48000[a{i}]" if audio else "")

    def take(start: float, frames: int, f: float) -> list:  # enough source for `frames` output frames, plus a margin
        return ["-ss", f"{start / GRID:.4f}", "-t", f"{frames * f / GRID + 0.2:.4f}", "-i", video]

    todo = []  # (inputs, filter graph, video label, audio label, output frames)
    for k, (s, e, f) in enumerate(runs):
        head, tail = (t if k and soft[k - 1] else 0), (t if k < len(soft) and soft[k] else 0)
        n = round((e - s) / f) - head - tail
        todo.append((take(s + head * f, n, f), chain(0, f), "[v0]", "[a0]", n))
        if tail:  # the last t frames of this run into the first t of the next
            s2, _, f2 = runs[k + 1]
            d = f"{t / GRID:.4f}"
            g = f"{chain(0, f)};{chain(1, f2)};[v0][v1]xfade=transition={name}:duration={d}:offset=0[v]"
            g += f";[a0]atrim=duration={d}[x];[x][a1]acrossfade=d={d}[a]" if audio else ""
            todo.append((take(e - t * f, t, f) + take(s2, t, f2), g, "[v]", "[a]", t))
    for i, (inputs, graph, v, a, n) in enumerate(todo):
        print(f"   {out.name}: part {i + 1}/{len(todo)}", flush=True)
        ffmpeg(["-y", *inputs, "-filter_complex", graph, "-map", v,
                *(["-map", a, "-c:a", "pcm_s16le", "-ac", "2"] if audio else []),
                "-t", f"{n / GRID:.4f}", *enc, parts / f"{i:04d}.mov"])
    (parts / "list.txt").write_text("".join(f"file '{i:04d}.mov'\n" for i in range(len(todo))), encoding="utf-8")
    ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", parts / "list.txt", "-c:v", "copy",
            *(["-c:a", "aac", "-b:a", "192k"] if audio else []), "-movflags", "+faststart", out])
    shutil.rmtree(parts, ignore_errors=True)  # the pieces this function made a moment ago


def outputs(video: Path, project: Path, target: str, frame: str, fps: float) -> list[tuple[Path, str, int]]:
    """(file, frame filter, fps) for each file asked for."""
    made, stem = [], video.stem[:STEM]
    if target in ("youtube", "both"):
        made.append((project / f"{stem}_youtube.mp4", "scale=trunc(iw/2)*2:trunc(ih/2)*2", 60 if fps > 31 else 30))
    if target in ("social", "both"):
        made.append((project / f"{stem}_vertical.mp4", FRAMES[frame], 30))
    return made


def clean(video: Path, project: Path, args) -> list[str]:
    """The cut: everything but the stale parts, plus whatever the commands say."""
    length, fps, audio = probe(video)
    plan, bad = parse(lines(args.commands), length)
    if bad:
        sys.exit(refused(bad, length))
    project.mkdir(parents=True, exist_ok=True)
    print(f"Video:   {video} ({clock(length)})\nProject: {project}\n   The plan:", flush=True)
    for key in ("cut", "keep", "speed"):
        for span in plan[key]:
            print(f"   {key:5} {clock(span[0])}-{clock(span[1])}" + (f" at {span[2]}x" if key == "speed" else ""))
    pauses = f"cut when {plan['pause_seconds']} s or longer, {plan['pad_seconds']} s left at each end"
    fade = plan["transition"] + (f" {plan['transition_seconds']} s" if plan["transition"] != "none" else "")
    print(f"   pauses: {pauses if plan['remove_pauses'] else 'kept'} | transition: {fade}", flush=True)

    stale_file, key = project / "stale.json", source_key(video)
    if args.fresh:
        stale_file.unlink(missing_ok=True)
    stale = cached(stale_file, key)
    if stale is None:  # none yet, or saved for another file or an older version of this one
        print("\n=== finding pauses (frozen picture, no sound) ===", flush=True)
        stale = stale_spans(video, length, audio)
        stale_file.write_text(json.dumps({**key, "spans": stale}), encoding="utf-8")
    runs = timeline(length, plan, stale)
    if not runs:
        sys.exit("Nothing left after the cuts: check the commands")
    t = 0 if plan["transition"] == "none" else round(plan["transition_seconds"] * GRID)
    soft = joins(runs, t)
    after = (sum(round((e - s) / f) for s, e, f in runs) - t * sum(soft)) / GRID
    cuts = sum(runs[k][1] != runs[k + 1][0] for k in range(len(runs) - 1)) + (runs[0][0] > 0) + \
        (runs[-1][1] < round(length * GRID))
    print(f"   {cuts} cuts: {clock(length)} -> {clock(after)}", flush=True)
    (project / "edit_plan.json").write_text(json.dumps({
        "commands": args.commands, "plan": plan, "stale": stale, "cuts": cuts, "before": length, "after": after,
        "runs": [[s / GRID, e / GRID, f] for s, e, f in runs]}, indent=1), encoding="utf-8")

    made = []
    for out, vf, rate in outputs(video, project, args.target, args.frame, fps):
        enc = encoder(video, vf)
        print(f"\n=== {out.name} ===\n   encoder: {'NVENC (GPU)' if enc is NVENC else 'libx264 (CPU)'}", flush=True)
        render(video, runs, (plan["transition"], t), vf, rate, audio, out, enc)
        made.append(str(out))
    return made


def main() -> None:
    p = argparse.ArgumentParser(description="Quick edit: cut the pauses out of a video, plus typed commands (no AI)",
                                epilog="Commands:\n" + FORMS, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--for", dest="target", choices=["youtube", "social", "both"], default="youtube",
                   help="youtube = landscape at the source size (default); social = 1080x1920 vertical; both")
    p.add_argument("--frame", choices=list(FRAMES), default="fit",
                   help="vertical: fit = whole frame over a blurred fill (screen recordings); crop = fill the screen")
    p.add_argument("--commands", default="", help='e.g. "trim start 5, cut 2:10-2:40, transition none"')
    p.add_argument("--fresh", action="store_true", help="find the pauses again even if this video was analyzed before")
    args = p.parse_args()
    video = args.video.resolve()
    if not video.is_file():
        sys.exit(f"No such video: {video}")
    if bad := check(args.commands):  # before FFmpeg is even looked for
        sys.exit(refused(bad))
    environment()
    made = clean(video, project_dir(video), args)
    print("\nDone.\n" + "\n".join(made), flush=True)


def demo() -> None:
    """Self-check of secs(), checked(), parse(), check(), both(), timeline(), joins() and the pause-cache key."""
    assert secs("1:02:03") == 3723 and secs("2:05") == 125 and secs("90s") == 90 and secs(7) == 7
    plan = checked({"cut": [{"start": "1:00", "end": "0:50"}, {"start": "x", "end": "1"}, {"start": 5}],
                    "speed": [{"start": "0:10", "end": "0:20", "factor": 99}], "transition": "explode",
                    "pause_seconds": "abc", "transition_seconds": 9, "remove_pauses": "no"}, 100)
    assert plan["cut"] == [[50.0, 60.0]] and plan["speed"] == [[10.0, 20.0, 8.0]], plan  # junk dropped, clamped
    assert plan["transition"] == "fade" and plan["pause_seconds"] == PAUSE and plan["transition_seconds"] == 1.5
    assert plan["remove_pauses"] is True  # only a real false turns pause removal off

    plan, odd = parse(["cut 0:00 - 0:10", "speed 1:00 to 2:30 3x", "pauses off", "pad 1", "transition slideleft 0.6",
                       "keep 4:50\u20135:30.", "trim end 5", "transition explode", "watch", "make it pop",
                       "trim end soon", "speed 0:10-0:20", "speed 0:10-0:20 20", "cut 9:00-9:30", "pad abc",
                       "cut 1:00-2:00 3", "cut 1:75-2:00", "pad 4", "pauses 0", "transition fade 5"], 300)
    assert plan["cut"] == [[0.0, 10.0], [295.0, 300.0]] and plan["speed"] == [[60.0, 150.0, 3.0]], plan
    assert plan["keep"] == [[290.0, 300.0]], plan  # en dash and a full stop are fine; clamped to the video
    assert not plan["remove_pauses"] and plan["pad_seconds"] == 1 and plan["transition"] == "slideleft", plan
    assert plan["transition_seconds"] == 0.6, plan
    assert odd == ["transition explode", "watch", "make it pop", "trim end soon", "speed 0:10-0:20",
                   "speed 0:10-0:20 20", "cut 9:00-9:30", "pad abc", "cut 1:00-2:00 3", "cut 1:75-2:00", "pad 4",
                   "pauses 0", "transition fade 5"], odd  # out of range is refused too, never clamped
    assert check("cut 75:00-1:02:03.5s, pauses 60, pad 0, transition fade 1.5") == []
    assert lines(" a ;b,\n\n c ") == ["a", "b", "c"] and check("") == []
    assert check("trim start 5\ncut 1:00-1:30, transition none; pauses 3.") == []
    assert check("cut 9:00-9:30\nhello") == ["hello"]  # length unknown: syntax only
    assert check("cut 9:00-9:30", 300) == ["cut 9:00-9:30"] and "trim start" in refused(["x"])

    assert both([[0, 5], [10, 20]], [[3, 12], [12, 15], [30, 40]]) == [[3, 5], [10, 15]]
    assert both([[0, 5]], []) == [] and both([[0, 5]], [[5, 9]]) == []  # touching is not overlapping

    base = checked({}, 60)
    stale = [[10, 13], [30, 31], [40, 40.9]]  # a 3 s pause, then two too short to cut
    assert timeline(60, base, stale) == [(0, 312, 1.0), (378, 1800, 1.0)]  # 10.4-12.6 s: the pause less PAD each end
    assert timeline(60, {**base, "remove_pauses": False}, stale) == [(0, 1800, 1.0)]
    runs = timeline(60, {**base, "cut": [[0, 5], [20, 20.3]], "keep": [[11, 12]], "speed": [[50, 60, 2.0]]}, stale)
    assert runs == [(150, 312, 1.0), (330, 360, 1.0), (378, 600, 1.0), (609, 1500, 1.0), (1500, 1800, 2.0)], runs
    assert joins(runs, 12) == [True, True, True, False]  # the speed change at 50 s is no cut
    assert joins(runs, 15) == [False, False, True, False]  # the 1 s kept span can't give 0.5 s to both sides
    assert timeline(60, {**base, "cut": [[0, 30], [30.3, 60]]}, []) == []  # a 0.3 s sliver is not kept

    with tempfile.TemporaryDirectory() as tmp:
        v, f = Path(tmp) / "a.mp4", Path(tmp) / "stale.json"
        v.write_bytes(b"x")
        key = source_key(v)
        assert cached(f, key) is None  # nothing saved yet
        f.write_text(json.dumps({**key, "spans": [[1, 2]]}), encoding="utf-8")
        assert cached(f, key) == [[1, 2]] and cached(f, {**key, "source": str(Path(tmp) / "b.mp4")}) is None
        v.write_bytes(b"xy")
        assert cached(f, source_key(v)) is None  # the source changed: analyze again
        f.write_text("[[1, 2]]", encoding="utf-8")  # an older stale.json with no key
        assert cached(f, key) is None
        one, two = project_dir(Path(tmp) / "x" / "a.mp4"), project_dir(Path(tmp) / "y" / "a.mp4")
        assert one != two and one.name.startswith("a-") and one.parent == OUT, (one, two)
        assert len(project_dir(Path(tmp) / ("n" * 99 + ".mp4")).name) == STEM + 9  # long names are shortened
    print("self-check ok")


if __name__ == "__main__":
    demo() if sys.argv[1:] == ["--demo"] else main()
