"""
patch_edit.py
=============
Multi-hunk text edits with line context and fuzzy anchor matching.

An edit is {"find": str, "replace": str, "line": int?, "before": str?, "after": str?}:
  find     the text to replace (a few lines of it is best)
  replace  what it becomes
  line     optional 1-based line hint - picks the nearest match when `find` occurs more than once
  before / after  optional context lines expected right above / below `find`

Each hunk is located in this order, and the first that yields a unique match wins:
  1. exact       - the text occurs verbatim
  2. whitespace  - line by line, ignoring indentation and trailing/inner spacing differences
  3. fuzzy       - the most similar window of lines (difflib ratio >= FUZZY_MIN), clearly
                   better than the runner-up
Ambiguous matches are never guessed: the hunk fails and says how to disambiguate.

dry_run() computes everything without touching the file (it feeds the approval
diff); apply_to_text() is all-or-nothing - if any hunk fails, nothing changes.
Line endings (CRLF / LF) of the original text are preserved.
"""
from __future__ import annotations

import difflib
import re

FUZZY_MIN = 0.86
FUZZY_MARGIN = 0.03


class PatchError(Exception):
    pass


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _lines(text: str) -> list:
    return text.split("\n")


def _offsets(lines: list) -> list:
    out, pos = [], 0
    for line in lines:
        out.append(pos)
        pos += len(line) + 1
    return out


def _context_ok(lines, start, end, before: str, after: str) -> bool:
    if before:
        want = [_norm(l) for l in _lines(before.strip("\n"))]
        have = [_norm(l) for l in lines[max(0, start - len(want)):start]]
        if have[-len(want):] != want:
            return False
    if after:
        want = [_norm(l) for l in _lines(after.strip("\n"))]
        have = [_norm(l) for l in lines[end:end + len(want)]]
        if have != want:
            return False
    return True


def _pick(cands: list, line_hint, what: str):
    """cands: list of (start_line, end_line, score). Unique or hint-resolved, else error."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    if line_hint:
        cands = sorted(cands, key=lambda c: abs(c[0] + 1 - int(line_hint)))
        if abs(cands[0][0] + 1 - int(line_hint)) < abs(cands[1][0] + 1 - int(line_hint)):
            return cands[0]
    starts = ", ".join(str(c[0] + 1) for c in cands[:6])
    raise PatchError(f"{what} matches {len(cands)} places (lines {starts}); add a 'line' hint or "
                     "'before'/'after' context to say which one.")


def locate(text: str, edit: dict) -> dict:
    """Where one hunk applies: {start, end (char offsets), line_start, line_end, method, score}."""
    find = edit.get("find")
    if not isinstance(find, str) or not find:
        raise PatchError("'find' must be non-empty text copied from the file.")
    lines = _lines(text)
    offs = _offsets(lines)
    find_lines = _lines(find.strip("\n")) if find.strip("\n") else [find]
    n = len(find_lines)
    before, after = edit.get("before") or "", edit.get("after") or ""
    try:
        hint = int(edit["line"]) if edit.get("line") not in (None, "") else None
    except (TypeError, ValueError):
        hint = None                                    # a bad hint is ignored, not fatal

    # 1. exact
    exact = []
    for m in re.finditer(re.escape(find), text):
        ls = text.count("\n", 0, m.start())
        # The last line the match touches (a trailing "\n" in `find` does not
        # reach into the next line - the 'after' context starts right there).
        le = text.count("\n", 0, max(m.start(), m.end() - 1)) + 1
        if _context_ok(lines, ls, le, before, after):
            exact.append((ls, le, 1.0, m.start(), m.end()))
    if exact:
        c = _pick([(e[0], e[1], e[2]) for e in exact], hint, "The 'find' text")
        e = next(x for x in exact if x[0] == c[0])
        return {"start": e[3], "end": e[4], "line_start": e[0] + 1, "line_end": e[1], "method": "exact", "score": 1.0}

    # 2. whitespace-insensitive, whole lines
    want = [_norm(l) for l in find_lines]
    ws = [(i, i + n, 1.0) for i in range(0, len(lines) - n + 1)
          if [_norm(l) for l in lines[i:i + n]] == want and _context_ok(lines, i, i + n, before, after)]
    c = _pick(ws, hint, "The 'find' text (ignoring spacing)")
    method = "whitespace"
    if c is None:
        # 3. fuzzy: windows of n-1..n+1 lines, scored on normalised text
        target = "\n".join(want)
        scored = []
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(0, len(lines) - size + 1):
                if not _context_ok(lines, i, i + size, before, after):
                    continue
                cand = "\n".join(_norm(l) for l in lines[i:i + size])
                r = difflib.SequenceMatcher(None, target, cand, autojunk=False).ratio()
                if r >= FUZZY_MIN:
                    scored.append((i, i + size, r))
        if not scored:
            raise PatchError("The 'find' text was not found (not even approximately). Read the file again and "
                             "copy the exact lines you want to change.")
        scored.sort(key=lambda s: -s[2])
        best = scored[0]
        rivals = [s for s in scored[1:] if not (s[0] < best[1] and best[0] < s[1])]   # non-overlapping
        if rivals and best[2] - rivals[0][2] < FUZZY_MARGIN:
            c = _pick([best, rivals[0]], hint, "The 'find' text (approximately)")
        else:
            c = best
        method = "fuzzy"
    start = offs[c[0]]
    end = offs[c[1] - 1] + len(lines[c[1] - 1])
    return {"start": start, "end": end, "line_start": c[0] + 1, "line_end": c[1], "method": method,
            "score": round(c[2], 3)}


def apply_to_text(text: str, edits: list) -> tuple[str, list]:
    """All hunks or none. Returns (new_text, matches). Raises PatchError."""
    if not isinstance(edits, list) or not edits:
        raise PatchError("'edits' must be a non-empty list of {find, replace} hunks.")
    # Pure CRLF files are edited as LF and converted back. A file with MIXED
    # endings is edited as-is (converting it back would make every line CRLF);
    # whitespace-insensitive matching still ignores the stray '\r's.
    crlf = "\r\n" in text and text.count("\n") == text.count("\r\n")
    work = text.replace("\r\n", "\n") if crlf else text
    located = []
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("replace"), str):
            raise PatchError(f"Hunk {i}: needs 'find' and 'replace' strings.")
        norm_edit = {k: (v.replace("\r\n", "\n") if isinstance(v, str) else v) for k, v in edit.items()}
        try:
            loc = locate(work, norm_edit)
        except PatchError as e:
            raise PatchError(f"Hunk {i}: {e}") from None
        replacement = norm_edit["replace"]
        if loc["method"] != "exact":
            # A non-exact match covers whole lines WITHOUT their final newline,
            # so a find/replace pair that both end (or start) with a newline
            # must not add one more.
            if norm_edit["find"].endswith("\n") and replacement.endswith("\n"):
                replacement = replacement[:-1]
            if norm_edit["find"].startswith("\n") and replacement.startswith("\n"):
                replacement = replacement[1:]
            # A whole-line match replaces whole lines. If the model's replacement
            # lost all indentation that the matched lines had, restore it line by
            # line (line i gets the indentation of matched line i, or the last one).
            orig = work[loc["start"]:loc["end"]].split("\n")
            rep = replacement.split("\n")
            indents = [l[:len(l) - len(l.lstrip())] for l in orig]
            if any(indents) and not any(l[:1].isspace() for l in rep if l):
                replacement = "\n".join((indents[min(j, len(indents) - 1)] + l) if l else l for j, l in enumerate(rep))
        located.append((loc, replacement, i))
    located.sort(key=lambda x: x[0]["start"])
    for (a, _, ia), (b, _, ib) in zip(located, located[1:]):
        if b["start"] < a["end"]:
            raise PatchError(f"Hunks {ia} and {ib} overlap (lines {a['line_start']}-{a['line_end']} and "
                             f"{b['line_start']}-{b['line_end']}); merge them into one hunk.")
    out = work
    for loc, replacement, _ in reversed(located):
        out = out[:loc["start"]] + replacement + out[loc["end"]:]
    if crlf:
        out = out.replace("\n", "\r\n")
    matches = [{"hunk": i, "lines": f"{loc['line_start']}-{loc['line_end']}", "method": loc["method"],
                "score": loc["score"]} for loc, _, i in located]
    return out, matches


def dry_run(text: str, edits: list, name: str = "file") -> dict:
    """What apply would do, without doing it: {ok, text, diff, matches} or {ok: False, error}."""
    try:
        new, matches = apply_to_text(text, edits)
    except PatchError as e:
        return {"ok": False, "error": str(e)}
    diff = "".join(l if l.endswith("\n") else l + "\n" for l in difflib.unified_diff(
        text.replace("\r\n", "\n").splitlines(keepends=True), new.replace("\r\n", "\n").splitlines(keepends=True),
        fromfile=f"{name} (now)", tofile=f"{name} (after)"))
    return {"ok": True, "text": new, "diff": diff or "(no change)\n", "matches": matches}


def describe_matches(matches: list) -> str:
    return "; ".join(f"hunk {m['hunk']} at lines {m['lines']}"
                     + ("" if m["method"] == "exact" else f" ({m['method']} match {round(m['score'] * 100)}%)")
                     for m in matches)
