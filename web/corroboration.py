"""
corroboration.py
================
How well a claim is confirmed, counted in independent ORIGINS rather than links: five sites carrying the
same Reuters story are one source, not five, and a claim only one outlet reports is a possibility until
someone else confirms it. Used by

  * Research (agent/research.py): the findings the report writer sees are tagged {single source} /
    {2 independent} / {confirmed: N independent}, and every report ends with "How well confirmed";
  * the "Claim timelines" composer tool (agent/tasks._with_timelines): who reported each claim first and
    who confirmed it later - research reports and sourced answers alike;
  * the sources saved with a reply (agent/sessions.sources_for): each one's origin.

Independence is decided without a model, from what the source registry (sources.py) already holds:
  * one site: subdomains and a brand's other domains are one origin (edition.cnn.com; bbc.com and bbc.co.uk)
  * wire copies: a page carrying a wire story ("(Reuters) -", "Associated Press", "(AFP)") counts as that
    wire, so do the wire's own sites
  * copied text: two sources whose passages mostly match (shared five-word runs) are one origin
A source can't be shown to be independent - only to be a copy - so the counts are upper bounds of
independence, said as "independent sources" and never as proof.
"""
from __future__ import annotations

import re
from datetime import datetime

# Second-level suffixes: bbc.co.uk is the site "bbc.co.uk", not "co.uk".
_MULTI = {"co.uk", "org.uk", "gov.uk", "ac.uk", "com.au", "net.au", "org.au", "gov.au", "co.il", "org.il", "gov.il",
          "ac.il", "co.jp", "or.jp", "ne.jp", "com.br", "co.in", "co.nz", "com.cn", "com.tr", "co.za", "com.mx",
          "com.ar", "com.sg", "com.hk", "co.kr", "com.tw", "com.ua", "co.id", "com.my", "com.eg", "com.sa"}
# One newsroom under several domains (not merely one owner: separate newsrooms of one company are separate).
FAMILIES = {
    "bbc.co.uk": "BBC", "bbc.com": "BBC", "theguardian.com": "The Guardian", "guardian.co.uk": "The Guardian",
    "haaretz.com": "Haaretz", "haaretz.co.il": "Haaretz", "ynet.co.il": "Ynet", "ynetnews.com": "Ynet",
    "aljazeera.com": "Al Jazeera", "aljazeera.net": "Al Jazeera", "nbcnews.com": "NBC News", "msnbc.com": "NBC News",
    "dw.com": "Deutsche Welle", "dw.de": "Deutsche Welle", "france24.com": "France 24", "rfi.fr": "RFI",
    "kan.org.il": "Kan", "kanisrael.co.il": "Kan", "timesofisrael.com": "The Times of Israel",
    "jpost.com": "The Jerusalem Post", "i24news.tv": "i24NEWS",
}
WIRES = {"reuters.com": "Reuters", "apnews.com": "AP", "ap.org": "AP", "afp.com": "AFP"}
# A wire's dateline or credit in the page's own text: the story is the wire's, wherever it is published.
_WIRE_TEXT = (
    (re.compile(r"\(Reuters\)|\bReuters\s+[-–—]|\bReporting by\b[^.]{0,120}\bReuters\b", re.I), "Reuters"),
    (re.compile(r"\(AP\)|\bThe Associated Press\b|\bAssociated Press\b", re.I), "AP"),
    (re.compile(r"\(AFP\)|\bAgence France-Presse\b|\bAFP\s+[-–—]", re.I), "AFP"),
)
_WORD = re.compile(r"[a-z0-9֐-׿؀-ۿ]+")
_CITES = re.compile(r"\[(\d{1,3}(?:\s*(?:,|–|-)\s*\d{1,3})*)(?:\s*·[^\]]*)?\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z֐-׿\"'(])")
COPY_OVERLAP = 0.5        # share of five-word runs two passages must have in common to be one text


def site(domain: str) -> str:
    """The registrable site of a host: edition.cnn.com -> cnn.com, www.bbc.co.uk -> bbc.co.uk."""
    parts = [p for p in str(domain or "").lower().split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _text(item: dict) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title", "snippet", "excerpt"))


def origin_of(item: dict) -> str:
    """The origin one source counts as (before copied text is compared)."""
    home = site(item.get("domain") or "")
    if home in WIRES:
        return WIRES[home]
    text = _text(item)
    for pattern, wire in _WIRE_TEXT:
        if pattern.search(text):
            return wire
    return FAMILIES.get(home, home or "unknown")


def _shingles(item: dict) -> set:
    words = _WORD.findall(str(item.get("excerpt") or item.get("snippet") or "").lower())
    return {" ".join(words[i:i + 5]) for i in range(len(words) - 4)}


def origins(items: list) -> dict:
    """{source number: origin label} - wire copies and copied passages joined under one origin."""
    label = {it["n"]: origin_of(it) for it in items}
    shingles = {it["n"]: _shingles(it) for it in items}
    parent = {n: n for n in label}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    numbers = sorted(label)
    by_origin: dict = {}
    for n in numbers:                                       # same named origin: one group
        first = by_origin.setdefault(label[n], n)
        parent[find(n)] = find(first)
    for i, a in enumerate(numbers):                         # copied text: one group
        for b in numbers[i + 1:]:
            sa, sb = shingles[a], shingles[b]
            if len(sa) >= 8 and len(sb) >= 8 and len(sa & sb) / min(len(sa), len(sb)) >= COPY_OVERLAP:
                parent[find(b)] = find(a)
    # A group is named after its wire if it has one, else after its earliest source.
    wires, names = set(WIRES.values()), {}
    for n in numbers:
        root = find(n)
        if root not in names or (label[n] in wires and names[root] not in wires):
            names[root] = label[n]
    return {n: names[find(n)] for n in numbers}


def cited(text: str) -> list:
    """Source numbers cited in a piece of text: [3], [2, 5], [4-6], [3 · news site · trust: high]."""
    out = []
    for m in _CITES.finditer(str(text or "")):
        for part in re.split(r"\s*,\s*", m.group(1)):
            bounds = re.split(r"\s*[–-]\s*", part)
            try:
                lo, hi = int(bounds[0]), int(bounds[-1])
            except ValueError:
                continue
            for n in range(lo, min(hi, lo + 20) + 1):
                if n not in out:
                    out.append(n)
    return out


def assess(numbers, items: list) -> dict:
    """{sources, independent, origins, level, label} for the source numbers one claim cites."""
    by_n = {it["n"]: it for it in items}
    nums = [n for n in numbers if n in by_n]
    groups = origins([by_n[n] for n in nums]) if nums else {}
    distinct = list(dict.fromkeys(groups[n] for n in nums))
    k, m = len(distinct), len(nums)
    if k == 0:
        level, text = "none", ""
    elif k == 1:
        level = "single"
        text = f"single source ({distinct[0]})" if m == 1 else f"one origin ({distinct[0]}, carried by {m} sites)"
    elif k == 2:
        level, text = "two", "2 independent sources"
    else:
        level, text = "confirmed", f"confirmed by {k} independent sources"
    return {"sources": m, "independent": k, "origins": distinct, "level": level, "label": text}


def tag(assessment: dict) -> str:
    """The short tag a finding carries for the report writer."""
    return {"single": "{single source}", "two": "{2 independent}"}.get(
        assessment["level"], f"{{confirmed: {assessment['independent']} independent}}" if assessment["level"] == "confirmed" else "")


def annotate(findings: str, items: list) -> str:
    """Research findings with each cited bullet tagged by how well it is confirmed."""
    out = []
    for line in str(findings or "").splitlines():
        nums = cited(line) if re.match(r"\s*(?:[-*•]|\d+[.)])\s+", line) else []
        t = tag(assess(nums, items)) if nums else ""
        out.append(f"{line.rstrip()} {t}" if t else line)
    return "\n".join(out)


def claims(text: str) -> list:
    """[(claim, [numbers])]: every bullet or sentence of a reply that cites sources, headings skipped."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", ">")):
            continue
        if re.match(r"(?:[-*•]|\d+[.)])\s+", line):
            units = [re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)]
        else:
            units = _SENTENCE.split(line)
        for unit in units:
            nums = cited(unit)
            if nums:
                clean = re.sub(r"\s+", " ", _CITES.sub("", unit)).strip(" .;:-")
                clean = re.sub(r"\*\*|__|`", "", clean)
                if len(clean) >= 12:
                    out.append((clean, nums))
    return out


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1].rsplit(" ", 1)[0] + "…"


def report_section(report: str, items: list, most_single: int = 8) -> str:
    """"How well confirmed" for a research report: how many of its cited claims rest on how many independent
    origins, and the single-source ones - the possibilities, not the established facts."""
    rated = [(c, assess(nums, items)) for c, nums in claims(report)]
    rated = [(c, a) for c, a in rated if a["level"] != "none"]
    if not rated:
        return ""
    count = {lvl: sum(1 for _c, a in rated if a["level"] == lvl) for lvl in ("confirmed", "two", "single")}
    lines = ["## How well confirmed",
             "Counted in independent origins: several sites carrying the same wire story or the same text count once.",
             f"- **Confirmed by 3 or more independent sources:** {count['confirmed']} claim{'s' if count['confirmed'] != 1 else ''}",
             f"- **Two independent sources:** {count['two']} claim{'s' if count['two'] != 1 else ''}",
             f"- **One source only** - treat as possible, not established: {count['single']} claim{'s' if count['single'] != 1 else ''}"]
    singles = []
    for c, a in rated:
        if a["level"] == "single" and c not in singles:
            singles.append(c)
            lines.append(f"  - {_short(c, 150)} - only {a['origins'][0]}")
            if len(singles) >= most_single:
                break
    return "\n".join(lines)


def _day(value) -> str:
    """A source's date as YYYY-MM-DD, or "" when it has none that can be read."""
    text = str(value or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[:40], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def timelines(text: str, items: list, most: int = 6) -> str:
    """"Claim timelines": for the claims backed by the most sources, who reported each first and who
    confirmed it later, in date order (sources without a readable date last). "" when nothing is cited."""
    by_n = {it["n"]: it for it in items}
    picked, seen = [], set()
    for c, nums in sorted(claims(text), key=lambda cn: -len({n for n in cn[1] if n in by_n})):
        nums = [n for n in nums if n in by_n]
        key = frozenset(nums)
        if not nums or key in seen:
            continue
        seen.add(key)
        picked.append((c, nums))
        if len(picked) >= most:
            break
    if not picked:
        return ""
    lines = ["## Claim timelines", "Who reported each claim first, and who confirmed it later - dates as the sources give them."]
    for c, nums in picked:
        groups = origins([by_n[n] for n in nums])
        dated = sorted(nums, key=lambda n: (_day(by_n[n].get("date")) or "9999", n))
        lines.append(f"\n**{_short(c, 140)}**")
        seen_origins = []
        for n in dated:
            it, when = by_n[n], _day(by_n[n].get("date"))
            who = site(it.get("domain") or "") or it.get("domain") or "?"
            if not seen_origins:
                role = "first reported"
            elif groups[n] in seen_origins:
                role = f"repeats {groups[n]} - not independent"
            else:
                role = "confirmed independently"
            seen_origins.append(groups[n])
            lines.append(f"- {when or 'no date'} · {who} [{n}] - {role}")
    return "\n".join(lines)


def origin_labels(items: list) -> dict:
    """{n: origin} for the sources shown under a reply, only where the origin isn't the site itself
    (a Yahoo page carrying a Reuters story -> "Reuters"; reuters.com and bbc.co.uk get none)."""
    groups = origins(items)
    own = lambda it: WIRES.get(site(it.get("domain") or "")) or FAMILIES.get(site(it.get("domain") or "")) or site(it.get("domain") or "")  # noqa: E731
    return {it["n"]: groups[it["n"]] for it in items if groups.get(it["n"]) and groups[it["n"]] != own(it)}
