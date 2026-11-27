"""TimeTap - a reaction game. The button is RED, when it turns GREEN the first one to tap wins the round.

$timetap                 open the game (works only in the TimeTap channel)  - alias $tt
$timetap set             (admin) pick the TimeTap channel from a menu        - also: $timetap channel
$timetap stats [@member] your (or someone's) reaction stats
$timetap stop            stop the running game (host or admin)

Modes (YOU choose, nothing is automatic, and nothing starts until the host presses Start):
  Training  solo practice, 10 rounds, stats + personal records
  Duo       1v1
  Trio      1v1, the winner then plays the 3rd player
  Squad     Red vs Blue, equal teams, EVEN players only (2v2 ... 5v5)
            players are paired red[i] vs blue[i]; the team with more match wins takes it,
            a tie is settled by a decider between a winner of each team

Rules: 3 hearts each. Tapping while the button is RED = false start = -1 heart and 0.5s lock-out. 0 hearts = eliminated.
The green signal comes after a random time (1-8s) - there is NO countdown to guess. First to 3 round wins takes a match.
Eliminated players watch the live scoreboard. Channel + stats live in kon.db (part of every backup).
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
from core.minigames import (BLUE, GOLD, GREEN, RED, GameCog, GameSettings, GameStopped, MiniGame, TEAM_NAME,
                            hearts, open_channel_picker, team_icon)

log = logging.getLogger("kon.timetap")

HEARTS = 3
ROUNDS_TO_WIN = 3
GREEN_TIMEOUT = 8.0          # nobody taps within 8s after green = idle round
LOCKOUT = 0.5                # after a false start you can't tap for 0.5s
TRAIN_ROUNDS = 10


# ------------------------------------------------------------------ pure helpers (stats)
def new_session() -> dict:
    return {"taps": [], "false": 0, "rounds_won": 0, "match_wins": 0, "best_clean": 0}


def apply_session(st: dict, s: dict, *, training: bool, won_title: bool) -> list:
    """Adds one finished game of one player to his saved stats. Returns the NEW personal records (text)."""
    recs = []
    for k in ("games", "false_starts", "taps", "total_ms", "rounds_won", "match_wins", "titles", "train_runs", "best_clean"):
        st.setdefault(k, 0)
    st["games"] += 1
    st["false_starts"] += s["false"]
    st["rounds_won"] += s["rounds_won"]
    st["match_wins"] += s["match_wins"]
    st["titles"] += 1 if won_title else 0
    taps = s["taps"]
    if taps:
        st["taps"] += len(taps)
        st["total_ms"] += sum(taps)
        best = min(taps)
        if st.get("best_ms") is None or best < st["best_ms"]:
            st["best_ms"] = best
            recs.append(f"Fastest reaction: **{best}ms**")
    if training:
        st["train_runs"] += 1
        if len(taps) >= 3:
            avg = round(sum(taps) / len(taps))
            if st.get("train_best_avg") is None or avg < st["train_best_avg"]:
                st["train_best_avg"] = avg
                recs.append(f"Best training average: **{avg}ms**")
        if s["best_clean"] > st["best_clean"]:
            st["best_clean"] = s["best_clean"]
            recs.append(f"Longest clean streak: **{s['best_clean']}** rounds")
    return recs


def tier(ms: int) -> str:
    if ms < 220:
        return f"{I.s('bolt')} Superhuman"
    if ms < 300:
        return f"{I.s('fire')} Lightning"
    if ms < 400:
        return f"{I.s('sparkles')} Sharp"
    if ms < 550:
        return f"{I.s('thumbs_up')} Solid"
    return f"{I.s('hourglass')} Warm up"


# ------------------------------------------------------------------ one round
class Round:
    def __init__(self, players):
        self.players = set(players)
        self.phase = "arming"           # arming (clicks ignored) -> red -> arming -> green -> done
        self.event = asyncio.Event()
        self.green_t = 0.0
        self.winner: Optional[int] = None
        self.rt = 0
        self.out: Optional[int] = None  # player who lost his last heart by a false start
        self.locked: dict = {}
        self.fs: list = []              # false starters in this round


class TapView(discord.ui.View):
    def __init__(self, game: "TimeTapGame", training: bool = False):
        super().__init__(timeout=None)
        self.game = game
        self.btn = discord.ui.Button(label="Get ready", emoji=I.e("hourglass"), style=discord.ButtonStyle.secondary,
                                     disabled=True)
        self.btn.callback = self.on_tap
        self.add_item(self.btn)
        self.end_btn = None
        if training:
            self.end_btn = discord.ui.Button(label="End session", emoji=I.e("stop"), style=discord.ButtonStyle.secondary, row=1)
            self.end_btn.callback = self.on_end
            self.add_item(self.end_btn)

    def set_state(self, state: str):
        b = self.btn
        if state == "red":
            b.label, b.style, b.emoji, b.disabled = "WAIT", discord.ButtonStyle.danger, I.e("light_red"), False
        elif state == "green":
            b.label, b.style, b.emoji, b.disabled = "TAP!", discord.ButtonStyle.success, I.e("light_green"), False
        else:
            b.label, b.style, b.emoji, b.disabled = "Next round...", discord.ButtonStyle.secondary, I.e("hourglass"), True

    def lock_all(self):
        for c in self.children:
            c.disabled = True

    async def on_tap(self, interaction: discord.Interaction):
        g, rd, uid = self.game, self.game.round, interaction.user.id
        if rd is None or uid not in rd.players:
            if await ratelimit.guard_interaction(interaction, "button"):
                await interaction.response.send_message(
                    f"{I.s('show')} You're **spectating** - watch the match and wait for the next one!", ephemeral=True)
            return
        now = time.monotonic()                     # everything below until the first await is decided in order
        if rd.phase == "red":
            if now < rd.locked.get(uid, 0):
                return await interaction.response.defer()          # still locked - spam does nothing
            rd.locked[uid] = now + LOCKOUT
            g.false_start(uid, rd)
            left = g.hearts[uid]
            text = (f"{I.s('light_red')} **False start!** You tapped while it was red - {hearts(left)}\n"
                    + (f"{I.s('skull')} You're out of hearts." if left <= 0 else f"Locked for {LOCKOUT}s."))
            return await interaction.response.send_message(text, ephemeral=True)
        if rd.phase == "green":
            rd.phase = "done"
            rd.winner = uid
            rd.rt = max(1, int((now - rd.green_t) * 1000))
            rd.event.set()
            return await interaction.response.defer()
        if rd.phase == "done" and rd.winner and uid != rd.winner:
            return await interaction.response.send_message(f"{I.s('clock')} Too slow - <@{rd.winner}> was first.", ephemeral=True)
        await interaction.response.defer()

    async def on_end(self, interaction: discord.Interaction):
        g = self.game
        if interaction.user.id != g.players[0]:
            return await interaction.response.send_message(err("This is not your session."), ephemeral=True)
        g.end_training = True
        rd = g.round
        if rd and rd.phase != "done":
            rd.phase = "done"
            rd.event.set()
        await interaction.response.defer()


# ------------------------------------------------------------------ the game
class TimeTapGame(MiniGame):
    key = "timetap"
    title = "TimeTap"
    icon = "stopwatch"
    color = BRAND
    modes = ("training", "duo", "trio", "squad")

    def __init__(self, cog, mode, channel, host):
        super().__init__(cog, mode, channel, host)
        self.hearts: dict = {}
        self.sess = defaultdict(new_session)
        self.round: Optional[Round] = None
        self.end_training = False
        self.titles: set = set()
        self.started = False
        self._saved = False

    # ----- texts -----
    def how_to(self) -> str:
        if self.mode == "training":
            return (f"Practice alone - **{TRAIN_ROUNDS} rounds**, your best / average reaction and records are saved.\n"
                    f"{I.s('light_red')} Red = **don't tap** (-1 {I.s('heart')}, 3 false starts end the session)\n"
                    f"{I.s('light_green')} Green = **TAP!** as fast as you can.")
        return (f"Wait for the button to turn **green**, then tap it **before your opponent**!\n"
                f"{I.s('light_red')} **Red = don't tap.** An early tap costs a {I.s('heart')} (3 hearts, 0 = eliminated) "
                f"and locks you for {LOCKOUT}s.\n"
                f"{I.s('light_green')} **Green = TAP!** The first tap wins the round - first to **{ROUNDS_TO_WIN}** wins the match.\n"
                f"{I.s('hourglass')} The signal comes after a **random** time - there is no countdown to guess.")

    def tag(self, uid: int) -> str:
        return hearts(self.hearts.get(uid, HEARTS), HEARTS)

    def avg_best(self, uid: int):
        t = self.sess[uid]["taps"]
        return (round(sum(t) / len(t)), min(t)) if t else (None, None)

    # ----- hooks -----
    async def on_start(self):
        self.started = True
        self.hearts = {u: HEARTS for u in self.players}
        for u in self.players:
            self.sess[u]            # every player gets a session (so "games played" is counted)

    def false_start(self, uid: int, rd: Round):
        self.hearts[uid] -= 1
        self.sess[uid]["false"] += 1
        rd.fs.append(uid)
        if self.hearts[uid] <= 0:
            rd.out = uid
            rd.phase = "done"
            rd.event.set()

    def on_match_end(self, winner: int, loser: int):
        self.sess[winner]["match_wins"] += 1

    def persist(self) -> dict:
        """Writes this game into the saved stats once. Returns {user id: [new records]}."""
        if self._saved or not self.started:
            return {}
        self._saved = True
        gid = self.channel.guild.id
        data = self.cog.settings.stats(gid)
        out = {}
        for uid in self.players:
            st = data.setdefault(str(uid), {})
            out[uid] = apply_session(st, self.sess[uid], training=self.mode == "training", won_title=uid in self.titles)
        self.cog.settings.save_stats(gid, data)
        return out

    async def on_finish(self):
        self.persist()

    # ----- embeds -----
    def vs_embed(self, a, b, score, label, state, text="", rnd=0) -> discord.Embed:
        color = {"red": RED, "green": GREEN, "end": GOLD}.get(state, BRAND)
        e = discord.Embed(color=color, title=f"{I.s('stopwatch')} TimeTap · {label}")
        if state == "intro":
            text = (f"<@{a}> {I.s('swap_small')} <@{b}>\n\nGet ready - keep your finger on the button.\n"
                    f"The signal comes at a **random** time.")
        elif state == "red":
            text = f"# {I.s('light_red')} WAIT...\nDon't tap yet - an early tap costs a {I.s('heart')}."
        elif state == "green":
            text = f"# {I.s('light_green')} TAP NOW!"
        e.description = text
        for uid in (a, b):
            tm = self.teams.get(uid)
            e.add_field(name=f"{team_icon(tm) + ' ' if tm else ''}{self.name(uid)}", inline=True,
                        value=f"{hearts(self.hearts[uid], HEARTS)}\n{I.s('star_fill')} **{score[uid]}** / {ROUNDS_TO_WIN}")
        e.set_footer(text=f"Round {rnd} · first to {ROUNDS_TO_WIN} · false start = -1 heart" if rnd else
                     f"First to {ROUNDS_TO_WIN} rounds · false start = -1 heart")
        return e

    # ----- the round engine (red -> random wait -> green) -----
    @staticmethod
    def random_delay() -> float:
        if random.random() < 0.2:                 # sometimes very quick so nobody can "count"
            return random.uniform(1.0, 2.2)
        return random.uniform(2.5, 8.0)

    async def run_round(self, msg, view: TapView, rd: Round, render):
        self.round = rd
        view.set_state("red")
        await self.edit(msg, embed=render("red"), view=view)
        rd.phase = "red"
        await self.wait_event(rd.event, self.random_delay())
        if self.stopped:
            raise GameStopped
        if rd.event.is_set():                    # someone lost his last heart (or the session was ended)
            rd.phase = "done"
            return
        rd.phase = "arming"                       # taps between "I send green" and "green is shown" are ignored
        view.set_state("green")
        await self.edit(msg, embed=render("green"), view=view)
        rd.green_t = time.monotonic()
        rd.phase = "green"
        await self.wait_event(rd.event, GREEN_TIMEOUT)
        if self.stopped:
            raise GameStopped
        if rd.phase == "green":
            rd.phase = "done"                     # nobody tapped

    # ----- versus match -----
    async def play_match(self, a: int, b: int, label: str) -> int:
        score = {a: 0, b: 0}
        view = TapView(self)
        msg = await self.channel.send(content=f"<@{a}> <@{b}>", embed=self.vs_embed(a, b, score, label, "intro"), view=view)
        await self.sleep(3.5)
        winner, rnd, idle = None, 0, 0
        while winner is None:
            rnd += 1
            rd = Round({a, b})
            await self.run_round(msg, view, rd, lambda st, rnd=rnd: self.vs_embed(a, b, score, label, st, rnd=rnd))
            view.set_state("done")
            lines, state = [], "result"
            for u in rd.fs:
                lines.append(f"{I.s('light_red')} <@{u}> false started - {hearts(self.hearts[u], HEARTS)}")
            if rd.out is not None:
                winner = b if rd.out == a else a
                lines.append(f"{I.s('skull')} <@{rd.out}> is out of hearts - <@{winner}> wins the match!")
                state = "end"
            elif rd.winner:
                idle = 0
                w = rd.winner
                score[w] += 1
                self.sess[w]["taps"].append(rd.rt)
                self.sess[w]["rounds_won"] += 1
                lines.insert(0, f"{I.s('bolt')} <@{w}> tapped first - **{rd.rt}ms** {tier(rd.rt)}")
                if score[w] >= ROUNDS_TO_WIN:
                    winner, state = w, "end"
                    lines.append(f"{I.s('trophy')} <@{w}> wins the match **{score[a]}-{score[b]}**!")
            else:
                idle += 1
                lines.insert(0, f"{I.s('hourglass')} Nobody tapped - round replayed.")
                if idle >= 2:
                    winner = max((a, b), key=lambda u: (score[u], self.hearts[u], random.random()))
                    lines.append(f"{I.s('warning')} Both players went AFK - <@{winner}> takes it on score.")
                    state = "end"
            await self.edit(msg, embed=self.vs_embed(a, b, score, label, state, "\n".join(lines), rnd), view=view)
            if winner is None:
                await self.sleep(2.5)
        self.round = None
        view.lock_all()
        await self.edit(msg, view=view)
        return winner

    # ----- training -----
    def train_embed(self, state, uid, text="", rnd=0) -> discord.Embed:
        color = {"red": RED, "green": GREEN, "end": GOLD}.get(state, BRAND)
        e = discord.Embed(color=color, title=f"{I.s('target')} TimeTap · Training")
        if state == "intro":
            text = (f"<@{uid}> - **{TRAIN_ROUNDS} rounds**. Tap when it turns green, never while it is red.\n"
                    f"The signal comes at a **random** time.")
        elif state == "red":
            text = f"# {I.s('light_red')} WAIT...\nDon't tap yet."
        elif state == "green":
            text = f"# {I.s('light_green')} TAP NOW!"
        e.description = text
        avg, best = self.avg_best(uid)
        e.add_field(name="Hearts", value=hearts(self.hearts[uid], HEARTS), inline=True)
        e.add_field(name="Best", value=f"{best}ms" if best else "-", inline=True)
        e.add_field(name="Average", value=f"{avg}ms" if avg else "-", inline=True)
        e.set_footer(text=f"Round {rnd}/{TRAIN_ROUNDS}" if rnd else f"{TRAIN_ROUNDS} rounds")
        return e

    async def play_training(self):
        uid = self.players[0]
        prev_best = self.cog.settings.stats(self.channel.guild.id).get(str(uid), {}).get("best_ms")
        view = TapView(self, training=True)
        msg = await self.channel.send(content=f"<@{uid}>", embed=self.train_embed("intro", uid), view=view)
        await self.sleep(3.5)
        rnd = misses = clean = 0
        s = self.sess[uid]
        while rnd < TRAIN_ROUNDS and self.hearts[uid] > 0 and not self.end_training and misses < 2:
            rnd += 1
            rd = Round({uid})
            await self.run_round(msg, view, rd, lambda st, rnd=rnd: self.train_embed(st, uid, rnd=rnd))
            if self.end_training:
                break
            view.set_state("done")
            lines = [f"{I.s('light_red')} False start! {hearts(self.hearts[uid], HEARTS)}" for _ in rd.fs]
            if rd.winner:
                misses = 0
                s["taps"].append(rd.rt)
                s["rounds_won"] += 1
                clean = 0 if rd.fs else clean + 1
                s["best_clean"] = max(s["best_clean"], clean)
                lines.insert(0, f"{I.s('bolt')} **{rd.rt}ms** {tier(rd.rt)}")
                if prev_best is None or rd.rt < prev_best:
                    prev_best = rd.rt
                    lines.append(f"{I.s('trophy')} New personal best!")
            elif rd.out is None:
                misses += 1
                clean = 0
                lines.insert(0, f"{I.s('hourglass')} You didn't tap in time.")
            else:
                clean = 0
            await self.edit(msg, embed=self.train_embed("result", uid, "\n".join(lines), rnd), view=view)
            if rnd < TRAIN_ROUNDS and self.hearts[uid] > 0 and misses < 2:
                await self.sleep(2.2)
        self.round = None
        view.lock_all()
        await self.edit(msg, view=view)
        recs = self.persist().get(uid, [])
        avg, best = self.avg_best(uid)
        e = discord.Embed(color=GOLD, title=f"{I.s('trophy')} Training complete")
        reason = (f"{I.s('skull')} 3 false starts - session over." if self.hearts[uid] <= 0 else
                  f"{I.s('hourglass')} You stopped tapping - session ended." if misses >= 2 else "")
        e.description = f"<@{uid}> {reason}"
        e.add_field(name="Taps", value=f"{len(s['taps'])}/{rnd}", inline=True)
        e.add_field(name="Best", value=f"{best}ms" if best else "-", inline=True)
        e.add_field(name="Average", value=f"{avg}ms" if avg else "-", inline=True)
        e.add_field(name="False starts", value=str(s["false"]), inline=True)
        e.add_field(name="Clean streak", value=str(s["best_clean"]), inline=True)
        if recs:
            e.add_field(name=f"{I.s('sparkles')} New personal records", inline=False,
                        value="\n".join(f"{I.s('star_fill')} {r}" for r in recs))
        e.set_footer(text=f"See all your numbers with {PREFIX}timetap stats · times include your ping")
        await self.channel.send(embed=e)

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
            e = discord.Embed(color=GOLD, title=f"{I.s('trophy')} TimeTap Champion")
            e.description = f"# {I.s('crown')} <@{champ}>"
            e.add_field(name="Hearts left", value=hearts(self.hearts[champ], HEARTS), inline=True)
            content = f"<@{champ}>"
        taps = [(min(s["taps"]), u) for u, s in self.sess.items() if s["taps"]]
        if taps:
            ms, u = min(taps)
            e.add_field(name=f"{I.s('bolt')} Fastest tap", value=f"<@{u}> · **{ms}ms**", inline=True)
        e.add_field(name="Match wins", inline=True, value="\n".join(
            f"<@{u}> · {s['match_wins']}" for u, s in self.sess.items() if s["match_wins"]) or "-")
        e.set_footer(text=f"GG! Play again with {PREFIX}timetap")
        await self.channel.send(content=content, embed=e)


# ------------------------------------------------------------------ the cog
class TimeTap(GameCog, commands.Cog):
    """TimeTap reaction game."""

    def __init__(self, bot):
        self.bot = bot
        self.settings = GameSettings(bot, "timetap")
        self._init_games()

    def cog_unload(self):
        self.cog_unload_games()

    def new_game(self, mode, channel, host) -> TimeTapGame:
        g = TimeTapGame(self, mode, channel, host)
        self.games[channel.id] = g
        return g

    @commands.hybrid_group(name="timetap", aliases=["tt"], fallback="play",
                           description="Play TimeTap - tap the moment the button turns green")
    @commands.guild_only()
    async def timetap(self, ctx: commands.Context):
        await self.open_game(
            ctx, label="TimeTap", icon="stopwatch", command="timetap", modes=["training", "duo", "trio", "squad"],
            intro=(f"**Be faster than your opponent.** The button is red - the moment it turns green, tap it!\n\n"
                   f"{I.s('target')} **Training** - solo practice + stats\n"
                   f"{I.s('swap')} **Duo** - 1v1\n"
                   f"{I.s('users')} **Trio** - 1v1, the winner faces the 3rd player\n"
                   f"{I.s('shield_star')} **Squad** - {I.s('team_red')} Red vs {I.s('team_blue')} Blue (even players only)"))

    @timetap.command(name="channel", aliases=["set"], description="(Admin) Pick the channel TimeTap works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def tt_channel(self, ctx: commands.Context):
        await open_channel_picker(ctx, label="TimeTap", icon="stopwatch", command="timetap",
                                  current=self.settings.channel(ctx.guild.id),
                                  save=self.settings.set_channel, clear=self.settings.clear_channel)

    @timetap.command(name="stop", description="Stop the running TimeTap game (host or admin)")
    @commands.guild_only()
    async def tt_stop(self, ctx: commands.Context):
        await self.stop_game(ctx, "TimeTap")

    @timetap.command(name="stats", description="Your TimeTap reaction stats")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def tt_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        st = self.settings.stats(ctx.guild.id).get(str(member.id))
        if not st or not st.get("games"):
            return await ctx.reply(f"{I.s('stopwatch')} {member.display_name} hasn't played TimeTap yet - "
                                   f"start with `{PREFIX}timetap`.", mention_author=False)
        taps = st.get("taps", 0)
        avg = round(st.get("total_ms", 0) / taps) if taps else None
        e = discord.Embed(color=BRAND, title=f"{I.s('stopwatch')} TimeTap stats - {member.display_name}")
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name=f"{I.s('bolt')} Best reaction", value=f"{st['best_ms']}ms" if st.get("best_ms") else "-", inline=True)
        e.add_field(name=f"{I.s('stopwatch')} Average", value=f"{avg}ms" if avg else "-", inline=True)
        e.add_field(name=f"{I.s('light_red')} False starts", value=str(st.get("false_starts", 0)), inline=True)
        e.add_field(name=f"{I.s('trophy')} Match wins", value=str(st.get("match_wins", 0)), inline=True)
        e.add_field(name=f"{I.s('crown')} Titles", value=str(st.get("titles", 0)), inline=True)
        e.add_field(name=f"{I.s('gamepad')} Games", value=str(st.get("games", 0)), inline=True)
        rec = [f"{I.s('star_fill')} Fastest reaction: **{st['best_ms']}ms**" if st.get("best_ms") else None,
               f"{I.s('star_fill')} Best training average: **{st['train_best_avg']}ms**" if st.get("train_best_avg") else None,
               f"{I.s('star_fill')} Longest clean streak: **{st['best_clean']}** rounds" if st.get("best_clean") else None]
        rec = [r for r in rec if r]
        if rec:
            e.add_field(name=f"{I.s('sparkles')} Personal records", value="\n".join(rec), inline=False)
        e.set_footer(text="Reaction times include your ping")
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot):
    await bot.add_cog(TimeTap(bot))
