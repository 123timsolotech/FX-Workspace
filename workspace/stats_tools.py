"""
stats_tools.py
==============
Charts and statistics the assistant makes itself (the composer tool "charts"; agent/auto_tools.py turns it on
for questions about numbers):

  compute_stats   exact statistics - describe (count, sum, mean, median, mode, spread, quartiles, outliers),
                  relation (Pearson r, a least-squares line, R²), growth (change, % change, CAGR), a moving
                  average - on numbers the model passes or on columns of an attached CSV. A model doing this in
                  its head gets medians and standard deviations wrong; this never does.
  make_chart      a checked chart for the chat to draw: bar, hbar, stacked, line, area, pie, donut, scatter.
                  The page draws it from a ```chart block (index.html renderChart) - agent/tasks._with_charts puts
                  each chart made this turn under the reply. Numbers are checked here: a chart is drawn from
                  exactly what was passed, never padded or invented.

Pure Python (statistics, math) - no new dependency. Both are read-only: they change nothing on the PC.
"""
from __future__ import annotations

import json
import math
import statistics

MAX_POINTS = 500
MAX_SERIES = 12
CHART_TYPES = ("bar", "hbar", "stacked", "line", "area", "pie", "donut", "scatter")


class StatsError(ValueError):
    pass


def _r(x, digits: int = 6):
    """A number for the model and the chat: 6 significant digits, ints kept as ints."""
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, int):
        return x
    if not math.isfinite(x):
        return None
    if x == int(x) and abs(x) < 1e15:
        return int(x)
    return float(f"{x:.{digits}g}")


def to_number(v):
    """A number from a number or a string like "1,234.5", "$12", "45%", "(3.2)" (negative); None if it isn't one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v or "").strip().replace(",", "").replace("−", "-")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    for sym in ("$", "€", "£", "₪", "¥"):
        s = s.replace(sym, "")
    s = s.rstrip("%").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    if not math.isfinite(n):
        return None
    return -n if neg else n


def numbers(values, what: str = "values") -> list:
    if not isinstance(values, (list, tuple)):
        raise StatsError(f"{what} must be a list of numbers.")
    out, bad = [], []
    for v in values[:100_000]:
        n = to_number(v)
        if n is None:
            if v not in (None, ""):
                bad.append(v)
            continue
        out.append(n)
    if bad and len(bad) > len(out):
        raise StatsError(f"{what}: most entries aren't numbers (e.g. {str(bad[0])[:40]!r}).")
    return out


def _quantile(sorted_vals: list, q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default, Excel's PERCENTILE.INC)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def describe(values) -> dict:
    xs = numbers(values)
    if not xs:
        raise StatsError("No numbers to describe.")
    s = sorted(xs)
    q1, q3 = _quantile(s, 0.25), _quantile(s, 0.75)
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    counts: dict = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    top = max(counts.values())
    modes = sorted(k for k, c in counts.items() if c == top) if top > 1 else []
    out = {"count": len(xs), "sum": _r(math.fsum(xs)), "mean": _r(statistics.fmean(xs)), "median": _r(statistics.median(s)),
           "mode": [_r(m) for m in modes[:5]], "min": _r(s[0]), "max": _r(s[-1]), "range": _r(s[-1] - s[0]),
           "q1": _r(q1), "q3": _r(q3), "iqr": _r(iqr), "p10": _r(_quantile(s, 0.10)), "p90": _r(_quantile(s, 0.90)),
           "outliers": [_r(x) for x in xs if x < lo_fence or x > hi_fence][:20]}
    if len(xs) > 1:
        out["stdev_sample"] = _r(statistics.stdev(xs))
        out["stdev_population"] = _r(statistics.pstdev(xs))
        out["variance_sample"] = _r(statistics.variance(xs))
        mean = statistics.fmean(xs)
        out["coefficient_of_variation"] = _r(statistics.stdev(xs) / mean) if mean else None
    return out


def relation(x, y) -> dict:
    xs, ys = numbers(x, "x"), numbers(y, "y")
    if len(xs) != len(ys):
        raise StatsError(f"x has {len(xs)} numbers and y has {len(ys)} - they must pair up.")
    n = len(xs)
    if n < 3:
        raise StatsError("A relation needs at least 3 pairs.")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = math.fsum((a - mx) ** 2 for a in xs)
    syy = math.fsum((b - my) ** 2 for b in ys)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(xs, ys))
    if sxx == 0 or syy == 0:
        raise StatsError("One of the series doesn't vary, so there is no relation to measure.")
    r = sxy / math.sqrt(sxx * syy)
    slope = sxy / sxx
    strength = ("very strong" if abs(r) >= 0.8 else "strong" if abs(r) >= 0.6 else "moderate" if abs(r) >= 0.4
                else "weak" if abs(r) >= 0.2 else "very weak or none")
    return {"pairs": n, "pearson_r": _r(r, 4), "r_squared": _r(r * r, 4), "slope": _r(slope),
            "intercept": _r(my - slope * mx), "strength": f"{strength}, {'positive' if r > 0 else 'negative'}",
            "note": "Correlation shows the numbers move together - not that one causes the other."}


def growth(values, periods: float | None = None) -> dict:
    xs = numbers(values)
    if len(xs) < 2:
        raise StatsError("Growth needs at least two numbers (first and last).")
    first, last = xs[0], xs[-1]
    out = {"first": _r(first), "last": _r(last), "change": _r(last - first)}
    if first != 0:
        out["pct_change"] = _r((last - first) / abs(first) * 100, 4)
    steps = [(b - a) / abs(a) * 100 for a, b in zip(xs, xs[1:]) if a != 0]
    if steps:
        out["pct_change_each_step"] = [_r(s, 4) for s in steps[:60]]
    n = periods if periods else len(xs) - 1
    if first > 0 and last > 0 and n:
        out["cagr_pct"] = _r(((last / first) ** (1 / n) - 1) * 100, 4)
        out["cagr_periods"] = _r(n)
    return out


def moving_average(values, window: int) -> dict:
    xs = numbers(values)
    window = int(window or 0)
    if not 2 <= window <= len(xs):
        raise StatsError(f"The window must be between 2 and the number of values ({len(xs)}).")
    return {"window": window, "values": [_r(statistics.fmean(xs[i - window + 1:i + 1])) for i in range(window - 1, len(xs))]}


# ---- attached CSV files ------------------------------------------------------------------------------
def _table(file_ref, files) -> dict:
    """An attached table by its name or id (this turn's attachments, composer_tools.FileStore)."""
    import composer_tools
    for f in files or ():
        if file_ref in (f.get("id"), f.get("name")) and f.get("kind") == "table" and f.get("path"):
            return composer_tools.read_table(f["path"])
    names = ", ".join(f.get("name", "") for f in files or () if f.get("kind") == "table") or "none"
    raise StatsError(f"No attached table called {str(file_ref)[:60]!r} (attached tables: {names}).")


def _column(table: dict, name) -> list:
    cols = table["columns"]
    idx = next((i for i, c in enumerate(cols) if c == name), None)
    if idx is None:
        idx = next((i for i, c in enumerate(cols) if c.strip().lower() == str(name).strip().lower()), None)
    if idx is None:
        raise StatsError(f"No column {str(name)[:60]!r}. Columns: {', '.join(c[:40] for c in cols[:30])}.")
    return [r[idx] if idx < len(r) else "" for r in table["rows"]]


def _safe_name(s) -> str:
    return " ".join(str(s or "").split())[:60]


# ---- the tools -----------------------------------------------------------------------------------------
def compute_stats(args: dict, files=None) -> dict:
    """The compute_stats tool. args: values | series {name: values} | file + columns; and optional x, y (a
    relation), periods (growth), window (moving average)."""
    try:
        data: dict = {}
        if args.get("file"):
            table = _table(args["file"], files)
            cols = args.get("columns") or ([args["column"]] if args.get("column") else [])
            if not cols:
                cols = [c for c in table["columns"] if sum(to_number(v) is not None for v in _column(table, c)) >= 0.8 * max(1, len(table["rows"]))]
            for c in cols[:MAX_SERIES]:
                data[_safe_name(c)] = _column(table, c)
            if args.get("x") and args.get("y") and isinstance(args["x"], str):
                args = {**args, "x": _column(table, args["x"]), "y": _column(table, args["y"])}
        elif isinstance(args.get("series"), dict):
            data = {_safe_name(k): v for k, v in list(args["series"].items())[:MAX_SERIES]}
        elif args.get("values") is not None:
            data = {"values": args["values"]}
        out: dict = {"ok": True, "label": "COMPUTED (exact, by the app - quote these numbers as they are)"}
        if data:
            out["describe"] = {k: describe(v) for k, v in data.items()}
            if args.get("growth") or args.get("periods"):
                out["growth"] = {k: growth(v, args.get("periods")) for k, v in data.items()}
            if args.get("window"):
                out["moving_average"] = {k: moving_average(v, args["window"]) for k, v in data.items()}
        if args.get("x") is not None and args.get("y") is not None:
            out["relation"] = relation(args["x"], args["y"])
        if len(out) == 2:
            raise StatsError("Pass values, series {name: values}, an attached file's columns, or x and y.")
        return out
    except StatsError as e:
        return {"ok": False, "status": "permanent_failure", "message": str(e)}


def make_chart(args: dict, files=None) -> dict:
    """The make_chart tool: checks the chart and returns it for the chat. args: type, title, labels, series
    [{name, values}] (scatter: points [[x, y]]), x_label, y_label, unit, source; or file + x + y (columns)
    with agg sum | mean | count | none."""
    try:
        kind = str(args.get("type") or "bar").lower()
        if kind not in CHART_TYPES:
            raise StatsError(f"type must be one of {', '.join(CHART_TYPES)}.")
        labels, series = list(args.get("labels") or []), args.get("series") or []
        if args.get("file"):
            labels, series = _from_table(args, files, kind)
        if not isinstance(series, list) or not series:
            raise StatsError("series must be a list like [{\"name\": \"Sales\", \"values\": [1, 2, 3]}].")
        if len(series) > MAX_SERIES:
            raise StatsError(f"At most {MAX_SERIES} series.")
        clean = []
        for i, s in enumerate(series):
            if not isinstance(s, dict):
                raise StatsError("Each series is an object with a name and values.")
            name = _safe_name(s.get("name") or f"Series {i + 1}")
            if kind == "scatter":
                pts = []
                for p in (s.get("points") or [])[:MAX_POINTS]:
                    if isinstance(p, (list, tuple)) and len(p) == 2 and to_number(p[0]) is not None and to_number(p[1]) is not None:
                        pts.append([_r(to_number(p[0])), _r(to_number(p[1]))])
                if len(pts) < 2:
                    raise StatsError(f"{name}: a scatter series needs points [[x, y], ...] (at least 2).")
                clean.append({"name": name, "points": pts})
                continue
            vals = s.get("values")
            if not isinstance(vals, list) or not vals:
                raise StatsError(f"{name}: values must be a list of numbers.")
            nums = [to_number(v) for v in vals[:MAX_POINTS]]
            if any(n is None for n in nums):
                raise StatsError(f"{name}: every value must be a number (use 0 only if the value really is 0).")
            clean.append({"name": name, "values": [_r(n) for n in nums]})
        if kind != "scatter":
            n = len(clean[0]["values"])
            if any(len(s["values"]) != n for s in clean):
                raise StatsError("Every series needs the same number of values.")
            labels = [_safe_name(x) for x in labels[:MAX_POINTS]]
            if labels and len(labels) != n:
                raise StatsError(f"There are {len(labels)} labels for {n} values - give one label per value.")
            if not labels:
                labels = [str(i + 1) for i in range(n)]
            if kind in ("pie", "donut"):
                if len(clean) != 1:
                    raise StatsError("A pie or donut chart takes exactly one series.")
                if any(v < 0 for v in clean[0]["values"]):
                    raise StatsError("A pie or donut chart can't show negative values - use a bar chart.")
                if not any(clean[0]["values"]):
                    raise StatsError("Every value is 0 - there is nothing to show.")
        chart = {"type": kind, "title": _safe_name(args.get("title"))[:80], "labels": labels if kind != "scatter" else [],
                 "series": clean}
        for k in ("x_label", "y_label", "unit", "source"):
            if args.get(k):
                chart[k] = " ".join(str(args[k]).split())[:80 if k != "source" else 160]
        return {"ok": True, "chart": chart,
                "message": "Done - the chat draws this chart under your reply. Describe what it shows in a sentence or "
                           "two; don't repeat the numbers as a table unless asked."}
    except StatsError as e:
        return {"ok": False, "status": "permanent_failure", "message": str(e)}


def _from_table(args: dict, files, kind: str):
    table = _table(args["file"], files)
    x = args.get("x")
    ys = args.get("y") if isinstance(args.get("y"), list) else [args.get("y")] if args.get("y") else []
    if not x or not ys:
        raise StatsError("With a file, give x (the column for the labels) and y (one or more number columns).")
    agg = str(args.get("agg") or "none").lower()
    xs = _column(table, x)
    if kind == "scatter":
        out = []
        for y in ys[:MAX_SERIES]:
            pts = [[a, b] for a, b in zip(xs, _column(table, y)) if to_number(a) is not None and to_number(b) is not None]
            out.append({"name": y, "points": pts[:MAX_POINTS]})
        return [], out
    if agg in ("sum", "mean", "count"):
        ys = ys[:MAX_SERIES]
        ycols = {y: _column(table, y) for y in ys}
        order, groups = [], {}
        for i, key in enumerate(xs):
            key = _safe_name(key)
            if key not in groups:
                order.append(key)
                groups[key] = {y: [] for y in ys}
            for y in ys:
                n = to_number(ycols[y][i]) if agg != "count" else 1.0
                if n is not None:
                    groups[key][y].append(n)
        if len(order) > MAX_POINTS:
            raise StatsError(f"{len(order)} groups - too many to draw; group by a coarser column.")
        fn = {"sum": math.fsum, "mean": lambda v: statistics.fmean(v) if v else 0.0, "count": len}[agg]
        return order, [{"name": f"{y} ({agg})", "values": [fn(groups[k][y]) for k in order]} for y in ys]
    if len(xs) > MAX_POINTS:
        raise StatsError(f"{len(xs)} rows - too many to draw one by one; use agg sum, mean or count.")
    return list(xs), [{"name": y, "values": _column(table, y)} for y in ys[:MAX_SERIES]]


def chart_block(chart: dict) -> str:
    """The ```chart block the page draws (index.html renderChart)."""
    return "```chart\n" + json.dumps(chart, ensure_ascii=False, separators=(",", ":")) + "\n```"
