"""Shared engine of the round based party games: Guess the Anime, Guess the Emoji and Simon Says.

Same feel as BlackTea / Handfight:
  * the game only works in the channel an admin picked (`$<game> set` opens a channel menu)
  * `$<game> [easy|medium|hard]` opens a lobby right away - it NEVER starts by itself, the host presses Start
  * 2-10 players, a live scoreboard after every round, a winner screen and a Play Again button
  * wins / points / best score of every player are saved in kon.db (so they are part of every backup)

A game class only has to describe its rounds (`play()`); the lobby, stopping, scoring, stats and winner screen live here.
"""
import asyncio
import logging
import time
from typing import Optional

import discord

from .config import BRAND, PREFIX
from .icons import I, err
from .minigames import GOLD, GREEN, LOBBY_IDLE, NO_PING, GameSettings, gate, is_admin, open_channel_picker

log = logging.getLogger("kon.party")

LEVELS = {
    "easy":   {"label": "Easy",   "icon": "thumbs_up", "base": 100},
    "medium": {"label": "Medium", "icon": "bolt",      "base": 150},
    "hard":   {"label": "Hard",   "icon": "fire",      "base": 200},
}
DEFAULT_LEVEL = "medium"
ROUND_SECONDS = 15
SPEED_BONUS = 100                # up to +100 points for answering instantly
MEDALS = ("🥇", "🥈", "🥉")


class GameStopped(Exception):
    pass


def rank_mark(n: int) -> str:
    return MEDALS[n] if n < len(MEDALS) else f"`{n + 1}.`"


def speed_points(base: int, elapsed: float, seconds: float, factor: float = 1.0) -> int:
    """Correct answer = base points + up to SPEED_BONUS for being fast (full bonus at 0s, none at the end of the timer)."""
    left = max(0.0, 1 - elapsed / seconds)
    return int(round((base + SPEED_BONUS * left) * factor))


def stop_button(game: "Party", row=None) -> discord.ui.Button:
    """Red Stop button under a running round: host or an admin ends the game at once."""
    b = discord.ui.Button(label="Stop", emoji=I.e("stop"), style=discord.ButtonStyle.danger, row=row)

    async def press(interaction: discord.Interaction):
        u = interaction.user
        if u.id != game.host.id and not getattr(u.guild_permissions, "manage_guild", False) \
                and not interaction.client.is_owner_fast(u.id, interaction.guild_id):
            return await interaction.response.send_message(err("Only the host or an admin can stop the game."), ephemeral=True)
        game.stop()
        await interaction.response.send_message(f"{I.s('stop')} Stopping {game.title}...", ephemeral=True)
    b.callback = press
    return b


# ============================================================================ lobby
class PartyLobby(discord.ui.View):
    def __init__(self, game: "Party"):
        super().__init__(timeout=None)
        self.game = game
        self._add("Join", "user_add", discord.ButtonStyle.success, self.join)
        self._add("Leave", "logout", discord.ButtonStyle.secondary, self.leave)
        self._add("Start", "play", discord.ButtonStyle.primary, self.start)
        self._add("Cancel", "cross", discord.ButtonStyle.secondary, self.cancel)
        self.level_sel = None
        self.rounds_btn = None
        if game.uses_difficulty:
            self.level_sel = discord.ui.Select(placeholder="Difficulty (host only)", row=1, options=[
                discord.SelectOption(label=v["label"], value=k, emoji=I.e(v["icon"]), description=game.level_info(k))
                for k, v in LEVELS.items()])
            self.level_sel.callback = self.pick_level
            self.add_item(self.level_sel)
            self.sync()
        if len(game.round_options) > 1:
            self.rounds_btn = self._add(game.rounds_label(), "repeat", discord.ButtonStyle.secondary, self.cycle_rounds, row=2)

    def _add(self, label, icon, style, cb, row=0):
        b = discord.ui.Button(label=label, emoji=I.e(icon), style=style, row=row)
        b.callback = cb
        self.add_item(b)
        return b

    def sync(self):
        if self.level_sel:
            for o in self.level_sel.options:
                o.default = o.value == self.game.difficulty

    def is_host(self, member) -> bool:
        return member.id == self.game.host.id or member.guild_permissions.manage_guild

    async def join(self, interaction: discord.Interaction):
        g = self.game
        if interaction.user.id in g.players:
            return await interaction.response.send_message(err("You are already in this game."), ephemeral=True)
        if len(g.players) >= g.max_players:
            return await interaction.response.send_message(err("The lobby is full."), ephemeral=True)
        g.players.append(interaction.user.id)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    async def leave(self, interaction: discord.Interaction):
        g = self.game
        if interaction.user.id == g.host.id:
            return await interaction.response.send_message(err("You are the host - press **Cancel** to close the lobby."), ephemeral=True)
        if interaction.user.id not in g.players:
            return await interaction.response.send_message(err("You are not in this game."), ephemeral=True)
        g.players.remove(interaction.user.id)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    async def start(self, interaction: discord.Interaction):
        g = self.game
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host can start the game."), ephemeral=True)
        if len(g.players) < g.min_players:
            return await interaction.response.send_message(err(f"You need at least **{g.min_players} players** to start."), ephemeral=True)
        g.start_event.set()
        await interaction.response.defer()

    async def cancel(self, interaction: discord.Interaction):
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host or an admin can cancel."), ephemeral=True)
        self.game.stop()
        await interaction.response.defer()

    async def pick_level(self, interaction: discord.Interaction):
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host can change the difficulty."), ephemeral=True)
        self.game.difficulty = self.level_sel.values[0]
        self.sync()
        await interaction.response.edit_message(embed=self.game.lobby_embed(), view=self)

    async def cycle_rounds(self, interaction: discord.Interaction):
        g = self.game
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host can change the number of rounds."), ephemeral=True)
        opts = list(g.round_options)
        g.rounds = opts[(opts.index(g.rounds) + 1) % len(opts)]
        self.rounds_btn.label = g.rounds_label()
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)


class PlayAgainView(discord.ui.View):
    """Under the winner screen: any player of that game can open the next lobby with the same people."""

    def __init__(self, game: "Party"):
        super().__init__(timeout=900)
        self.game = game
        self.message: Optional[discord.Message] = None
        b = discord.ui.Button(label="Play Again", emoji=I.e("repeat"), style=discord.ButtonStyle.success)
        b.callback = self.again
        self.add_item(b)

    async def again(self, interaction: discord.Interaction):
        g = self.game
        cog = g.cog
        if interaction.user.id not in g.players:
            return await interaction.response.send_message(err("Only the players of that game can start a rematch."), ephemeral=True)
        if g.channel.id in cog.games:
            return await interaction.response.send_message(err("A game is already running here."), ephemeral=True)
        ng = cog.make_game(g.channel, interaction.user, g.difficulty)
        ng.rounds = g.rounds
        ng.players = [interaction.user.id] + [u for u in g.players if u != interaction.user.id]
        self.stop()
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await cog.launch(ng)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# ============================================================================ one game
class Party:
    key = "party"
    title = "Party"
    icon = "gamepad"
    color = BRAND
    min_players, max_players = 2, 10
    uses_difficulty = True
    round_options = (10,)

    def __init__(self, cog, channel, host: discord.Member, difficulty: str = DEFAULT_LEVEL):
        self.cog, self.channel, self.host = cog, channel, host
        self.difficulty = difficulty if difficulty in LEVELS else DEFAULT_LEVEL
        self.rounds = self.round_options[0]
        self.players: list = [host.id]
        self.scores: dict = {}
        self.correct: dict = {}
        self.state = "lobby"                         # lobby -> playing -> done
        self.start_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.lobby_msg: Optional[discord.Message] = None
        self.task: Optional[asyncio.Task] = None

    # ---------- what a game fills in ----------
    def how_to(self) -> str:
        return ""

    def level_info(self, level: str) -> str:
        return ""

    async def play(self):
        raise NotImplementedError

    def results(self) -> tuple:
        """-> (winner ids, [(uid, text), ...] best first)"""
        order = sorted(self.players, key=lambda u: (-self.scores.get(u, 0), -self.correct.get(u, 0)))
        top = self.scores.get(order[0], 0)
        winners = [u for u in order if self.scores.get(u, 0) == top] if top > 0 else []
        return winners, [(u, f"**{self.scores.get(u, 0)}** pts · {self.correct.get(u, 0)} correct") for u in order]

    def update_stats(self, st: dict, uid: int, won: bool):
        for k in ("games", "wins", "points", "correct", "best"):
            st.setdefault(k, 0)
        st["games"] += 1
        st["wins"] += 1 if won else 0
        st["points"] += self.scores.get(uid, 0)
        st["correct"] += self.correct.get(uid, 0)
        st["best"] = max(st["best"], self.scores.get(uid, 0))

    # ---------- helpers ----------
    @property
    def level(self) -> dict:
        return LEVELS[self.difficulty]

    def rounds_label(self) -> str:
        return f"Rounds: {self.rounds}"

    def stop(self):
        self.stop_event.set()
        self.start_event.set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    async def wait_any(self, evt: asyncio.Event, timeout: float):
        """Wait for `evt` or the timeout - and leave at once (GameStopped) when the host stops the game."""
        a, b = asyncio.ensure_future(evt.wait()), asyncio.ensure_future(self.stop_event.wait())
        try:
            await asyncio.wait({a, b}, timeout=max(0.0, timeout), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (a, b):
                t.cancel()
        if self.stopped:
            raise GameStopped

    async def pause(self, seconds: float):
        await self.wait_any(asyncio.Event(), seconds)

    def board(self, deltas: Optional[dict] = None, ids: Optional[list] = None) -> str:
        order = sorted(ids or self.players, key=lambda u: -self.scores.get(u, 0))
        lines = []
        for n, uid in enumerate(order):
            gain = f" `+{deltas[uid]}`" if deltas and deltas.get(uid) else ""
            lines.append(f"{rank_mark(n)} <@{uid}> · **{self.scores.get(uid, 0)}**{gain}")
        return "\n".join(lines) or "-"

    # ---------- lobby ----------
    def lobby_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"{I.s(self.icon)} {self.title}", color=self.color, description=self.how_to())
        if self.uses_difficulty:
            lv = self.level
            e.add_field(name="Difficulty", value=f"{I.s(lv['icon'])} **{lv['label']}**\n{self.level_info(self.difficulty)}", inline=True)
        if len(self.round_options) > 1 or self.uses_difficulty:
            e.add_field(name="Rounds", value=f"**{self.rounds}** · {ROUND_SECONDS}s each", inline=True)
        names = "\n".join(f"{i}. <@{u}>" + (f" {I.s('crown')}" if u == self.host.id else "") for i, u in enumerate(self.players, 1))
        e.add_field(name=f"Players ({len(self.players)}/{self.max_players})", value=names, inline=False)
        e.set_footer(text=f"Press Join · the host presses Start (it never starts by itself) · needs {self.min_players}+ players")
        return e

    async def close_lobby(self, note: str):
        if self.lobby_msg:
            try:
                await self.lobby_msg.edit(embed=discord.Embed(title=f"{I.s(self.icon)} {self.title}", color=self.color, description=note), view=None)
            except discord.HTTPException:
                pass

    # ---------- the run ----------
    async def run(self):
        try:
            try:
                await asyncio.wait_for(self.start_event.wait(), LOBBY_IDLE)
            except asyncio.TimeoutError:
                return await self.close_lobby(f"{I.s('clock')} Nobody started the game, so the lobby was closed.")
            if self.stopped:
                return await self.close_lobby(f"{I.s('stop')} The lobby was closed.")
            self.state = "playing"
            for u in self.players:
                self.scores.setdefault(u, 0)
                self.correct.setdefault(u, 0)
            await self.close_lobby(f"{I.s('play')} The game has started - good luck!")
            try:
                await self.play()
            except GameStopped:
                return await self.channel.send(f"{I.s('stop')} {self.title} was stopped.")
            self.state = "done"
            await self.finish()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed", self.title)
            try:
                await self.channel.send(err(f"{self.title} hit an error and had to stop."))
            except discord.HTTPException:
                pass
        finally:
            self.cog.games.pop(self.channel.id, None)

    async def finish(self):
        try:
            stats = self.cog.settings.stats(self.channel.guild.id)
            winners_now, _ = self.results()
            for uid in self.players:
                self.update_stats(stats.setdefault(str(uid), {}), uid, uid in winners_now)
            self.cog.settings.save_stats(self.channel.guild.id, stats)
        except Exception:
            log.exception("%s: saving stats failed", self.title)
        winners, rows = self.results()
        e = discord.Embed(title=f"{I.s('trophy')} {self.title} · Final results", color=GOLD)
        if winners:
            e.description = "\n".join(f"# {I.s('crown')} <@{u}>" for u in winners) + ("\n**It's a tie!**" if len(winners) > 1 else "")
        else:
            e.description = "Nobody scored this time."
        e.add_field(name="Scoreboard", inline=False, value="\n".join(f"{rank_mark(n)} <@{u}> · {t}" for n, (u, t) in enumerate(rows)))
        note = self.finish_note()
        e.set_footer(text=(note + " · " if note else "") + "GG! · press Play Again for a rematch")
        view = PlayAgainView(self)
        view.message = await self.channel.send(content=" ".join(f"<@{u}>" for u in winners) or None, embed=e, view=view)

    def finish_note(self) -> str:
        return f"{self.level['label']}" if self.uses_difficulty else ""


# ============================================================================ cog mixin
class PartyCog:
    """Mixin for the cog of a party game: channel gate, `open`, `stats`, `top`, `stop`."""
    game_cls = Party
    label = "Party"
    icon = "gamepad"
    command = "party"
    extra_perms: tuple = ()

    def _init_party(self, bot):
        self.settings = GameSettings(bot, self.game_cls.key)
        self.games: dict = {}

    def cog_unload(self):
        for g in list(self.games.values()):
            g.stop()

    def make_game(self, channel, host, difficulty):
        g = self.game_cls(self, channel, host, difficulty)
        self.games[channel.id] = g
        return g

    async def launch(self, game: Party):
        game.lobby_msg = await game.channel.send(embed=game.lobby_embed(), view=PartyLobby(game))
        game.task = asyncio.create_task(game.run())

    async def open(self, ctx, difficulty: str):
        difficulty = (difficulty or DEFAULT_LEVEL).lower()
        if difficulty in ("play", "start", "open", "game"):
            difficulty = DEFAULT_LEVEL
        if self.game_cls.uses_difficulty and difficulty not in LEVELS:
            return await ctx.reply(f"Pick a difficulty: `{PREFIX}{self.command} easy`, `medium` or `hard`.", mention_author=False)
        if not await gate(ctx, label=self.label, icon=self.icon, command=self.command,
                          cid=self.settings.channel(ctx.guild.id), extra_perms=self.extra_perms):
            return
        if ctx.channel.id in self.games:
            return await ctx.reply(err(f"A {self.label} game is already running here - join it or wait for it to finish."),
                                   mention_author=False, ephemeral=True)
        game = self.make_game(ctx.channel, ctx.author, difficulty)
        if ctx.interaction:
            await ctx.reply(f"{I.s(self.icon)} Opening the lobby...", ephemeral=True)
        await self.launch(game)

    async def pick_channel(self, ctx):
        await open_channel_picker(ctx, label=self.label, icon=self.icon, command=self.command,
                                  current=self.settings.channel(ctx.guild.id), save=self.settings.set_channel,
                                  clear=self.settings.clear_channel, extra_perms=self.extra_perms)

    async def stop_game(self, ctx):
        game = self.games.get(ctx.channel.id)
        if game is None:
            return await ctx.reply(err(f"No {self.label} game is running here."), mention_author=False, ephemeral=True)
        if ctx.author.id != game.host.id and not await is_admin(ctx):
            return await ctx.reply(err("Only the host or an admin can stop the game."), mention_author=False, ephemeral=True)
        game.stop()
        await ctx.reply(f"{I.s('stop')} Stopping {self.label}...", mention_author=False, ephemeral=True)

    # ----- stats / leaderboard -----
    def stats_fields(self, st: dict) -> list:
        return [("Wins", st.get("wins", 0)), ("Games", st.get("games", 0)), ("Best score", st.get("best", 0)),
                ("Total points", st.get("points", 0)), ("Correct", st.get("correct", 0))]

    def sort_key(self, st: dict):
        return (st.get("wins", 0), st.get("points", 0))

    def top_text(self, st: dict) -> str:
        return f"**{st.get('wins', 0)}** wins · {st.get('points', 0)} pts"

    async def show_stats(self, ctx, member: Optional[discord.Member]):
        member = member or ctx.author
        st = self.settings.stats(ctx.guild.id).get(str(member.id))
        if not st or not st.get("games"):
            return await ctx.reply(f"{I.s(self.icon)} {member.display_name} hasn't played {self.label} yet - start with `{PREFIX}{self.command}`.",
                                   mention_author=False)
        e = discord.Embed(color=BRAND, title=f"{I.s(self.icon)} {self.label} · {member.display_name}")
        e.set_thumbnail(url=member.display_avatar.url)
        for name, val in self.stats_fields(st):
            e.add_field(name=name, value=f"**{val}**", inline=True)
        await ctx.reply(embed=e, mention_author=False)

    async def show_top(self, ctx):
        stats = {k: v for k, v in self.settings.stats(ctx.guild.id).items() if v.get("games")}
        if not stats:
            return await ctx.reply(f"{I.s(self.icon)} Nobody has played {self.label} here yet.", mention_author=False)
        order = sorted(stats.items(), key=lambda kv: self.sort_key(kv[1]), reverse=True)[:10]
        lines = [f"{rank_mark(n)} <@{uid}> · {self.top_text(st)}" for n, (uid, st) in enumerate(order)]
        e = discord.Embed(color=BRAND, title=f"{I.s('trophy')} {self.label} · Top players", description="\n".join(lines))
        await ctx.reply(embed=e, mention_author=False, allowed_mentions=NO_PING)
