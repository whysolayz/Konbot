"""Guess the Emoji - emojis hide a compound word, type the word in the chat.

$guessemoji [easy|medium|hard]   open a lobby (alias $ge) - works only in the Guess the Emoji channel
$guessemoji set                  (admin) pick the channel from a menu
$guessemoji stats [@member]      wins, points and best game
$guessemoji top                  best players of this server
$guessemoji stop                 stop the running game (host or admin)

Example: 🌧️ + 🏹 = RAIN + BOW = RAINBOW   ·   🍳 + 🎂 + 🥞 = PAN + CAKE = PANCAKE
2-10 players · 10 / 15 / 20 rounds (host picks) · 15 seconds per puzzle · Hint and Skip buttons.
Points: 100 / 150 / 200 (Easy / Medium / Hard) + up to 100 for speed. A hint costs 30% of that round's points.
Skip needs more than half of the players. After every round the scoreboard is shown.
Puzzles live in assets/guess_emoji.json (easy / medium / hard) - add your own, no code needed.
Tip: give the bot Manage Messages - it then deletes right answers so nobody can copy them.
"""
import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.config import BASE
from core.icons import I, err
from core.party import LEVELS, ROUND_SECONDS, Party, PartyCog, speed_points, stop_button

log = logging.getLogger("kon.guessemoji")
PUZZLES_FILE = BASE / "assets" / "guess_emoji.json"
YELLOW = 0xF5C542
GOOD, BAD = 0x57F287, 0xED4245
HINT_FACTOR = 0.7


def norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def load_puzzles() -> dict:
    try:
        with open(PUZZLES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: [x for x in data.get(k, []) if x.get("clue") and x.get("a")] for k in LEVELS}
    except (OSError, ValueError):
        return {k: [] for k in LEVELS}


class RoundView(discord.ui.View):
    def __init__(self, game: "EmojiGame"):
        super().__init__(timeout=None)
        self.game = game
        self.hint_btn = discord.ui.Button(label="Hint", emoji=I.e("info"), style=discord.ButtonStyle.secondary)
        self.hint_btn.callback = self.hint
        self.skip_btn = discord.ui.Button(label=self.skip_label(), emoji=I.e("fast_forward"), style=discord.ButtonStyle.secondary)
        self.skip_btn.callback = self.skip
        self.add_item(self.hint_btn)
        self.add_item(self.skip_btn)
        self.add_item(stop_button(game))

    def skip_label(self) -> str:
        g = self.game
        return f"Skip ({len(g.skips)}/{g.skips_needed})"

    async def _player(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message(f"{I.s('show')} You're **spectating** - join the next game!", ephemeral=True)
            return False
        return True

    async def hint(self, interaction: discord.Interaction):
        g = self.game
        if not await self._player(interaction):
            return
        if g.hint_used:
            return await interaction.response.send_message(err("The hint is already shown."), ephemeral=True)
        g.hint_used = True
        self.hint_btn.disabled = True
        await interaction.response.edit_message(embed=g.round_embed(), view=self)

    async def skip(self, interaction: discord.Interaction):
        g = self.game
        if not await self._player(interaction):
            return
        if interaction.user.id in g.skips:
            return await interaction.response.send_message(err("You already voted to skip."), ephemeral=True)
        g.skips.add(interaction.user.id)
        if len(g.skips) >= g.skips_needed:
            g.round_done.set()
            return await interaction.response.defer()
        self.skip_btn.label = self.skip_label()
        await interaction.response.edit_message(embed=g.round_embed(), view=self)


class EmojiGame(Party):
    key = "guessemoji"
    title = "Guess the Emoji"
    icon = "sparkles"
    color = YELLOW
    round_options = (10, 15, 20)

    def __init__(self, cog, channel, host, difficulty):
        super().__init__(cog, channel, host, difficulty)
        self.puzzle: Optional[dict] = None
        self.message: Optional[discord.Message] = None
        self.solved: dict = {}                     # uid -> seconds
        self.skips: set = set()
        self.hint_used = False
        self.open = False
        self.round_no = 0
        self.round_start = 0.0
        self.deadline = 0
        self.round_done = asyncio.Event()

    @property
    def skips_needed(self) -> int:
        return len(self.players) // 2 + 1

    def how_to(self) -> str:
        return (f"Emojis hide a **compound word** - type it in the chat before the timer ends.\n"
                f"`🌧️ + 🏹` → RAIN + BOW → **rainbow**\n\n"
                f"**How to play**\n"
                f"• **{ROUND_SECONDS}s** per puzzle, as many tries as you like.\n"
                f"• Faster answers earn a **speed bonus**. A **Hint** costs 30% of the round's points.\n"
                f"• **Skip** needs more than half of the players. The scoreboard is shown after every round {I.s('trophy')}")

    def level_info(self, level: str) -> str:
        return f"{LEVELS[level]['base']} pts per word · {'two clear emojis' if level == 'easy' else 'three emoji clues' if level == 'medium' else 'tricky pairs'}"

    def hint_text(self) -> str:
        word = self.puzzle["a"]
        shape = " ".join(c if i == 0 else "_" for i, c in enumerate(word.upper()))
        parts = " + ".join(str(len(p)) for p in self.puzzle["parts"])
        return f"`{shape}` · {parts} letters"

    def round_embed(self) -> discord.Embed:
        e = discord.Embed(color=YELLOW, title=f"{I.s(self.icon)} Guess the Emoji · Round {self.round_no}/{self.rounds}",
                          description=f"# {self.puzzle['clue']}\nType the **word** in the chat.\n\n{I.s('hourglass')} ends <t:{self.deadline}:R>")
        if self.hint_used:
            e.add_field(name=f"{I.s('info')} Hint", value=self.hint_text(), inline=False)
        e.add_field(name="Solved", value=f"**{len(self.solved)}/{len(self.players)}**", inline=True)
        e.add_field(name="Worth", value=f"**{self.level['base']}** + speed" + (" -30%" if self.hint_used else ""), inline=True)
        e.set_footer(text=f"{self.level['label']} · Hint and Skip are below")
        return e

    def pick_puzzle(self, used: set) -> dict:
        pool = self.cog.puzzles.get(self.difficulty) or []
        fresh = [p for p in pool if p["a"] not in used]
        if not fresh:
            fresh = [p for lv in LEVELS for p in self.cog.puzzles.get(lv, []) if p["a"] not in used] or pool
        return random.choice(fresh)

    # ----- a chat message from a player -----
    async def handle_guess(self, message: discord.Message):
        uid = message.author.id
        if not self.open or uid not in self.players or uid in self.solved:
            return
        text = message.content.strip()
        if not text or len(text) > 30 or len(text.split()) > 3:           # normal chat is not a guess
            return
        guess = norm(text)
        if not guess:
            return
        right = {self.puzzle["a"], *[norm(a) for a in self.puzzle.get("alt", [])]}
        try:
            if guess in right:
                self.solved[uid] = time.monotonic() - self.round_start
                try:
                    await message.delete()                                   # keeps the answer secret (needs Manage Messages)
                except discord.HTTPException:
                    await message.add_reaction(I.e("check") or "✅")
                asyncio.create_task(self.refresh())
                if len(self.solved) >= len(self.players):
                    self.round_done.set()
            else:
                await message.add_reaction(I.e("cross") or "❌")
        except discord.HTTPException:
            pass

    async def refresh(self):
        if self.message and self.open:
            try:
                await self.message.edit(embed=self.round_embed())
            except discord.HTTPException:
                pass

    async def play(self):
        me = self.channel.guild.me
        if not self.channel.permissions_for(me).manage_messages:
            await self.channel.send(f"{I.s('info')} Tip: give me **Manage Messages** and I'll hide right answers from the chat.")
        used: set = set()
        for n in range(1, self.rounds + 1):
            self.round_no = n
            self.puzzle = self.pick_puzzle(used)
            used.add(self.puzzle["a"])
            self.solved, self.skips, self.hint_used, self.open = {}, set(), False, True
            self.round_done = asyncio.Event()
            self.round_start = time.monotonic()
            self.deadline = int(time.time()) + ROUND_SECONDS
            view = RoundView(self)
            self.message = await self.channel.send(content=" ".join(f"<@{u}>" for u in self.players) if n == 1 else None,
                                                   embed=self.round_embed(), view=view)
            await self.wait_any(self.round_done, ROUND_SECONDS)
            self.open = False
            view.stop()
            skipped = len(self.skips) >= self.skips_needed
            gained, lines = {}, []
            factor = HINT_FACTOR if self.hint_used else 1.0
            for uid in self.players:
                if uid in self.solved and not skipped:
                    pts = speed_points(self.level["base"], self.solved[uid], ROUND_SECONDS, factor)
                    gained[uid] = pts
                    self.scores[uid] = self.scores.get(uid, 0) + pts
                    self.correct[uid] = self.correct.get(uid, 0) + 1
                    lines.append(f"{I.s('check')} <@{uid}> · **+{pts}** · {self.solved[uid]:.1f}s")
                else:
                    lines.append(f"{I.s('cross')} <@{uid}> · " + ("skipped" if skipped else "not solved"))
            word = self.puzzle["a"].upper()
            parts = " + ".join(self.puzzle["parts"])
            e = discord.Embed(color=GOOD if gained else BAD, title=f"{I.s(self.icon)} Round {n}/{self.rounds} · {word}",
                              description=f"{self.puzzle['clue']} = {parts} = **{word}**\n\n" + "\n".join(lines))
            e.add_field(name=f"{I.s('trophy')} Scoreboard", value=self.board(gained), inline=False)
            e.set_footer(text="Skipped by vote" if skipped else "Next round coming up..." if n < self.rounds else "That was the last round!")
            try:
                await self.message.edit(embed=e, view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            if n < self.rounds:
                await self.pause(5)


class GuessEmoji(PartyCog, commands.Cog):
    """Guess the Emoji."""
    game_cls = EmojiGame
    label = "Guess the Emoji"
    icon = "sparkles"
    command = "guessemoji"
    extra_perms = ("add_reactions",)

    def __init__(self, bot):
        self.bot = bot
        self._init_party(bot)
        self.puzzles = load_puzzles()
        if not any(self.puzzles.values()):
            log.warning("assets/guess_emoji.json is missing or empty - Guess the Emoji has no puzzles")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        game = self.games.get(message.channel.id)
        if game is not None and game.state == "playing":
            await game.handle_guess(message)

    @commands.hybrid_group(name="guessemoji", aliases=["ge"], fallback="play", description="Play Guess the Emoji with friends")
    @app_commands.describe(difficulty="easy, medium or hard")
    @app_commands.choices(difficulty=[app_commands.Choice(name=v["label"], value=k) for k, v in LEVELS.items()])
    @commands.guild_only()
    async def guessemoji(self, ctx: commands.Context, difficulty: str = "medium"):
        if not any(self.puzzles.values()):
            return await ctx.reply(err("The puzzle list (`assets/guess_emoji.json`) is missing - tell the bot owner."), mention_author=False)
        await self.open(ctx, difficulty)

    @guessemoji.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Guess the Emoji works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ge_channel(self, ctx: commands.Context):
        await self.pick_channel(ctx)

    @guessemoji.command(name="stop", description="Stop the running Guess the Emoji game (host or admin)")
    @commands.guild_only()
    async def ge_stop(self, ctx: commands.Context):
        await self.stop_game(ctx)

    @guessemoji.command(name="stats", description="Your Guess the Emoji record")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def ge_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        await self.show_stats(ctx, member)

    @guessemoji.command(name="top", description="Best Guess the Emoji players of this server")
    @commands.guild_only()
    async def ge_top(self, ctx: commands.Context):
        await self.show_top(ctx)


async def setup(bot):
    await bot.add_cog(GuessEmoji(bot))
