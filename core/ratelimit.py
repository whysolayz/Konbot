"""Sliding-window rate limiter used for commands, buttons, VC creation, renames, drags."""
import collections
import time

import discord
from discord.ext import commands

from .icons import I


class RateLimited(commands.CommandError):
    def __init__(self, retry_after: float, bucket: str = "command"):
        super().__init__(f"rate limited ({bucket})")
        self.retry_after = retry_after
        self.bucket = bucket


# bucket -> (max hits, per seconds)
RULES = {
    "command": (5, 10),     # prefix + slash commands, per user
    "guild": (40, 10),      # all commands, per server
    "button": (6, 10),      # interface buttons, per user
    "modal": (4, 20),       # modal / select submits, per user
    "create_vc": (2, 60),   # join-to-create, per user
    "rename": (2, 600),     # Discord only allows 2 channel renames / 10 min, per channel
    "drag": (1, 30),        # /dragme, per user
}


class RateLimiter:
    def __init__(self):
        self._h: dict[tuple, tuple[int, collections.deque]] = {}
        self._ops = 0

    def hit(self, key: tuple, limit: int, per: int) -> float:
        """Returns 0 if allowed (and records the hit) else seconds to wait."""
        now = time.monotonic()
        _, dq = self._h.setdefault(key, (per, collections.deque()))
        while dq and now - dq[0] >= per:
            dq.popleft()
        if len(dq) >= limit:
            return per - (now - dq[0])
        dq.append(now)
        self._ops += 1
        if self._ops % 500 == 0:
            self.sweep()
        return 0.0

    def sweep(self):
        now = time.monotonic()
        for k in [k for k, (per, dq) in self._h.items() if not dq or now - dq[-1] >= per]:
            self._h.pop(k, None)


limiter = RateLimiter()


def check(bucket: str, key) -> float:
    limit, per = RULES[bucket]
    return limiter.hit((bucket, key), limit, per)


def fmt(seconds: float) -> str:
    seconds = max(0.1, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds + 0.5), 60)
    return f"{m}m {s}s"


def limited_text(retry_after: float) -> str:
    return f"{I.s('hourglass')} Slow down. Try again in **{fmt(retry_after)}**."


async def command_check(ctx: commands.Context) -> bool:
    """Global check for every prefix / slash command."""
    if ctx.bot.is_owner_fast(ctx.author.id, ctx.guild.id if ctx.guild else None):      # no await = no delay
        return True
    ra = check("command", ctx.author.id)
    if not ra and ctx.guild:
        ra = check("guild", ctx.guild.id)
    if ra:
        raise RateLimited(ra, "command")
    return True


async def guard_interaction(interaction: discord.Interaction, bucket: str = "button") -> bool:
    """True if allowed. If limited, tells the user (ephemeral) and returns False."""
    if interaction.client.is_owner_fast(interaction.user.id, interaction.guild_id):
        return True
    ra = check(bucket, interaction.user.id)
    if not ra:
        return True
    try:
        if interaction.response.is_done():
            await interaction.followup.send(limited_text(ra), ephemeral=True)
        else:
            await interaction.response.send_message(limited_text(ra), ephemeral=True)
    except discord.HTTPException:
        pass
    return False
