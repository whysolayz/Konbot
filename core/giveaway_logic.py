"""Giveaway logic that has nothing to do with Discord objects (so it is easy to test).

* default config + emoji / button defaults
* duration parsing ("1d 2h", "90m", "1w")
* requirement checks (roles, messages, invites, "say X to @person")
* winner picking
"""
import copy
import random
import re
import string
import time
from typing import Iterable, Optional

BRAND = 0xFFB832

UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.I)
MIN_DURATION = 10
MAX_DURATION = 90 * 86400
MAX_WINNERS = 20
MAX_TEMPLATES = 25
KEEP_ENDED = 60                       # ended giveaways that stay saved (for $giveaway reroll)
KEEP_ENDED_DAYS = 45

# ------------------------------------------------------------------ emojis + buttons
# key -> (label in the editor, default). Anything can be replaced with a unicode emoji, <:name:id>, <a:name:id> or a bare emoji ID.
EMOJI_FIELDS = {
    "title": ("Title emoji", "🎉"),
    "prize": ("Prize emoji", "🎁"),
    "winners": ("Winners emoji", "🏆"),
    "host": ("Host emoji", "👤"),
    "ends": ("Ends emoji", "⏰"),
    "req": ("Requirements emoji", "📋"),
    "entries": ("Entries emoji", "👥"),
    "done": ("Requirement done emoji", "✅"),
    "todo": ("Requirement missing emoji", "❌"),
    "ended": ("Ended emoji", "🔒"),
}
DEFAULT_EMOJIS = {k: v[1] for k, v in EMOJI_FIELDS.items()}

BUTTON_STYLES = {"blurple": "primary", "blue": "primary", "primary": "primary",
                 "green": "success", "success": "success",
                 "grey": "secondary", "gray": "secondary", "secondary": "secondary",
                 "red": "danger", "danger": "danger"}

DEFAULT_CFG = {
    "title": "GIVEAWAY",
    "prize": "",
    "description": "Click the button below to enter!",
    "winners": 1,
    "duration": 3600,
    "color": BRAND,
    "image": None,                    # {"url", "ref"} (core.media item)
    "thumbnail": None,
    "channel": None,                  # where the giveaway is posted (saved in templates too)
    "emojis": {},                     # key -> emoji string; missing = default, "" = hidden
    "buttons": {
        "join": {"label": "Join Giveaway", "emoji": "🎉", "style": "primary", "ended_label": "Giveaway Ended"},
        "entries": {"emoji": "👥", "show": True},
        "progress": {"label": "My Progress", "emoji": "📊", "show": True},
    },
    "prize_role": {"role": None, "mode": "permanent", "seconds": 7 * 86400},
    "points": 0,
    "req": {
        "roles": [], "roles_mode": "any",
        "messages": {"count": 0, "exclude": []},          # exclude = channel AND category ids
        "invites": 0,
        "task": {"enabled": False, "phrase": "", "target": None, "channel": None},
    },
}


def new_config() -> dict:
    return copy.deepcopy(DEFAULT_CFG)


def fill(base: dict, incoming: Optional[dict]) -> dict:
    """`incoming` on top of `base`; keys that are missing in `incoming` keep the default (templates of older versions)."""
    out = copy.deepcopy(base)
    if not isinstance(incoming, dict):
        return out
    for k, v in incoming.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict) and k not in ("emojis",):
            out[k] = fill(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def full_config(incoming: Optional[dict]) -> dict:
    return fill(DEFAULT_CFG, incoming)


def emoji_of(cfg: dict, key: str) -> str:
    """The emoji (as text) to show for `key`: the saved one, the default, or "" when it was hidden."""
    saved = (cfg.get("emojis") or {}).get(key)
    if saved is None:
        return DEFAULT_EMOJIS.get(key, "")
    return saved


# ------------------------------------------------------------------ time
def parse_duration(text: str) -> Optional[int]:
    """'1d 2h' -> seconds. A plain number means minutes. None when it can't be read."""
    t = (text or "").strip().lower().replace(",", " ").replace(" and ", " ")
    if not t:
        return None
    if re.fullmatch(r"\d+", t):
        return int(t) * 60
    total, rest = 0.0, t
    for m in _DUR.finditer(t):
        total += float(m.group(1)) * UNITS[m.group(2)[0]]
    rest = _DUR.sub(" ", t).strip()
    if rest or total <= 0:
        return None
    return int(total)


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{sec}s" if (sec or not (d or h or m)) else ""]
    return " ".join(p for p in parts if p)


# ------------------------------------------------------------------ requirements
def has_requirements(cfg: dict) -> bool:
    r = cfg.get("req") or {}
    t = r.get("task") or {}
    return bool(r.get("roles") or (r.get("messages") or {}).get("count") or r.get("invites")
                or (t.get("enabled") and t.get("phrase")))


def channel_excluded(exclude: Iterable[int], *ids: Optional[int]) -> bool:
    """True when the message's channel, its parent (thread) or its category is in the exclude list."""
    ex = {int(x) for x in exclude or ()}
    return any(i is not None and int(i) in ex for i in ids)


_PUNCT = str.maketrans({c: " " for c in string.punctuation})


def norm_text(text: str) -> str:
    return " ".join((text or "").lower().translate(_PUNCT).split())


def task_matches(task: dict, content: str, channel_ids: Iterable[Optional[int]], mention_ids: Iterable[int],
                 reply_author: Optional[int]) -> bool:
    """Did this message do the task? (phrase inside the text, optional person mentioned / replied to, optional channel)"""
    if not task.get("enabled") or not norm_text(task.get("phrase")):
        return False
    if norm_text(task["phrase"]) not in norm_text(content):
        return False
    if task.get("channel") and int(task["channel"]) not in {int(c) for c in channel_ids if c is not None}:
        return False
    target = task.get("target")
    if target:
        if int(target) not in {int(m) for m in mention_ids} and reply_author != int(target):
            return False
    return True


def evaluate(cfg: dict, rec: dict, uid: int, role_ids: set, valid_invites: int) -> list:
    """Every requirement of the giveaway for one member -> [{"key", "ok", "have", "need"}]."""
    req = cfg.get("req") or {}
    out = []
    roles = [int(r) for r in req.get("roles") or []]
    if roles:
        have = [r for r in roles if r in role_ids]
        ok = (len(have) == len(roles)) if req.get("roles_mode") == "all" else bool(have)
        out.append({"key": "roles", "ok": ok, "have": len(have), "need": len(roles) if req.get("roles_mode") == "all" else 1})
    need = int((req.get("messages") or {}).get("count") or 0)
    if need:
        have = int((rec.get("counts") or {}).get(str(uid), 0))
        out.append({"key": "messages", "ok": have >= need, "have": min(have, need), "need": need})
    need = int(req.get("invites") or 0)
    if need:
        out.append({"key": "invites", "ok": valid_invites >= need, "have": valid_invites, "need": need})
    task = req.get("task") or {}
    if task.get("enabled") and task.get("phrase"):
        done = int(uid) in {int(x) for x in rec.get("task") or []}
        out.append({"key": "task", "ok": done, "have": int(done), "need": 1})
    return out


def qualified(checks: list) -> bool:
    return all(c["ok"] for c in checks)


# ------------------------------------------------------------------ winners
_rng = random.SystemRandom()


def pick_winners(candidates: Iterable[int], n: int, exclude: Iterable[int] = (), rng=None) -> list:
    pool = [c for c in dict.fromkeys(candidates) if c not in set(exclude)]
    n = max(0, min(int(n), len(pool)))
    return (rng or _rng).sample(pool, n) if n else []


# ------------------------------------------------------------------ saved data
def fill_data(stored: Optional[dict]) -> dict:
    d = stored if isinstance(stored, dict) else {}
    d.setdefault("counter", 0)
    for k in ("templates", "active", "ended"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    if not isinstance(d.get("temp_roles"), list):
        d["temp_roles"] = []
    d.setdefault("last", None)
    return d


def prune_ended(ended: dict, now: Optional[float] = None) -> int:
    """Drop old ended giveaways (keeps the newest KEEP_ENDED, none older than KEEP_ENDED_DAYS). Returns how many were removed."""
    now = now or time.time()
    keep = sorted(ended.items(), key=lambda kv: kv[1].get("ended") or 0, reverse=True)
    drop = [k for n, (k, v) in enumerate(keep) if n >= KEEP_ENDED or now - (v.get("ended") or now) > KEEP_ENDED_DAYS * 86400]
    for k in drop:
        ended.pop(k, None)
    return len(drop)


# ------------------------------------------------------------------ invites
def diff_invites(prev: dict, cur: dict) -> Optional[int]:
    """Who invited the member that just joined? prev / cur = {code: (uses, max_uses, inviter_id)} before / after the join.

    The invite whose use counter went up is the one. A single-use / limited invite is deleted by Discord when its last use
    is taken - then it is missing in `cur` but was one use away from its limit. Unclear (vanity link, two joins at the
    same moment) = None, nobody is credited.
    """
    up = [c for c, (uses, _mx, _who) in cur.items() if uses > (prev.get(c) or (0, 0, None))[0]]
    if len(up) == 1:
        return cur[up[0]][2]
    if not up:
        gone = [c for c, (uses, mx, _w) in prev.items() if c not in cur and mx and uses + 1 >= mx]
        if len(gone) == 1:
            return prev[gone[0]][2]
    return None
