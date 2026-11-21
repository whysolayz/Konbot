"""Shared pieces for the mini games (TimeTap, Handfight):

* GameSettings    - per-server channel (+ stats) saved in kon.db  (so every backup / export has it)
* open_channel_picker - "$<game> set" -> a channel SELECT MENU (nobody has to ping a channel)
* gate            - "this game only works in the channel an admin set"
* MiniGame        - lobby (no autostart!) + Duo / Trio / Squad (Red vs Blue) tournament + scoreboard / spectators
"""
import asyncio
import logging
import random
import time
from typing import Optional

import discord

from .config import BRAND, PREFIX
from .errors import VCError
from .icons import I, err, ok
from .ui import OwnedView

log = logging.getLogger("kon.minigames")

RED = 0xED4245
BLUE = 0x3B82F6
GREEN = 0x57F287
GOLD = 0xF5C542
NO_PING = discord.AllowedMentions.none()
LOBBY_IDLE = 900            # a lobby nobody starts is closed after 15 minutes (the game NEVER starts by itself)

MODES = {
    "training": {"label": "Training", "icon": "target", "min": 1, "max": 1,
                 "desc": "Solo practice - your reaction stats are saved"},
    "duo":      {"label": "Duo", "icon": "swap", "min": 2, "max": 2,
                 "desc": "1v1 - the winner of the match wins"},
    "trio":     {"label": "Trio", "icon": "users", "min": 3, "max": 3,
                 "desc": "1v1, the winner then faces the 3rd player"},
    "squad":    {"label": "Squad", "icon": "shield_star", "min": 4, "max": 10,
                 "desc": "Red vs Blue - equal teams, even players only"},
}
TEAM_NAME = {"red": "Red", "blue": "Blue"}


def hearts(n: int, total: int = 3) -> str:
    n = max(0, min(n, total))
    return I.s("heart") * n + I.s("heart_broken") * (total - n)


def team_icon(team: Optional[str]) -> str:
    return I.s("team_red") if team == "red" else I.s("team_blue") if team == "blue" else ""


def missing_perms(channel, me, extra=()) -> list:
    need = ("view_channel", "send_messages", "embed_links") + tuple(extra)
    p = channel.permissions_for(me)
    return [n.replace("_", " ").title() for n in need if not getattr(p, n, False)]


# ============================================================================ settings (kon.db)
class GameSettings:
    """channel + stats of one game, stored in the `settings` table of kon.db -> part of every backup."""

    def __init__(self, bot, key: str):
        self.bot, self.key = bot, key

    def channel(self, gid: int) -> Optional[int]:
        return self.bot.db.get_settings(gid, self.key).get("channel")

    def set_channel(self, gid: int, cid: int):
        d = self.bot.db.get_settings(gid, self.key)
        d["channel"] = cid
        self.bot.db.set_settings(gid, self.key, d)

    def clear_channel(self, gid: int) -> bool:
        d = self.bot.db.get_settings(gid, self.key)
        if d.pop("channel", None) is None:
            return False
        self.bot.db.set_settings(gid, self.key, d)
        return True

    def stats(self, gid: int) -> dict:
        return self.bot.db.get_settings(gid, self.key + "_stats")

    def save_stats(self, gid: int, data: dict):
        self.bot.db.set_settings(gid, self.key + "_stats", data)


async def is_admin(ctx) -> bool:
    return bool(ctx.author.guild_permissions.manage_guild or await ctx.bot.is_owner_in(ctx.author, ctx.guild.id))


async def gate(ctx, *, label: str, icon: str, command: str, cid: Optional[int], extra_perms=()) -> bool:
    """True when the game may be used here. Otherwise tells the user why and returns False."""
    if cid is None:
        if await is_admin(ctx):
            await ctx.reply(f"{I.s(icon)} No **{label}** channel yet - run `{PREFIX}{command} set` and pick one.",
                            mention_author=False)
        else:
            await ctx.reply(err(f"An admin hasn't set a {label} channel yet."), mention_author=False)
        return False
    if ctx.channel.id != cid:
        kw = {"delete_after": 8} if ctx.interaction is None else {}
        await ctx.reply(f"{I.s(icon)} {label} only works in <#{cid}>.", mention_author=False, ephemeral=True, **kw)
        return False
    miss = missing_perms(ctx.channel, ctx.guild.me, extra_perms)
    if miss:
        await ctx.reply(err(f"I need **{', '.join(miss)}** in this channel."), mention_author=False)
        return False
    return True


# ============================================================================ channel picker ($game set)
class ChannelPicker(OwnedView):
    def __init__(self, invoker_id: int, *, label: str, icon: str, command: str, current: Optional[int],
                 save, clear, extra_perms=()):
        super().__init__(invoker_id, timeout=120)
        self.label, self.icon, self.command = label, icon, command
        self.save, self.clear, self.extra_perms = save, clear, extra_perms
        self.sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                            placeholder=f"Select the {label} channel...", min_values=1, max_values=1)
        self.sel.callback = self.picked
        self.add_item(self.sel)
        if current:
            b = discord.ui.Button(label="Remove channel", emoji=I.e("cross"), style=discord.ButtonStyle.secondary)
            b.callback = self.removed
            self.add_item(b)

    async def picked(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(self.sel.values[0].id)
        if ch is None:
            return await interaction.response.send_message(err("I can't see that channel."), ephemeral=True)
        miss = missing_perms(ch, interaction.guild.me, self.extra_perms)
        if miss:
            return await interaction.response.send_message(
                err(f"I need **{', '.join(miss)}** in {ch.mention} first."), ephemeral=True)
        self.save(interaction.guild.id, ch.id)
        self.stop()
        e = discord.Embed(color=GREEN, title=f"{I.s('check')} {self.label} channel set",
                          description=f"{self.label} now works only in {ch.mention}.\nStart it there with `{PREFIX}{self.command}`.")
        await interaction.response.edit_message(embed=e, view=None)

    async def removed(self, interaction: discord.Interaction):
        self.clear(interaction.guild.id)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(color=BRAND, description=ok(f"{self.label} channel removed.")), view=None)


async def open_channel_picker(ctx, *, label: str, icon: str, command: str, current: Optional[int], save, clear,
                              extra_perms=()):
    e = discord.Embed(color=BRAND, title=f"{I.s(icon)} {label} channel", description=(
        f"**Current:** {f'<#{current}>' if current else '*not set*'}\n"
        f"Pick the channel from the menu below - no need to ping it."))
    view = ChannelPicker(ctx.author.id, label=label, icon=icon, command=command, current=current,
                         save=save, clear=clear, extra_perms=extra_perms)
    view.message = await ctx.reply(embed=e, view=view, mention_author=False)


# ============================================================================ mode picker
class ModePicker(OwnedView):
    """The game NEVER picks a mode for you - the host chooses it here, then the lobby opens."""

    def __init__(self, cog, host: discord.Member, channel, modes: list):
        super().__init__(host.id, timeout=120)
        self.cog, self.host, self.channel = cog, host, channel
        self.sel = discord.ui.Select(placeholder="Choose a mode...", options=[
            discord.SelectOption(label=MODES[m]["label"], value=m, emoji=I.e(MODES[m]["icon"]),
                                 description=MODES[m]["desc"]) for m in modes])
        self.sel.callback = self.picked
        self.add_item(self.sel)

    async def picked(self, interaction: discord.Interaction):
        if self.channel.id in self.cog.games:
            self.stop()
            return await interaction.response.edit_message(content="A game is already running here.", embed=None, view=None)
        game = self.cog.new_game(self.sel.values[0], self.channel, self.host)
        self.cog.pending.discard(self.channel.id)
        self.stop()
        game.lobby_msg = interaction.message
        game.view = LobbyView(game)
        await interaction.response.edit_message(embed=game.lobby_embed(), view=game.view)
        game.task = asyncio.create_task(game.run())

    async def on_timeout(self):
        self.cog.pending.discard(self.channel.id)
        await super().on_timeout()


# ============================================================================ lobby
class LobbyView(discord.ui.View):
    def __init__(self, game: "MiniGame"):
        super().__init__(timeout=None)
        self.game = game
        mode = game.mode
        if mode == "squad":
            self._add("Join Red", I.e("team_red"), discord.ButtonStyle.danger, lambda i: self.join(i, "red"))
            self._add("Join Blue", I.e("team_blue"), discord.ButtonStyle.primary, lambda i: self.join(i, "blue"))
        elif mode != "training":
            self._add("Join", I.e("user_add"), discord.ButtonStyle.success, self.join)
        if mode != "training":
            self._add("Leave", I.e("logout"), discord.ButtonStyle.secondary, self.leave)
        self._add("Start", I.e("play"), discord.ButtonStyle.success, self.start, row=1)
        self._add("Cancel", I.e("cross"), discord.ButtonStyle.secondary, self.cancel, row=1)
        self.series_btn = None
        if game.series_options:                       # e.g. Handfight: Best of 3 <-> Best of 10 (host only)
            self.series_btn = self._add(game.series_label(), I.e("repeat"), discord.ButtonStyle.primary,
                                        self.cycle_series, row=1)

    def _add(self, label, emoji, style, cb, row=0):
        b = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        b.callback = cb
        self.add_item(b)
        return b

    async def cycle_series(self, interaction: discord.Interaction):
        g = self.game
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host can change the match length."), ephemeral=True)
        g.cycle_series()
        self.series_btn.label = g.series_label()
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    def is_host(self, member) -> bool:
        return member.id == self.game.host.id or member.guild_permissions.manage_guild

    async def join(self, interaction: discord.Interaction, team: Optional[str] = None):
        g = self.game
        problem = g.add_player(interaction.user, team)
        if problem:
            return await interaction.response.send_message(err(problem), ephemeral=True)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    async def leave(self, interaction: discord.Interaction):
        g = self.game
        if interaction.user.id == g.host.id:
            return await interaction.response.send_message(err("You are the host - press **Cancel** to close the lobby."), ephemeral=True)
        if interaction.user.id not in g.players:
            return await interaction.response.send_message(err("You are not in this game."), ephemeral=True)
        g.remove_player(interaction.user.id)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    async def start(self, interaction: discord.Interaction):
        g = self.game
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host can start the game."), ephemeral=True)
        problem = g.start_error()
        if problem:
            return await interaction.response.send_message(err(problem), ephemeral=True)
        g.start_event.set()
        await interaction.response.defer()

    async def cancel(self, interaction: discord.Interaction):
        if not self.is_host(interaction.user):
            return await interaction.response.send_message(err("Only the host or an admin can cancel."), ephemeral=True)
        self.game.stop()
        await interaction.response.defer()


class GameStopped(Exception):
    pass


# ============================================================================ the game base
class MiniGame:
    """Subclasses set key/title/icon/color/how_to and implement play_match() (+ play_training() for TimeTap)."""
    key = "game"
    title = "Game"
    icon = "gamepad"
    color = BRAND
    modes = ("duo", "squad")
    series_options: tuple = ()                    # e.g. (3, 10) = "Best of 3" / "Best of 10"; empty = no such setting

    def __init__(self, cog, mode: str, channel, host: discord.Member):
        self.cog, self.mode, self.channel, self.host = cog, mode, channel, host
        self.players: list = [host.id]
        self.teams: dict = {host.id: "red"} if mode == "squad" else {}
        self.names: dict = {host.id: host.display_name}
        self.state = "lobby"                      # lobby -> playing -> done
        self.stopped = False
        self.expired = False
        self.start_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self._never = asyncio.Event()
        self.lobby_msg: Optional[discord.Message] = None
        self.board_msg: Optional[discord.Message] = None
        self.view: Optional[LobbyView] = None
        self.task: Optional[asyncio.Task] = None
        # tournament state
        self.alive: set = set()
        self.out: list = []                       # eliminated players (they spectate)
        self.now: Optional[tuple] = None          # (a, b, label)
        self.upcoming: list = []
        self.champion: Optional[int] = None

    # ----- match length (only games with series_options) -----
    best_of: int = 0

    def series_label(self) -> str:
        return f"Best of {self.best_of}"

    def cycle_series(self):
        opts = list(self.series_options)
        self.best_of = opts[(opts.index(self.best_of) + 1) % len(opts)] if self.best_of in opts else opts[0]

    # ----- text pieces subclasses can override -----
    def how_to(self) -> str:
        return ""

    def tag(self, uid: int) -> str:
        """extra text after a player on the scoreboard (TimeTap shows hearts here)"""
        return ""

    def name(self, uid: int) -> str:
        return self.names.get(uid, "Player")

    # ----- lobby -----
    def add_player(self, member, team: Optional[str]) -> Optional[str]:
        m = MODES[self.mode]
        uid = member.id
        if self.mode == "squad":
            half = m["max"] // 2
            size = sum(1 for t in self.teams.values() if t == team)
            if uid in self.players:
                if self.teams.get(uid) == team:
                    return f"You're already in Team {TEAM_NAME[team]}."
                if size >= half:
                    return f"Team {TEAM_NAME[team]} is full ({half}/{half})."
                self.teams[uid] = team
                return None
            if len(self.players) >= m["max"] or size >= half:
                return f"Team {TEAM_NAME[team]} is full ({half}/{half})." if size >= half else "The lobby is full."
            self.players.append(uid)
            self.teams[uid] = team
        else:
            if uid in self.players:
                return "You are already in this game."
            if len(self.players) >= m["max"]:
                return "The lobby is full."
            self.players.append(uid)
        self.names[uid] = member.display_name
        return None

    def remove_player(self, uid: int):
        if uid in self.players:
            self.players.remove(uid)
        self.teams.pop(uid, None)

    def start_error(self) -> Optional[str]:
        m, n = MODES[self.mode], len(self.players)
        if self.mode == "squad":
            red = sum(1 for t in self.teams.values() if t == "red")
            blue = sum(1 for t in self.teams.values() if t == "blue")
            if n < m["min"] or n % 2:
                return f"Squad needs an **even** number of players (minimum 4). Now: {n}."
            if red != blue:
                return f"Teams must be equal - Red **{red}** · Blue **{blue}**. Ask someone to switch."
            return None
        if n != m["min"]:
            return f"{m['label']} needs exactly **{m['min']}** player{'s' if m['min'] != 1 else ''} ({n}/{m['min']})."
        return None

    def lobby_embed(self, status: Optional[str] = None) -> discord.Embed:
        m = MODES[self.mode]
        e = discord.Embed(title=f"{I.s(self.icon)} {self.title} · {m['label']}", color=self.color,
                          description=self.how_to())
        if self.mode == "squad":
            for t, col in (("red", "Red"), ("blue", "Blue")):
                members = [u for u in self.players if self.teams.get(u) == t]
                e.add_field(name=f"{team_icon(t)} Team {col} ({len(members)})",
                            value="\n".join(f"<@{u}>" + (f" {I.s('crown')}" if u == self.host.id else "") for u in members) or "*empty*",
                            inline=True)
        else:
            e.add_field(name=f"Players ({len(self.players)}/{m['max']})", inline=False, value="\n".join(
                f"{i}. <@{u}>" + (f" {I.s('crown')}" if u == self.host.id else "") for i, u in enumerate(self.players, 1)))
        e.set_footer(text=status or "The game starts ONLY when the host presses Start.")
        return e

    async def lobby_wait(self):
        try:
            await asyncio.wait_for(self.start_event.wait(), LOBBY_IDLE)
        except asyncio.TimeoutError:
            self.expired = True
            self.stop()
        if self.view:
            self.view.stop()
            for item in self.view.children:
                item.disabled = True
        e = self.lobby_embed()
        if self.stopped:
            e.title, e.color = f"{I.s(self.icon)} {self.title} - {'lobby expired' if self.expired else 'cancelled'}", RED
        else:
            e.title = f"{I.s(self.icon)} {self.title} · {MODES[self.mode]['label']} - started!"
        e.set_footer(text=None)
        await self.edit(self.lobby_msg, embed=e, view=self.view)

    # ----- helpers -----
    def stop(self):
        self.stopped = True
        self.start_event.set()
        self.stop_event.set()

    async def edit(self, msg, **kw) -> bool:
        if msg is None:
            return False
        kw.setdefault("allowed_mentions", NO_PING)
        try:
            await msg.edit(**kw)
            return True
        except discord.HTTPException:
            return False

    async def wait_event(self, ev: asyncio.Event, timeout: float) -> bool:
        """True when `ev` fired. False on timeout or when the game was stopped."""
        t1 = asyncio.ensure_future(ev.wait())
        t2 = asyncio.ensure_future(self.stop_event.wait())
        done, pending = await asyncio.wait({t1, t2}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        return t1 in done and not self.stopped

    async def sleep(self, seconds: float):
        await self.wait_event(self._never, seconds)
        if self.stopped:
            raise GameStopped

    # ----- scoreboard (what spectators watch) -----
    def board_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"{I.s(self.icon)} {self.title} · {MODES[self.mode]['label']} - live board",
                          color=self.color)
        if self.now:
            a, b, label = self.now
            e.description = f"{I.s('play')} **{label}**\n<@{a}> {I.s('swap_small')} <@{b}>"
        elif self.champion is None:
            e.description = f"{I.s('hourglass')} Next match is loading..."
        squad = self.mode == "squad"
        alive = [u for u in self.players if u in self.alive]

        def line(u):
            return f"{team_icon(self.teams.get(u)) + ' ' if squad else ''}<@{u}> {self.tag(u)}".strip()
        if alive:
            e.add_field(name=f"Still in ({len(alive)})", value="\n".join(line(u) for u in alive), inline=True)
        if self.out:
            e.add_field(name=f"{I.s('show')} Spectating ({len(self.out)})",
                        value="\n".join(f"{I.s('skull')} <@{u}>" for u in self.out), inline=True)
        if self.upcoming:
            e.add_field(name="Up next", inline=False, value="\n".join(
                f"{i}. <@{a}> {I.s('swap_small')} <@{b}>" for i, (a, b) in enumerate(self.upcoming, 1)))
        e.set_footer(text="Eliminated players can keep watching this channel live.")
        return e

    async def refresh_board(self):
        await self.edit(self.board_msg, embed=self.board_embed())

    # ----- flow -----
    async def play_match(self, a: int, b: int, label: str) -> int:
        raise NotImplementedError

    async def play_training(self):
        raise NotImplementedError

    async def on_start(self):
        pass

    def on_match_end(self, winner: int, loser: int):
        pass

    async def on_finish(self):
        pass

    async def announce_winner(self, winners: list, team: Optional[str]):
        raise NotImplementedError

    def eliminate(self, uid: int):
        self.alive.discard(uid)
        if uid not in self.out:
            self.out.append(uid)

    async def match(self, a: int, b: int, label: str, upcoming=()) -> int:
        self.now, self.upcoming = (a, b, label), list(upcoming)
        await self.refresh_board()
        winner = await self.play_match(a, b, label)
        if self.stopped:
            raise GameStopped
        loser = b if winner == a else a
        self.eliminate(loser)
        self.on_match_end(winner, loser)
        self.now = None
        await self.refresh_board()
        await self.sleep(2.5)
        return winner

    async def tournament(self):
        if self.mode == "duo":
            a, b = self.players
            self.champion = await self.match(a, b, "Final")
            return await self.announce_winner([self.champion], None)
        if self.mode == "trio":
            order = random.sample(self.players, 3)
            w = await self.match(order[0], order[1], "Match 1")
            self.champion = await self.match(w, order[2], "Final")
            return await self.announce_winner([self.champion], None)
        # ---- squad: red[i] vs blue[i]; more match wins = team wins; tie -> a winner of each team plays the decider ----
        reds = [u for u in self.players if self.teams[u] == "red"]
        blues = [u for u in self.players if self.teams[u] == "blue"]
        random.shuffle(reds)
        random.shuffle(blues)
        pairs = list(zip(reds, blues))
        wins = {"red": [], "blue": []}
        for i, (r, b) in enumerate(pairs):
            w = await self.match(r, b, f"Match {i + 1}/{len(pairs)}", upcoming=pairs[i + 1:])
            wins[self.teams[w]].append(w)
        if len(wins["red"]) != len(wins["blue"]):
            team = "red" if len(wins["red"]) > len(wins["blue"]) else "blue"
            winners = wins[team]
        else:
            r, b = random.choice(wins["red"]), random.choice(wins["blue"])
            await self.channel.send(f"{I.s('fire')} **It's tied {len(wins['red'])}-{len(wins['blue'])}!** "
                                    f"Decider: <@{r}> vs <@{b}>", allowed_mentions=NO_PING)
            await self.sleep(2)
            w = await self.match(r, b, "Decider")
            team = self.teams[w]
            winners = [u for u in wins[team]]
        self.champion = winners[0]
        await self.announce_winner(winners, team)

    async def play(self):
        self.state = "playing"
        self.alive = set(self.players)
        await self.on_start()
        if self.mode == "training":
            return await self.play_training()
        self.board_msg = await self.channel.send(embed=self.board_embed())
        await self.sleep(1.5)
        await self.tournament()

    async def run(self):
        try:
            await self.lobby_wait()
            if self.stopped:
                return
            await self.play()
        except GameStopped:
            await self.channel.send(f"{I.s('stop')} {self.title} was stopped.")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed in channel %s", self.title, self.channel.id)
            try:
                await self.channel.send(f"{I.s('warning')} {self.title} hit an error and stopped.")
            except discord.HTTPException:
                pass
        finally:
            self.state = "done"
            try:
                await self.on_finish()
            except Exception:
                log.exception("%s: saving stats failed", self.title)
            self.cog.games.pop(self.channel.id, None)


# ============================================================================ cog base (shared commands logic)
class GameCog:
    """Mixin: games dict + 'one game per channel' + the mode picker."""

    def _init_games(self):
        self.games: dict = {}
        self.pending: set = set()

    def cog_unload_games(self):
        for g in list(self.games.values()):
            g.stop()

    async def open_game(self, ctx, *, label: str, icon: str, command: str, modes: list, intro: str):
        cid = self.settings.channel(ctx.guild.id)
        if not await gate(ctx, label=label, icon=icon, command=command, cid=cid):
            return
        if ctx.channel.id in self.games or ctx.channel.id in self.pending:
            return await ctx.reply(err(f"A {label} game is already running here - join it or wait for it to finish."),
                                   mention_author=False, ephemeral=True)
        self.pending.add(ctx.channel.id)
        e = discord.Embed(color=BRAND, title=f"{I.s(icon)} {label}", description=intro)
        e.set_footer(text="Pick a mode - nothing starts until you press Start in the lobby.")
        view = ModePicker(self, ctx.author, ctx.channel, modes)
        try:
            view.message = await ctx.reply(embed=e, view=view, mention_author=False)
        except Exception:
            self.pending.discard(ctx.channel.id)
            raise

    async def stop_game(self, ctx, label: str):
        game = self.games.get(ctx.channel.id)
        if game is None:
            return await ctx.reply(err(f"No {label} game is running here."), mention_author=False, ephemeral=True)
        if ctx.author.id != game.host.id and not await is_admin(ctx):
            return await ctx.reply(err("Only the host or an admin can stop the game."), mention_author=False, ephemeral=True)
        game.stop()
        await ctx.reply(f"{I.s('stop')} Stopping {label}...", mention_author=False)
