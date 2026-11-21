"""Handfight - rock / paper / scissors with lobbies, teams and a live board.

$handfight                 open the game (works only in the Handfight channel)  - alias $hf
$handfight set             (admin) pick the Handfight channel from a menu        - also: $handfight channel
$handfight stats [@member] wins, losses and favourite hand
$handfight stop            stop the running game (host or admin)

Modes (you pick, nothing is automatic, nothing starts until the host presses Start):
  Duo     1v1
  Squad   Red vs Blue, equal teams, EVEN players only (2v2 ... 5v5) - same team system as TimeTap

Everybody locks a hand in secret (only you see your pick). Default is Best of 3 (first to 2 round wins); the host can
switch the lobby to Best of 10 (first to 6). A draw is replayed. When a game ends everybody gets a Play Again button.
Channel + stats live in kon.db (part of every backup).
"""
import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
from core.config import BRAND, PREFIX
from core.icons import I, err
from core.minigames import (BLUE, GOLD, GREEN, RED, GameCog, GameSettings, GameStopped, LobbyView, MiniGame,
                            TEAM_NAME, open_channel_picker, team_icon)

log = logging.getLogger("kon.handfight")

SERIES = {3: 2, 10: 6}          # best of N -> round wins needed (Best of 10 = first to 6, so it can never end level)
DEFAULT_BEST_OF = 3
PICK_SECONDS = 25
CHOICES = {"rock": "Rock", "paper": "Paper", "scissors": "Scissors"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def judge(x: str, y: str) -> int:
    """0 = draw, 1 = x wins, 2 = y wins"""
    if x == y:
        return 0
    return 1 if BEATS[x] == y else 2


def new_session() -> dict:
    return {"rounds_won": 0, "rounds_lost": 0, "draws": 0, "match_wins": 0, "match_losses": 0,
            "rock": 0, "paper": 0, "scissors": 0}


def apply_session(st: dict, s: dict, won_title: bool):
    for k in ("games", "titles", *new_session().keys()):
        st.setdefault(k, 0)
    st["games"] += 1
    st["titles"] += 1 if won_title else 0
    for k, v in s.items():
        st[k] += v


class Round:
    def __init__(self, players, deadline: int):
        self.players = set(players)
        self.picks: dict = {}
        self.phase = "pick"
        self.event = asyncio.Event()
        self.deadline = deadline


class PickView(discord.ui.View):
    def __init__(self, game: "HandfightGame"):
        super().__init__(timeout=None)
        self.game = game
        for key, label in CHOICES.items():
            b = discord.ui.Button(label=label, emoji=I.e(key), style=discord.ButtonStyle.primary)
            b.callback = (lambda i, k=key: self.pick(i, k))
            self.add_item(b)

    def lock_all(self):
        for c in self.children:
            c.disabled = True

    def unlock_all(self):
        for c in self.children:
            c.disabled = False

    async def pick(self, interaction: discord.Interaction, choice: str):
        g, rd, uid = self.game, self.game.round, interaction.user.id
        if rd is None or uid not in rd.players:
            if await ratelimit.guard_interaction(interaction, "button"):
                await interaction.response.send_message(
                    f"{I.s('show')} You're **spectating** - watch the fight and wait for the next one!", ephemeral=True)
            return
        if rd.phase != "pick":
            return await interaction.response.defer()
        if uid in rd.picks:
            return await interaction.response.send_message(
                err(f"You already locked **{CHOICES[rd.picks[uid]]}** - no changes!"), ephemeral=True)
        rd.picks[uid] = choice
        g.sess[uid][choice] += 1
        both = len(rd.picks) == len(rd.players)
        await interaction.response.send_message(
            f"{I.s(choice)} You locked **{CHOICES[choice]}**." + ("" if both else " Waiting for your opponent..."), ephemeral=True)
        if both:
            rd.event.set()
        else:
            asyncio.create_task(g.refresh_pick())


class PlayAgainView(discord.ui.View):
    """Shown under the winner announcement - any player (winner or loser) can open a rematch lobby."""

    def __init__(self, game: "HandfightGame"):
        super().__init__(timeout=600)
        self.game = game
        self.message: Optional[discord.Message] = None
        b = discord.ui.Button(label="Play Again", emoji=I.e("repeat"), style=discord.ButtonStyle.success)
        b.callback = self.again
        self.add_item(b)

    async def again(self, interaction: discord.Interaction):
        g = self.game
        cog, ch = g.cog, g.channel
        if interaction.user.id not in g.players:
            return await interaction.response.send_message(
                err("Only the players of that game can start a rematch - use the command to start your own."), ephemeral=True)
        if ch.id in cog.games or ch.id in cog.pending:
            return await interaction.response.send_message(err("A game is already running here."), ephemeral=True)
        ng = cog.new_game(g.mode, ch, interaction.user)          # the player who pressed is the new host
        ng.best_of = g.best_of
        ng.players = list(g.players)                              # same players, same teams - just press Start
        ng.teams = dict(g.teams)
        ng.names = {**g.names, interaction.user.id: interaction.user.display_name}
        self.stop()
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        ng.view = LobbyView(ng)
        ng.lobby_msg = await ch.send(embed=ng.lobby_embed(), view=ng.view)
        ng.task = asyncio.create_task(ng.run())

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HandfightGame(MiniGame):
    key = "handfight"
    title = "Handfight"
    icon = "rock"
    color = BRAND
    modes = ("duo", "squad")
    series_options = tuple(SERIES)                # (3, 10) -> host toggles "Best of 3" / "Best of 10" in the lobby

    @property
    def to_win(self) -> int:
        return SERIES.get(self.best_of, SERIES[DEFAULT_BEST_OF])

    def __init__(self, cog, mode, channel, host):
        super().__init__(cog, mode, channel, host)
        self.best_of = DEFAULT_BEST_OF
        self.sess = defaultdict(new_session)
        self.round: Optional[Round] = None
        self.msg: Optional[discord.Message] = None
        self.cur: Optional[tuple] = None          # (a, b, label, score, rnd)
        self.titles: set = set()
        self.started = False
        self._saved = False

    def how_to(self) -> str:
        return (f"Lock in **{I.s('rock')} Rock, {I.s('paper')} Paper or {I.s('scissors')} Scissors** - your pick stays secret until both have chosen.\n"
                f"{I.s('rock')} beats {I.s('scissors')} · {I.s('scissors')} beats {I.s('paper')} · {I.s('paper')} beats {I.s('rock')}\n"
                f"**Best of {self.best_of}** - first to **{self.to_win}** round wins takes the match. A draw is replayed.\n"
                f"{I.s('hourglass')} You have {PICK_SECONDS}s per round - no pick = you lose the round.")

    async def on_start(self):
        self.started = True
        for u in self.players:
            self.sess[u]

    def on_match_end(self, winner: int, loser: int):
        self.sess[winner]["match_wins"] += 1
        self.sess[loser]["match_losses"] += 1

    async def on_finish(self):
        if self._saved or not self.started:
            return
        self._saved = True
        gid = self.channel.guild.id
        data = self.cog.settings.stats(gid)
        for uid in self.players:
            apply_session(data.setdefault(str(uid), {}), self.sess[uid], uid in self.titles)
        self.cog.settings.save_stats(gid, data)

    # ----- embeds -----
    def fight_embed(self, state: str, text: str = "") -> discord.Embed:
        a, b, label, score, rnd = self.cur
        rd = self.round
        color = {"end": GOLD, "reveal": BRAND}.get(state, BRAND)
        e = discord.Embed(color=color, title=f"{I.s('rock')} Handfight · {label}")
        if state == "pick":
            text = (f"# {I.s('rock')} {I.s('paper')} {I.s('scissors')}\n"
                    f"Choose your hand with the buttons below - only you see your pick.\n"
                    f"{I.s('hourglass')} ends <t:{rd.deadline}:R>")
        e.description = text
        for uid in (a, b):
            tm = self.teams.get(uid)
            status = ""
            if state == "pick":
                status = f"{I.s('check')} Locked in" if rd and uid in rd.picks else f"{I.s('hourglass')} Choosing..."
            e.add_field(name=f"{team_icon(tm) + ' ' if tm else ''}{self.name(uid)}", inline=True,
                        value=f"{I.s('star_fill')} **{score[uid]}** / {self.to_win}" + (f"\n{status}" if status else ""))
        e.set_footer(text=f"Round {rnd} · Best of {self.best_of} · first to {self.to_win}")
        return e

    async def refresh_pick(self):
        if self.round and self.round.phase == "pick":
            await self.edit(self.msg, embed=self.fight_embed("pick"))

    # ----- one match -----
    async def play_match(self, a: int, b: int, label: str) -> int:
        score = {a: 0, b: 0}
        view = PickView(self)
        self.msg = None
        winner, rnd, idle = None, 0, 0
        while winner is None:
            rnd += 1
            rd = Round({a, b}, int(time.time()) + PICK_SECONDS)
            self.round, self.cur = rd, (a, b, label, score, rnd)
            view.unlock_all()
            if self.msg is None:
                self.msg = await self.channel.send(content=f"<@{a}> <@{b}>", embed=self.fight_embed("pick"), view=view)
            else:
                await self.edit(self.msg, embed=self.fight_embed("pick"), view=view)
            await self.wait_event(rd.event, PICK_SECONDS)
            if self.stopped:
                raise GameStopped
            rd.phase = "reveal"
            view.lock_all()
            lines, state = [], "reveal"
            if len(rd.picks) == 2:
                await self.edit(self.msg, embed=self.fight_embed(
                    "reveal", f"# {I.s('rock')} ... {I.s('paper')} ... {I.s('scissors')} ...\n**Rock... Paper... Scissors...**"), view=view)
                await self.sleep(1.6)
                pa, pb = rd.picks[a], rd.picks[b]
                head = f"# {I.s(pa)} **VS** {I.s(pb)}\n<@{a}> · **{CHOICES[pa]}**   |   <@{b}> · **{CHOICES[pb]}**"
                r = judge(pa, pb)
                if r == 0:
                    for u in (a, b):
                        self.sess[u]["draws"] += 1
                    lines = [head, f"{I.s('swap')} **Draw!** Both picked {CHOICES[pa]} - go again."]
                else:
                    w, l = (a, b) if r == 1 else (b, a)
                    score[w] += 1
                    self.sess[w]["rounds_won"] += 1
                    self.sess[l]["rounds_lost"] += 1
                    lines = [head, f"{I.s('bolt')} <@{w}> wins the round! **{CHOICES[rd.picks[w]]}** beats **{CHOICES[rd.picks[l]]}**"]
                    idle = 0
                    if score[w] >= self.to_win:
                        winner, state = w, "end"
            elif len(rd.picks) == 1:
                w = next(iter(rd.picks))
                l = b if w == a else a
                score[w] += 1
                self.sess[w]["rounds_won"] += 1
                self.sess[l]["rounds_lost"] += 1
                idle = 0
                lines = [f"{I.s('hourglass')} <@{l}> didn't pick in time - <@{w}> wins the round."]
                if score[w] >= self.to_win:
                    winner, state = w, "end"
            else:
                idle += 1
                lines = [f"{I.s('hourglass')} Nobody picked - round replayed."]
                if idle >= 2:
                    winner = random.choice((a, b)) if score[a] == score[b] else max((a, b), key=lambda u: score[u])
                    lines.append(f"{I.s('warning')} Both players went AFK - <@{winner}> takes it.")
                    state = "end"
            if winner is not None:
                lines.append(f"{I.s('trophy')} <@{winner}> wins the match **{score[a]}-{score[b]}**!")
            await self.edit(self.msg, embed=self.fight_embed(state, "\n".join(lines)), view=view)
            if winner is None:
                await self.sleep(3)
        self.round = None
        return winner

    # ----- winner -----
    async def announce_winner(self, winners: list, team: Optional[str]):
        if team:
            members = [u for u in self.players if self.teams[u] == team]
            self.titles = set(members)
            e = discord.Embed(color=RED if team == "red" else BLUE, title=f"{I.s('trophy')} TEAM {TEAM_NAME[team].upper()} WINS!")
            e.description = (f"# {team_icon(team)} Team {TEAM_NAME[team]}\n"
                             + "\n".join(f"{I.s('crown')} <@{u}>" for u in members))
            content = " ".join(f"<@{u}>" for u in members)
        else:
            champ = winners[0]
            self.titles = {champ}
            e = discord.Embed(color=GOLD, title=f"{I.s('trophy')} Handfight Champion")
            e.description = f"# {I.s('crown')} <@{champ}>"
            content = f"<@{champ}>"
        e.add_field(name="Match wins", inline=True, value="\n".join(
            f"<@{u}> · {s['match_wins']}" for u, s in self.sess.items() if s["match_wins"]) or "-")
        e.set_footer(text=f"GG! · Best of {self.best_of} · press Play Again for a rematch")
        view = PlayAgainView(self)
        view.message = await self.channel.send(content=content, embed=e, view=view)


class Handfight(GameCog, commands.Cog):
    """Handfight (rock paper scissors)."""

    def __init__(self, bot):
        self.bot = bot
        self.settings = GameSettings(bot, "handfight")
        self._init_games()

    def cog_unload(self):
        self.cog_unload_games()

    def new_game(self, mode, channel, host) -> HandfightGame:
        g = HandfightGame(self, mode, channel, host)
        self.games[channel.id] = g
        return g

    @commands.hybrid_group(name="handfight", aliases=["hf"], fallback="play",
                           description="Play Handfight - rock, paper, scissors")
    @commands.guild_only()
    async def handfight(self, ctx: commands.Context):
        await self.open_game(
            ctx, label="Handfight", icon="rock", command="handfight", modes=["duo", "squad"],
            intro=(f"**Rock, Paper, Scissors** - but with lobbies, teams and a live board.\n"
                   f"Default **Best of 3** - the host can switch to **Best of 10** in the lobby.\n\n"
                   f"{I.s('swap')} **Duo** - 1v1\n"
                   f"{I.s('shield_star')} **Squad** - {I.s('team_red')} Red vs {I.s('team_blue')} Blue (even players only)"))

    @handfight.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Handfight works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def hf_channel(self, ctx: commands.Context):
        await open_channel_picker(ctx, label="Handfight", icon="rock", command="handfight",
                                  current=self.settings.channel(ctx.guild.id),
                                  save=self.settings.set_channel, clear=self.settings.clear_channel)

    @handfight.command(name="stop", description="Stop the running Handfight game (host or admin)")
    @commands.guild_only()
    async def hf_stop(self, ctx: commands.Context):
        await self.stop_game(ctx, "Handfight")

    @handfight.command(name="stats", description="Your Handfight record")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def hf_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        st = self.settings.stats(ctx.guild.id).get(str(member.id))
        if not st or not st.get("games"):
            return await ctx.reply(f"{I.s('rock')} {member.display_name} hasn't played Handfight yet - "
                                   f"start with `{PREFIX}handfight`.", mention_author=False)
        hands = {k: st.get(k, 0) for k in CHOICES}
        fav = max(hands, key=hands.get) if any(hands.values()) else None
        e = discord.Embed(color=BRAND, title=f"{I.s('rock')} Handfight stats - {member.display_name}")
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name=f"{I.s('trophy')} Match wins", value=str(st.get("match_wins", 0)), inline=True)
        e.add_field(name=f"{I.s('skull')} Match losses", value=str(st.get("match_losses", 0)), inline=True)
        e.add_field(name=f"{I.s('crown')} Titles", value=str(st.get("titles", 0)), inline=True)
        e.add_field(name=f"{I.s('bolt')} Rounds won", value=str(st.get("rounds_won", 0)), inline=True)
        e.add_field(name=f"{I.s('swap')} Draws", value=str(st.get("draws", 0)), inline=True)
        e.add_field(name=f"{I.s('gamepad')} Games", value=str(st.get("games", 0)), inline=True)
        if fav:
            e.add_field(name="Favourite hand", inline=False,
                        value=f"{I.s(fav)} **{CHOICES[fav]}** · " + "   ".join(f"{I.s(k)} {v}" for k, v in hands.items()))
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot):
    await bot.add_cog(Handfight(bot))
