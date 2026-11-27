"""Guess the Anime - a multiplayer quiz (same style as BlackTea / Handfight).

$guessanime [easy|medium|hard]   open a lobby (alias $ga) - works only in the Guess the Anime channel
$guessanime set                  (admin) pick the channel from a menu
$guessanime stats [@member]      wins, points and best game
$guessanime top                  best players of this server
$guessanime stop                 stop the running game (host or admin)

2-10 players · 10 rounds · 15 seconds per question · 4 answer buttons.
Points: a right answer gives 100 / 150 / 200 (Easy / Medium / Hard) + up to 100 for speed. After every round the scoreboard is shown.
Questions live in assets/guess_anime.json (easy / medium / hard) - add your own lines there, no code needed.
"""
import asyncio
import json
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
from core.config import BASE
from core.icons import I, err
from core.party import (LEVELS, ROUND_SECONDS, Party, PartyCog, speed_points)

log = logging.getLogger("kon.guessanime")
QUESTIONS_FILE = BASE / "assets" / "guess_anime.json"
LETTERS = "ABCD"
GOLDEN = 0xFFB832
GOOD, BAD = 0x57F287, 0xED4245


def load_questions() -> dict:
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: [x for x in data.get(k, []) if x.get("q") and x.get("a")] for k in LEVELS}
    except (OSError, ValueError):
        return {k: [] for k in LEVELS}


class AnswerView(discord.ui.View):
    def __init__(self, game: "AnimeGame", options: list):
        super().__init__(timeout=None)
        self.game = game
        for i, name in enumerate(options):
            b = discord.ui.Button(label=name[:80], style=discord.ButtonStyle.primary, row=i // 2)
            b.callback = (lambda inter, n=name: self.pick(inter, n))
            self.add_item(b)

    async def pick(self, interaction: discord.Interaction, name: str):
        g, uid = self.game, interaction.user.id
        if uid not in g.players:
            if await ratelimit.guard_interaction(interaction, "button"):
                await interaction.response.send_message(f"{I.s('show')} You're **spectating** - join the next game!", ephemeral=True)
            return
        if g.open is False:
            return await interaction.response.defer()
        if uid in g.answers:
            return await interaction.response.send_message(err(f"You already locked **{g.answers[uid][0]}**."), ephemeral=True)
        g.answers[uid] = (name, time.monotonic() - g.round_start)
        await interaction.response.send_message(f"{I.s('check')} Locked in **{name}**.", ephemeral=True)
        if len(g.answers) >= len(g.players):
            g.all_in.set()
        else:
            asyncio.create_task(g.refresh())


class AnimeGame(Party):
    key = "guessanime"
    title = "Guess the Anime"
    icon = "star_fill"
    color = GOLDEN
    round_options = (10,)

    def __init__(self, cog, channel, host, difficulty):
        super().__init__(cog, channel, host, difficulty)
        self.message: Optional[discord.Message] = None
        self.question: Optional[dict] = None
        self.options: list = []
        self.answers: dict = {}
        self.open = False
        self.round_no = 0
        self.round_start = 0.0
        self.deadline = 0
        self.all_in = asyncio.Event()

    def how_to(self) -> str:
        return (f"Read the clue and **tap the anime** it describes before the timer runs out.\n\n"
                f"**How to play**\n"
                f"• **{self.rounds} rounds**, **{ROUND_SECONDS}s** each, one answer per question.\n"
                f"• Right answer = base points + **speed bonus** (up to +100).\n"
                f"• The scoreboard is shown after every round {I.s('trophy')}")

    def level_info(self, level: str) -> str:
        return f"{LEVELS[level]['base']} pts per answer · {'famous shows' if level == 'easy' else 'popular shows' if level == 'medium' else 'deeper cuts'}"

    # ----- one round -----
    def round_embed(self) -> discord.Embed:
        e = discord.Embed(color=GOLDEN, title=f"{I.s(self.icon)} Guess the Anime · Round {self.round_no}/{self.rounds}",
                          description=f"### {self.question['q']}\n\n{I.s('hourglass')} ends <t:{self.deadline}:R>")
        e.add_field(name="Answered", value=f"**{len(self.answers)}/{len(self.players)}**", inline=True)
        e.add_field(name="Worth", value=f"**{self.level['base']}** + speed", inline=True)
        e.set_footer(text=f"{self.level['label']} · tap one answer below")
        return e

    async def refresh(self):
        if self.message and self.open:
            try:
                await self.message.edit(embed=self.round_embed())
            except discord.HTTPException:
                pass

    def pick_question(self, used: set) -> dict:
        pool = self.cog.questions.get(self.difficulty) or []
        fresh = [q for q in pool if q["a"] not in used]
        if len(fresh) < 1:                                           # pool too small: allow repeats from other levels
            fresh = [q for lv in LEVELS for q in self.cog.questions.get(lv, []) if q["a"] not in used] or pool
        return random.choice(fresh)

    def make_options(self, q: dict) -> list:
        same = [x["a"] for x in self.cog.questions.get(self.difficulty, []) if x["a"] != q["a"]]
        other = [x["a"] for lv in LEVELS if lv != self.difficulty for x in self.cog.questions.get(lv, []) if x["a"] != q["a"]]
        random.shuffle(same), random.shuffle(other)
        wrong = (same + other)[:3]
        opts = wrong + [q["a"]]
        random.shuffle(opts)
        return opts

    async def play(self):
        used: set = set()
        for n in range(1, self.rounds + 1):
            self.round_no = n
            self.question = self.pick_question(used)
            used.add(self.question["a"])
            self.options = self.make_options(self.question)
            self.answers, self.open = {}, True
            self.all_in = asyncio.Event()
            self.round_start = time.monotonic()
            self.deadline = int(time.time()) + ROUND_SECONDS
            view = AnswerView(self, self.options)
            self.message = await self.channel.send(content=" ".join(f"<@{u}>" for u in self.players) if n == 1 else None,
                                                   embed=self.round_embed(), view=view)
            await self.wait_any(self.all_in, ROUND_SECONDS)
            self.open = False
            view.stop()
            gained = {}
            right = self.question["a"]
            lines = []
            for uid in self.players:
                pick = self.answers.get(uid)
                if pick is None:
                    lines.append(f"{I.s('hourglass')} <@{uid}> · no answer")
                elif pick[0] == right:
                    pts = speed_points(self.level["base"], pick[1], ROUND_SECONDS)
                    gained[uid] = pts
                    self.scores[uid] = self.scores.get(uid, 0) + pts
                    self.correct[uid] = self.correct.get(uid, 0) + 1
                    lines.append(f"{I.s('check')} <@{uid}> · **+{pts}** · {pick[1]:.1f}s")
                else:
                    lines.append(f"{I.s('cross')} <@{uid}> · {pick[0]}")
            e = discord.Embed(color=GOOD if gained else BAD,
                              title=f"{I.s(self.icon)} Round {n}/{self.rounds} · {right}",
                              description=f"> {self.question['q']}\n\n" + "\n".join(lines))
            e.add_field(name=f"{I.s('trophy')} Scoreboard", value=self.board(gained), inline=False)
            e.set_footer(text="Next round coming up..." if n < self.rounds else "That was the last round!")
            try:
                await self.message.edit(embed=e, view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            if n < self.rounds:
                await self.pause(5)


class GuessAnime(PartyCog, commands.Cog):
    """Guess the Anime."""
    game_cls = AnimeGame
    label = "Guess the Anime"
    icon = "star_fill"
    command = "guessanime"

    def __init__(self, bot):
        self.bot = bot
        self._init_party(bot)
        self.questions = load_questions()
        if not any(self.questions.values()):
            log.warning("assets/guess_anime.json is missing or empty - Guess the Anime has no questions")

    @commands.hybrid_group(name="guessanime", aliases=["ga"], fallback="play", description="Play Guess the Anime with friends")
    @app_commands.describe(difficulty="easy, medium or hard")
    @app_commands.choices(difficulty=[app_commands.Choice(name=v["label"], value=k) for k, v in LEVELS.items()])
    @commands.guild_only()
    async def guessanime(self, ctx: commands.Context, difficulty: str = "medium"):
        if not any(self.questions.values()):
            return await ctx.reply(err("The question list (`assets/guess_anime.json`) is missing - tell the bot owner."), mention_author=False)
        await self.open(ctx, difficulty)

    @guessanime.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Guess the Anime works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ga_channel(self, ctx: commands.Context):
        await self.pick_channel(ctx)

    @guessanime.command(name="stop", description="Stop the running Guess the Anime game (host or admin)")
    @commands.guild_only()
    async def ga_stop(self, ctx: commands.Context):
        await self.stop_game(ctx)

    @guessanime.command(name="stats", description="Your Guess the Anime record")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def ga_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        await self.show_stats(ctx, member)

    @guessanime.command(name="top", description="Best Guess the Anime players of this server")
    @commands.guild_only()
    async def ga_top(self, ctx: commands.Context):
        await self.show_top(ctx)


async def setup(bot):
    await bot.add_cog(GuessAnime(bot))
