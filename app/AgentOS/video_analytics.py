"""Read-only video performance views over SilvaDes' existing SQLite snapshots.

No sync, credentials, model calls or database migrations belong here. Missing
metrics stay None; cumulative totals are never presented as period gains.
"""
import datetime as dt
import math
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


WINDOWS = (7, 28, 90)
MAX_VIDEOS = 300
PLATFORMS = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
DAILY_METRICS = {"youtube": "views", "instagram": "views", "tiktok": "views_total"}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _sum(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _change(current, previous):
    return (current - previous) / previous * 100 if current is not None and previous else None


def _url(value):
    value = str(value or "")[:2000]
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def _empty(days, message):
    return {"available": False, "message": message, "days": days,
            "start": None, "end": None, "previous_start": None, "previous_end": None,
            "latest_snapshot": None, "platforms": [], "trend": [], "videos": [],
            "video_count": 0, "truncated": False,
            "summary": {"views": None, "previous_views": None, "change_pct": None,
                        "comparison_available": False,
                        "views_label": "Channel views in selected period", "reporting_platforms": [],
                        "likes": None, "comments": None, "shares": None, "posts": None,
                        "engagement_rate": None, "lifetime_views": None}, "coverage": []}


def _snapshot_gain(points, start, end):
    """Exact boundaries only: skipped days must not masquerade as known gains."""
    first, last = points.get(start - dt.timedelta(days=1)), points.get(end)
    return last - first if first is not None and last is not None and last >= first else None


def _channels(db, start, end, previous_start, previous_end):
    metrics = {}
    for date, platform, account, metric, value in db.execute(
            "SELECT date, platform, account, metric, value FROM account_day "
            "WHERE platform IN ('youtube','instagram','tiktok') AND date>=? AND date<=?",
            (str(previous_start - dt.timedelta(days=1)), str(end))):
        day, value = _date(date), _number(value)
        if day and value is not None:
            metrics.setdefault((platform, account), {}).setdefault(metric, {})[day] = value
    channels = []
    for (platform, account), account_metrics in sorted(metrics.items()):
        primary = account_metrics.get(DAILY_METRICS[platform], {})
        current = {d: v for d, v in primary.items() if start <= d <= end}
        previous = {d: v for d, v in primary.items() if previous_start <= d <= previous_end}
        notes = []
        if platform == "tiktok":
            views = _snapshot_gain(primary, start, end)
            previous_views = _snapshot_gain(primary, previous_start, previous_end)
            comparison_available = views is not None and previous_views is not None
            trend = [{"date": str(d), "views": primary[d] - primary[d - dt.timedelta(days=1)]}
                     for d in sorted(current) if d - dt.timedelta(days=1) in primary
                     and primary[d] >= primary[d - dt.timedelta(days=1)]]
            coverage_days = len(trend)
            notes.append("TikTok views are changes between saved totals; period gains need both boundary snapshots.")
        else:
            views = _sum(current.values())
            # Sparse daily API data can omit zero days. Expose observed days and
            # require history reaching the previous window before comparing it.
            first = db.execute("SELECT min(date) FROM account_day WHERE platform=? AND account=? AND metric=?",
                               (platform, account, DAILY_METRICS[platform])).fetchone()[0]
            previous_views = _sum(previous.values()) if _date(first) and _date(first) <= previous_start else None
            comparison_available = len(current) == len(previous) == (end - start).days + 1
            trend = [{"date": str(d), "views": v} for d, v in sorted(current.items())]
            coverage_days = len(current)
            if platform == "instagram":
                notes.append("Instagram channel views include all account content, including feed posts.")
        latest = max(primary) if primary else None
        if coverage_days < (end - start).days + 1:
            notes.append(f"{coverage_days} observed days in this window; missing days are not filled with zero.")
        if latest and latest < end:
            notes.append(f"Latest channel data: {latest}; platform reporting can lag.")
        if not comparison_available:
            notes.append("Period comparison is unavailable until both windows have complete coverage.")
        def period(metric):
            return _sum(v for d, v in account_metrics.get(metric, {}).items() if start <= d <= end)
        watch_minutes = period("watch_minutes")
        watch_points = account_metrics.get("watch_minutes", {})
        paired_dates = set(current) & set(watch_points)
        paired_views = sum(current[d] for d in paired_dates)
        paired_watch_minutes = sum(watch_points[d] for d in paired_dates)
        if paired_dates and (len(paired_dates) != len(current) or
                             len(paired_dates) != sum(start <= d <= end for d in watch_points)):
            notes.append(f"Average watch duration uses {len(paired_dates)} days with both views and watch time.")
        gained, lost = period("subs_gained"), period("subs_lost")
        channels.append({"platform": platform, "account": str(account)[:200],
                         "label": PLATFORMS[platform], "start": str(start), "end": str(end),
                         "latest_date": str(latest) if latest else None, "views": views,
                         "previous_views": previous_views, "change_pct": _change(views, previous_views)
                         if comparison_available else None, "comparison_available": comparison_available,
                         "coverage_days": coverage_days, "trend": trend, "notes": notes,
                         "watch_minutes": watch_minutes,
                         "average_watch_seconds": paired_watch_minutes * 60 / paired_views if paired_views else None,
                         "average_watch_coverage_days": len(paired_dates),
                         "net_subscribers": gained - lost if gained is not None and lost is not None else None,
                         "likes": period("likes"), "comments": period("comments"), "shares": period("shares")})
    return channels


def _videos(db, days):
    # Each post uses its own newest snapshot. A failed source cannot silently
    # disappear because another platform has a newer collection date.
    rows = db.execute("""SELECT p.platform,p.post_id,p.account,p.created,p.title,p.url,p.kind,d.date,d.metric,d.value
        FROM post p JOIN post_day d ON d.platform=p.platform AND d.post_id=p.post_id
        WHERE (p.platform='youtube' AND p.kind='public') OR
              (p.platform='tiktok' AND p.kind='video') OR
              (p.platform='instagram' AND p.kind IN ('reels','video'))
        ORDER BY p.platform,p.post_id,d.date DESC""").fetchall()
    posts = {}
    for platform, pid, account, created, title, url, kind, date, metric, value in rows:
        day, value = _date(date), _number(value)
        if not day or value is None:
            continue
        key = (platform, pid)
        post = posts.setdefault(key, {"platform": platform, "post_id": str(pid)[:200],
                                      "account": str(account)[:200], "created": str(created or "")[:40],
                                      "title": str(title or "Untitled video")[:200], "url": _url(url),
                                      "kind": kind, "snapshots": {}})
        post["snapshots"].setdefault(day, {})[metric] = value
    videos = []
    for post in posts.values():
        snapshots = post.pop("snapshots")
        dates = [d for d, m in snapshots.items() if "views" in m]
        if not dates:
            continue
        latest = max(dates)
        metrics = snapshots[latest]
        baseline = latest - dt.timedelta(days=days)
        period_views = _snapshot_gain({d: m.get("views") for d, m in snapshots.items()},
                                     baseline + dt.timedelta(days=1), latest)
        views = metrics.get("views")
        likes, comments = metrics.get("likes"), metrics.get("comments")
        interactions = None
        if likes is not None and comments is not None:
            interactions = likes + comments + (metrics.get("shares") or 0) + (metrics.get("saved") or 0)
        basis = [name for name in ("likes", "comments", "shares", "saved") if name in metrics]
        videos.append({**post, "snapshot_date": str(latest), "views": views, "likes": likes,
                       "comments": comments, "shares": metrics.get("shares"), "saves": metrics.get("saved"),
                       "reach": metrics.get("reach"), "engagement_rate": interactions / views * 100
                       if interactions is not None and views else None,
                       "engagement_basis": " + ".join(basis) + " / views" if interactions is not None else None,
                       "period_views": period_views, "period_start": str(baseline + dt.timedelta(days=1)),
                       "period_end": str(latest), "watch_seconds": None, "completion_pct": None})
    videos.sort(key=lambda v: (v["views"] is not None, v["views"] or 0), reverse=True)
    return videos


def build_report(db_path, days=28):
    """Return a bounded, JSON-ready report, without creating or modifying a DB.

    ``days`` accepts only 7/28/90; callers should reject other values before
    passing them here. This validation also protects non-HTTP callers.
    """
    if type(days) is not int or days not in WINDOWS:
        raise ValueError("days must be 7, 28 or 90")
    report = _empty(days, "Video analytics are not available yet. Run the existing analytics sync to collect data.")
    path = Path(db_path)
    if not path.is_file():
        return report
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            db.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"account_day", "post", "post_day"}.issubset(tables):
                report["message"] = "The analytics database does not yet contain the required video data."
                return report
            latest = db.execute("SELECT max(date) FROM account_day WHERE "
                                "(platform IN ('youtube','instagram') AND metric='views') OR "
                                "(platform='tiktok' AND metric='views_total')").fetchone()[0]
            latest_snapshot = db.execute("SELECT max(date) FROM post_day").fetchone()[0]
            end = _date(latest) or _date(latest_snapshot)
            if not end:
                return report
            start = end - dt.timedelta(days=days - 1)
            previous_end = start - dt.timedelta(days=1)
            previous_start = start - dt.timedelta(days=days)
            channels = _channels(db, start, end, previous_start, previous_end)
            videos = _videos(db, days)
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError):
        report["message"] = "The saved analytics data could not be read. The existing dashboard and sync are still available."
        return report
    reporting = [p for p in channels if p["views"] is not None]
    current = _sum(p["views"] for p in reporting)
    previous = _sum(p["previous_views"] for p in reporting) if reporting and all(p["previous_views"] is not None for p in reporting) else None
    trend = {}
    for platform in channels:
        for point in platform["trend"]:
            trend[point["date"]] = trend.get(point["date"], 0) + point["views"]
    def combined(metric):
        return _sum(p[metric] for p in reporting) if reporting and all(p[metric] is not None for p in reporting) else None
    coverage = ["Channel charts show observed account views; platform view definitions differ.",
                "Video library metrics are lifetime totals at each saved snapshot, independent of the selected window.",
                "Video period gains require snapshots exactly one selected window apart. Missing baselines stay unavailable.",
                "Engagement is available likes + comments + shares + saves, divided by views; available actions differ by platform.",
                "Per-video watch time, retention, completion and thumbnail click rate are not collected by this sync."]
    coverage.extend(f"{p['label']} {p['account']}: {note}" for p in channels for note in p["notes"])
    comparison_available = bool(reporting) and all(p["comparison_available"] for p in reporting)
    report.update({"available": bool(channels or videos), "message": "Saved analytics; no new sync was started.",
                   "start": str(start), "end": str(end), "previous_start": str(previous_start),
                   "previous_end": str(previous_end), "latest_snapshot": latest_snapshot,
                   "platforms": channels, "trend": [{"date": d, "views": v} for d, v in sorted(trend.items())],
                   "videos": videos[:MAX_VIDEOS], "video_count": len(videos), "truncated": len(videos) > MAX_VIDEOS,
                   "summary": {"views": current, "previous_views": previous, "change_pct": _change(current, previous)
                               if comparison_available else None, "comparison_available": comparison_available,
                               "views_label": "Channel views in selected period",
                               "reporting_platforms": sorted({p["platform"] for p in reporting}),
                               "likes": combined("likes"), "comments": combined("comments"),
                               "shares": combined("shares"), "posts": len(videos),
                               "engagement_rate": None, "lifetime_views": _sum(v["views"] for v in videos)},
                   "coverage": coverage})
    return report
