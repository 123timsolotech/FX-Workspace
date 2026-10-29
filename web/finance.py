"""
finance.py
==========
Market data and investment-research support: numbers from the sources that
publish them, each labelled with how solid it is.

Evidence classes (EVIDENCE) - every figure and every web source gets one, so a
report can keep what happened apart from what someone expects or believes:

  FACT         official, primary: SEC filings (XBRL company facts), US Treasury,
               BLS, World Bank, ECB, central banks, exchanges, a company's own
               investor-relations pages (its reported numbers - not its outlook)
  MARKET       market data from a vendor: prices, volume (Yahoo Finance)
  COMPUTED     worked out here from FACT / MARKET figures (returns, volatility,
               drawdown, P/E, margins, growth) - the method is stated
  REPORTED     journalism: news agencies and financial press - someone reports it
  ESTIMATE     forecasts: analyst consensus, price targets, guidance, models
  OPINION      analysis and commentary: blogs, newsletters, contributor sites
  SPECULATION  social media and forums: rumours, tips, unverified claims
  PROMOTIONAL  paid or issuer-written promotion: press-release wires, sponsored

Keyless sources: SEC EDGAR (company list, company facts, filings), Yahoo Finance
(ticker search, daily prices, news headlines), Nasdaq headlines, US Treasury
yield curve, BLS (CPI, unemployment), World Bank, ECB exchange rates.
Optional keys (Settings > Web Search, data/search_keys.json): FRED, Finnhub,
Alpha Vantage - estimates, company news, more macro series.

All requests go through the network layer (netlayer, the "search" component)
inside the Web Search mode's scope: Local Only and Disabled refuse them.
Nothing here gives advice: it reports numbers and where they come from.
"""
from __future__ import annotations

import csv
import io
import math
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from html import unescape as html_unescape
from urllib.parse import quote, urlparse

import netlayer

# ==============================================================================
# Evidence classes and the source registry
# ==============================================================================
EVIDENCE = {
    "FACT": "official figure from the publisher itself",
    "MARKET": "market data from a data vendor",
    "COMPUTED": "calculated here from reported figures",
    "REPORTED": "reported by a news organisation",
    "ESTIMATE": "a forecast, target or guidance - may not happen",
    "OPINION": "analysis or commentary - one view",
    "SPECULATION": "social media or forum claim - unverified",
    "PROMOTIONAL": "written or paid for by the issuer - promotion",
}
# How far a class weighs when sources disagree (the reports rank by it).
WEIGHT = {"FACT": 1.0, "MARKET": 0.95, "COMPUTED": 0.9, "REPORTED": 0.7, "ESTIMATE": 0.5, "OPINION": 0.4,
          "PROMOTIONAL": 0.25, "SPECULATION": 0.15}

# domain (or a suffix of it) -> class. The first match from the most specific domain wins.
_DOMAINS = {
    "FACT": ("sec.gov", "treasury.gov", "fiscaldata.treasury.gov", "bls.gov", "bea.gov", "census.gov",
             "federalreserve.gov", "stlouisfed.org", "worldbank.org", "imf.org", "oecd.org", "ecb.europa.eu",
             "bankofengland.co.uk", "boj.or.jp", "bis.org", "eurostat.ec.europa.eu", "nasdaqtrader.com",
             "finra.org", "fdic.gov", "cftc.gov", "gov.uk", "europa.eu"),
    "MARKET": ("finance.yahoo.com", "query1.finance.yahoo.com", "stooq.com", "marketwatch.com/investing",
               "tradingview.com", "investing.com", "google.com/finance", "morningstar.com/stocks", "macrotrends.net",
               "companiesmarketcap.com", "stockanalysis.com"),
    "REPORTED": ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "apnews.com", "cnbc.com", "barrons.com",
                 "marketwatch.com", "economist.com", "nytimes.com", "bbc.co.uk", "bbc.com", "theguardian.com",
                 "axios.com", "fortune.com", "forbes.com/sites/[staff]", "businessinsider.com", "nasdaq.com",
                 "finance.yahoo.com/news", "morningstar.com/news", "techcrunch.com", "theverge.com", "nikkei.com",
                 "handelsblatt.com", "calcalistech.com", "globes.co.il"),
    "ESTIMATE": ("tipranks.com", "zacks.com", "marketbeat.com", "wallstreetzen.com", "financhill.com",
                 "stockforecast", "coincodex.com", "walletinvestor.com", "longforecast.com", "gov.capital"),
    "OPINION": ("seekingalpha.com", "fool.com", "investorplace.com", "benzinga.com", "thestreet.com", "substack.com",
                "medium.com", "gurufocus.com", "simplywall.st", "morningstar.com", "forbes.com", "investopedia.com",
                "kiplinger.com", "nerdwallet.com"),
    "SPECULATION": ("reddit.com", "stocktwits.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
                    "facebook.com", "discord.com", "t.me", "hotcopper.com.au", "boards.fool", "4chan.org"),
    "PROMOTIONAL": ("prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com", "newsfilecorp.com",
                    "einpresswire.com", "openpr.com", "sponsored", "streetinsider.com/Press"),
}


def classify(url: str, title: str = "") -> str:
    """A web source's evidence class, from its address (and a title hint for forecasts)."""
    try:
        p = urlparse(url)
    except ValueError:
        return "OPINION"
    host = (p.hostname or "").lower().removeprefix("www.")
    path = (p.path or "").lower()
    best, best_len = None, -1
    for kind, entries in _DOMAINS.items():
        for entry in entries:
            dom, _, sub = entry.split("/[")[0].partition("/")       # "morningstar.com/news" -> domain, path
            if not (host == dom or host.endswith("." + dom)):
                continue
            if sub and not path.lstrip("/").startswith(sub.lower()):
                continue
            if len(entry) > best_len:                             # the most specific entry wins
                best, best_len = kind, len(entry)
    if best is None:
        if host.endswith(".gov") or ".gov." in host or host.endswith(".int"):
            best = "FACT"
        elif re.search(r"(^|\.)(ir|investors?|investor-relations)\.", host) or "/investor" in path:
            best = "FACT"                          # a company's own IR pages: its reported numbers
        else:
            best = "OPINION"
    t = (title or "").lower()
    if best in ("REPORTED", "OPINION", "MARKET") and re.search(
            r"\b(price target|forecast|prediction|outlook 20\d\d|could reach|will reach|expected to|worth this much by|"
            r"by 20[3-9]\d)\b", t):
        return "ESTIMATE"
    # Headlines that sell a view are commentary, whatever site carries them (Yahoo republishes Motley Fool).
    if best in ("REPORTED", "MARKET") and re.search(
            r"\b(should (you|investors) buy|stocks? to buy|i'?m buying|buy (it )?now|best stocks?|no-brainer|millionaire|"
            r"soar|skyrocket|reasons? (why|to)|my (top|favorite)|could make you|don'?t miss|before it|i'?m selling)\b", t):
        return "OPINION"
    return best


def label(url: str, title: str = "") -> str:
    kind = classify(url, title)
    return f"{kind} - {EVIDENCE[kind]}"


# ==============================================================================
# HTTP (through the network layer)
# ==============================================================================
DEFAULT_SEC_CONTACT = "Agentic Workspace research-app@agentic-workspace.local"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
_settings_fn = lambda: {}                   # noqa: E731 - api/server configures the real one
_cache: dict = {}
_cache_lock = threading.Lock()


def configure(settings_fn):
    global _settings_fn
    _settings_fn = settings_fn


class DataError(Exception):
    pass


def _cached(key: str, ttl: float, fn):
    """Market data changes slowly within minutes; filings within hours. A temporary
    chat still reads through (privacy.py: nothing of it is written - this cache is
    in memory only and holds public data, not the question)."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        if len(_cache) > 400:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def _get(url: str, *, headers: dict | None = None, timeout: float = 20.0, private: bool | None = False):
    try:
        with netlayer.client("search", timeout=timeout, private=private, follow_redirects=True,
                             headers=headers or {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/plain, */*"}) as c:
            r = c.get(url)
    except netlayer.RouteBlocked:
        raise
    except Exception as e:
        raise DataError(f"{urlparse(url).hostname}: {type(e).__name__}: {str(e)[:120]}")
    if r.status_code == 429:
        raise DataError(f"{urlparse(url).hostname} is rate-limiting requests - try again in a minute.")
    if r.status_code >= 400:
        raise DataError(f"{urlparse(url).hostname} answered {r.status_code}.")
    return r


def _sec_headers() -> dict:
    contact = str((_settings_fn() or {}).get("sec_contact") or "").strip() or DEFAULT_SEC_CONTACT
    return {"User-Agent": contact, "Accept-Encoding": "gzip, deflate"}


def _key(provider: str) -> str:
    try:
        import search_engine
        return search_engine.KEYS.get(provider) or ""
    except Exception:
        return ""


# ==============================================================================
# Companies: SEC EDGAR
# ==============================================================================
def _sec_tickers() -> list:
    def load():
        r = _get("https://www.sec.gov/files/company_tickers_exchange.json", headers=_sec_headers(), timeout=30)
        data = r.json()
        return [dict(zip(data["fields"], row)) for row in data.get("data") or []]
    return _cached("sec_tickers", 24 * 3600, load)


def resolve(query: str) -> dict | None:
    """A ticker or a company name -> {"ticker", "cik", "name", "exchange"} (US-listed via SEC;
    anything else via Yahoo's search, without a CIK)."""
    q = (query or "").strip()
    if not q:
        return None
    qu = q.upper().removeprefix("$")
    try:
        rows = _sec_tickers()
    except DataError:
        rows = []
    for r in rows:
        if r.get("ticker") == qu:
            return {"ticker": r["ticker"], "cik": int(r["cik"]), "name": r["name"], "exchange": r.get("exchange") or ""}
    ql = q.lower()
    named = [r for r in rows if ql in str(r.get("name", "")).lower()]
    if named:
        named.sort(key=lambda r: (not str(r["name"]).lower().startswith(ql), len(r["name"])))
        r = named[0]
        return {"ticker": r["ticker"], "cik": int(r["cik"]), "name": r["name"], "exchange": r.get("exchange") or ""}
    try:
        r = _get(f"https://query1.finance.yahoo.com/v1/finance/search?q={quote(q)}&quotesCount=5&newsCount=0")
        for item in r.json().get("quotes") or []:
            if item.get("symbol") and item.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND", "INDEX", "CRYPTOCURRENCY", "CURRENCY", "FUTURE"):
                sym = item["symbol"]
                cik = next((int(x["cik"]) for x in rows if x.get("ticker") == sym.upper()), None)
                return {"ticker": sym, "cik": cik, "name": item.get("longname") or item.get("shortname") or sym,
                        "exchange": item.get("exchDisp") or item.get("exchange") or "", "type": item.get("quoteType")}
    except DataError:
        pass
    return None


# (label, candidate us-gaap tags in order, unit)
METRICS = [
    ("Revenue", ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerIncludingAssessedTax"), "USD"),
    ("Gross profit", ("GrossProfit",), "USD"),
    ("Operating income", ("OperatingIncomeLoss",), "USD"),
    ("Net income", ("NetIncomeLoss", "ProfitLoss"), "USD"),
    ("EPS (diluted)", ("EarningsPerShareDiluted", "EarningsPerShareBasic"), "USD/shares"),
    ("Operating cash flow", ("NetCashProvidedByUsedInOperatingActivities",), "USD"),
    ("Capital expenditure", ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                             "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"), "USD"),
    ("Cash", ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), "USD"),
    ("Total assets", ("Assets",), "USD"),
    ("Total liabilities", ("Liabilities",), "USD"),
    ("Long-term debt", ("LongTermDebtNoncurrent", "LongTermDebt"), "USD"),
    ("Shareholders' equity", ("StockholdersEquity",), "USD"),
    ("Dividends per share", ("CommonStockDividendsPerShareDeclared",), "USD/shares"),
]


def _annual(facts: dict, tags: tuple, unit: str) -> list:
    """[(fiscal year, value, filed, form)] from 10-K full-year figures, newest first - from the
    candidate tag with the most recent figure (companies switch tags; an old one stops in 2012)."""
    found = [rows for rows in (_annual_tag(facts, tag, unit) for tag in tags) if rows]
    return max(found, key=lambda rows: rows[0][0]) if found else []


def _annual_tag(facts: dict, tag: str, unit: str) -> list:
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for tag in (tag,):
        units = ((gaap.get(tag) or {}).get("units") or {}).get(unit)
        if not units:
            continue
        by_year = {}
        for u in units:
            if u.get("form") not in ("10-K", "10-K/A", "20-F", "40-F") or u.get("fp") != "FY":
                continue
            start, end = u.get("start"), u.get("end")
            if start and end:          # a flow (revenue): only a full year, not a quarter inside the 10-K
                try:
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                except ValueError:
                    continue
                if not 340 <= days <= 380:
                    continue
            year = int(str(end or "0")[:4] or 0)
            if year and (year not in by_year or u.get("filed", "") > by_year[year][2]):
                by_year[year] = (year, u.get("val"), u.get("filed", ""), u.get("form"))
        if by_year:
            return sorted(by_year.values(), reverse=True)
    return []


def fundamentals(cik: int, years: int = 5) -> dict:
    """Reported annual figures (SEC XBRL company facts) and ratios computed from them."""
    def load():
        return _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json", headers=_sec_headers(), timeout=40).json()
    facts = _cached(f"facts:{cik}", 6 * 3600, load)
    out = {"name": facts.get("entityName"), "cik": cik, "metrics": {},
           "source": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-K"}
    for name, tags, unit in METRICS:
        rows = _annual(facts, tags, unit)[:years]
        if rows:
            out["metrics"][name] = rows
    dei = ((facts.get("facts") or {}).get("dei") or {}).get("EntityCommonStockSharesOutstanding", {})
    shares = sorted((dei.get("units") or {}).get("shares") or [], key=lambda u: u.get("end", ""))
    if shares:
        out["shares"] = (shares[-1]["val"], shares[-1]["end"])
    m = out["metrics"]

    def latest(k):
        return m[k][0][1] if m.get(k) else None
    comp = {}
    rev, ni, op, gp = latest("Revenue"), latest("Net income"), latest("Operating income"), latest("Gross profit")
    if rev:
        if gp is not None:
            comp["Gross margin"] = gp / rev
        if op is not None:
            comp["Operating margin"] = op / rev
        if ni is not None:
            comp["Net margin"] = ni / rev
        if len(m.get("Revenue", [])) >= 2 and m["Revenue"][1][1]:
            comp["Revenue growth (last year)"] = rev / m["Revenue"][1][1] - 1
        r = m.get("Revenue", [])
        if len(r) >= 4 and r[-1][1] and r[0][1] and r[0][1] > 0 and r[-1][1] > 0:
            comp[f"Revenue CAGR ({r[-1][0]}-{r[0][0]})"] = (r[0][1] / r[-1][1]) ** (1 / (r[0][0] - r[-1][0])) - 1
    ocf, capex = latest("Operating cash flow"), latest("Capital expenditure")
    if ocf is not None and capex is not None and m["Operating cash flow"][0][0] == m["Capital expenditure"][0][0]:
        comp["Free cash flow"] = ocf - capex             # only from the same fiscal year
    eq, liab = latest("Shareholders' equity"), latest("Total liabilities")
    if eq and liab is not None and eq > 0:
        comp["Liabilities / equity"] = liab / eq
    if eq and ni is not None and eq > 0:
        comp["Return on equity"] = ni / eq
    out["computed"] = comp
    return out


MAJOR_FORMS = ("10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "S-1", "10-K/A", "10-Q/A")


def filings(cik: int, forms=MAJOR_FORMS + ("4", "SC 13G", "SC 13D"), limit: int = 12) -> list:
    """Recent filings: [{"form", "date", "title", "url"}]."""
    def load():
        return _get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=_sec_headers(), timeout=30).json()
    data = _cached(f"subs:{cik}", 3600, load)
    recent = (data.get("filings") or {}).get("recent") or {}
    out = []
    for i, form in enumerate(recent.get("form") or []):
        if form not in forms:
            continue
        acc = recent["accessionNumber"][i].replace("-", "")
        doc = recent["primaryDocument"][i]
        out.append({"form": form, "date": recent["filingDate"][i], "title": recent.get("primaryDocDescription", [""] * (i + 1))[i] or form,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"})
        if len(out) >= limit:
            break
    return out


def profile(cik: int) -> dict:
    data = _cached(f"subs:{cik}", 3600, lambda: _get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                                                   headers=_sec_headers(), timeout=30).json())
    return {k: data.get(k) for k in ("name", "sicDescription", "stateOfIncorporation", "fiscalYearEnd", "exchanges",
                                      "tickers", "category", "website")}


# ==============================================================================
# Prices (Yahoo Finance chart API) and what they show
# ==============================================================================
RANGES = ("1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max")


def prices(ticker: str, rng: str = "5y") -> dict:
    """{"ticker", "currency", "dates": [...], "close": [...], "source"} - daily (weekly past 5y)."""
    rng = rng if rng in RANGES else "5y"
    interval = "1wk" if rng in ("10y", "max") else "1d"

    def load():
        errors = []
        for host in ("query1", "query2"):
            try:
                return _yahoo(host)
            except DataError as e:
                errors.append(str(e))
        try:
            return _nasdaq_prices(ticker, rng)
        except DataError as e:
            errors.append(str(e))
        raise DataError("; ".join(dict.fromkeys(errors)))

    def _yahoo(host):
        r = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(ticker)}?range={rng}&interval={interval}"
                 "&includeAdjustedClose=true")
        res = ((r.json().get("chart") or {}).get("result") or [None])[0]
        if not res:
            raise DataError(f"No price history for {ticker}.")
        ts = res.get("timestamp") or []
        ind = res.get("indicators") or {}
        adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose")
        close = adj or ((ind.get("quote") or [{}])[0] or {}).get("close") or []
        rows = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), c) for t, c in zip(ts, close) if c is not None]
        meta = res.get("meta") or {}
        return {"ticker": meta.get("symbol") or ticker, "currency": meta.get("currency") or "", "interval": interval,
                "exchange": meta.get("fullExchangeName") or "", "price": meta.get("regularMarketPrice"),
                "dates": [d for d, _ in rows], "close": [c for _, c in rows],
                "source": f"https://finance.yahoo.com/quote/{quote(ticker)}/history"}
    return _cached(f"px:{ticker}:{rng}", 900, load)


def _nasdaq_prices(ticker: str, rng: str) -> dict:
    """The fallback when Yahoo rate-limits: Nasdaq's daily history (US listings; not split-adjusted)."""
    days = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653, "max": 3653}[rng]
    start = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
    sym = quote(ticker.split(".")[0].upper())
    data = {}
    for cls in ("stocks", "etf"):
        data = _get(f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}&fromdate={start}&limit=9999",
                    timeout=25).json() or {}
        if (data.get("data") or {}).get("tradesTable"):
            break
    rows = ((((data.get("data") or {}).get("tradesTable") or {}).get("rows")) or [])
    pts = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            pts.append((d, float(str(row["close"]).replace("$", "").replace(",", ""))))
        except (KeyError, ValueError):
            continue
    if not pts:
        raise DataError(f"Nasdaq has no price history for {ticker}.")
    pts.sort()
    return {"ticker": ticker.upper(), "currency": "USD", "interval": "1d", "exchange": "", "price": pts[-1][1],
            "dates": [d for d, _ in pts], "close": [c for _, c in pts],
            "source": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/historical"}


def price_stats(px: dict) -> dict:
    """Returns, volatility, drawdown and moving averages from a price series (COMPUTED)."""
    c, d = px["close"], px["dates"]
    if len(c) < 2:
        return {}
    daily = px.get("interval") == "1d"
    per_year = 252 if daily else 52
    last = c[-1]
    out = {"last close": last, "as of": d[-1], f"return since {d[0]}": last / c[0] - 1 if c[0] else None}

    def ret(n):
        return last / c[-n - 1] - 1 if len(c) > n and c[-n - 1] else None
    for name, n in (("1 month", 21), ("6 months", 126), ("1 year", 252), ("5 years", 1260)) if daily else \
            (("1 year", 52), ("5 years", 260), ("10 years", 520)):
        v = ret(n)
        if v is not None:
            out[f"return {name}"] = v
    years = len(c) / per_year
    if years >= 1 and c[0] > 0:
        out[f"annualised return ({d[0]} to {d[-1]})"] = (last / c[0]) ** (1 / years) - 1
    rets = [math.log(b / a) for a, b in zip(c, c[1:]) if a and b and a > 0 and b > 0]
    if len(rets) > 20:
        out["annualised volatility"] = statistics.pstdev(rets[-per_year:]) * math.sqrt(per_year)
    peak, mdd, mdd_at = c[0], 0.0, d[0]
    for price, day in zip(c, d):
        peak = max(peak, price)
        dd = price / peak - 1
        if dd < mdd:
            mdd, mdd_at = dd, day
    out["max drawdown"] = mdd
    out["max drawdown low on"] = mdd_at
    window = c[-per_year:]
    out["52-week high"], out["52-week low"] = max(window), min(window)
    if daily and len(c) >= 200:
        out["50-day average"] = sum(c[-50:]) / 50
        out["200-day average"] = sum(c[-200:]) / 200
    return out


# ==============================================================================
# The economy: US Treasury, BLS, World Bank, ECB; FRED with a key
# ==============================================================================
BLS_SERIES = {"cpi": ("CUUR0000SA0", "Consumer Price Index (all urban, not seasonally adjusted)"),
              "core cpi": ("CUUR0000SA0L1E", "CPI less food and energy"),
              "unemployment": ("LNS14000000", "Unemployment rate (%)"),
              "payrolls": ("CES0000000001", "Nonfarm payrolls (thousands)")}
WORLD_BANK = {"gdp growth": ("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)"), "inflation": ("FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)"),
              "gdp": ("NY.GDP.MKTP.CD", "GDP (current US$)"), "debt": ("GC.DOD.TOTL.GD.ZS", "Central government debt (% of GDP)")}


def treasury_yields() -> dict:
    year = datetime.now().year

    def load():
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
        rows = list(csv.DictReader(io.StringIO(_get(url).text)))
        if not rows:
            raise DataError("The Treasury yield curve returned no rows.")
        latest = rows[0]
        return {"date": latest.get("Date"), "yields": {k: float(v) for k, v in latest.items() if k != "Date" and v},
                "source": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"}
    return _cached("ust", 3 * 3600, load)


def bls(series: str) -> dict:
    sid, name = BLS_SERIES[series]

    def load():
        data = _get(f"https://api.bls.gov/publicAPI/v1/timeseries/data/{sid}").json()
        pts = ((data.get("Results") or {}).get("series") or [{}])[0].get("data") or []
        pts = [p for p in pts if p.get("period", "").startswith("M") and p.get("period") != "M13"]
        if not pts:
            raise DataError("BLS returned no data.")
        latest = pts[0]
        year_ago = next((p for p in pts if p["year"] == str(int(latest["year"]) - 1) and p["period"] == latest["period"]), None)
        out = {"series": name, "latest": float(latest["value"]), "period": f"{latest['periodName']} {latest['year']}",
               "source": f"https://data.bls.gov/timeseries/{sid}"}
        if year_ago and series in ("cpi", "core cpi"):
            out["change over 12 months"] = float(latest["value"]) / float(year_ago["value"]) - 1
        return out
    return _cached(f"bls:{series}", 6 * 3600, load)


def world_bank(indicator: str, country: str = "US") -> dict:
    code, name = WORLD_BANK[indicator]

    def load():
        data = _get(f"https://api.worldbank.org/v2/country/{quote(country)}/indicator/{code}?format=json&per_page=8").json()
        pts = [p for p in (data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []) if p.get("value") is not None]
        if not pts:
            raise DataError("The World Bank returned no data.")
        return {"series": f"{name} - {pts[0]['country']['value']}", "points": [(p["date"], p["value"]) for p in pts[:6]],
                "source": f"https://data.worldbank.org/indicator/{code}?locations={country}"}
    return _cached(f"wb:{indicator}:{country}", 24 * 3600, load)


def fx(currency: str) -> dict:
    cur = currency.upper()

    def load():
        text = _get(f"https://data-api.ecb.europa.eu/service/data/EXR/D.{quote(cur)}.EUR.SP00.A?lastNObservations=1&format=csvdata").text
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise DataError(f"The ECB has no rate for {cur}.")
        return {"series": f"{cur} per 1 EUR (ECB reference rate)", "latest": float(rows[-1]["OBS_VALUE"]),
                "date": rows[-1]["TIME_PERIOD"], "source": "https://data.ecb.europa.eu/"}
    return _cached(f"fx:{cur}", 6 * 3600, load)


def fred(series_id: str) -> dict:
    key = _key("fred")
    if not key:
        raise DataError("FRED needs a free API key (Settings > Web Search > Finance keys).")

    def load():
        data = _get(f"https://api.stlouisfed.org/fred/series/observations?series_id={quote(series_id)}&api_key={quote(key)}"
                    "&file_type=json&sort_order=desc&limit=13").json()
        obs = [o for o in data.get("observations") or [] if o.get("value") not in (".", None)]
        if not obs:
            raise DataError(f"FRED has no data for {series_id}.")
        return {"series": series_id, "latest": float(obs[0]["value"]), "date": obs[0]["date"],
                "year ago": float(obs[-1]["value"]) if len(obs) > 12 else None,
                "source": f"https://fred.stlouisfed.org/series/{series_id}"}
    return _cached(f"fred:{series_id}", 6 * 3600, load)


# ==============================================================================
# News headlines and (with keys) estimates
# ==============================================================================
def headlines(ticker: str, limit: int = 10, name: str = "") -> list:
    """[{"title", "url", "date", "kind"}] from Yahoo and Nasdaq feeds, only the ones about the company
    (the feeds mix in market chatter). kind: REPORTED unless the link or the headline says otherwise."""
    import xml.etree.ElementTree as ET
    words = {ticker.lower()} | {w.lower() for w in re.findall(r"[A-Za-z]{3,}", name or "")
                                if w.lower() not in ("inc", "corp", "corporation", "company", "holdings", "group", "the", "ltd", "plc", "class")}
    out, seen = [], set()
    for url in (f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US",
                f"https://www.nasdaq.com/feed/rssoutbound?symbol={quote(ticker)}"):
        try:
            root = ET.fromstring(_get(url, timeout=15).content)
        except (DataError, ET.ParseError):
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link or title.lower() in seen:
                continue
            if not any(re.search(rf"\b{re.escape(w)}\b", title.lower()) for w in words):
                continue                               # about the market, not this company
            seen.add(title.lower())
            out.append({"title": html_unescape(title), "url": link, "date": (item.findtext("pubDate") or "")[:25],
                        "kind": classify(link, title)})
    return out[:limit]


def estimates(ticker: str) -> dict:
    """Analyst figures (ESTIMATE) - only with an Alpha Vantage or Finnhub key."""
    av = _key("alphavantage")
    if av:
        d = _get(f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={quote(ticker)}&apikey={quote(av)}").json()
        if d.get("Symbol"):
            keep = ("AnalystTargetPrice", "AnalystRatingStrongBuy", "AnalystRatingBuy", "AnalystRatingHold",
                    "AnalystRatingSell", "AnalystRatingStrongSell", "ForwardPE", "PEGRatio", "TrailingPE")
            return {"provider": "Alpha Vantage", "values": {k: d.get(k) for k in keep if d.get(k) not in (None, "None", "-")},
                    "source": f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={ticker}"}
    fh = _key("finnhub")
    if fh:
        rec = _get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={quote(ticker)}&token={quote(fh)}").json()
        if isinstance(rec, list) and rec:
            r = rec[0]
            return {"provider": "Finnhub", "values": {k: r.get(k) for k in ("period", "strongBuy", "buy", "hold", "sell", "strongSell")},
                    "source": f"https://finnhub.io/docs/api/recommendation-trends"}
    raise DataError("Analyst estimates need an Alpha Vantage or Finnhub key (Settings > Web Search > Finance keys).")


# ==============================================================================
# Formatting and the market_data tool
# ==============================================================================
def fmt_money(v, cur: str = "USD") -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    sym = "$" if cur == "USD" else ""
    tail = "" if cur == "USD" else f" {cur}"
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            return f"{sign}{sym}{a / div:,.2f}{suf}{tail}"
    return f"{sign}{sym}{a:,.2f}{tail}"


def pct(v, signed: bool = True) -> str:
    if v is not None and not signed:
        return f"{v * 100:.1f}%"
    return "n/a" if v is None else f"{v * 100:+.1f}%" if abs(v) < 10 else f"{v * 100:+,.0f}%"


def _reg():
    import sources
    return sources.current()


def _cite(url: str, title: str, snippet: str = "", date: str = "") -> int:
    return _reg().add(url, title=title, snippet=snippet, date=date, read=True, note="finance data")


def company_brief(query: str, rng: str = "5y", include_news: bool = True) -> tuple[str, dict]:
    """Everything the free sources say about one company, as labelled lines citing [n].
    Returns (text, data)."""
    co = resolve(query)
    if not co:
        raise DataError(f"Couldn't find a listed company or ticker for '{query}'.")
    lines, data = [f"## {co['name']} ({co['ticker']}{', ' + co['exchange'] if co.get('exchange') else ''})"], {"company": co}
    px = None
    try:
        px = prices(co["ticker"], rng)
        st = price_stats(px)
        n = _cite(px["source"], f"{co['ticker']} price history - Yahoo Finance", date=px["dates"][-1] if px["dates"] else "")
        cur = px.get("currency") or "USD"
        data["prices"] = st
        lines.append(f"[MARKET] Last close {fmt_money(st.get('last close'), cur)} on {st.get('as of')} [{n}]")
        lines.append("[COMPUTED from the price history] " + "; ".join(
            f"{k} {pct(v, 'volatility' not in k) if 'return' in k or 'volatility' in k or k == 'max drawdown' else fmt_money(v, cur) if isinstance(v, (int, float)) else v}"
            for k, v in st.items() if k not in ("last close", "as of")) + f" [{n}]")
    except (DataError, netlayer.RouteBlocked) as e:
        lines.append(f"(Prices unavailable: {e})")
    if co.get("cik"):
        try:
            f = fundamentals(co["cik"])
            data["fundamentals"] = f
            n = _cite(f["source"], f"{f['name']} - SEC filings (10-K company facts)")
            per_share = {name for name, _, unit in METRICS if unit == "USD/shares"}
            for name, rows in f["metrics"].items():
                vals = ", ".join(f"FY{y} {v:,.2f}" if name in per_share else f"FY{y} {fmt_money(v)}" for y, v, *_ in rows)
                note = " (as filed - years before a stock split are not adjusted)" if name in per_share else ""
                lines.append(f"[FACT] {name}: {vals}{note} [{n}]")
            if f.get("shares"):
                lines.append(f"[FACT] Shares outstanding: {f['shares'][0]:,.0f} (as of {f['shares'][1]}) [{n}]")
            comp = dict(f.get("computed") or {})
            if px and f.get("shares") and st.get("last close"):
                mcap = st["last close"] * f["shares"][0]
                comp["Market cap (last close x shares)"] = mcap
                eps = f["metrics"].get("EPS (diluted)")
                if eps and eps[0][1] and eps[0][1] > 0:
                    comp[f"P/E (last close / FY{eps[0][0]} diluted EPS)"] = st["last close"] / eps[0][1]
                rev = f["metrics"].get("Revenue")
                if rev and rev[0][1]:
                    comp[f"Price / sales (FY{rev[0][0]})"] = mcap / rev[0][1]
                if comp.get("Free cash flow"):
                    comp["Free-cash-flow yield"] = comp["Free cash flow"] / mcap
            if comp:
                lines.append("[COMPUTED from the filings" + (" and the last close" if px else "") + "] " + "; ".join(
                    f"{k} {pct(v) if isinstance(v, float) and abs(v) < 5 and 'P/E' not in k and 'sales' not in k and 'Liabilities' not in k else fmt_money(v) if abs(v) > 1e5 else f'{v:,.2f}'}"
                    for k, v in comp.items()) + f" [{n}]")
            fl = filings(co["cik"], forms=MAJOR_FORMS, limit=6)        # not the stream of insider Form 4s
            data["filings"] = fl
            if fl:
                parts = []
                for x in fl:
                    k = _cite(x["url"], f"{co['ticker']} {x['form']} filed {x['date']}", date=x["date"])
                    parts.append(f"{x['form']} {x['date']} [{k}]")
                lines.append("[FACT] Recent filings: " + ", ".join(parts))
        except (DataError, netlayer.RouteBlocked) as e:
            lines.append(f"(SEC data unavailable: {e})")
    else:
        lines.append("(Not an SEC filer - no US filings; figures from other sources only.)")
    try:
        est = estimates(co["ticker"])
        n = _cite(est["source"], f"{co['ticker']} analyst estimates - {est['provider']}")
        lines.append(f"[ESTIMATE - analysts' expectations, may not happen] " + "; ".join(f"{k} {v}" for k, v in est["values"].items()) + f" [{n}]")
    except (DataError, netlayer.RouteBlocked):
        pass
    if include_news:
        try:
            news = headlines(co["ticker"], 8, co.get("name") or "")
            data["news"] = news
            for h in news:
                n = _cite(h["url"], h["title"], date=h["date"])
                lines.append(f"[{h['kind']}] {h['title']} ({h['date'][:16]}) [{n}]")
        except (DataError, netlayer.RouteBlocked):
            pass
    return "\n".join(lines), data


def macro_brief() -> str:
    lines = ["## The economy (latest official figures)"]
    for fn, args in ((treasury_yields, ()), (bls, ("cpi",)), (bls, ("core cpi",)), (bls, ("unemployment",)),
                     (world_bank, ("gdp growth",))):
        try:
            d = fn(*args)
        except (DataError, netlayer.RouteBlocked, KeyError):
            continue
        n = _cite(d["source"], d.get("series") or "US Treasury daily yield curve", date=str(d.get("date") or d.get("period") or ""))
        if fn is treasury_yields:
            y = d["yields"]
            lines.append(f"[FACT] Treasury yields on {d['date']}: " + ", ".join(f"{k} {v:.2f}%" for k, v in y.items()
                                                                               if k in ("3 Mo", "2 Yr", "10 Yr", "30 Yr")) + f" [{n}]")
        elif "points" in d:
            lines.append(f"[FACT] {d['series']}: " + ", ".join(f"{a} {b:.1f}" for a, b in d["points"][:4]) + f" [{n}]")
        else:
            extra = f", {pct(d['change over 12 months'])} over 12 months" if d.get("change over 12 months") is not None else ""
            lines.append(f"[FACT] {d['series']}: {d['latest']:,} ({d['period']}{extra}) [{n}]")
    return "\n".join(lines) if len(lines) > 1 else ""


LEGEND = ("Evidence labels: " + "; ".join(f"{k} = {v}" for k, v in EVIDENCE.items()) +
          ". Treat FACT, MARKET and COMPUTED as what happened; ESTIMATE as what someone expects; OPINION and "
          "SPECULATION as views to weigh, never as facts.")


def market_data_tool(args: dict) -> dict:
    """The model's market_data tool. action: company | prices | filings | macro | fx | series."""
    import search_engine
    mode = search_engine.turn_mode()
    if mode in ("local", "disabled"):
        return {"ok": False, "status": "permanent_failure",
                "message": f"Market data needs the network, and the Web Search mode is {search_engine.MODE_INFO[mode]['label']}."}
    action = str(args.get("action") or "company").lower()
    symbol = str(args.get("symbol") or args.get("query") or "").strip()
    try:
        with search_engine.scope(mode):
            if action in ("company", "fundamentals", "quote"):
                if not symbol:
                    return {"ok": False, "message": "Give a ticker or company name as symbol."}
                text, _ = company_brief(symbol, str(args.get("range") or "5y"), include_news=bool(args.get("news", True)))
            elif action == "prices":
                co = resolve(symbol) or {"ticker": symbol}
                rng = str(args.get("range") or "2y")
                rng = {"1mo": "3mo", "6mo": "1y", "1y": "2y"}.get(rng, rng)    # a little more, so "return 1 year" etc. exist
                px = prices(co["ticker"], rng)
                st = price_stats(px)
                n = _cite(px["source"], f"{px['ticker']} price history - Yahoo Finance")
                text = f"[MARKET/COMPUTED] {px['ticker']} ({px.get('currency')}): " + "; ".join(
                    f"{k} {pct(v, 'volatility' not in k) if 'return' in k or 'volatility' in k or k == 'max drawdown' else (f'{v:,.2f}' if isinstance(v, float) else v)}"
                    for k, v in st.items()) + f" [{n}]"
            elif action == "filings":
                co = resolve(symbol)
                if not co or not co.get("cik"):
                    return {"ok": False, "message": f"{symbol} isn't an SEC filer."}
                parts = [f"{x['form']} {x['date']} - {x['title']} [{_cite(x['url'], co['ticker'] + ' ' + x['form'] + ' ' + x['date'], date=x['date'])}]"
                         for x in filings(co["cik"], limit=int(args.get("limit") or 12))]
                text = f"[FACT] {co['name']} recent SEC filings:\n" + "\n".join(parts)
            elif action == "macro":
                text = macro_brief() or "No macro data could be read right now."
            elif action == "fx":
                d = fx(symbol or "USD")
                text = f"[FACT] {d['series']}: {d['latest']} on {d['date']} [{_cite(d['source'], d['series'], date=d['date'])}]"
            elif action == "series":
                d = fred(symbol)
                text = (f"[FACT] FRED {d['series']}: {d['latest']} on {d['date']}" + (f" (a year earlier {d['year ago']})" if d.get("year ago") is not None else "")
                        + f" [{_cite(d['source'], 'FRED ' + d['series'], date=d['date'])}]")
            else:
                return {"ok": False, "message": "action must be company, prices, filings, macro, fx or series."}
    except netlayer.RouteBlocked as e:
        return {"ok": False, "status": "permanent_failure", "blocked": True, "message": str(e)}
    except DataError as e:
        return {"ok": False, "status": "retryable", "message": str(e)}
    return {"ok": True, "message": f"{text}\n\n{LEGEND} Cite figures as [n]. This is data, not advice."}
