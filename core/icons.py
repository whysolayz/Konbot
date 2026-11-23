"""Custom icons = Application Emojis.

On startup every icon in USED is uploaded once (as `kon_<name>`) from assets/icons.
If an upload fails the bot falls back to a unicode emoji, so nothing breaks.
"""
import asyncio
import hashlib
import logging
import re

import discord

from .config import ICON_DIR

log = logging.getLogger("kon.icons")
EMOJI_PREFIX = "kon_"

UPLOAD_PARALLEL = 12                          # emoji uploads running at the same time (discord.py waits by itself on a rate limit)
NAME_RE = re.compile(r"(kon_.+?)_([0-9a-f]{6})")   # kon_<icon>_<6 hex of the image hash>: a changed image = a new name = re-upload

FALLBACK = {
    "lock": "🔒", "unlock": "🔓", "hide": "🙈", "show": "👁️", "logout": "🚪", "ban": "🚫",
    "user_add": "➕", "users": "👥", "signal": "📶", "edit": "✏️", "volume": "🔊", "crown": "👑",
    "check": "✅", "cross": "❌", "hourglass": "⏳", "clock": "⏱️", "info": "ℹ️", "mute": "🔇",
    "swap": "🔁", "left": "◀️", "right": "▶️", "speaker": "🔊", "mic": "🎙️", "settings": "⚙️",
    "warning": "⚠️", "tools": "🛠️", "bolt": "⚡", "refresh": "🔄", "shield_check": "🛡️",
    "user_minus": "➖", "leaf": "🍃", "member": "👤", "headset": "🎧", "help": "❓",
    "check_outline": "✅", "cross_filled": "❌", "home": "🏠", "menu": "📋", "sparkles": "✨",
    "stopwatch": "⏱️", "plus": "➕", "minus": "➖", "trophy": "🏆", "coins": "🪙", "wallet": "💰", "gift_box": "📦",
    "heart": "💛", "message": "💬", "mic": "🎙️",
    "play": "▶️", "terminal": "💻", "star_fill": "⭐", "gift": "🎁", "link": "🔗",
    "image": "🖼️", "palette": "🎨", "upload": "📤", "tag": "🏷️", "diamond": "💠",     # self-role editor
    "heart_broken": "🖤", "skull": "💀", "stop": "🛑", "repeat": "🔁",
    "fire": "🔥", "gamepad": "🎮", "target": "🎯", "folder": "📁", "user_star": "🌟", "grid": "🔲",   # aura / games / info
    "rock": "🪨", "paper": "📄", "scissors": "✂️", "tap": "👆", "team_red": "🔴", "team_blue": "🔵",     # Handfight / TimeTap
    "light_red": "🔴", "light_green": "🟢", "shield_star": "🛡️", "swap_small": "⚔️", "thumbs_up": "👍",
    "ticket": "🎫", "user_plus_arrow": "🙋", "messages": "📜", "mail": "📥",     # tickets (claim / logs / opened log)
    "bell_ring": "🔔",                                                          # AFK mentions
}

# every icon used anywhere in the bot (kept small: emoji uploads are rate limited)
USED = list(FALLBACK.keys() - {"member"})


class Icons:
    def __init__(self):
        self.map: dict[str, discord.Emoji] = {}
        self.refreshed = False                           # True when icons were re-uploaded (emoji IDs changed)

    async def load(self, bot: discord.Client):
        try:
            existing = {e.name: e for e in await bot.fetch_application_emojis()}
        except Exception as e:  # old discord.py / no permission
            log.warning("Could not fetch application emojis (%s) - using unicode fallbacks", e)
            return
        by_base: dict[str, list] = {}
        for e in existing.values():
            if e.name.startswith(EMOJI_PREFIX):
                m = NAME_RE.fullmatch(e.name)
                by_base.setdefault(m.group(1) if m else e.name, []).append(e)
        sem = asyncio.Semaphore(UPLOAD_PARALLEL)

        async def one(name: str):
            key = EMOJI_PREFIX + name
            path = ICON_DIR / f"{name}.png"
            olds = by_base.get(key, [])
            if not path.exists():
                if olds:
                    self.map[name] = olds[0]
                return
            data = path.read_bytes()
            want = f"{key}_{hashlib.sha1(data).hexdigest()[:6]}"
            for e in olds:
                if e.name == want:                      # already uploaded with this exact image
                    self.map[name] = e
                    return
            async with sem:
                for e in olds:                          # old / green image -> remove
                    try:
                        await e.delete()
                    except Exception as ex:
                        log.warning("could not delete old icon %s: %s", e.name, ex)
                try:
                    self.map[name] = await bot.create_application_emoji(name=want, image=data)
                    self.refreshed = True
                except Exception as ex:
                    log.warning("icon upload failed for %s: %s", want, ex)

        await asyncio.gather(*(one(n) for n in USED))
        log.info("icons ready: %d/%d custom", len(self.map), len(USED))

    def s(self, name: str) -> str:
        """icon as text (for embeds / messages)"""
        e = self.map.get(name)
        return str(e) if e else FALLBACK.get(name, "▫️")

    def e(self, name: str):
        """icon for buttons / selects"""
        return self.map.get(name) or FALLBACK.get(name)


I = Icons()


def ok(text: str) -> str:
    return f"{I.s('check')} {text}"


def err(text: str) -> str:
    return f"{I.s('cross')} {text}"
