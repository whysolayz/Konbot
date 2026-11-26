"""Emoji helpers for $ei, the shop and the self-roles.

Emoji IDs and :name: are turned into real emoji codes, so animated emojis can be used without Nitro.
In a message you can write
    123456789012345678        a bare emoji ID
    :name:                    the plain name Discord sends for external emojis
    <a:name:123456789012345678> / <:name:123...>   already a full emoji (the animated flag is fixed if it is wrong)
and `resolve_text` turns all of them into the real `<a:name:id>` / `<:name:id>` code.

Lookup order for an ID: emojis of every server the bot is in -> the bot's own application emojis ->
the Discord CDN (works for emojis of servers the bot is NOT in; the name is unknown then, which Discord doesn't need).
"""
import asyncio
import logging
import re
import time
from typing import Optional

import aiohttp
import discord

from .errors import VCError
from .icons import FALLBACK, I

log = logging.getLogger("kon.emoji")

CUSTOM = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
NAME = re.compile(r"(?<![\w<:]):([A-Za-z0-9_]{2,32}):(?!\d)")
BARE_ID = re.compile(r"(?<![\w<:@#&!/.=?-])(\d{17,20})(?![\w>])")      # not part of a mention / link / <:name:id>
MAX_PROBES = 8                                                      # CDN look-ups per message (unknown IDs only)

_app_cache = {"t": 0.0, "items": []}
_probe_cache: dict[int, Optional[tuple]] = {}


async def app_emojis(bot) -> list:
    """The bot's own application emojis (cached for 2 minutes)."""
    if time.monotonic() - _app_cache["t"] < 120:
        return _app_cache["items"]
    try:
        _app_cache["items"] = await bot.fetch_application_emojis()
    except Exception:
        pass
    _app_cache["t"] = time.monotonic()
    return _app_cache["items"]


async def _probe_cdn(eid: int) -> Optional[tuple]:
    """(name, animated) for an emoji the bot can't see, or None. Animated = the .gif has more than one frame."""
    if eid in _probe_cache:
        return _probe_cache[eid]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.gif?size=32") as r:
                if r.status == 200:
                    data = await r.read()
                    if data[:3] == b"GIF" and data.count(b"\x21\xf9\x04") > 1:
                        _probe_cache[eid] = ("emoji", True)
                        return _probe_cache[eid]
            async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.png?size=32") as r:
                if r.status == 200:
                    _probe_cache[eid] = ("emoji", False)
                    return _probe_cache[eid]
                if r.status == 404:
                    _probe_cache[eid] = None          # really doesn't exist - don't ask again
    except (aiohttp.ClientError, TimeoutError):
        pass
    return None


async def find_emoji(bot, eid: int, probe: bool = True) -> Optional[tuple]:
    """(name, animated) or None."""
    em = bot.get_emoji(eid)
    if em is not None:
        return em.name, em.animated
    em = next((e for e in await app_emojis(bot) if e.id == eid), None)
    if em is not None:
        return em.name, em.animated
    return await _probe_cdn(eid) if probe else None


async def find_by_name(bot, name: str, guild: Optional[discord.Guild] = None):
    """Emoji object by name (this server first, exact case first)."""
    low = name.lower()
    pool = [e for e in bot.emojis if e.name.lower() == low and e.is_usable()]
    pool.sort(key=lambda e: (guild is None or e.guild_id != guild.id, e.name != name))
    if pool:
        return pool[0]
    return next((e for e in await app_emojis(bot) if e.name.lower() == low), None)


def code(name: str, animated: bool, eid: int) -> str:
    return f"<{'a' if animated else ''}:{name}:{eid}>"


async def resolve_text(bot, text: str, guild: Optional[discord.Guild] = None) -> tuple[str, int]:
    """-> (text with real emoji codes, how many emojis were filled in / fixed)."""
    changed = 0

    # 1) full codes: fix the animated flag when we know the emoji (a static code for an animated emoji shows a still image)
    known: dict[int, tuple] = {}
    for m in CUSTOM.finditer(text):
        eid = int(m.group(3))
        if eid not in known:
            hit = await find_emoji(bot, eid, probe=False)
            if hit:
                known[eid] = hit

    def fix_custom(m: re.Match) -> str:
        nonlocal changed
        eid = int(m.group(3))
        if eid in known:
            name, animated = known[eid]
            new = code(name, animated, eid)
            if new != m.group(0):
                changed += 1
            return new
        return m.group(0)

    text = CUSTOM.sub(fix_custom, text)

    # 2) :name:
    by_name: dict[str, str] = {}
    for m in NAME.finditer(text):
        n = m.group(1)
        if n not in by_name:
            em = await find_by_name(bot, n, guild)
            if em is not None:
                by_name[n] = str(em)

    def sub_name(m: re.Match) -> str:
        nonlocal changed
        hit = by_name.get(m.group(1))
        if hit:
            changed += 1
            return hit
        return m.group(0)

    text = NAME.sub(sub_name, text)

    # 3) bare IDs
    by_id: dict[str, str] = {}
    probes = 0
    for m in BARE_ID.finditer(text):
        raw = m.group(1)
        if raw in by_id:
            continue
        eid = int(raw)
        hit = await find_emoji(bot, eid, probe=False)
        if hit is None and probes < MAX_PROBES:
            probes += 1
            hit = await _probe_cdn(eid)
        if hit:
            by_id[raw] = code(hit[0], hit[1], eid)

    def sub_id(m: re.Match) -> str:
        nonlocal changed
        hit = by_id.get(m.group(1))
        if hit:
            changed += 1
            return hit
        return m.group(0)

    text = BARE_ID.sub(sub_id, text)
    return text, changed


async def normalize(bot, guild: Optional[discord.Guild], text: str) -> Optional[str]:
    """What the user typed -> the emoji string we store ('<a:name:id>' / '<:name:id>' / unicode), None = no emoji.

    Works with: an emoji ID (123456789012345678), :name:, a full <:name:id> code, a Kon icon name or a normal emoji.
    Raises VCError when it is not an emoji at all (so nothing broken is ever saved)."""
    from .icons import I           # late import: icons loads after the bot is up
    text = (text or "").strip()
    if not text:
        return None
    if text in I.map:
        return str(I.map[text])
    fixed, _ = await resolve_text(bot, text, guild)
    fixed = fixed.strip()
    if CUSTOM.fullmatch(fixed):
        return fixed
    if fixed.isdigit():
        raise VCError("I can't find an emoji with that ID. (`$ei` lists the IDs of animated emojis.)")
    if len(fixed) <= 16 and not re.fullmatch(r"[A-Za-z_ ]+", fixed) and not fixed.startswith(("http", "<")):
        return fixed                                   # a normal unicode emoji
    raise VCError("That emoji doesn't work. Use a normal emoji, `:name:` or the emoji ID.")


def to_partial_checked(text: Optional[str]) -> Optional[discord.PartialEmoji]:
    """Stored emoji string -> PartialEmoji for a button / select (None if there is nothing usable)."""
    if not text:
        return None
    name = text.strip()
    if name in FALLBACK:                              # a Kon icon name ("gift") -> the uploaded Kon icon
        e = I.map.get(name)
        return (discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated) if e
                else discord.PartialEmoji(name=FALLBACK[name]))
    try:
        pe = discord.PartialEmoji.from_str(text.strip())
    except Exception:
        return None
    return pe if (pe.id is not None or pe.name) else None


# ---------------------------------------------------------------------------------------------
# Deleted-emoji safety: a custom emoji that was deleted from its server makes Discord reject any button / select option
# that still uses it ("Invalid Form Body ... emoji.id: Invalid emoji"). These helpers tell whether an emoji still exists,
# so the bot can show one of ITS OWN icons in that place instead (the stored emoji is kept).
# ---------------------------------------------------------------------------------------------
TTL = 300                                           # seconds a CDN answer is trusted
_cdn_cache: dict[int, tuple[float, bool]] = {}
_gone: set[int] = set()                             # IDs we KNOW were deleted (guild emoji update event)


def ids_in(text: Optional[str]) -> list[int]:
    return [int(m.group(3)) for m in CUSTOM.finditer(text or "")]


def forget(ids) -> None:
    """These emojis were just deleted: never trust a cached 'exists' answer for them again."""
    for i in ids:
        _gone.add(int(i))
        _cdn_cache.pop(int(i), None)
        _probe_cache.pop(int(i), None)


def restore(ids) -> None:
    for i in ids:
        _gone.discard(int(i))
        _cdn_cache.pop(int(i), None)


async def exists(bot, eid: int) -> bool:
    """Does this custom emoji still exist? Unknown (network trouble) counts as True so nothing is hidden by mistake."""
    if eid in _gone:
        return False
    if bot.get_emoji(eid) is not None:
        return True
    if any(e.id == eid for e in await app_emojis(bot)):
        return True
    hit = _cdn_cache.get(eid)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.png?size=16") as r:
                if r.status == 200:
                    _cdn_cache[eid] = (time.monotonic(), True)
                    return True
                if r.status in (404, 400):
                    _cdn_cache[eid] = (time.monotonic(), False)
                    return False
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return True


async def missing(bot, *texts) -> set[int]:
    """IDs of the custom emojis in these texts that no longer exist."""
    out: set[int] = set()
    for t in texts:
        for eid in ids_in(t):
            if eid not in out and not await exists(bot, eid):
                out.add(eid)
    return out


async def resolve(bot, text: str, guild: Optional[discord.Guild] = None) -> str:
    """What the person typed -> a real emoji string (emoji ID, :name: and <:name:id> all work; no Nitro needed)."""
    text = (text or "").strip()
    if not text:
        return text
    return (await resolve_text(bot, text, guild))[0]


# ---------------------------------------------------------------------------------------------
# Name sanitizer (VC rename, AFK reason): only emojis the bot can really use stay in the text.
# usable = an emoji of the CURRENT server, of any server the bot is in, or one of the bot's own application emojis.
# Anything else (emoji of a foreign server, deleted emoji, unknown :name: / ID) is removed automatically, so no
# "name:id" garbage ever ends up in a channel name.
# ---------------------------------------------------------------------------------------------
async def usable(bot, guild: Optional[discord.Guild], eid: int) -> Optional[tuple]:
    """(name, animated) when the bot can use this emoji, else None. No network probing."""
    em = guild.get_emoji(eid) if guild is not None else None
    if em is None:
        em = bot.get_emoji(eid)
    if em is not None:
        return (em.name, em.animated) if getattr(em, "available", True) else None
    em = next((e for e in await app_emojis(bot) if e.id == eid), None)
    return (em.name, em.animated) if em is not None else None


async def sanitize(bot, guild: Optional[discord.Guild], text: str, limit: Optional[int] = None,
                   keep_custom: bool = True) -> tuple[str, int]:
    """-> (clean text, number of emojis that were removed).

    * `<:name:id>` / `<a:name:id>` stays (animated flag fixed) only if the emoji is usable, otherwise it is removed
    * `:name:` and bare emoji IDs become the real code when a usable emoji matches, otherwise they are removed
    * `limit` cuts the text without ever splitting an emoji code in half
    """
    text = text or ""
    removed = 0

    hits: dict[int, Optional[tuple]] = {}
    for m in CUSTOM.finditer(text):
        eid = int(m.group(3))
        if eid not in hits:
            hits[eid] = await usable(bot, guild, eid)

    def fix_code(m: re.Match) -> str:
        nonlocal removed
        hit = hits.get(int(m.group(3)))
        if hit is None:
            removed += 1
            return ""
        return code(hit[0], hit[1], int(m.group(3)))

    text = CUSTOM.sub(fix_code, text)

    by_name: dict[str, Optional[str]] = {}
    for m in NAME.finditer(text):
        n = m.group(1)
        if n not in by_name:
            em = await find_by_name(bot, n, guild)
            by_name[n] = str(em) if em is not None else None

    def fix_name(m: re.Match) -> str:
        nonlocal removed
        hit = by_name.get(m.group(1))
        if hit:
            return hit
        removed += 1
        return ""

    text = NAME.sub(fix_name, text)

    ids: dict[str, Optional[str]] = {}
    for m in BARE_ID.finditer(text):
        raw = m.group(1)
        if raw not in ids:
            hit = await usable(bot, guild, int(raw))
            ids[raw] = code(hit[0], hit[1], int(raw)) if hit else None
    text = BARE_ID.sub(lambda m: ids.get(m.group(1)) or m.group(0), text)       # a plain number that is no emoji stays

    if not keep_custom:                                 # channel names can't show custom emojis (Discord prints the raw code)
        removed += len(CUSTOM.findall(text))
        text = CUSTOM.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if limit is not None and len(text) > limit:
        cut = text[:limit]
        last = cut.rfind("<")
        if last != -1 and ">" not in cut[last:]:        # the cut went through an emoji code -> drop the broken half
            cut = cut[:last]
        text = cut.rstrip()
    return text, removed
