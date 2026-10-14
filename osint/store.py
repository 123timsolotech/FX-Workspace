"""The world monitor's local store: data/osint/osint.db (SQLite).

  cur     the latest position of everything that moves (flights, military flights) - one row each
  pos     position history, thinned to one point a minute per thing, for tracks and replay
  events  things that happen at a place and time (earthquakes, news events, space-weather alerts)

Everything is kept on this PC. prune() holds it to the day and size limits (settings osint_days,
osint_max_mb): oldest history and events go first.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "osint", "osint.db")
HISTORY_EVERY_S = 60           # one history point a minute per thing
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path = ""
_last_hist: dict = {}          # (layer, id) -> t of its last history point

SCHEMA = """
CREATE TABLE IF NOT EXISTS cur (layer TEXT, id TEXT, t REAL, lat REAL, lon REAL, alt REAL, hdg REAL, spd REAL,
                                label TEXT, props TEXT, PRIMARY KEY (layer, id));
CREATE TABLE IF NOT EXISTS pos (layer TEXT, id TEXT, t REAL, lat REAL, lon REAL, alt REAL, hdg REAL, spd REAL);
CREATE INDEX IF NOT EXISTS pos_id ON pos (id, t);
CREATE INDEX IF NOT EXISTS pos_t ON pos (t);
CREATE TABLE IF NOT EXISTS events (uid TEXT PRIMARY KEY, layer TEXT, t REAL, lat REAL, lon REAL, kind TEXT,
                                   title TEXT, url TEXT, source TEXT, props TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events (layer, t);
"""


def db() -> sqlite3.Connection:
    global _conn, _conn_path
    with _lock:
        if _conn is None or _conn_path != DB_PATH:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn_path = DB_PATH
            _last_hist.clear()
        return _conn


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def put_positions(layer: str, rows: list, now: float | None = None, max_age_s: float = 180, history: bool = True) -> int:
    """Upsert the latest positions of one layer ([{id, t, lat, lon, alt, hdg, spd, label, props}]),
    add a history point where a minute has passed (unless history=False - the 20-minute world snapshot
    would be most of the store), and drop what hasn't been seen for max_age_s."""
    now = now or time.time()
    cur_rows, hist = [], []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        t = float(r.get("t") or now)
        cur_rows.append((layer, str(r["id"]), t, r["lat"], r["lon"], r.get("alt"), r.get("hdg"), r.get("spd"),
                         str(r.get("label") or "")[:60], json.dumps(r.get("props") or {}, separators=(",", ":"))))
        key = (layer, str(r["id"]))
        if history and t - _last_hist.get(key, 0) >= HISTORY_EVERY_S:
            _last_hist[key] = t
            hist.append((layer, str(r["id"]), t, r["lat"], r["lon"], r.get("alt"), r.get("hdg"), r.get("spd")))
    with _lock:
        c = db()
        c.executemany("INSERT OR REPLACE INTO cur VALUES (?,?,?,?,?,?,?,?,?,?)", cur_rows)
        c.executemany("INSERT INTO pos VALUES (?,?,?,?,?,?,?,?)", hist)
        c.execute("DELETE FROM cur WHERE layer = ? AND t < ?", (layer, now - max_age_s))
        c.commit()
    return len(cur_rows)


def put_events(rows: list) -> int:
    """Add events ([{uid, layer, t, lat, lon, kind, title, url, source, props}]); ones already kept are skipped."""
    vals = [(str(r["uid"]), r["layer"], float(r["t"]), r.get("lat"), r.get("lon"), str(r.get("kind") or ""),
             str(r.get("title") or "")[:300], str(r.get("url") or "")[:500], str(r.get("source") or "")[:80],
             json.dumps(r.get("props") or {}, separators=(",", ":"))) for r in rows]
    with _lock:
        c = db()
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
        c.commit()
        return c.total_changes - before


def _pos_dict(row) -> dict:
    layer, id_, t, lat, lon, alt, hdg, spd, label, props = row
    return {"layer": layer, "id": id_, "t": t, "lat": lat, "lon": lon, "alt": alt, "hdg": hdg, "spd": spd,
            "label": label, "props": json.loads(props or "{}")}


def current(layers, bounds: tuple | None = None, limit: int = 20000) -> list:
    """Latest positions of these layers, optionally inside (south, west, north, east)."""
    layers = [layers] if isinstance(layers, str) else list(layers)
    q = f"SELECT * FROM cur WHERE layer IN ({','.join('?' * len(layers))})"
    args: list = list(layers)
    if bounds:
        s, w, n, e = bounds
        q += " AND lat BETWEEN ? AND ?"
        args += [s, n]
        if w <= e:
            q += " AND lon BETWEEN ? AND ?"
            args += [w, e]
    q += " ORDER BY t DESC LIMIT ?"
    args.append(limit)
    with _lock:
        return [_pos_dict(r) for r in db().execute(q, args).fetchall()]


def find(query: str, limit: int = 5) -> list:
    """Current things whose id, label (callsign) or registration match."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    with _lock:
        rows = db().execute("SELECT * FROM cur WHERE lower(id) = ? OR lower(trim(label)) = ? OR props LIKE ? LIMIT ?",
                            (q, q, f'%"reg":"{q.upper()}"%', limit)).fetchall()
        if not rows:
            rows = db().execute("SELECT * FROM cur WHERE lower(label) LIKE ? LIMIT ?", (f"{q}%", limit)).fetchall()
    return [_pos_dict(r) for r in rows]


def track(id_: str, since: float) -> list:
    with _lock:
        rows = db().execute("SELECT t, lat, lon, alt, hdg, spd FROM pos WHERE id = ? AND t >= ? ORDER BY t", (id_, since)).fetchall()
    return [dict(zip(("t", "lat", "lon", "alt", "hdg", "spd"), r)) for r in rows]


def at(layers, t: float, window: float = 150.0, bounds: tuple | None = None, limit: int = 20000) -> list:
    """Where everything of these layers was at time t (rewind): each thing's last history point from `window` seconds
    before t to 45 s after it - history keeps one a minute, and a feed that runs every 45 s records most things
    every other run, so a strict "before t" left gaps. Labels come from the latest positions where still known."""
    layers = [layers] if isinstance(layers, str) else list(layers)
    q = f"SELECT layer, id, max(t), lat, lon, alt, hdg, spd FROM pos WHERE layer IN ({','.join('?' * len(layers))}) AND t BETWEEN ? AND ?"
    args: list = [*layers, t - window, t + 45]
    if bounds:
        s, w, n, e = bounds
        q += " AND lat BETWEEN ? AND ?"
        args += [s, n]
        if w <= e:
            q += " AND lon BETWEEN ? AND ?"
            args += [w, e]
    q += " GROUP BY layer, id LIMIT ?"            # SQLite: the bare columns come from the max(t) row
    args.append(limit)
    with _lock:
        c = db()
        rows = c.execute(q, args).fetchall()
        labels = dict(((r[0], r[1]), (r[2], r[3])) for r in c.execute(
            f"SELECT layer, id, label, props FROM cur WHERE layer IN ({','.join('?' * len(layers))})", layers).fetchall())
    out = []
    for layer, id_, tt, lat, lon, alt, hdg, spd in rows:
        label, props = labels.get((layer, id_), ("", "{}"))
        out.append({"layer": layer, "id": id_, "t": tt, "lat": lat, "lon": lon, "alt": alt, "hdg": hdg, "spd": spd,
                    "label": label, "props": json.loads(props or "{}")})
    return out


def history_range() -> tuple:
    """(oldest, newest) history time, or (None, None) with no history yet."""
    with _lock:
        return tuple(db().execute("SELECT min(t), max(t) FROM pos").fetchone())


def count_in(layer: str, t: float, bounds: tuple, window: float = 300.0) -> int:
    """How many different things of a layer were inside bounds in the `window` seconds before t."""
    s, w, n, e = bounds
    q, args = "SELECT count(DISTINCT id) FROM pos WHERE layer = ? AND t BETWEEN ? AND ? AND lat BETWEEN ? AND ?", [layer, t - window, t, s, n]
    if w <= e:
        q += " AND lon BETWEEN ? AND ?"
        args += [w, e]
    with _lock:
        return db().execute(q, args).fetchone()[0]


def events(layers=None, since: float = 0, bounds: tuple | None = None, limit: int = 500) -> list:
    q, args = "SELECT * FROM events WHERE t >= ?", [since]
    if layers:
        layers = [layers] if isinstance(layers, str) else list(layers)
        q += f" AND layer IN ({','.join('?' * len(layers))})"
        args += layers
    if bounds:
        s, w, n, e = bounds
        q += " AND lat BETWEEN ? AND ?"
        args += [s, n]
        if w <= e:
            q += " AND lon BETWEEN ? AND ?"
            args += [w, e]
    q += " ORDER BY t DESC LIMIT ?"
    args.append(limit)
    keys = ("uid", "layer", "t", "lat", "lon", "kind", "title", "url", "source", "props")
    with _lock:
        rows = db().execute(q, args).fetchall()
    return [{**dict(zip(keys, r)), "props": json.loads(r[9] or "{}")} for r in rows]


def count_since(layer: str, since: float) -> int:
    with _lock:
        return db().execute("SELECT count(*) FROM events WHERE layer = ? AND t >= ?", (layer, since)).fetchone()[0]


def counts() -> dict:
    with _lock:
        c = db()
        cur = dict(c.execute("SELECT layer, count(*) FROM cur GROUP BY layer").fetchall())
        ev = dict(c.execute("SELECT layer, count(*) FROM events GROUP BY layer").fetchall())
    return {"current": cur, "events": ev}


def size_mb() -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    return total / 1e6


def prune(days: float, max_mb: float, now: float | None = None) -> dict:
    """Drop history and events older than `days`; then, while the store is over max_mb, the oldest day of them."""
    now = now or time.time()
    cut = now - days * 86400
    removed = 0
    with _lock:
        c = db()
        removed += c.execute("DELETE FROM pos WHERE t < ?", (cut,)).rowcount
        removed += c.execute("DELETE FROM events WHERE t < ?", (cut,)).rowcount
        c.commit()
        for _ in range(30):
            if size_mb() <= max_mb:
                break
            oldest = c.execute("SELECT min(t) FROM pos").fetchone()[0] or now
            oldest_e = c.execute("SELECT min(t) FROM events").fetchone()[0] or now
            step = min(oldest, oldest_e) + 86400
            if step > now:
                break
            removed += c.execute("DELETE FROM pos WHERE t < ?", (step,)).rowcount
            removed += c.execute("DELETE FROM events WHERE t < ?", (step,)).rowcount
            c.commit()
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")
    return {"removed": removed, "size_mb": round(size_mb(), 1)}
