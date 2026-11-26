"""Small pure helpers behind Kon's "nobody else has this" touches (no Discord objects in here, so they are easy to test).

* spark()            ▁▃▅▇█ mini graph of recent numbers      ($pulse latency graph, $aura memory)
* streak()           how many readings in a row have the same sign
* closest()          "did you mean ...?" for a mistyped command
* search_commands()  `$help <word>` finds commands by name, old name, alias or description
"""
import difflib
import re
from typing import Iterable, Optional, Sequence

BLOCKS = "▁▂▃▄▅▆▇█"


def spark(values: Sequence[float], lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    """Numbers -> one character each. lo/hi fix the scale (aura: -100..100); without them the data's own range is used."""
    vals = [float(v) for v in values]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1e-9:
        return BLOCKS[3] * len(vals)
    out = []
    for v in vals:
        t = (min(max(v, lo), hi) - lo) / (hi - lo)
        out.append(BLOCKS[min(len(BLOCKS) - 1, int(t * len(BLOCKS)))])
    return "".join(out)


def streak(values: Sequence[float]) -> tuple:
    """values are newest-first. -> (+1 / -1 / 0, how many in a row from the newest one)."""
    if not values:
        return 0, 0
    sign = lambda v: 1 if v > 0 else -1 if v < 0 else 0
    first, n = sign(values[0]), 0
    for v in values:
        if sign(v) != first:
            break
        n += 1
    return first, n


def closest(word: str, names: Iterable[str], n: int = 2) -> list:
    """Best matches for a mistyped command: typos (difflib) first, then names that start with what was typed."""
    word = word.lower().strip()
    pool = sorted({x.lower() for x in names if x})
    if len(word) < 4 or not word.isalpha():
        return []
    out = difflib.get_close_matches(word, pool, n=n, cutoff=0.78)
    for x in pool:                                       # swapped letters: "lokc" -> "lock"
        if len(out) < n and x not in out and len(x) == len(word) and sorted(x) == sorted(word):
            out.append(x)
    if len(out) < n and len(word) >= 4:
        out += [x for x in pool if x.startswith(word) and x not in out][: n - len(out)]
    return out[:n]


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def search_commands(query: str, entries: Iterable[tuple], limit: int = 8) -> list:
    """entries: (key, [names / aliases], description). Returns keys, best first.
    name == word 100, name starts with 60, word in description 30, close typo 25, name contains 20."""
    q = _words(query)
    if not q:
        return []
    scored = []
    for key, names, desc in entries:
        names = [x.lower() for x in names]
        dwords = set(_words(desc))
        total = 0
        for w in q:
            best = 0
            for nm in names:
                if nm == w:
                    best = max(best, 100)
                elif nm.startswith(w):
                    best = max(best, 60)
                elif len(w) >= 3 and w in nm:
                    best = max(best, 20)
                elif len(w) >= 4 and difflib.SequenceMatcher(None, w, nm).ratio() >= 0.8:
                    best = max(best, 25)
            if w in dwords:
                best = max(best, 30)
            elif len(w) >= 4 and any(d.startswith(w) for d in dwords):
                best = max(best, 18)
            if best == 0:
                total = 0
                break                                      # every typed word must match something
            total += best
        if total:
            scored.append((total, key))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [k for _, k in scored[:limit]]
