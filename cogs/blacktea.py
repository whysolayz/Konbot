"""BlackTea - a word-bomb game (like tictactoe: it only works in the channel an admin sets).

$blacktea [easy|medium|hard]   open a lobby right away (no "play" needed; alias: $bt) - works only in the BlackTea channel
                               the game NEVER starts by itself - the host presses Start
$blacktea set                  (admin) pick the BlackTea channel from a menu (also: $blacktea channel [#channel])
$setblacktea [#channel]        same as above
$blacktea reset                (admin) remove the BlackTea channel setting
$blacktea stop                 stop the running game (host or admin)

How it plays: everybody gets 3 lives. On your turn the bot shows 2-3 letters and you must send a real
English word that STARTS WITH those letters (e.g. `STR` -> strong, street) before the timer ends. Time out = lose a life.
Last player standing wins. A word can only be used once per game.

Modes:   easy   2 starting letters (very common), 15s
         medium 3 starting letters, 10s
         hard   3 rarer starting letters, 7s, words must be 4+ letters

Files:   assets/words.txt  - the word list (one lowercase word per line); assets/words_extra.txt - your own extra words
         data/blacktea.json - the channel settings (inside data/, so it is part of the backup)
"""
import asyncio
import collections
import json
import logging
import os
import random
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.config import BASE, DATA_DIR, PREFIX
from core.icons import I
from core.minigames import LOBBY_IDLE, open_channel_picker

log = logging.getLogger("kon.blacktea")

SETTINGS_FILE = DATA_DIR / "blacktea.json"
WORDS_FILE = BASE / "assets" / "words.txt"
EXTRA_WORDS_FILE = BASE / "assets" / "words_extra.txt"     # optional: add words missing from words.txt (one per line)

TEA = 0xC47F3B           # tea brown
WIN = 0xF5C542
RED = 0xED4245

MAX_PLAYERS = 20
LIVES = 3

MODES = {
    "easy":   {"label": "Easy",   "icon": "thumbs_up", "seconds": 15, "n": 2, "lo": 300,  "hi": 10**9, "min_len": 3,
               "info": "2 starting letters · 15s per turn"},
    "medium": {"label": "Medium", "icon": "bolt", "seconds": 10, "n": 3, "lo": 100,  "hi": 3000,  "min_len": 3,
               "info": "3 starting letters · 10s per turn"},
    "hard":   {"label": "Hard",   "icon": "fire", "seconds": 7,  "n": 3, "lo": 30,   "hi": 100,   "min_len": 4,
               "info": "3 rare starting letters · 7s per turn · words 4+ letters"},
}
DEFAULT_MODE = "medium"
ONE_WORD = re.compile(r"[a-z]{2,20}")
NO_PING = discord.AllowedMentions.none()


def hearts(n: int) -> str:
    return I.s("heart") * n + I.s("heart_broken") * (LIVES - n)


async def react(message: discord.Message, name: str, fallback: str):
    """React with the custom icon; if Discord refuses it, fall back to the unicode emoji."""
    try:
        await message.add_reaction(I.e(name) or fallback)
    except discord.HTTPException:
        try:
            await message.add_reaction(fallback)
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------ settings
def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("channels", {})
                return data
    except (OSError, ValueError):
        pass
    return {"channels": {}}


def save_settings(data: dict):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# ------------------------------------------------------------------ lobby UI
class LobbyView(discord.ui.View):
    def __init__(self, game: "Game"):
        super().__init__(timeout=None)
        self.game = game
        self.sync_mode()

    def sync_mode(self):
        for opt in self.mode_select.options:
            opt.default = opt.value == self.game.mode

    def is_host(self, member) -> bool:
        return member.id == self.game.host.id or member.guild_permissions.manage_guild

    @discord.ui.button(label="Join", emoji=I.e("user_add"), style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id in g.players:
            return await interaction.response.send_message("You are already in this game.", ephemeral=True)
        if len(g.players) >= MAX_PLAYERS:
            return await interaction.response.send_message("The lobby is full.", ephemeral=True)
        g.players.append(interaction.user.id)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Leave", emoji=I.e("logout"), style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id == g.host.id:
            return await interaction.response.send_message(
                f"You are the host - use `{PREFIX}blacktea stop` to cancel the game.", ephemeral=True)
        if interaction.user.id not in g.players:
            return await interaction.response.send_message("You are not in this game.", ephemeral=True)
        g.players.remove(interaction.user.id)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Start", emoji=I.e("play"), style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_host(interaction.user):
            return await interaction.response.send_message("Only the host can start the game.", ephemeral=True)
        if len(self.game.players) < 2:
            return await interaction.response.send_message("You need at least **2 players** to start.", ephemeral=True)
        self.game.start_event.set()
        await interaction.response.defer()

    @discord.ui.select(placeholder="Difficulty (host only)", row=1, options=[
        discord.SelectOption(label=m["label"], value=k, emoji=I.e(m["icon"]), description=m["info"])
        for k, m in MODES.items()])
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self.is_host(interaction.user):
            return await interaction.response.send_message("Only the host can change the difficulty.", ephemeral=True)
        self.game.mode = select.values[0]
        self.sync_mode()
        await interaction.response.edit_message(embed=self.game.lobby_embed(), view=self)


# ------------------------------------------------------------------ the game
class Game:
    def __init__(self, cog: "BlackTea", channel: discord.TextChannel, host: discord.Member, mode: str):
        self.cog, self.channel, self.host, self.mode = cog, channel, host, mode
        self.players = [host.id]
        self.lives = {}
        self.used = set()
        self.state = "lobby"          # lobby -> playing -> done
        self.start_event = asyncio.Event()
        self.answer_event = asyncio.Event()
        self.answer: Optional[str] = None
        self.current: Optional[int] = None
        self.fragment = ""
        self.stopped = False
        self.deadline = 0
        self.lobby_msg: Optional[discord.Message] = None
        self.view: Optional[LobbyView] = None
        self.start_ts = 0
        self.task: Optional[asyncio.Task] = None
        self.order: list = []

    @property
    def cfg(self) -> dict:
        return MODES[self.mode]

    # ----- embeds -----
    def lobby_embed(self) -> discord.Embed:
        c = self.cfg
        e = discord.Embed(title=f"{I.s('leaf')} BlackTea", color=TEA, description=(
            f"A word is hiding in every turn - **find it before the tea gets cold!**\n\n"
            f"**How to play**\n"
            f"• You get **{LIVES}** {I.s('heart')} lives.\n"
            f"• On your turn the bot shows some letters - send a **real English word** that **starts with them** "
            f"(e.g. `ST` → **storm**, `STR` → **street**) "
            f"within **{c['seconds']}s**.\n"
            f"• No answer = you lose a life. Words can't be repeated.\n"
            f"• Last one standing wins {I.s('trophy')}"))
        e.add_field(name="Difficulty", value=f"{I.s(c['icon'])} **{c['label']}**\n{c['info']}", inline=True)
        names = "\n".join(f"{i}. <@{uid}>" + (f" {I.s('crown')}" if uid == self.host.id else "")
                          for i, uid in enumerate(self.players, 1))
        e.add_field(name=f"Players ({len(self.players)}/{MAX_PLAYERS})", value=names, inline=False)
        e.set_footer(text="Press Join · the host picks the difficulty and presses Start (it never starts by itself)")
        return e

    def turn_embed(self, rnd: int) -> discord.Embed:
        c = self.cfg
        e = discord.Embed(color=TEA, description=(
            f"<@{self.current}>, send a word that **starts with**\n"
            f"# `{self.fragment.upper()}`\n"
            f"{I.s('hourglass')} ends <t:{self.deadline}:R>"))
        board = "\n".join(
            (f"{I.s('play')} " if uid == self.current else f"{I.s('minus')} ") + f"<@{uid}> {hearts(self.lives[uid])}"
            for uid in self.order)
        e.add_field(name="Players", value=board, inline=False)
        e.set_footer(text=f"{c['label']} mode · turn {rnd} · words used: {len(self.used)}")
        e.title = f"{I.s('leaf')} BlackTea"
        return e

    # ----- flow -----
    async def run(self):
        try:
            await self.lobby()
            if self.stopped:
                return
            if len(self.players) < 2:
                await self.channel.send(f"{I.s('leaf')} Not enough players joined - BlackTea cancelled.")
                return
            await self.play()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("BlackTea crashed in channel %s", self.channel.id)
            try:
                await self.channel.send(f"{I.s('warning')} BlackTea hit an error and stopped.")
            except discord.HTTPException:
                pass
        finally:
            self.state = "done"
            self.cog.games.pop(self.channel.id, None)

    async def lobby(self):
        self.view = LobbyView(self)
        self.lobby_msg = await self.channel.send(embed=self.lobby_embed(), view=self.view)
        expired = False
        try:
            await asyncio.wait_for(self.start_event.wait(), LOBBY_IDLE)      # NO autostart: only the host's Start button
        except asyncio.TimeoutError:
            expired = True
            self.stopped = True
        self.view.stop()
        for item in self.view.children:
            item.disabled = True
        e = self.lobby_embed()
        if expired:
            e.title, e.color = f"{I.s('leaf')} BlackTea - lobby expired (nobody pressed Start)", RED
        elif self.stopped:
            e.title, e.color = f"{I.s('leaf')} BlackTea - cancelled", RED
        elif len(self.players) < 2:
            e.title, e.color = f"{I.s('leaf')} BlackTea - cancelled (not enough players)", RED
        else:
            e.title = f"{I.s('leaf')} BlackTea - started!"
        try:
            await self.lobby_msg.edit(embed=e, view=self.view)
        except discord.HTTPException:
            pass

    async def play(self):
        self.state = "playing"
        self.order = list(self.players)
        random.shuffle(self.order)
        self.lives = {uid: LIVES for uid in self.order}
        c = self.cfg
        recent = collections.deque(maxlen=10)
        idx, rnd = 0, 0
        longest = ""
        await asyncio.sleep(1.5)

        while len(self.order) > 1 and not self.stopped:
            rnd += 1
            idx %= len(self.order)
            self.current = self.order[idx]
            self.fragment = self.cog.pick(self.mode, recent)
            recent.append(self.fragment)
            self.answer = None
            self.answer_event.clear()
            self.deadline = int(time.time()) + c["seconds"]
            msg = await self.channel.send(content=f"<@{self.current}>", embed=self.turn_embed(rnd))

            try:
                await asyncio.wait_for(self.answer_event.wait(), c["seconds"])
            except asyncio.TimeoutError:
                pass
            uid, frag, word = self.current, self.fragment, self.answer
            self.current = None
            if self.stopped:
                break

            if word:
                self.used.add(word)
                if len(word) > len(longest):
                    longest = word
                idx += 1
                text = f"{I.s('check')} <@{uid}> · **{word}** (`{frag.upper()}`)"
            else:
                self.lives[uid] -= 1
                left = self.lives[uid]
                if left <= 0:
                    text = f"{I.s('clock')} <@{uid}> ran out of time on `{frag.upper()}`"
                    self.order.remove(uid)       # idx now already points at the next player
                else:
                    idx += 1
                    text = f"{I.s('clock')} <@{uid}> ran out of time on `{frag.upper()}` - {hearts(left)}"
            try:
                await msg.edit(content=text, embed=None, allowed_mentions=NO_PING)
            except discord.HTTPException:
                pass
            if not word and self.lives[uid] <= 0:
                await self.channel.send(f"{I.s('skull')} <@{uid}> is out!", allowed_mentions=NO_PING)
            await asyncio.sleep(1.2)

        if self.stopped:
            await self.channel.send(f"{I.s('stop')} BlackTea was stopped.")
            return
        winner = self.order[0]
        e = discord.Embed(title=f"{I.s('leaf')} BlackTea", color=WIN,
                          description=f"{I.s('trophy')} <@{winner}> wins with {hearts(self.lives[winner])} left!")
        e.add_field(name="Turns", value=str(rnd), inline=True)
        e.add_field(name="Words played", value=str(len(self.used)), inline=True)
        if longest:
            e.add_field(name="Longest word", value=longest, inline=True)
        e.set_footer(text=f"{c['label']} mode · play again with {PREFIX}blacktea")
        await self.channel.send(content=f"<@{winner}>", embed=e)

    # ----- a message from the player whose turn it is -----
    async def handle_word(self, message: discord.Message):
        word = message.content.strip().lower().strip(" .,!?;:'\"*_~`")
        if not ONE_WORD.fullmatch(word):
            return                                   # normal chat (several words, emojis...) is ignored
        c = self.cfg
        try:
            if word in self.used:
                return await react(message, "repeat", "🔁")
            if (not word.startswith(self.fragment)                                 # the word must START with the letters
                    or len(word) < c["min_len"] or word not in self.cog.words):
                return await react(message, "cross", "❌")
            self.answer = word
            self.answer_event.set()
            await react(message, "check", "✅")
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------ the cog
class BlackTea(commands.Cog):
    """BlackTea word game."""

    def __init__(self, bot):
        self.bot = bot
        self.games: dict = {}
        self.words: frozenset = frozenset()
        self.pools: dict = {}
        self.settings = load_settings()
        self.ready = False

    async def cog_load(self):
        try:
            self.words, self.pools = await asyncio.to_thread(self._build)
            self.ready = True
            log.info("BlackTea ready: %d words", len(self.words))
        except OSError:
            log.error("BlackTea: word list %s is missing - copy assets/words.txt", WORDS_FILE)

    @staticmethod
    def _build():
        with open(WORDS_FILE, "r", encoding="utf-8") as f:
            words = {w for w in (line.strip().lower() for line in f) if w}
        try:
            with open(EXTRA_WORDS_FILE, "r", encoding="utf-8") as f:
                words |= {w for w in (line.strip().lower() for line in f) if w.isalpha()}
        except OSError:
            pass
        words = frozenset(words)
        counts = {2: collections.Counter(), 3: collections.Counter()}
        for w in words:
            if not w.isalpha():
                continue
            for n, cnt in counts.items():
                if len(w) >= n:
                    cnt[w[:n]] += 1                      # how many words START with these letters
        pools = {}
        for key, m in MODES.items():
            pools[key] = [s for s, k in counts[m["n"]].items()
                          if m["lo"] <= k <= m["hi"] and any(ch in "aeiouy" for ch in s)
                          and (m["n"] == 2 or sum(ch in "aeiou" for ch in s) < 3)]
        return words, pools

    def pick(self, mode: str, recent) -> str:
        pool = self.pools[mode]
        for _ in range(20):
            frag = random.choice(pool)
            if frag not in recent:
                return frag
        return random.choice(pool)

    def cog_unload(self):
        for g in list(self.games.values()):
            g.stopped = True
            g.start_event.set()
            g.answer_event.set()

    # ----- channel gate -----
    def game_channel(self, guild_id: int) -> Optional[int]:
        return self.settings["channels"].get(str(guild_id))

    # ----- listening for answers -----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        game = self.games.get(message.channel.id)
        if game is None or game.state != "playing" or message.author.id != game.current:
            return
        await game.handle_word(message)

    # ----- commands -----
    @commands.hybrid_group(name="blacktea", aliases=["bt"], fallback="play",
                           description="Start a BlackTea word game")
    @app_commands.describe(mode="Difficulty: easy, medium or hard")
    @app_commands.choices(mode=[app_commands.Choice(name=m["label"], value=k) for k, m in MODES.items()])
    @commands.guild_only()
    async def blacktea(self, ctx: commands.Context, mode: str = DEFAULT_MODE):
        mode = (mode or DEFAULT_MODE).lower()
        if mode in ("play", "start", "open", "game"):      # old habit: "$blacktea play" still works, but plain "$blacktea" is enough
            mode = DEFAULT_MODE
        if mode not in MODES:
            return await ctx.send(f"Pick a mode: `{PREFIX}blacktea easy`, `medium` or `hard`.")
        if not self.ready:
            return await ctx.send("BlackTea's word list is missing (`assets/words.txt`) - tell the bot owner.")

        cid = self.game_channel(ctx.guild.id)
        if cid is None:
            if ctx.author.guild_permissions.manage_guild:
                return await ctx.send(f"No BlackTea channel yet - set one with `{PREFIX}blacktea channel #channel`.")
            return await ctx.send("An admin hasn't set a BlackTea channel yet.")
        if ctx.channel.id != cid:
            return await ctx.send(f"{I.s('leaf')} BlackTea only works in <#{cid}>.", ephemeral=True)
        if ctx.channel.id in self.games:
            return await ctx.send("A BlackTea game is already running here - join it or wait for it to finish.",
                                  ephemeral=True)
        need = ctx.channel.permissions_for(ctx.guild.me)
        if not (need.send_messages and need.embed_links and need.add_reactions and need.read_message_history):
            return await ctx.send("I need **Send Messages, Embed Links, Add Reactions and Read Message History** "
                                  "in this channel.")

        game = Game(self, ctx.channel, ctx.author, mode)
        self.games[ctx.channel.id] = game
        if ctx.interaction:
            await ctx.send(f"{I.s('leaf')} Opening the lobby...", ephemeral=True)
        game.task = asyncio.create_task(game.run())

    def _save_channel(self, gid: int, cid: int):
        self.settings["channels"][str(gid)] = cid
        save_settings(self.settings)

    def _clear_channel(self, gid: int):
        if self.settings["channels"].pop(str(gid), None) is not None:
            save_settings(self.settings)

    @blacktea.command(name="channel", aliases=["set"], description="(Admin) Set the channel where BlackTea works")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def bt_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        if channel is None:                       # "$blacktea set" -> channel select menu (no ping needed)
            return await open_channel_picker(
                ctx, label="BlackTea", icon="leaf", command="blacktea", current=self.game_channel(ctx.guild.id),
                save=self._save_channel, clear=self._clear_channel, extra_perms=("add_reactions", "read_message_history"))
        me = channel.permissions_for(ctx.guild.me)
        if not (me.send_messages and me.embed_links and me.add_reactions and me.read_message_history):
            return await ctx.send(f"I need Send Messages, Embed Links, Add Reactions and Read Message History in "
                                  f"{channel.mention} first.")
        self.settings["channels"][str(ctx.guild.id)] = channel.id
        save_settings(self.settings)
        await ctx.send(f"{I.s('leaf')} BlackTea will now work only in {channel.mention}. Start a game there with "
                       f"`{PREFIX}blacktea`.")

    @commands.command(name="setblacktea", description="(Admin) Set the BlackTea channel")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def setblacktea(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        await self.bt_channel(ctx, channel)

    @blacktea.command(name="reset", description="(Admin) Remove the BlackTea channel setting")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def bt_reset(self, ctx: commands.Context):
        if self.settings["channels"].pop(str(ctx.guild.id), None) is None:
            return await ctx.send("No BlackTea channel was set.")
        save_settings(self.settings)
        await ctx.send(f"{I.s('leaf')} BlackTea channel removed.")

    @blacktea.command(name="stop", description="Stop the running BlackTea game (host or admin)")
    @commands.guild_only()
    async def bt_stop(self, ctx: commands.Context):
        game = self.games.get(ctx.channel.id)
        if game is None:
            return await ctx.send("No BlackTea game is running here.", ephemeral=True)
        if ctx.author.id != game.host.id and not ctx.author.guild_permissions.manage_guild:
            return await ctx.send("Only the host or an admin can stop the game.", ephemeral=True)
        game.stopped = True
        game.start_event.set()
        game.answer_event.set()
        await ctx.send(f"{I.s('stop')} Stopping BlackTea...", ephemeral=True)


async def setup(bot):
    await bot.add_cog(BlackTea(bot))
