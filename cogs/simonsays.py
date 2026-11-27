"""Simon Says - watch the colours, then repeat the exact order with the buttons.

$simonsays                 open a lobby (aliases $simon, $ss) - works only in the Simon Says channel
$simonsays set             (admin) pick the channel from a menu
$simonsays stats [@member] best level, wins and games
$simonsays top             personal high scores of this server
$simonsays stop            stop the running game (host or admin)

2-10 players. The bot flashes a sequence (it starts with 3 colours). Press the same colours in the same order before the timer ends.
Everybody who got it right goes on, and the next level adds one more colour (and flashes a bit faster).
A wrong colour or running out of time = eliminated. The last player left wins; if everybody drops in the same level,
the one who got furthest wins. Your best level is saved as your personal high score.
"""
import asyncio
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.icons import I, err
from core.party import Party, PartyCog, rank_mark

log = logging.getLogger("kon.simonsays")

COLORS = {
    "red":    ("Red",    "🔴", discord.ButtonStyle.danger,    0xED4245),
    "blue":   ("Blue",   "🔵", discord.ButtonStyle.primary,   0x3B82F6),
    "green":  ("Green",  "🟢", discord.ButtonStyle.success,   0x57F287),
    "yellow": ("Yellow", "🟡", discord.ButtonStyle.secondary, 0xFEE75C),
}
KEYS = list(COLORS)
START_LEN = 3
MAX_LEVEL = 25
NEUTRAL = 0x2B2D31
BLUE = 0x5865F2


def show(seq) -> str:
    return " ".join(COLORS[k][1] for k in seq)


class PadView(discord.ui.View):
    def __init__(self, game: "SimonGame"):
        super().__init__(timeout=None)
        self.game = game
        for key, (label, emoji, style, _) in COLORS.items():
            b = discord.ui.Button(label=label, emoji=emoji, style=style)
            b.callback = (lambda i, k=key: self.press(i, k))
            self.add_item(b)

    async def press(self, interaction: discord.Interaction, key: str):
        g, uid = self.game, interaction.user.id
        if uid not in g.players:
            return await interaction.response.send_message(f"{I.s('show')} You're **spectating** - join the next game!", ephemeral=True)
        st = g.state.get(uid)
        if not g.input_open or st is None:
            if uid in g.out:
                return await interaction.response.send_message(f"{I.s('skull')} You're out - cheer for the others!", ephemeral=True)
            return await interaction.response.defer()
        if st != "playing":
            return await interaction.response.send_message(
                f"{I.s('check')} You already finished this level." if st == "done" else f"{I.s('skull')} You're out.", ephemeral=True)
        n = len(g.seq)
        if key == g.seq[g.pos[uid]]:
            g.pos[uid] += 1
            if g.pos[uid] == n:
                g.state[uid] = "done"
                g.times[uid] = time.monotonic() - g.input_start
                text = f"{I.s('check')} Perfect! **{n}/{n}**"
            else:
                text = f"{I.s('check')} **{g.pos[uid]}/{n}** · keep going"
        else:
            g.state[uid] = "out"
            g.reason[uid] = "wrong colour"
            text = f"{I.s('cross')} Wrong colour - you're out."
        if uid not in g.acks:
            g.acks[uid] = interaction
            await interaction.response.send_message(text, ephemeral=True)
        else:
            await interaction.response.defer()
            try:
                await g.acks[uid].edit_original_response(content=text)
            except discord.HTTPException:
                pass
        g.dirty = True
        if all(g.state[u] != "playing" for u in g.state):
            g.level_done.set()


class SimonGame(Party):
    key = "simonsays"
    title = "Simon Says"
    icon = "gamepad"
    color = BLUE
    uses_difficulty = False
    round_options = (1,)

    def __init__(self, cog, channel, host, difficulty):
        super().__init__(cog, channel, host, difficulty)
        self.seq: list = []
        self.alive: list = []
        self.out: set = set()
        self.cleared: dict = {}
        self.last_prog: dict = {}
        self.state: dict = {}
        self.pos: dict = {}
        self.times: dict = {}
        self.reason: dict = {}
        self.acks: dict = {}
        self.input_open = False
        self.input_start = 0.0
        self.deadline = 0
        self.dirty = False
        self.level_done = asyncio.Event()
        self.message: Optional[discord.Message] = None
        self.level = 0
        self.new_best: set = set()

    def how_to(self) -> str:
        return (f"Watch the colours flash, then press them **in the same order**.\n"
                f"`🔴 → 🔵 → 🟢`\n\n"
                f"**How to play**\n"
                f"• Every level adds **one more colour** and gets a little faster.\n"
                f"• Wrong colour or too slow = **eliminated**.\n"
                f"• Last player standing wins {I.s('trophy')} - if everybody falls in the same level, the one who got furthest wins.\n"
                f"• Your best level is saved as your **personal high score**.")

    @property
    def time_limit(self) -> int:
        return min(45, 6 + 2 * len(self.seq))

    # ----- embeds -----
    def watch_embed(self, text: str, color: int = BLUE, footer: str = "") -> discord.Embed:
        e = discord.Embed(color=color, title=f"{I.s(self.icon)} Simon Says · Level {self.level}", description=text)
        if footer:
            e.set_footer(text=footer)
        return e

    def input_embed(self) -> discord.Embed:
        lines = []
        for u in self.alive:
            st = self.state.get(u, "playing")
            if st == "done":
                lines.append(f"{I.s('check')} <@{u}> · done in {self.times.get(u, 0):.1f}s")
            elif st == "out":
                lines.append(f"{I.s('cross')} <@{u}> · {self.reason.get(u, 'out')}")
            else:
                lines.append(f"{I.s('hourglass')} <@{u}> · {self.pos.get(u, 0)}/{len(self.seq)}")
        e = discord.Embed(color=BLUE, title=f"{I.s(self.icon)} Simon Says · Level {self.level}",
                          description=f"### Your turn - repeat all **{len(self.seq)}** colours!\n{I.s('hourglass')} ends <t:{self.deadline}:R>")
        e.add_field(name="Players", value="\n".join(lines) or "-", inline=False)
        e.set_footer(text="Press the colours in the order you saw them")
        return e

    async def refresh(self):
        if self.message and self.input_open:
            try:
                await self.message.edit(embed=self.input_embed())
            except discord.HTTPException:
                pass

    async def flash(self):
        speed = max(0.75, 1.15 - 0.03 * self.level)
        await self.message.edit(embed=self.watch_embed("### Watch closely...", footer=f"{len(self.seq)} colours"), view=None)
        await self.pause(1.6)
        for i, key in enumerate(self.seq, 1):
            _, emoji, _, color = COLORS[key]
            await self.message.edit(embed=self.watch_embed(f"# {emoji}", color, f"{i} / {len(self.seq)}"))
            await self.pause(speed)
            await self.message.edit(embed=self.watch_embed("# ⚫", NEUTRAL, f"{i} / {len(self.seq)}"))
            await self.pause(0.25)

    # ----- the game -----
    async def play(self):
        self.alive = list(self.players)
        self.cleared = {u: 0 for u in self.players}
        self.last_prog = {u: 0 for u in self.players}
        self.seq = [random.choice(KEYS) for _ in range(START_LEN - 1)]
        self.message = await self.channel.send(content=" ".join(f"<@{u}>" for u in self.players),
                                               embed=self.watch_embed("### Get ready..."))
        await self.pause(2.5)
        while self.alive and self.level < MAX_LEVEL:
            self.level += 1
            self.seq.append(random.choice(KEYS))
            await self.flash()
            # ----- input phase -----
            self.state = {u: "playing" for u in self.alive}
            self.pos = {u: 0 for u in self.alive}
            self.times, self.reason, self.acks = {}, {}, {}
            self.level_done = asyncio.Event()
            self.input_start = time.monotonic()
            self.deadline = int(time.time()) + self.time_limit
            self.input_open, self.dirty = True, False
            view = PadView(self)
            await self.message.edit(embed=self.input_embed(), view=view)
            end = time.monotonic() + self.time_limit
            while not self.level_done.is_set() and time.monotonic() < end:
                await self.wait_any(self.level_done, min(1.5, max(0.1, end - time.monotonic())))
                if self.dirty:
                    self.dirty = False
                    await self.refresh()
            self.input_open = False
            view.stop()
            # ----- who goes on -----
            survivors, lines = [], []
            for u in self.alive:
                if self.state.get(u) == "done":
                    survivors.append(u)
                    self.cleared[u] = self.level
                    lines.append(f"{I.s('check')} <@{u}> · {self.times.get(u, 0):.1f}s")
                else:
                    self.out.add(u)
                    self.last_prog[u] = self.pos.get(u, 0)
                    why = self.reason.get(u) or "ran out of time"
                    lines.append(f"{I.s('cross')} <@{u}> · {why} ({self.pos.get(u, 0)}/{len(self.seq)})")
            self.alive = survivors
            e = discord.Embed(color=0x57F287 if survivors else 0xED4245,
                              title=f"{I.s(self.icon)} Level {self.level} · {show(self.seq)}", description="\n".join(lines))
            e.add_field(name="Still in", value=" ".join(f"<@{u}>" for u in survivors) or "*nobody*", inline=False)
            e.set_footer(text=("Next level: one more colour..." if len(survivors) > 1 and self.level < MAX_LEVEL
                               else "We have a result!"))
            try:
                await self.message.edit(embed=e, view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            if len(survivors) <= 1:
                break
            await self.pause(4)

    # ----- results / stats -----
    def rank_key(self, u: int) -> tuple:
        return (self.cleared.get(u, 0), 1 if u in self.alive else 0, self.last_prog.get(u, 0))

    def results(self) -> tuple:
        order = sorted(self.players, key=self.rank_key, reverse=True)
        top = self.rank_key(order[0])
        winners = [u for u in order if self.rank_key(u) == top] if top[0] > 0 else []
        rows = []
        for u in order:
            txt = f"reached level **{self.cleared.get(u, 0)}**"
            if u in self.alive:
                txt += " · still standing"
            if u in self.new_best:
                txt += " · new personal best!"
            rows.append((u, txt))
        return winners, rows

    def update_stats(self, st: dict, uid: int, won: bool):
        for k in ("games", "wins", "best", "levels"):
            st.setdefault(k, 0)
        lvl = self.cleared.get(uid, 0)
        if lvl > st["best"]:
            if st["games"] > 0:
                self.new_best.add(uid)
            st["best"] = lvl
        st["games"] += 1
        st["wins"] += 1 if won else 0
        st["levels"] += lvl


class SimonSays(PartyCog, commands.Cog):
    """Simon Says."""
    game_cls = SimonGame
    label = "Simon Says"
    icon = "gamepad"
    command = "simonsays"

    def __init__(self, bot):
        self.bot = bot
        self._init_party(bot)

    def stats_fields(self, st: dict) -> list:
        return [("Best level", st.get("best", 0)), ("Wins", st.get("wins", 0)), ("Games", st.get("games", 0)),
                ("Levels cleared", st.get("levels", 0))]

    def sort_key(self, st: dict):
        return (st.get("best", 0), st.get("wins", 0))

    def top_text(self, st: dict) -> str:
        return f"best level **{st.get('best', 0)}** · {st.get('wins', 0)} wins"

    @commands.hybrid_group(name="simonsays", aliases=["simon", "ss"], fallback="play", description="Play Simon Says - repeat the colour order")
    @commands.guild_only()
    async def simonsays(self, ctx: commands.Context):
        await self.open(ctx, "medium")

    @simonsays.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Simon Says works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ss_channel(self, ctx: commands.Context):
        await self.pick_channel(ctx)

    @simonsays.command(name="stop", description="Stop the running Simon Says game (host or admin)")
    @commands.guild_only()
    async def ss_stop(self, ctx: commands.Context):
        await self.stop_game(ctx)

    @simonsays.command(name="stats", description="Your Simon Says high score")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def ss_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        await self.show_stats(ctx, member)

    @simonsays.command(name="top", description="Best Simon Says players of this server")
    @commands.guild_only()
    async def ss_top(self, ctx: commands.Context):
        await self.show_top(ctx)


async def setup(bot):
    await bot.add_cog(SimonSays(bot))
