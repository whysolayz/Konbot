"""$konping / $koninfo, $help (clean command list - everybody only sees what THEY can use) and $konextra."""
import asyncio
import inspect
import io
import math
import time
from collections import deque
from typing import Optional

import platform

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import chart, checks, flair, names
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import OwnedView

CATEGORIES = {
    "VC": ("Custom VC", "speaker"), "Drag": ("Drag", "swap"), "Interface": ("Setup & Admin", "tools"),
    "AFK": ("AFK", "clock"), "Points": ("Points & Badges", "heart"), "Shop": ("Point Shop", "coins"),
    "Admin": ("Setup & Admin", "tools"), "Prefixes": ("No-Prefix & Aliases", "link"), "Backup": ("Backup", "gift_box"),
    "Meta": ("General", "home"), "Autoreact": ("Auto Reacter", "sparkles"), "RoleCmds": ("Role Commands", "crown"),
    "SelfRoles": ("Self Roles", "crown"), "Expressions": ("Emojis & Stickers", "sparkles"), "Purge": ("Cleanup", "tools"),
    "Aura": ("Aura", "fire"), "RoleTransfer": ("Role Transfer", "crown"), "TicTacToe": ("Tic Tac Toe", "gamepad"), "CmdControl": ("Command Control", "lock"),
    "BlackTea": ("BlackTea", "leaf"), "TimeTap": ("TimeTap", "stopwatch"), "Handfight": ("Handfight", "rock"), "Games": ("Games", "gamepad"), "GuessAnime": ("Guess the Anime", "star_fill"),
    "GuessEmoji": ("Guess the Emoji", "sparkles"), "SimonSays": ("Simon Says", "gamepad"), "ImageHost": ("Image Links", "image"),
    "Tickets": ("Tickets", "headset"), "Giveaway": ("Giveaways", "gift"),
}
# $help is split into a few big groups (key -> (title, icon, short text, [categories in it]))
GROUPS = {
    "general": ("General & Utility", "home", "Help, AFK, emojis, image links, aliases", ["Meta", "AFK", "Expressions", "ImageHost", "Prefixes"]),
    "fun": ("Fun & Games", "gamepad", "Games, Aura", ["Games", "TimeTap", "Handfight", "BlackTea", "GuessAnime", "GuessEmoji", "SimonSays", "TicTacToe", "Aura"]),
    "voice": ("Voice", "speaker", "Custom VC and drag", ["VC", "Drag"]),
    "economy": ("Points & Shop", "coins", "Points, badges, leaderboard, role shop", ["Points", "Shop"]),
    "community": ("Community, Tickets & Giveaways", "headset", "Tickets, giveaways, self roles, autoreact, role commands", ["Tickets", "Giveaway", "SelfRoles", "Autoreact", "RoleCmds", "RoleTransfer"]),
    "mod": ("Moderation & Admin", "tools", "Cleanup, command control, setup, backup", ["Purge", "CmdControl", "Interface", "Admin", "Backup"]),
}
GROUP_OF = {cog: key for key, (_, _, _, cogs) in GROUPS.items() for cog in cogs}

# short, plain descriptions (key = command name without prefix). Anything missing falls back to the docstring.
DESC = {
    "vclock": "Lock your VC", "vcunlock": "Unlock your VC", "vchide": "Hide your VC", "vcunhide": "Show your VC again",
    "vckick": "Kick someone from your VC", "vcban": "Ban someone from your VC", "vcunban": "Unban someone",
    "vcunmute": "Unmute someone", "vcdeafen": "Server-deafen someone", "vcundeafen": "Undeafen someone",
    "vcmuteall": "Mute everyone in your VC", "vcunmuteall": "Unmute everyone in your VC", "vcbans": "List who is banned from your VC",
    "vcclaim": "Claim the VC when its owner left (all vc commands also work as a reply)",
    "vcpermit": "Let someone join your VC", "vcpermitall": "Permit everyone currently in your VC",
    "vctransfer": "Give your VC to someone else", "vcmute": "Mute someone for a while (default 10m)",
    "vclimit": "Set the user limit (0 = no limit)", "vcrename": "Rename your VC", "vcstatus": "Set the VC status text",
    "vcbitrate": "Change audio quality", "vcinfo": "Who owns this VC", "denydrags": "Block / allow drag requests to your VC",
    "dragme": "Ask a VC owner to drag you in",
    "afk": "Go AFK (optional reason, max 50 characters)", "afktag": "Turn the [AFK] nickname tag on/off",
    "profile": "Your points, chat and VC progress", "leaderboard": "Leaderboard: `$lb` opens points, switch to messages / voice with the menu (Previous / Find Me / Next)",
    "store": "Open the role shop (pages, balance, buy with the menu)", "pointshop": "Open the role shop", "pointshop config": "Add / edit / reorder shop roles",
    "point": "Points settings overview", "point config": "Points, cooldown and channel rules",
    "point add": "Give points (@member or user ID)", "point remove": "Take points (@member or user ID, also for people who left)", "point set": "Set exact points (@member or user ID)", "point reset": "Wipe a member's data (@member or user ID)",
    "badge": "Show badges", "badge give": "Give a badge", "badge remove": "Remove all badges (or one by name)",
    "noprefix": "Show no-prefix settings", "noprefix role": "Role that doesn't need the prefix",
    "noprefix add": "Give one member no-prefix", "noprefix remove": "Remove a member's no-prefix",
    "noprefix clear": "Remove the no-prefix role",
    "alias": "List custom aliases", "alias list": "List custom aliases",
    "alias add": "Create an alias (e.g. `pa` = `vcpermit all`)", "alias remove": "Delete an alias",
    "setrole": "`$setrole manager <role ID>` creates `$setmanager @user` (or reply) that gives that role", "setrole list": "List the role commands of this server", "setrole remove": "Delete a role command",
    "ar": "React with your emoji when someone pings you", "ar setup": "Same as `$ar <emoji>`",
    "ar off": "Turn your autoreact off", "ar config": "Roles that may use autoreact",
    "ar config add": "Give roles access", "ar config remove": "Take access from roles",
    "ar config clear": "Remove all roles",
    "selfrole": "Self-role overview", "selfrole setup": "Open the self-role editor", "setsr": "Set the self-role channel and post it",
    "editsr": "Edit banner, title, roles, emojis", "addemoji": "Add emojis from a reply, or from an attached / replied image or GIF (`$ae name`) - buttons: Undo / Remove / Rename (`$ae rename <emoji> <new name>`)",
    "addsticker": "Add a sticker from a reply, or from an attached / replied image (`$as name`) - buttons: Undo / Remove / Rename",
    "purgebot": "Delete bot + command messages (default 10, max 500)",
    "purgemember": "Delete a member's messages: `$p @member 50` or reply + `$p` (default 10, max 500)",
    "customvc": "Set the join-to-create channel", "customvc config": "Who can't be vc-banned, kicked or muted", "interface": "Post the voice control panel",
    "sync": "Sync slash commands", "backup": "Backup status", "backup refresh": "Back up right now",
    "backupload": "Reply to a data file (zip / db / json) with this to merge it (`replace` = backup wins)", "backup config": "Send the readable config + players list",
    "konping": "Heartbeat graph, latency, uptime and status", "koninfo": "Servers, members and bot stats",
    "aura": "Show your aura (or @member's) - random aura can be negative too", "aura config": "Fix aura, random on/off, switch a person off",
    "transfer": "Move roles and points from one person to another (also for people who LEFT: `$transfer <user id|name> @to`), untick roles, Undo, History (Kon admins)",
    "tictactoe": "Tic Tac Toe info", "tictactoe play": "Challenge a member (only in the game channel)",
    "tictactoe admin": "Set the Tic Tac Toe channel (`$tictactoe set` opens a channel menu)",
    "games": "All games, the channel each one works in and how to start them",
    "timetap": "Open TimeTap - reaction game (pick Training / Duo / Trio / Squad)", "timetap play": "Open TimeTap (`$tt`)",
    "timetap channel": "Pick the TimeTap channel from a menu (`$timetap set`)", "timetap stats": "Your reaction stats and records",
    "timetap stop": "Stop the running TimeTap game",
    "handfight": "Open Handfight - rock paper scissors (Duo / Squad)", "handfight play": "Open Handfight (`$hf`)",
    "handfight channel": "Pick the Handfight channel from a menu (`$handfight set`)", "handfight stats": "Your Handfight record",
    "handfight stop": "Stop the running Handfight game",
    "guessanime": "Guess the Anime - character pictures, 2-10 players, 10 rounds (`$ga easy|medium|hard`, default medium)", "guessanime play": "Open a Guess the Anime lobby",
    "guessanime channel": "Pick the Guess the Anime channel (`$guessanime set`)", "guessanime stats": "Your Guess the Anime record",
    "guessanime top": "Best Guess the Anime players", "guessanime stop": "Stop the running Guess the Anime game",
    "guessemoji": "Guess the Emoji - emojis hide a word, type it (`$ge easy|medium|hard`)", "guessemoji play": "Open a Guess the Emoji lobby",
    "guessemoji channel": "Pick the Guess the Emoji channel (`$guessemoji set`)", "guessemoji stats": "Your Guess the Emoji record",
    "guessemoji top": "Best Guess the Emoji players", "guessemoji stop": "Stop the running Guess the Emoji game",
    "simonsays": "Simon Says - repeat the colour order (`$simon`)", "simonsays play": "Open a Simon Says lobby",
    "simonsays channel": "Pick the Simon Says channel (`$simonsays set`)", "simonsays stats": "Your Simon Says high score",
    "simonsays top": "Best Simon Says players", "simonsays stop": "Stop the running Simon Says game",
    "aura history": "Graph of the last random aura readings (`$aura history @member`)",
    "blacktea": "Start a BlackTea word game (`$bt easy|medium|hard`)", "blacktea play": "Start a BlackTea word game",
    "blacktea channel": "Pick the BlackTea channel from a menu (`$blacktea set`)", "setblacktea": "Set the BlackTea channel",
    "blacktea reset": "Remove the BlackTea channel", "blacktea stop": "Stop the running BlackTea game",
    "imgchannel": "Set the private channel that stores converted images", "imgchannelreset": "Remove the image channel",
    "tolink": "Turn an image / GIF / banner into a saved link (attach, reply or paste a link)",
    "links": "List saved image links", "linkget": "Fresh link + preview of a saved image", "linkdel": "Delete a saved image",
    "ei": "List animated emojis with their IDs (`$ei name`, `$ei here`, `$ei <id>`)",
    "point enable": "Count points in a channel again", "point disable": "Stop counting points in a channel",
    "disablecommand": "Turn commands off in channels / categories / the whole server (`$db`, `$db <cmd> everywhere`)",
    "enablecommand": "Turn commands back on (`$eb`, `$eb <cmd> everywhere` removes every rule)",
    "giveaway": "Easy giveaway panel (1 Details · 2 Rules · 3 Rewards · 4 Style · Templates) - or quick start: `$giveaway 1h 1 Discord Nitro` (admins)",
    "giveaway help": "Short guide: how to start, manage and reroll giveaways",
    "giveaway end": "End a running giveaway now (reply to it or give its number)", "giveaway reroll": "Pick new winner(s) of an ended giveaway",
    "giveaway cancel": "Cancel a running giveaway", "giveaway list": "Running + recently ended giveaways",
    "giveaway templates": "Saved giveaway templates", "giveaway load": "Open the builder with a saved template",
    "ticket": "Ticket control panel: many panels, categories, panel message, banner, logs, roles (admins)",
    "ticket close": "Close this ticket (staff or the opener), saves a transcript",
    "ticket claim": "Claim / release this ticket (staff)", "ticket add": "Let a member into this ticket (staff)",
    "ticket remove": "Take a member out of this ticket (staff)", "ticket rename": "Rename this ticket channel (staff)",
    "ticket blacklist": "Block / unblock a member from opening tickets (admins)",
    "help": "This menu", "konextra": "Give a member Kon owner rights (this server only)",
    "konextra remove": "Take owner rights away", "konextra list": "Show the extra owners",
}

SECTIONS = (("member", "Everyone"), ("admin", "Admins"), ("owner", "Owner"))


true_owner_only = checks.true_owner_only          # kept for the old name


def fmt_secs(s: int) -> str:
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if x)


async def access_level(ctx: commands.Context, cmd: commands.Command) -> Optional[str]:
    """None = this user can't use the command (so it is hidden). Else 'member' / 'admin' / 'owner'."""
    level = "member"
    c = cmd
    while c is not None:                                     # the command's own checks + its parent groups'
        for chk in c.checks:
            try:
                r = chk(ctx)
                if inspect.isawaitable(r):
                    r = await r
                allowed = bool(r)
            except Exception:
                allowed = False
            if not allowed:
                return None
            q = getattr(chk, "__qualname__", "").lower()
            if "owner" in q:
                level = "owner"
            elif ("has_permissions" in q or "kon_admin" in q) and level != "owner":
                level = "admin"
        c = c.parent
    return level


def describe(cmd: commands.Command) -> str:
    d = DESC.get(cmd.qualified_name)
    if d:
        return d
    d = (cmd.description or cmd.short_doc or "").split("\n")[0].strip()
    if d.startswith(PREFIX) and " - " in d:                  # "$cmd args - text"  ->  "text"
        d = d.split(" - ", 1)[1]
    return d if d and not d.startswith(PREFIX) else ""


def _count(cogs: dict) -> int:
    return sum(len(v) for _, _, secs in cogs.values() for v in secs.values())


def short(text: str, n: int = 52) -> str:
    text = " ".join((text or "").replace("`", "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class HelpView(OwnedView):
    """$help: pick a category in the menu -> its commands. Previous / Next walk through EVERY page in order
    (overview -> category after category), so you can just keep pressing Next."""
    MAX_LINES = 12
    MAX_CHARS = 1500

    def __init__(self, ctx, pages: dict):
        super().__init__(ctx.author.id, timeout=300)
        self.pages = pages          # group key -> (title, icon, text, {cog: (title, icon, {level: [lines]})})
        self.flat: list = [None]    # [None] = overview, then one dict per page (a long category has several)
        self.first: dict[str, int] = {}          # category key -> index of its first page
        self.group_first: dict[str, int] = {}    # group key -> index of its first page
        self.cats: list = []                     # (cog key, title, icon, command count) in menu order
        for gkey, (gtitle, gicon, gtext, cogs) in pages.items():
            self.group_first[gkey] = len(self.flat)
            for cog, (ctitle, cicon, secs) in cogs.items():
                lines = [ln for level, _ in SECTIONS for ln in secs.get(level, [])]
                self.first[cog] = len(self.flat)
                self.cats.append((cog, ctitle, cicon, len(lines)))
                for chunk in self.split(lines):
                    self.flat.append({"cog": cog, "group": gtitle, "title": ctitle, "icon": cicon, "lines": chunk})
        self.pos = 0
        self.sel = discord.ui.Select(placeholder="Pick a category...", row=0, options=self.options())
        self.sel.callback = self.picked
        self.add_item(self.sel)
        self.prev.emoji, self.next.emoji, self.home.emoji = I.e("left"), I.e("right"), I.e("home")
        self.sync()

    @classmethod
    def split(cls, lines: list) -> list:
        out, chunk, size = [], [], 0
        for ln in lines or ["—"]:
            if chunk and (len(chunk) >= cls.MAX_LINES or size + len(ln) > cls.MAX_CHARS):
                out.append(chunk)
                chunk, size = [], 0
            chunk.append(ln)
            size += len(ln) + 1
        out.append(chunk)
        return out

    def current_cog(self) -> Optional[str]:
        item = self.flat[self.pos]
        return item["cog"] if item else None

    def options(self) -> list:
        cur = self.current_cog()
        opts = [discord.SelectOption(label="Overview", value="__all__", emoji=I.e("menu"), default=cur is None)]
        for cog, title, icon, n in self.cats[:24]:
            opts.append(discord.SelectOption(label=title[:100], value=cog, description=f"{n} command{'s' if n != 1 else ''}",
                                             emoji=I.e(icon), default=cog == cur))
        return opts

    def open(self, key: str):
        """key = category key, group key or '__all__'."""
        if key in self.first:
            self.pos = self.first[key]
        elif key in self.group_first:
            self.pos = self.group_first[key]
        else:
            self.pos = 0
        self.sync()

    def sync(self):
        self.sel.options = self.options()
        self.prev.disabled = self.home.disabled = self.pos <= 0
        self.next.disabled = self.pos >= len(self.flat) - 1

    def overview(self) -> discord.Embed:
        rows = []
        for gkey, (title, icon, text, cogs) in self.pages.items():
            names = " · ".join(ct for ct, _, _ in cogs.values())
            rows.append(f"{I.s(icon)} **{title}**\n-# {names}")
        return discord.Embed(title=f"{I.s('sparkles')} Kon Help", color=BRAND, description=(
            "\n".join(rows) + "\n\n-# Pick a category below, or press **Next** to browse them all"))

    def page(self) -> discord.Embed:
        item = self.flat[self.pos]
        # custom emojis do NOT render in an embed footer (they show as raw <:name:id>), so the legend goes in the description
        legend = f"\n\n-# {I.s('lock')} admin  ·  {I.s('crown')} owner"
        e = discord.Embed(title=f"{I.s(item['icon'])} {item['title']}", color=BRAND,
                          description="\n".join(item["lines"]) + legend)
        e.set_author(name=item["group"])
        e.set_footer(text=f"Page {self.pos}/{len(self.flat) - 1}")
        return e

    def current(self) -> discord.Embed:
        return self.overview() if self.pos == 0 else self.page()

    async def show(self, interaction: discord.Interaction):
        self.sync()
        await interaction.response.edit_message(embed=self.current(), view=self)

    async def picked(self, interaction: discord.Interaction):
        self.open(self.sel.values[0])
        await self.show(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pos = max(0, self.pos - 1)
        await self.show(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pos = min(len(self.flat) - 1, self.pos + 1)
        await self.show(interaction)

    @discord.ui.button(label="Home", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pos = 0
        await self.show(interaction)


class InfoView(OwnedView):
    PER_PAGE = 5

    def __init__(self, cog, ctx, owner: bool):
        super().__init__(ctx.author.id, timeout=300)
        self.cog, self.bot, self.owner = cog, ctx.bot, owner
        self.mode, self.page = "overview", 0
        self.render()

    def guilds(self):
        return sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)

    def overview(self) -> discord.Embed:
        b = self.bot
        gs = b.guilds
        members = sum(g.member_count or 0 for g in gs)
        humans = sum(1 for u in b.users if not u.bot)
        channels = sum(len(g.channels) for g in gs)
        lines = [
            f"{I.s('folder')} **Servers:** `{len(gs):,}`",
            f"{I.s('users')} **Members:** `{members:,}` (unique `{humans:,}`)",
            f"{I.s('message')} **Channels:** `{channels:,}`",
            f"{I.s('heart')} **Players:** `{b.db.counts()['users']:,}`",
            f"{I.s('signal')} **Latency:** `{b.latency * 1000:.0f} ms`",
            f"{I.s('clock')} **Uptime:** `{fmt_secs(time.time() - self.cog.started)}`",
            f"{I.s('terminal')} **Python:** `{platform.python_version()}` · **discord.py:** `{discord.__version__}`",
        ]
        return discord.Embed(title=f"{I.s('sparkles')} Kon Info", color=BRAND, description="\n".join(lines))

    def servers(self) -> discord.Embed:
        gs = self.guilds()
        pages = max(1, -(-len(gs) // self.PER_PAGE))
        self.page = max(0, min(self.page, pages - 1))
        chunk = gs[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
        lines = [f"**{self.page * self.PER_PAGE + i}.** **{g.name}**\n{I.s('users')} **Members:** `{g.member_count or 0:,}`\n"
                 f"{I.s('tag')} **ID:** `{g.id}`" for i, g in enumerate(chunk, 1)]
        e = discord.Embed(title=f"{I.s('folder')} Kon is in {len(gs)} server(s)", color=BLURPLE,
                          description="\n\n".join(lines) or "—")
        e.set_footer(text=f"Page {self.page + 1}/{pages} · {sum(g.member_count or 0 for g in gs):,} members in total")
        return e

    def render(self):
        self.clear_items()

        def btn(label, emoji, cb, style=discord.ButtonStyle.secondary, disabled=False):
            b = discord.ui.Button(label=label, emoji=I.e(emoji) if emoji else None, style=style, disabled=disabled)
            b.callback = cb
            self.add_item(b)
        btn("Overview", "info", self.go_overview, discord.ButtonStyle.primary if self.mode == "overview" else discord.ButtonStyle.secondary)
        if self.owner:
            pages = max(1, -(-len(self.bot.guilds) // self.PER_PAGE))
            btn("Servers", "folder", self.go_servers, discord.ButtonStyle.primary if self.mode == "servers" else discord.ButtonStyle.secondary)
            if self.mode == "servers":
                btn(None, "left", self.prev, disabled=self.page <= 0)
                btn(None, "right", self.next, disabled=self.page >= pages - 1)

    async def show(self, interaction):
        self.render()
        await interaction.response.edit_message(embed=self.overview() if self.mode == "overview" else self.servers(), view=self)

    async def go_overview(self, i):
        self.mode = "overview"
        await self.show(i)

    async def go_servers(self, i):
        self.mode, self.page = "servers", 0
        await self.show(i)

    async def prev(self, i):
        self.page -= 1
        await self.show(i)

    async def next(self, i):
        self.page += 1
        await self.show(i)


class Meta(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started = time.time()
        self._help_cache: dict = {}
        self._graph_cache = None
        self._cmd_list: Optional[list] = None
        self.beats: deque = deque(maxlen=40)          # (gateway ms, api ms) every 15 s = the last 10 minutes (for $pulse)
        self.sampler.start()

    def cog_unload(self):
        self.sampler.cancel()

    async def _api_ms(self) -> Optional[float]:
        """Round trip of one tiny, harmless request to Discord (GET /gateway)."""
        t0 = time.perf_counter()
        try:
            await self.bot.http.get_gateway()
        except Exception:
            return None
        return (time.perf_counter() - t0) * 1000

    @tasks.loop(seconds=15)
    async def sampler(self):
        ws = self.bot.latency * 1000
        if math.isfinite(ws):
            self.beats.append((ws, await self._api_ms()))

    @sampler.before_loop
    async def _sampler_wait(self):
        await self.bot.wait_until_ready()

    # ---------- $kon / $konping / $koninfo ----------
    @commands.hybrid_command(name="konping", aliases=["kping"], description="Kon's pulse: latency graph, uptime and status")
    async def kon_ping(self, ctx: commands.Context):
        """$pulse - Kon's heartbeat: live latency, a graph of the last 10 minutes, uptime and status."""
        ws = self.bot.latency * 1000
        if not math.isfinite(ws):
            ws = 0.0
        graph = asyncio.create_task(self._graph())               # PIL drawing runs in a thread while the reply is on its way
        t0 = time.perf_counter()
        msg = await ctx.reply(f"{I.s('refresh')} Checking...", mention_author=False)
        api = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        self.bot.db.ping()
        db = (time.perf_counter() - t0) * 1000

        def dot(ms, good, ok_):
            return I.s("light_green") if ms < good else (I.s("warning") if ms < ok_ else I.s("light_red"))
        worst = max(ws / 150, api / 300, db / 20)
        status, color = (("Healthy", BRAND) if worst < 1 else ("Slow", 0xF58219) if worst < 2.5 else ("Struggling", 0xED4245))
        if ws > 0:
            self.beats.append((ws, api))
        e = discord.Embed(title=f"{I.s('sparkles')} Kon Pulse", color=color, description=(
            f"{I.s('check')} **Status:** `{status}`\n"
            f"{I.s('signal')} **Gateway:** `{ws:.0f} ms` {dot(ws, 150, 300)}\n"
            f"{I.s('bolt')} **API:** `{api:.0f} ms` {dot(api, 300, 700)}\n"
            f"{I.s('folder')} **Database:** `{db:.1f} ms` {dot(db, 20, 100)}\n"
            f"{I.s('clock')} **Uptime:** `{fmt_secs(time.time() - self.started)}`"))
        file = None
        made = await graph
        if made:
            png, footer = made
            file = discord.File(io.BytesIO(png), "pulse.png")
            e.set_image(url="attachment://pulse.png")
            e.set_footer(text=footer)
        else:
            e.set_footer(text="The graph appears after a minute or so.")
        await msg.edit(content=None, embed=e, attachments=[file] if file else [])

    async def _graph(self):
        """-> (png bytes, footer text) or None while there are too few readings. Cached for 5 s (spam-proof)."""
        now = time.monotonic()
        if self._graph_cache and now - self._graph_cache[0] < 5:
            return self._graph_cache[1]
        if len(self.beats) < 3:
            return None
        beats = list(self.beats)
        gw = [b[0] for b in beats]
        series = [{"values": gw, "color": chart.GOLD, "label": "Gateway"}]
        ap = [b[1] for b in beats if b[1] is not None]
        if len(ap) == len(beats):
            series.append({"values": ap, "color": chart.SKY, "label": "API"})
        png = await asyncio.to_thread(chart.line_chart, series, unit="ms", min_span=20,
                                      x_left=f"{len(beats) * 15 // 60 or 1} min ago", x_right="now")
        out = (png, f"Gateway · best {min(gw):.0f} · avg {sum(gw) / len(gw):.0f} · worst {max(gw):.0f} ms")
        self._graph_cache = (now, out)
        return out

    @commands.hybrid_command(name="koninfo", aliases=["kinfo"], description="Servers, members and stats of Kon")
    async def kon_info(self, ctx: commands.Context):
        """$koninfo - how many servers / members Kon has (the server list is for the bot owner)."""
        owner = await self.bot.is_true_owner(ctx.author)
        view = InfoView(self, ctx, owner)
        view.message = await ctx.reply(embed=view.overview(), view=view, mention_author=False)

    # ---------- $help ----------
    async def command_help(self, ctx: commands.Context, cmd: commands.Command) -> discord.Embed:
        level = await access_level(ctx, cmd)
        shown = names.shown(cmd.qualified_name)
        usage = f"{PREFIX}{shown} {cmd.signature}".strip()
        text = describe(cmd) or (cmd.help or "").split("\n")[0] or "—"
        e = discord.Embed(title=f"{I.s('terminal')} {usage}", color=BRAND, description=text)
        root = cmd
        while root.parent is not None:
            root = root.parent
        if root.name in names.NEW_NAMES and shown != cmd.qualified_name:
            e.add_field(name="Old name", value=f"`{PREFIX}{cmd.qualified_name}` (still works)", inline=True)
        others = [a for a in cmd.aliases if a != names.NEW_NAMES.get(cmd.name)]
        if others:
            e.add_field(name="Aliases", value=" ".join(f"`{PREFIX}{a}`" for a in others[:10]), inline=True)
        e.add_field(name="Who", value={"member": "Everyone", "admin": "Admins", "owner": "Owner"}.get(level, "Everyone"), inline=True)
        cat = CATEGORIES.get(cmd.cog_name or "Meta", (cmd.cog_name or "General", "menu"))[0]
        e.add_field(name="Category", value=cat, inline=True)
        subs = sorted(getattr(cmd, "commands", ()), key=lambda c: c.name)
        if subs:
            lines = []
            for sc in subs[:12]:
                if await access_level(ctx, sc) is None or sc.hidden:
                    continue
                d = describe(sc)
                lines.append(f"`{PREFIX}{names.shown(sc.qualified_name)} {sc.signature}`".replace(" `", "`") + (f" — {d}" if d else ""))
            if lines:
                e.add_field(name="Subcommands", value="\n".join(lines)[:1000], inline=False)
        return e

    HELP_TTL = 60                      # seconds a built help menu is reused for the same person in the same server

    async def _pages_for(self, ctx: commands.Context) -> dict:
        """Everything this person may use, grouped for $help. Built once, then served from memory (instant $help)."""
        rc = self.bot.rolecmds(ctx.guild.id)
        key = (ctx.guild.id, ctx.author.id, tuple(sorted((n, d.get("role")) for n, d in rc.items())))
        hit = self._help_cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.HELP_TTL:
            return hit[1]
        if self._cmd_list is None:
            self._cmd_list = sorted(self.bot.walk_commands(), key=lambda c: c.qualified_name)
        pages: dict[str, tuple] = {}
        for cmd in self._cmd_list:
            if cmd.name == "rolecmd" or cmd.hidden:                  # internal dispatcher for custom role commands
                continue
            level = await access_level(ctx, cmd)
            if level is None:                                        # not allowed -> never shown
                continue
            self._add_line(pages, cmd, level)
        # the role commands of THIS server ($setmanager ...) are listed too, for people who may use them
        if ctx.author.guild_permissions.manage_roles or self.bot.is_owner_fast(ctx.author.id, ctx.guild.id):
            lines = []
            for n, d in sorted(rc.items()):
                r = ctx.guild.get_role(d["role"])
                lines.append(f"`{PREFIX}set{n} @user` {r.mention if r else '*deleted role*'} {I.s('lock')}")
            if lines:
                self._cog_bucket(pages, "RoleCmds")[2].setdefault("admin", []).extend(lines)
        order = list(GROUPS) + ["other"]
        pages = dict(sorted(pages.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))
        for gkey, (title, icon, text, cogs) in pages.items():       # inside a group: the order of GROUPS[...]
            if gkey in GROUPS:
                rank = GROUPS[gkey][3]
                pages[gkey] = (title, icon, text, dict(sorted(cogs.items(), key=lambda kv: rank.index(kv[0]) if kv[0] in rank else 99)))
        if len(self._help_cache) > 300:
            self._help_cache.clear()
        self._help_cache[key] = (now, pages)
        return pages

    @commands.command(name="help", aliases=["h"])
    @commands.guild_only()
    async def show_help(self, ctx: commands.Context, *, query: str = ""):
        """$help [category / command] - every command you can use, by category."""
        pages = await self._pages_for(ctx)
        view = HelpView(ctx, pages)

        q = query.strip().lstrip(PREFIX).strip().lower()
        if q:
            hit = next((k for k, v in pages.items() if q in (k, v[0].lower())), None)
            if hit is None:
                hit = next((cog for v in pages.values() for cog, (ct, _, _) in v[3].items()
                            if q in (cog.lower(), ct.lower())), None)
            if hit:
                view.open(hit)
                view.message = await ctx.reply(embed=view.current(), view=view, mention_author=False,
                                               allowed_mentions=discord.AllowedMentions.none())
                return
            cmd = self.bot.get_command(q)
            if cmd is not None and not cmd.hidden and await access_level(ctx, cmd) is not None:
                return await ctx.reply(embed=await self.command_help(ctx, cmd), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            found = await self.search_help(ctx, q)                 # "$help mute" -> every command about muting
            if found is not None:
                return await ctx.reply(embed=found, view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
        view.message = await ctx.reply(embed=view.overview(), view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())

    async def search_help(self, ctx: commands.Context, q: str) -> Optional[discord.Embed]:
        """Find commands by new name, old name, alias or what they do (only commands this person may use)."""
        entries, by_key = [], {}
        for cmd in self.bot.walk_commands():
            if cmd.name == "rolecmd" or cmd.hidden or await access_level(ctx, cmd) is None:
                continue
            nm = {cmd.qualified_name, names.shown(cmd.qualified_name), *[f"{cmd.full_parent_name} {a}".strip() for a in cmd.aliases]}
            words = [w for n in nm for w in n.split()] + list(nm)
            entries.append((cmd.qualified_name, words, describe(cmd)))
            by_key[cmd.qualified_name] = cmd
        hits = flair.search_commands(q, entries, limit=8)
        if not hits:
            return None
        lines = []
        for key in hits:
            cmd = by_key[key]
            d = short(describe(cmd), 60)
            lines.append(f"`{PREFIX}{names.shown(key)} {cmd.signature}`".replace(" `", "`") + (f" — {d}" if d else ""))
        e = discord.Embed(title=f"{I.s('target')} Commands for “{q[:40]}”", color=BRAND, description="\n".join(lines))
        e.set_footer(text=f"{PREFIX}help <command> for details")
        return e

    @staticmethod
    def _cog_bucket(pages: dict, cog: str) -> tuple:
        gkey = GROUP_OF.get(cog, "other")
        if gkey in GROUPS:
            title, icon, text, _ = GROUPS[gkey]
        else:
            title, icon, text = "More", "menu", "Everything else"
        group = pages.setdefault(gkey, (title, icon, text, {}))
        ctitle, cicon = CATEGORIES.get(cog, (cog, "menu"))
        return group[3].setdefault(cog, (ctitle, cicon, {}))

    def _add_line(self, pages: dict, cmd: commands.Command, level: str):
        cog = cmd.cog_name or "Meta"
        if cog == "Interface":
            cog = "Admin"
        usage = f"{PREFIX}{names.shown(cmd.qualified_name)} {cmd.signature}".strip()
        desc = short(describe(cmd))
        badge = {"admin": f" {I.s('lock')}", "owner": f" {I.s('crown')}"}.get(level, "")
        line = ("↳ " if cmd.parent is not None else "") + f"`{usage}`" + (f" — {desc}" if desc else "") + badge
        self._cog_bucket(pages, cog)[2].setdefault(level, []).append(line)

    # ---------- $konextra (bot owner only, PER SERVER) ----------
    @commands.group(name="konextra", aliases=["kextra"], invoke_without_command=True)
    @commands.guild_only()
    @true_owner_only()
    async def konextra(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """$konextra @member - give that member Kon owner permission IN THIS SERVER only."""
        if member is None:
            return await ctx.invoke(self.konextra_list)
        if member.bot:
            raise VCError("A bot can't be an owner.")
        if await self.bot.is_true_owner(member):
            raise VCError(f"{member.mention} is already the bot owner.")
        extra = self.bot.extra_owners(ctx.guild.id)
        if member.id in extra:
            raise VCError(f"{member.mention} is already an owner here.")
        extra.append(member.id)
        self.bot.store.save_soon()
        await ctx.reply(ok(f"{member.mention} is a **Kon owner** in **{ctx.guild.name}** only. "
                           f"Undo: `{PREFIX}konextra remove @user`."),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @konextra.command(name="remove", aliases=["delete", "del"])
    @commands.guild_only()
    @true_owner_only()
    async def konextra_remove(self, ctx: commands.Context, member: discord.User):
        """$konextra remove @member"""
        extra = self.bot.extra_owners(ctx.guild.id)
        if member.id not in extra:
            raise VCError(f"{member.mention} isn't an owner here.")
        extra.remove(member.id)
        self.bot.store.save_soon()
        await ctx.reply(ok(f"{member.mention} is no longer an owner here."), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @konextra.command(name="list")
    @commands.guild_only()
    @true_owner_only()
    async def konextra_list(self, ctx: commands.Context):
        """$konextra list - extra owners of this server"""
        extra = self.bot.extra_owners(ctx.guild.id)
        text = "\n".join(f"<@{i}>" for i in extra) if extra else "*nobody yet*"
        e = discord.Embed(title=f"{I.s('crown')} Extra owners · {ctx.guild.name}", description=text, color=BLURPLE)
        e.set_footer(text=f"{PREFIX}konextra @user to add · {PREFIX}konextra remove @user · this server only")
        await ctx.reply(embed=e, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Meta(bot))
