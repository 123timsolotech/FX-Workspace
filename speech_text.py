"""
speech_text.py
==============
Text as a person would say it, before it is synthesised (voice.speak). Voice engines read
"$215.94B", "+65.5%", "FY2026", "e.g." or "2026-09-25" badly or letter by letter; the
commercial assistants normalise first, and so does this:

  money      $215.94B -> 215.94 billion dollars · €3.5M -> 3.5 million euros · $12 -> 12 dollars
  numbers    1,200,000 -> 1200000 (the voice reads it whole) · 45.9x -> 45.9 times · 5k -> 5 thousand
  signs      +65.5% -> plus 65.5 percent · -8.9% -> minus 8.9 percent · ~40 -> about 40
  dates      2026-09-25 -> September 25, 2026 · FY2026 -> fiscal 2026 · Q3 -> Q 3
  ranges     2022-2026 -> 2022 to 2026 · 3-5 -> 3 to 5
  units      GB, MB, km/h, °C, ms, tok/s ...
  words      e.g., i.e., vs., etc., approx., w/, &
  symbols    emoji, arrows, bullets and leftover markup are dropped; "->" becomes "to"

The page already removes Markdown, code, links and citations (index.html Voice.speechText);
this is the language-level half. English rules - other languages pass through mostly as is.
"""
from __future__ import annotations

import re

_SCALE = {"k": "thousand", "K": "thousand", "m": "million", "M": "million", "mn": "million", "b": "billion", "B": "billion",
          "bn": "billion", "t": "trillion", "T": "trillion", "tn": "trillion"}
_CUR = {"$": "dollars", "US$": "dollars", "€": "euros", "£": "pounds", "¥": "yen", "₪": "shekels", "₹": "rupees"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_UNITS = [(r"km/h", "kilometers per hour"), (r"mph", "miles per hour"), (r"tok/s", "tokens a second"),
          (r"t/s", "tokens a second"), (r"GB/s", "gigabytes a second"), (r"MB/s", "megabytes a second"),
          (r"TB", "terabytes"), (r"GB", "gigabytes"), (r"MB", "megabytes"), (r"KB", "kilobytes"),
          (r"GHz", "gigahertz"), (r"MHz", "megahertz"), (r"ms", "milliseconds"), (r"km", "kilometers"),
          (r"kg", "kilograms"), (r"mm", "millimeters"), (r"cm", "centimeters"), (r"°C", "degrees Celsius"),
          (r"°F", "degrees Fahrenheit"), (r"kWh", "kilowatt hours"), (r"W", "watts"), (r"fps", "frames per second")]
_WORDS = [(r"\be\.g\.,?", "for example,"), (r"\bi\.e\.,?", "that is,"), (r"\bvs\.?(?=\s)", "versus"),
          (r"\betc\.", "et cetera."), (r"\bapprox\.", "approximately"), (r"\bw/o\b", "without"), (r"\bw/(?=\s)", "with"),
          (r"\bno\.\s?(?=\d)", "number "), (r"\bDr\.(?=\s)", "Doctor"), (r"\bMr\.(?=\s)", "Mister"),
          (r"\bMrs\.(?=\s)", "Missus"), (r"\bSt\.(?=\s[A-Z])", "Saint"), (r"\bYoY\b", "year over year"),
          (r"\bQoQ\b", "quarter over quarter"), (r"\bYTD\b", "year to date"), (r"\bAI\b", "A.I."),
          (r"\bCEO\b", "C.E.O."), (r"\bGPU\b", "G.P.U."), (r"\bCPU\b", "C.P.U."), (r"\bAPI\b", "A.P.I."),
          (r"\bUI\b", "U.I."), (r"\bFAQ\b", "F.A.Q."), (r"\bETA\b", "E.T.A.")]
_NUM = r"\d+(?:\.\d+)?"


def _money(m: re.Match) -> str:
    sym, num, scale = m.group(1), m.group(2).replace(",", ""), m.group(3) or ""
    word = _CUR.get(sym, "dollars")
    scale_word = _SCALE.get(scale, "")
    if num in ("1", "1.0") and not scale_word:
        word = word[:-1] if word.endswith("s") else word
    return f"{num} {scale_word + ' ' if scale_word else ''}{word}"


def _date(m: re.Match) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{_MONTHS[mo - 1]} {d}, {y}"
    return m.group(0)


def normalize(text: str) -> str:
    t = str(text or "")
    # Symbols and leftovers that should never be read out.
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", " ", t)           # emoji
    t = t.replace("→", " to ").replace("->", " to ").replace("←", " from ").replace("•", " ").replace("…", "...")
    # Quarters: "Q3" is fine for most voices, "Q3FY24" isn't.
    t = re.sub(r"\bQ([1-4])(?=[A-Za-z])", r"Q\1 ", t)
    # Dates and fiscal periods.
    t = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", _date, t)
    t = re.sub(r"\bFY\s?'?(\d{2,4})\b", lambda m: f"fiscal {m.group(1)}", t)
    t = re.sub(r"\bH([12])\s?(\d{4})\b", lambda m: f"the {'first' if m.group(1) == '1' else 'second'} half of {m.group(2)}", t)
    # Money before plain numbers: $215.94B, US$ 3.5 bn, €12
    t = re.sub(r"(US\$|[$€£¥₪₹])\s?(\d[\d,]*(?:\.\d+)?)\s?(bn|mn|tn|[kKmMbBtT])?\b", _money, t)
    t = re.sub(rf"({_NUM})\s?(bn|mn|tn)\b", lambda m: f"{m.group(1)} {_SCALE[m.group(2)]}", t)
    # 5k, 3.5M, 2B - but not a name like Mistral-7B (the voice reads "7 B" there).
    t = re.sub(rf"(?<![\w-])({_NUM})([kKMBT])\b(?![a-zA-Z])", lambda m: f"{m.group(1)} {_SCALE[m.group(2)]}", t)
    # Signs, percentages, multiples, approximations. After "up", "fell"... the sign is already said.
    moved = r"(?:up|down|rose|fell|gained|lost|increased|decreased|dropped|climbed|grew|shrank|returned|by|of)"
    t = re.sub(rf"\b({moved})\s+[+−–-]\s?(?=\d)", r"\1 ", t, flags=re.I)
    t = re.sub(rf"(?<![\w.])\+({_NUM})", r"plus \1", t)
    t = re.sub(rf"(?<![\w.])[-−–]({_NUM})\s?%", r"minus \1 percent", t)
    t = re.sub(r"\b([A-Z])/([A-Z])\b", r"\1 \2", t)                       # P/E, P/S: spelled, not "or"
    t = re.sub(r"(\d)\s?%", r"\1 percent", t)
    t = re.sub(rf"\b({_NUM})\s?x\b", r"\1 times", t)
    t = re.sub(rf"~\s?({_NUM})", r"about \1", t)
    t = re.sub(r"\s&\s", " and ", t)
    # Thousands separators: the voice reads 1200000 as one number, "1,200,000" as three.
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: m.group(0).replace(",", ""), t)
    # Ranges: years and small numbers.
    t = re.sub(r"\b((?:19|20)\d{2})\s?[-–]\s?((?:19|20)\d{2})\b", r"\1 to \2", t)
    t = re.sub(r"(?<![\w.-])(\d{1,3})\s?[–-]\s?(\d{1,3})(?![\w.%-])", r"\1 to \2", t)
    # Units after a number.
    for unit, word in _UNITS:
        t = re.sub(rf"(\d)\s?{re.escape(unit) if '/' in unit or '°' in unit else unit}\b", rf"\1 {word}", t)
    for pat, word in _WORDS:
        t = re.sub(pat, word, t)
    # "this/that" -> "this or that" (after the units, so km/h and tok/s are already words).
    t = re.sub(r"\band/or\b", "and or", t)
    t = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " or ", t)
    return _tidy(t)


def _tidy(t: str) -> str:
    """No markup left, one space, no space before punctuation."""
    t = re.sub(r"[*_`#|<>\[\]{}^\\]", " ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    return t.strip()


def plain(text: str) -> str:
    """Other languages: the English wording rules don't apply - only what should never be read out goes."""
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", " ", str(text or ""))
    t = t.replace("→", " ").replace("->", " ").replace("←", " ").replace("•", " ").replace("…", "...")
    return _tidy(t)
