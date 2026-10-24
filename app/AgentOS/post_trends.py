"""Post trends: which hashtags, weekdays and times of day beat your usual post. Export your own posts as CSV (TikTok
Studio, YouTube Studio's "Table data.csv", Meta Business Suite) and get one self-contained HTML page. No network, no
model, no sign-in: the file is read and nothing else.

Each post is judged against its own account in its own weeks: ratio = its views / the median views of the 15 nearest
posts on the same account within 45 days (at least 5 of them, else no ratio), so a big and a small account pool fairly
and growth or a slump doesn't make old or new posts look good or bad. Posts from the file's last 3 days are too new:
labelled, given no ratio and left out of every baseline.

    python post_trends.py export.csv      (writes post-trends.html next to it; --demo runs the self-check)
"""
import bisect
import csv
import datetime as dt
import heapq
import io
import re
import statistics
import sys
from html import escape
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
# header aliases, matched whole (trimmed, any case), best first
CAPTION = ("title", "caption", "description", "video title", "content", "post caption")
DATE = ("publish time", "video publish time", "post time", "date posted", "create time", "publish date", "upload date",
        "posted", "published", "date", "time")
VIEWS = ("views", "video views", "total views", "plays", "total plays", "impressions")
ACCOUNT = ("account", "account name", "account username", "username", "page", "page name")
GENERIC = {"fyp", "foryou", "foryoupage", "fy", "viral", "trending", "reels", "explore", "shorts"}  # on everything: say nothing
NEAREST, MIN_NEIGHBORS, WINDOW, NEW = 15, 5, dt.timedelta(days=45), dt.timedelta(days=3)
FEW = 3  # a hashtag, day or time slot on fewer judged posts than this is shown faded: too few to trust
TOP, TAGS = 10, 30
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SLOTS = [(5, "Morning 5-11"), (11, "Midday 11-3"), (15, "Afternoon 3-7"), (19, "Evening 7-11"), (23, "Late night 11-5")]
TAG = re.compile(r"#(\w+)")
# ponytail: slashed dates are read month first (US, what these studios export in English); a day-first file reads its
# days 1-12 swapped and skips the rest as unreadable (counted in info["skipped"]). Upgrade: read the whole file day
# first when any first field is over 12.
FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y",
           "%b %d, %Y", "%B %d, %Y")


def slot(hour):
    return next((name for start, name in reversed(SLOTS) if hour >= start), SLOTS[-1][1])  # 0-4 h wraps to late night


def pick(header, aliases):
    """Indexes of the alias columns present in header, best alias first. Whole names only: 'Average views' is not 'views'."""
    names = [h.strip().lower() for h in header]
    return [names.index(a) for a in aliases if a in names]


def when(text):
    """(datetime, has a time of day) or None. ISO (a time zone becomes this PC's clock), m/d/Y [H:M[:S] [AM/PM]],
    'Mon D, YYYY'."""
    s = text.strip()
    for f in (None, *FORMATS):
        try:
            d = dt.datetime.fromisoformat(s) if f is None else dt.datetime.strptime(s, f)
        except ValueError:
            continue
        if not 2000 <= d.year < 2100:  # year 1 or 9999 would overflow the 45-day window, pre-1970 breaks astimezone on Windows
            return None
        return (d.astimezone().replace(tzinfo=None) if d.tzinfo else d), ":" in s
    return None


def count(text):
    """'12,345' / '12 345' / '12345.0' -> 12345; anything else (blank, '-', negative) -> None."""
    try:
        v = float(re.sub(r"[,\s]", "", text))
    except ValueError:
        return None
    return int(v) if 0 <= v < 1e15 else None  # also drops nan and inf


def load(text):
    """CSV text -> (rows, info). A row: {caption, tags, when, timed, views, account}. info names the columns read
    (captions: every caption column, the first non-empty one per row is shown, hashtags come from all) and counts the
    rows skipped for an unreadable date or view count. Bad input: ValueError with a short message for a person."""
    if len(text) > MAX_BYTES:  # characters, close enough to bytes for a CSV
        raise ValueError("That file is over 5 MB. Export a shorter date range.")
    try:
        table = list(csv.reader(io.StringIO(text.lstrip("\N{BYTE ORDER MARK}"))))
    except csv.Error as e:
        raise ValueError(f"That is not a readable CSV file ({e}).") from None
    header = table[0] if table else []
    date, views = pick(header, DATE)[:1], pick(header, VIEWS)[:1]
    if not (date and views):
        raise ValueError("No date and views columns found. Export your posts as CSV from TikTok Studio, YouTube Studio "
                         "or Meta Business Suite: the first row needs headers like 'Publish time' and 'Views'.")
    caps, acct, width = pick(header, CAPTION), pick(header, ACCOUNT)[:1], len(header)
    body = table[1:]
    if body and "total" in {c.strip().lower() for c in body[0]}:  # YouTube's first line sums all the others
        body = body[1:]
    rows, skipped = [], 0
    for r in body:
        r = r + [""] * (width - len(r))
        w, v = when(r[date[0]]), count(r[views[0]])
        if w is None or v is None:
            skipped += any(c.strip() for c in r)  # blank lines are not worth a mention
            continue
        texts = [r[i].strip() for i in caps if r[i].strip()]
        rows.append({"caption": texts[0] if texts else "", "tags": sorted({t.lower() for t in TAG.findall(" ".join(texts))} - GENERIC),
                     "when": w[0], "timed": w[1], "views": v, "account": r[acct[0]].strip() if acct else ""})
    name = lambda i: header[i].strip()[:60]
    if not rows:
        raise ValueError(f"No row had a readable date in '{name(date[0])}' and a view count in '{name(views[0])}'.")
    return rows, {"caption": [name(i) for i in caps], "date": name(date[0]), "views": name(views[0]),
                  "account": name(acct[0]) if acct else None, "rows": len(rows), "skipped": skipped}


def report(rows, info=None):
    """rows (from load) -> plain dict: counts, notes, tables hashtags/days/slots ([label, posts, median ratio, too few]),
    top (the 10 best posts by ratio) and new (the too-new posts, newest first). info (from load) rides along."""
    if not rows:
        raise ValueError("No posts to read.")
    newest = max(p["when"] for p in rows)
    posts = sorted((dict(p, new=newest - p["when"] < NEW, ratio=None) for p in rows), key=lambda p: p["when"])
    for name in {p["account"] for p in posts}:
        pool = [p for p in posts if p["account"] == name and not p["new"]]  # in time order
        ts = [p["when"] for p in pool]
        for k, p in enumerate(pool):  # the 15 nearest sit within 15 places of p
            a, b = bisect.bisect_left(ts, p["when"] - WINDOW), bisect.bisect_right(ts, p["when"] + WINDOW)
            near = heapq.nsmallest(NEAREST, (q for q in pool[max(a, k - NEAREST):min(b, k + NEAREST + 1)] if q is not p),
                                   key=lambda q: abs(q["when"] - p["when"]))
            if len(near) >= MIN_NEIGHBORS and (base := statistics.median(q["views"] for q in near)):
                p["ratio"] = p["views"] / base
    rated = [p for p in posts if p["ratio"] is not None]

    def table(pairs):  # (label, ratio) pairs -> [label, posts, median ratio, too few]
        g = {}
        for k, x in pairs:
            g.setdefault(k, []).append(x)
        return [[k, len(v), statistics.median(v), len(v) < FEW] for k, v in g.items()]

    def card(p):
        return {"caption": p["caption"], "account": p["account"], "views": p["views"], "ratio": p["ratio"],
                "date": p["when"].strftime("%Y-%m-%d %H:%M" if p["timed"] else "%Y-%m-%d")}

    names = [n for _, n in SLOTS]
    tags = sorted(table(("#" + t, p["ratio"]) for p in rated for t in p["tags"]), key=lambda r: (not r[3], r[2]), reverse=True)
    fresh, notes = [p for p in posts if p["new"]], []
    if not any(p["caption"] for p in posts):
        notes.append("This file has no captions or titles, so there is no hashtag table.")
    elif not tags:
        notes.append("None of the judged posts has a hashtag (generic ones like #fyp are left out).")
    if not any(p["timed"] for p in posts):
        notes.append("This file has dates but no times of day, so there is no time-of-day table.")
    if fresh:
        notes.append(f"{len(fresh)} post(s) from the file's last 3 days are too new to judge: listed at the end, left out of every baseline.")
    if short := len(posts) - len(fresh) - len(rated):
        notes.append(f"{short} post(s) had fewer than {MIN_NEIGHBORS} other posts on the same account within 45 days, or a usual of 0 views, so no ratio.")
    return {"posts": len(posts), "rated": len(rated), "accounts": sorted({p["account"] for p in posts} - {""}),
            "first": posts[0]["when"].date().isoformat(), "last": newest.date().isoformat(), "notes": notes,
            "times": any(p["timed"] for p in posts),
            "tags": tags[:TAGS], "days": sorted(table((DAYS[p["when"].weekday()], p["ratio"]) for p in rated), key=lambda r: DAYS.index(r[0])),
            "slots": sorted(table((slot(p["when"].hour), p["ratio"]) for p in rated if p["timed"]), key=lambda r: names.index(r[0])),
            "top": [card(p) for p in sorted(rated, key=lambda p: p["ratio"], reverse=True)[:TOP]],
            "new": [card(p) for p in reversed(fresh)], "info": info or {}}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Post trends</title>
<script>if (parent !== window) document.documentElement.classList.add('embedded');  // inside HyperDesk: its colors</script>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#6f6d68;--grid:#e1e0d9;--series:#2a78d6;--ring:rgba(11,11,11,.10)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#9a9891;--grid:#2c2c2a;--series:#3987e5;--ring:rgba(255,255,255,.10)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#9a9891;--grid:#2c2c2a;--series:#3987e5;--ring:rgba(255,255,255,.10)}
:root.embedded{color-scheme:dark;--page:transparent;--surface:#0a0a0a;--ink:#e0e0e0;--ink2:#bdbdbd;--muted:#9a9a9a;--grid:#222;--series:#39ff14;--ring:#222}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:8px 16px 48px}h1{font-size:22px;margin:0}h2{font-size:17px;margin:28px 0 10px}
h3{font-size:14px;margin:0 0 6px;color:var(--ink2);font-weight:600}.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--surface);border-radius:10px;box-shadow:0 0 0 1px var(--ring);padding:16px}.scroll{overflow-x:auto}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr))}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600;font-size:13px}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.barcell{width:30%;min-width:40px;vertical-align:middle}td:first-child{overflow-wrap:anywhere}.bar{display:block;height:8px;border-radius:4px;background:var(--series);opacity:.75}
tr.few td{color:var(--muted)}tr.few .bar{opacity:.25}.clip{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul{margin:8px 0 0;padding-left:18px}li{margin:3px 0}
</style></head><body><main>
"""
FIT = ("<script>if (parent !== window) { const fit = () => parent.postMessage({sdaHeight: Math.ceil(document.body.getBoundingClientRect().height)}, '*'); "
       "fit(); addEventListener('load', fit); addEventListener('resize', fit); }</script>")  # HyperDesk sizes its frame from this


def html(r):
    """The report as one self-contained page (no links, files or network), light or dark with the system and in
    HyperDesk's colors inside its sandboxed frame. Everything from the file goes through escape."""
    e = lambda x: escape(str(x))
    x = lambda v: "too new" if v is None else f"{float(v):.2f}x"

    def grid(title, label, rows, empty):
        if not rows:
            return f'<div class="card"><h3>{e(title)}</h3><p class="muted small">{e(empty)}</p></div>'
        most = max(row[2] for row in rows) or 1
        body = "".join(f'<tr{" class=few" if few else ""}><td>{e(k)}</td><td class="num">{int(n)}</td><td class="num">{float(m):.2f}x</td>'
                       f'<td class="barcell"><span class="bar" style="width:{100 * float(m) / most:.0f}%"></span></td></tr>' for k, n, m, few in rows)
        return (f'<div class="card"><h3>{e(title)}</h3><div class="scroll"><table><tr><th>{e(label)}</th><th class="num">Posts</th>'
                f'<th class="num">Typical</th><th></th></tr>{body}</table></div></div>')

    def posts(title, rows, empty):
        if not rows:
            return f'<h2>{e(title)}</h2><p class="muted small">{e(empty)}</p>'
        multi = len(r["accounts"]) > 1
        body = "".join(f'<tr><td class="clip" title="{e(p["caption"])}">{e(p["caption"] or "(no caption)")}</td>'
                       + (f'<td>{e(p["account"])}</td>' if multi else "") + f'<td class="num">{e(p["date"])}</td><td class="num">{int(p["views"]):,}</td>'
                       f'<td class="num">{x(p["ratio"])}</td></tr>' for p in rows)
        return (f'<h2>{e(title)}</h2><div class="card scroll"><table><tr><th>Post</th>{"<th>Account</th>" if multi else ""}<th class="num">Date</th>'
                f'<th class="num">Views</th><th class="num">vs usual</th></tr>{body}</table></div>')

    i, accts = r.get("info") or {}, r["accounts"]
    read = []
    if i:
        read.append(f"Views from '{i['views']}', dates from '{i['date']}'")
        read.append("captions from " + ", ".join(f"'{c}'" for c in i["caption"]) if i["caption"] else "no caption column")
        if i.get("account"):
            read.append(f"accounts from '{i['account']}'")
        if i.get("skipped"):
            read.append(f"{i['skipped']} row(s) skipped: no readable date or view count")
    notes = "".join(f"<li>{e(n)}</li>" for n in r["notes"])
    return (PAGE + f'<h1>Post trends</h1><p class="muted small">{int(r["posts"])} posts from {e(r["first"])} to {e(r["last"])}'
            + (f' on {len(accts)} accounts: {e(", ".join(accts))}' if len(accts) > 1 else "") + f'; {int(r["rated"])} judged.'
            + (f' {e("; ".join(read))}.' if read else "") + '</p>'
            '<p class="small">"Typical" is the median of each post\'s views divided by its usual: the median views of the 15 nearest '
            'posts on the same account within 45 days. 1.00x is an ordinary post, 2.00x twice the usual views.</p>'
            + (f'<div class="card small"><ul>{notes}</ul></div>' if notes else "")
            + '<h2>What beats your usual</h2><div class="grid">'
            + grid("Hashtags", "Hashtag", r["tags"], "No hashtags to compare.")
            + grid("Day of week", "Day", r["days"], "Not enough posts to judge yet.")
            + grid("Time of day", "Time", r["slots"], "Not enough posts to judge yet." if r["times"] else "No times of day in this file.")
            + "</div>" + posts(f"Top {TOP} posts", r["top"], "No post could be judged yet: each needs 5 others within 45 days.")
            + posts("Too new to judge", r["new"], "None.") + "</main>" + FIT + "</body></html>")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python post_trends.py export.csv")
    src = Path(sys.argv[1])
    try:
        if src.stat().st_size > MAX_BYTES:
            raise ValueError("That file is over 5 MB. Export a shorter date range.")
        rows, info = load(src.read_text(encoding="utf-8-sig", errors="replace"))
        out = src.with_name("post-trends.html")
        out.write_text(html(report(rows, info)), encoding="utf-8")
    except (OSError, ValueError) as e:
        raise SystemExit(str(e)) from None
    print(out)


def demo():
    """Self-check over a generated export: a Total row, decoy and lower-priority columns, thousands separators, ISO and
    m/d/Y times, two accounts of very different size, a too-new viral post, then a date-only variant and bad input."""
    day0, lines = dt.datetime(2026, 1, 5), ["Content,Video title,Description,Date,Publish time,Page name,Average views,Impressions,Views",
                                            "Total,,,,,,,,1234567", "x,broken row,,,someday,A,,,10"]
    for i in range(40):
        d = day0 + dt.timedelta(days=i)
        if d.weekday() == 4:  # account A's Friday evening posts do 4x its usual; B never posts on Fridays
            text = "<script>alert(1)</script> friday" if i == 4 else ""  # Description wins over Video title when set
            views = "1,000,000" if i == 39 else "4,000"  # the newest post goes viral, but it is too new to count
            lines.append(f"a{i},Friday special #Cats #FYP,{text},Lifetime,{d:%Y-%m-%d}T20:00:00,A,7,50,\"{views}\"")
        else:
            lines.append(f"a{i},Daily post #dogs{' #rare' if i in (1, 2) else ''},,Lifetime,{d:%Y-%m-%d}T09:00:00,A,7,50,1000")
            lines.append(f"b{i},,Update #dogs,Lifetime,{d:%m/%d/%Y} 14:00,B,7,50,\"{'100,000' if i % 2 else '100 000'}\"")
    rows, info = load("\N{BYTE ORDER MARK}" + "\n".join(lines))
    assert info == {"caption": ["Description", "Video title", "Content"], "date": "Publish time", "views": "Views",
                    "account": "Page name", "rows": 74, "skipped": 1}, info
    assert rows[1]["when"] == dt.datetime(2026, 1, 5, 14) and rows[1]["views"] == 100_000 and rows[1]["caption"] == "Update #dogs"
    r = report(rows, info)
    assert r["posts"] == 74 and r["accounts"] == ["A", "B"] and len(r["new"]) == 5, r
    assert r["new"][0]["views"] == 1_000_000 and r["new"][0]["ratio"] is None and r["top"][0]["views"] == 4000
    assert [round(x["ratio"], 2) for x in r["top"][:5]] == [4.0] * 5 and r["top"][5]["ratio"] == 1.0  # B's 100k is B's usual
    assert r["tags"][0][:3] == ["#cats", 5, 4.0] and "#fyp" not in {t[0] for t in r["tags"]} and r["tags"][-1][::3] == ["#rare", True]
    assert [d[0] for d in r["days"]] == DAYS and r["days"][4][1:3] == [5, 4.0] and {d[2] for d in r["days"][:4]} == {1.0}
    assert r["slots"] == [["Morning 5-11", 32, 1.0, False], ["Midday 11-3", 32, 1.0, False], ["Evening 7-11", 5, 4.0, False]], r["slots"]
    page = html(r)
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; friday" in page and page.isascii()
    assert "<title>Post trends</title>" in page and "sdaHeight" in page and "1,000,000" in page and "too new" in page

    yt = ["Content,Video title,Video publish time,Views", "Total,,,\"98,765\""]  # YouTube Studio: date only
    yt += [f'v{i},Clip {i},"{d:%b} {d.day}, {d.year}",{500 + i}' for i in range(30) if (d := day0 + dt.timedelta(days=i))]
    r = report(*load("\n".join(yt)))
    assert r["posts"] == 30 and not r["slots"] and not r["tags"] and r["rated"] == 27 and r["top"][0]["date"] == "2026-01-31"
    assert any("no times of day" in n for n in r["notes"]) and any("has a hashtag" in n for n in r["notes"])
    assert "No times of day in this file." in html(r) and "Clip 26" in html(r)

    def spaced(n, gap=1):  # n posts gap days apart: the last 3 days are too new and never anyone's neighbour
        return [{"caption": "", "tags": [], "when": day0 + dt.timedelta(days=gap * k), "timed": False, "views": 100, "account": ""} for k in range(n)]
    assert [report(spaced(n))["rated"] for n in (8, 9)] == [0, 6] and report(spaced(9, 46))["rated"] == 0
    assert "no captions" in report(spaced(9))["notes"][0]
    for bad in ("Name,Score\nx,1", "Publish time,Views\nsoon,12\n2026-01-01,", "x" * (MAX_BYTES + 1), "Date,Views\n\"x\0"):
        try:
            load(bad)
        except ValueError as e:
            assert str(e) and len(str(e)) < 250, e
        else:
            raise AssertionError(bad[:40])
    print("self-check ok")


if __name__ == "__main__":
    demo() if sys.argv[1:] == ["--demo"] else main()
