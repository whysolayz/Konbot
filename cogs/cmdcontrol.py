"""$disablecommand / $db  +  $enablecommand / $eb - choose WHICH commands work in WHICH channels (or whole categories).

  $db                          -> panel: pick category -> commands -> channels / categories -> [Disable] [Enable]
  $db profile #general         -> turn $profile off in #general
  $db badge give #chat #games  -> several channels at once (a category ID / mention turns off the whole category)
  $db all #general             -> turn EVERY command off in #general
  $db profile everywhere       -> turn $profile off in ALL channels of the server
  $eb profile everywhere       -> remove it completely (works in all channels again, every channel rule is cleared)
  $db list                     -> what is turned off where
  $eb profile #general         -> turn it back on
  /disablecommand              -> same, with auto-complete for the command and a channel picker
"""
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.config import BLURPLE, BRAND, PREFIX
from core.errors import CommandDisabled, VCError
from core.icons import I, err, ok
from core.ui import OwnedView

SETTINGS_KEY = "cmd_disable"
ALL = "*"                    # every command
EVERYWHERE = 0               # "channel id" that means: every channel of the server
EVERYWHERE_WORDS = {"everywhere", "server", "global", "allchannels", "all-channels"}
COG = "cog:"                 # every command of one category, e.g. "cog:Points"
PROTECTED = {"disablecommand", "enablecommand", "help", "sync", "konextra"}   # can never be locked out
CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum,
                 discord.ChannelType.voice, discord.ChannelType.stage_voice, discord.ChannelType.category]
PER_PAGE = 23                # command options per page (+ the "all of this category" option)
NONE = discord.AllowedMentions.none()


def category_label(cog: str) -> str:
    from cogs.meta import CATEGORIES
    return CATEGORIES.get(cog, (cog, ""))[0]


def channel_ids(ch) -> set:
    """Ids that switch a channel off: the channel itself, its thread parent and its category."""
    ids = set()
    for c in (ch, getattr(ch, "parent", None)):
        if c is None:
            continue
        if getattr(c, "id", None):
            ids.add(c.id)
        if getattr(c, "category_id", None):
            ids.add(c.category_id)
    return ids


def where(guild: discord.Guild, cid: int) -> str:
    if cid == EVERYWHERE:
        return "**all channels**"
    ch = guild.get_channel(cid) or guild.get_thread(cid)
    if ch is None:
        return f"~~deleted `{cid}`~~"
    if isinstance(ch, discord.CategoryChannel):
        return f"**{ch.name}** (category)"
    return ch.mention


def root_of(cmd: commands.Command) -> commands.Command:
    while cmd.parent is not None:
        cmd = cmd.parent
    return cmd


class CmdControl(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._rules: dict[int, dict] = {}

    # ------------------------------------------------------------------ storage
    def rules(self, gid: int) -> dict:
        """{ key: [channel / category ids] }  key = '*', 'cog:Points' or a command name like 'badge give'."""
        r = self._rules.get(gid)
        if r is None:
            stored = self.bot.db.get_settings(gid, SETTINGS_KEY).get("rules", {})
            r = self._rules[gid] = {k: [int(x) for x in v] for k, v in stored.items() if v}
        return r

    def save(self, gid: int):
        self.bot.db.set_settings(gid, SETTINGS_KEY, {"rules": self.rules(gid)})

    def apply(self, gid: int, keys, cids, disable: bool) -> int:
        rules, changed = self.rules(gid), 0
        for k in keys:
            lst = rules.setdefault(k, [])
            if EVERYWHERE in cids:
                if disable:                                # off in every channel (one entry replaces channel rules)
                    if lst != [EVERYWHERE]:
                        lst[:] = [EVERYWHERE]
                        changed += 1
                elif lst:                                  # "remove": back on in every channel
                    lst.clear()
                    changed += 1
                if not lst:
                    rules.pop(k, None)
                continue
            for cid in cids:
                if disable and cid not in lst:
                    lst.append(cid)
                    changed += 1
                elif not disable and cid in lst:
                    lst.remove(cid)
                    changed += 1
            if not lst:
                rules.pop(k, None)
        if changed:
            self.save(gid)
        return changed

    # ------------------------------------------------------------------ the actual blocking
    async def cog_load(self):
        self.bot.add_check(self.check)
        try:                                           # run before the rate limiter, so blocked commands cost nothing
            self.bot._checks.insert(0, self.bot._checks.pop())
        except Exception:
            pass

    async def cog_unload(self):
        self.bot.remove_check(self.check)

    async def check(self, ctx: commands.Context) -> bool:
        if not ctx.guild or ctx.command is None:
            return True
        rules = self.rules(ctx.guild.id)
        if not rules:
            return True
        cmd = ctx.command
        if root_of(cmd).qualified_name in PROTECTED:
            return True
        keys = {ALL, COG + (cmd.cog_name or "")}
        c = cmd
        while c is not None:                           # "badge" off  ->  "badge give" off too
            keys.add(c.qualified_name)
            c = c.parent
        ids = channel_ids(ctx.channel) | {EVERYWHERE}
        for k in keys:
            lst = rules.get(k)
            if lst and ids.intersection(lst):
                raise CommandDisabled(cmd.qualified_name)
        return True

    # ------------------------------------------------------------------ catalog / names
    def catalog(self) -> dict:
        out: dict[str, list] = {}
        for cmd in self.bot.walk_commands():
            r = root_of(cmd)
            if r.qualified_name in PROTECTED or r.hidden:
                continue
            out.setdefault(cmd.cog_name or "Other", []).append(cmd)
        for v in out.values():
            v.sort(key=lambda c: c.qualified_name)
        return dict(sorted(out.items(), key=lambda kv: category_label(kv[0]).lower()))

    def label(self, key: str) -> str:
        if key == ALL:
            return "**all commands**"
        if key.startswith(COG):
            return f"**{category_label(key[len(COG):])}** commands"
        return f"`{PREFIX}{key}`"

    def resolve(self, text: str) -> Optional[str]:
        """'profile' / '$badge give' / 'all' / 'points' -> storage key (or None)."""
        t = text.strip().lower()
        if t.startswith(PREFIX.lower()):
            t = t[len(PREFIX):].strip()
        if not t:
            return None
        if t in ("all", "*", "everything"):
            return ALL
        if t.startswith(COG.lower()):
            for cog in self.catalog():
                if cog.lower() == t[len(COG):]:
                    return COG + cog
        cmd = self.bot.get_command(t)
        if cmd is not None and len(t.split()) == len(cmd.qualified_name.split()):   # get_command ignores extra words
            
            if root_of(cmd).qualified_name in PROTECTED:
                raise VCError(f"`{PREFIX}{cmd.qualified_name}` can't be turned off.")
            return cmd.qualified_name
        for cog in self.catalog():                      # category by its name: "points", "custom vc"
            if t in (cog.lower(), category_label(cog).lower()):
                return COG + cog
        return None

    def parse_keys(self, tokens: list):
        """Takes as many leading words as are commands ('badge give', 'profile', 'all') -> (keys, tokens left)."""
        keys, i = [], 0
        while i < len(tokens):
            for n in (3, 2, 1):
                if i + n > len(tokens):
                    continue
                k = self.resolve(" ".join(tokens[i:i + n]))
                if k:
                    if k not in keys:
                        keys.append(k)
                    i += n
                    break
            else:
                break
        return keys, tokens[i:]

    # ------------------------------------------------------------------ shared actions
    def status_embed(self, guild: discord.Guild, note: str = "") -> discord.Embed:
        rules = self.rules(guild.id)
        lines = []
        for k, ids in sorted(rules.items()):
            lines.append(f"{self.label(k)} → " + " · ".join(where(guild, i) for i in ids[:8])
                         + (f" +{len(ids) - 8} more" if len(ids) > 8 else ""))
        text = "\n".join(lines) or "*Nothing is off. Everything works everywhere.*"
        e = discord.Embed(title=f"{I.s('lock')} Disabled commands", color=BLURPLE, description=text[:4000])
        if note:
            e.add_field(name="Done", value=note[:1000], inline=False)
        return e

    def result_text(self, guild, keys, cids, disable: bool, changed: int) -> str:
        names = ", ".join(self.label(k) for k in keys)
        chans = ", ".join(where(guild, c) for c in cids)
        if EVERYWHERE in cids and not disable and changed:
            return ok(f"{names} {'is' if len(keys) == 1 else 'are'} back on in **all channels** (every channel rule removed).")
        if changed == 0:
            if EVERYWHERE in cids and not disable:
                return err(f"{names} {'has' if len(keys) == 1 else 'have'} no rules to remove.")
            if disable:
                return err(f"{names} {'is' if len(keys) == 1 else 'are'} already off in {chans}.")
            return err(f"{names} isn't off directly in {chans}. If the whole category is off, enable the category.")
        return ok(f"{names} {'off' if disable else 'on'} in {chans}.")

    async def run_text(self, ctx: commands.Context, args: str, disable: bool):
        g = ctx.guild
        args = args.strip()
        if not args:
            return await self.open_panel(ctx)
        tokens = args.split()
        word = tokens[0].lower()
        if word in ("list", "status", "show"):
            return await ctx.reply(embed=self.status_embed(g), mention_author=False, allowed_mentions=NONE)
        if word == "reset" and len(tokens) == 1:
            self.rules(g.id).clear()
            self.save(g.id)
            return await ctx.reply(ok("Everything is back on."), mention_author=False)
        if word in ("enable", "on", "remove") and disable:          # "$db enable profile #chat"
            return await self.run_text(ctx, " ".join(tokens[1:]), False)

        keys, rest = self.parse_keys(tokens)
        if not keys:
            raise VCError(f"I don't know `{tokens[0]}`. Use `{PREFIX}db` for a menu.")
        cids = []
        for tok in rest:
            if tok.lower() in EVERYWHERE_WORDS:
                if EVERYWHERE not in cids:
                    cids.append(EVERYWHERE)
                continue
            try:
                ch = await commands.GuildChannelConverter().convert(ctx, tok)
            except commands.BadArgument:
                raise VCError(f"I can't find `{tok}`. Mention the channel or paste its ID.")
            if ch.id not in cids:
                cids.append(ch.id)
        if not cids:
            cids = [ctx.channel.id]
        changed = self.apply(g.id, keys, cids, disable)
        await ctx.reply(self.result_text(g, keys, cids, disable, changed), mention_author=False, allowed_mentions=NONE)

    async def open_panel(self, ctx: commands.Context):
        view = DisableView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False, allowed_mentions=NONE)

    # ------------------------------------------------------------------ prefix commands
    @commands.command(name="disablecommand", aliases=["db", "disablecmd", "dcmd"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def disablecommand(self, ctx: commands.Context, *, args: str = ""):
        """$db [command] [#channel / category] - turn commands off in channels (no arguments = menu)."""
        await self.run_text(ctx, args, True)

    @commands.command(name="enablecommand", aliases=["eb", "enablecmd", "ecmd"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def enablecommand(self, ctx: commands.Context, *, args: str = ""):
        """$eb [command] [#channel / category] - turn commands back on."""
        await self.run_text(ctx, args, False)

    # ------------------------------------------------------------------ slash commands
    async def command_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = current.lower().strip().lstrip(PREFIX.lower())
        scored = []
        if not cur or "all".startswith(cur):
            scored.append((0, app_commands.Choice(name="All commands", value=ALL)))
        cat = self.catalog()
        for cog in cat:
            lab = category_label(cog)
            if not cur or cur in lab.lower() or cur in cog.lower():
                scored.append((1, app_commands.Choice(name=f"All {lab} commands"[:100], value=COG + cog)))
        for cmds in cat.values():
            for c in cmds:
                q = c.qualified_name
                if not cur or q.startswith(cur):
                    scored.append((2, app_commands.Choice(name=f"{PREFIX}{q}"[:100], value=q)))
                elif cur in q:
                    scored.append((3, app_commands.Choice(name=f"{PREFIX}{q}"[:100], value=q)))
        scored.sort(key=lambda x: x[0])
        return [c for _, c in scored[:25]]

    async def slash_run(self, interaction: discord.Interaction, command, channel, disable: bool, everywhere: bool = False):
        perms = interaction.user.guild_permissions
        if not (perms.manage_guild or await self.bot.is_owner_in(interaction.user, interaction.guild_id)):
            await interaction.response.defer(ephemeral=True)        # no rights = no reply
            return await interaction.delete_original_response()
        g = interaction.guild
        if not command:
            view = DisableView(self, interaction.user)
            await interaction.response.send_message(embed=view.embed(), view=view, allowed_mentions=NONE)
            view.message = await interaction.original_response()
            return
        try:
            key = self.resolve(command)
            if key is None:
                raise VCError(f"I don't know `{command}`. Pick one from the list.")
        except VCError as e:
            return await interaction.response.send_message(err(str(e)), ephemeral=True)
        cid = EVERYWHERE if everywhere else (channel.id if channel is not None else interaction.channel_id)
        changed = self.apply(g.id, [key], [cid], disable)
        await interaction.response.send_message(self.result_text(g, [key], [cid], disable, changed),
                                                allowed_mentions=NONE)

    @app_commands.command(name="disablecommand", description="Turn a command off in a channel or a whole category")
    @app_commands.describe(command="Which command (type to search)",
                           channel="Channel or category (empty = this channel)",
                           everywhere="True = ALL channels of the server")
    @app_commands.autocomplete(command=command_autocomplete)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def slash_disable(self, interaction: discord.Interaction, command: Optional[str] = None,
                            channel: Optional[discord.abc.GuildChannel] = None, everywhere: bool = False):
        await self.slash_run(interaction, command, channel, True, everywhere)

    @app_commands.command(name="enablecommand", description="Turn a command back on in a channel or a whole category")
    @app_commands.describe(command="Which command (type to search)",
                           channel="Channel or category (empty = this channel)",
                           everywhere="True = ALL channels of the server")
    @app_commands.autocomplete(command=command_autocomplete)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def slash_enable(self, interaction: discord.Interaction, command: Optional[str] = None,
                           channel: Optional[discord.abc.GuildChannel] = None, everywhere: bool = False):
        await self.slash_run(interaction, command, channel, False, everywhere)


class DisableView(OwnedView):
    """Menu: 1) category  2) commands  3) channels / categories  4) Disable or Enable."""

    def __init__(self, cog: CmdControl, invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        self.cat: Optional[str] = None          # chosen category (cog name) or ALL
        self.keys: list = []                    # chosen commands (storage keys)
        self.cids: list = []                    # chosen channel / category ids
        self.page = 0
        self.render()

    # ----- helpers -----
    def cat_cmds(self) -> list:
        return self.cog.catalog().get(self.cat, []) if self.cat and self.cat != ALL else []

    def pages(self) -> int:
        return max(1, -(-len(self.cat_cmds()) // PER_PAGE))

    def embed(self, note: str = "") -> discord.Embed:
        cmds = ", ".join(self.cog.label(k) for k in self.keys) or "*pick below*"
        chans = ", ".join(where(self.guild, c) for c in self.cids) or "*pick below*"
        e = discord.Embed(title=f"{I.s('lock')} Disable / Enable commands", color=BRAND, description=(
            f"**1.** Pick a category  ·  **2.** pick the commands  ·  **3.** pick channels or whole categories  ·  "
            f"**4.** press **Disable** or **Enable**  ·  the bottom buttons do **all channels** at once.\n\n"
            f"{I.s('terminal')} **Commands:** {cmds}\n{I.s('folder')} **Where:** {chans}"))
        if note:
            e.add_field(name="Done", value=note[:1000], inline=False)
        e.set_footer(text=f"Quick way: {PREFIX}db <command> #channel / everywhere  ·  {PREFIX}db list shows everything that is off")
        return e

    # ----- components -----
    def render(self):
        self.clear_items()
        cat = self.cog.catalog()

        # row 0 - category
        opts = [discord.SelectOption(label="All commands", value=ALL, emoji=I.e("tools"),
                                     default=self.cat == ALL)]
        for cog, cmds in list(cat.items())[:24]:
            opts.append(discord.SelectOption(label=category_label(cog)[:100], value=cog,
                                             description=f"{len(cmds)} commands", default=self.cat == cog))
        s0 = discord.ui.Select(placeholder="1. Category...", options=opts, row=0)
        s0.callback = self.pick_cat
        self.add_item(s0)

        # row 1 - commands of that category
        if self.cat and self.cat != ALL:
            cmds = self.cat_cmds()
            self.page = max(0, min(self.page, self.pages() - 1))
            chunk = cmds[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]
            copts = [discord.SelectOption(label=f"Every {category_label(self.cat)} command"[:100], value=COG + self.cat,
                                          emoji=I.e("grid"), default=(COG + self.cat) in self.keys)]
            for c in chunk:
                d = (c.description or c.short_doc or "").split("\n")[0]
                if d.startswith(PREFIX) and " - " in d:
                    d = d.split(" - ", 1)[1]
                copts.append(discord.SelectOption(label=f"{PREFIX}{c.qualified_name}"[:100], value=c.qualified_name,
                                                  description=(d[:100] or None), default=c.qualified_name in self.keys))
            self.shown = {o.value for o in copts}
            s1 = discord.ui.Select(placeholder="2. Commands...", options=copts, min_values=0,
                                   max_values=len(copts), row=1)
            s1.callback = self.pick_cmds
            self.add_item(s1)

        # row 2 - channels / categories
        shown = [ch for ch in (self.guild.get_channel(i) for i in self.cids[:25]) if ch is not None]
        s2 = discord.ui.ChannelSelect(placeholder="3. Channels or categories...", channel_types=CHANNEL_TYPES,
                                      min_values=0, max_values=25, default_values=shown, row=2)
        s2.callback = lambda i, s=s2: self.pick_chans(i, s)
        self.add_item(s2)

        # row 3 - buttons
        multi = self.cat not in (None, ALL) and self.pages() > 1
        for emoji, cb, dis in (("left", self.prev, not multi or self.page <= 0),
                               ("right", self.next, not multi or self.page >= self.pages() - 1)):
            b = discord.ui.Button(emoji=I.e(emoji), style=discord.ButtonStyle.secondary, disabled=dis, row=3)
            b.callback = cb
            self.add_item(b)
        for label, emoji, style, cb in (("Disable", "ban", discord.ButtonStyle.danger, self.do_disable),
                                        ("Enable", "check", discord.ButtonStyle.success, self.do_enable),
                                        ("List", "menu", discord.ButtonStyle.secondary, self.do_list)):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=3)
            b.callback = cb
            self.add_item(b)

        # row 4 - the whole server
        for label, emoji, style, cb in (("Disable in ALL channels", "ban", discord.ButtonStyle.danger, self.do_disable_all),
                                        ("Remove (enable in ALL)", "refresh", discord.ButtonStyle.success, self.do_enable_all)):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=4)
            b.callback = cb
            self.add_item(b)

    async def push(self, interaction: discord.Interaction, note: str = ""):
        self.render()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    # ----- callbacks -----
    async def pick_cat(self, interaction: discord.Interaction):
        v = interaction.data["values"][0]
        self.cat, self.page = v, 0
        if v == ALL:
            self.keys = [ALL]
        else:
            self.keys = [k for k in self.keys if k != ALL]
        await self.push(interaction)

    async def pick_cmds(self, interaction: discord.Interaction):
        picked = interaction.data.get("values", [])
        self.keys = [k for k in self.keys if k not in self.shown] + picked     # other pages keep their picks
        await self.push(interaction)

    async def pick_chans(self, interaction: discord.Interaction, sel: discord.ui.ChannelSelect):
        self.cids = [c.id for c in sel.values]
        await self.push(interaction)

    async def prev(self, interaction: discord.Interaction):
        self.page -= 1
        await self.push(interaction)

    async def next(self, interaction: discord.Interaction):
        self.page += 1
        await self.push(interaction)

    async def act(self, interaction: discord.Interaction, disable: bool):
        if not self.keys:
            return await interaction.response.send_message(err("Pick a command first."), ephemeral=True)
        if not self.cids:
            return await interaction.response.send_message(err("Pick a channel or category first."),
                                                           ephemeral=True)
        changed = self.cog.apply(self.guild.id, self.keys, self.cids, disable)
        await self.push(interaction, self.cog.result_text(self.guild, self.keys, self.cids, disable, changed))

    async def act_all(self, interaction: discord.Interaction, disable: bool):
        if not self.keys:
            return await interaction.response.send_message(err("Pick a command first."), ephemeral=True)
        changed = self.cog.apply(self.guild.id, self.keys, [EVERYWHERE], disable)
        await self.push(interaction, self.cog.result_text(self.guild, self.keys, [EVERYWHERE], disable, changed))

    async def do_disable_all(self, interaction: discord.Interaction):
        await self.act_all(interaction, True)

    async def do_enable_all(self, interaction: discord.Interaction):
        await self.act_all(interaction, False)

    async def do_disable(self, interaction: discord.Interaction):
        await self.act(interaction, True)

    async def do_enable(self, interaction: discord.Interaction):
        await self.act(interaction, False)

    async def do_list(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.cog.status_embed(self.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(CmdControl(bot))
