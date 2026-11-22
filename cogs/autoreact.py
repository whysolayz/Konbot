"""$ar - the bot reacts with your emoji whenever someone @pings YOU.

$ar <emoji>      set your own autoreact - ONE emoji (admins + roles added in $ar config)
$ar off          turn it off for yourself
$ar config       panel: pick roles that may use it (admins only)
"""
import logging
import re

import discord
from discord.ext import commands

from core import ratelimit
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import OwnedView

log = logging.getLogger("kon.autoreact")

MAX_EMOJIS = 1          # per person
MAX_PINGED = 5          # max different autoreacts added to one message
MAX_ROLES = 25          # roles that can get access (fits one select menu)
CUSTOM_EMOJI = re.compile(r"(<a?:\w{2,32}:\d{15,25}>)")
OFF_WORDS = {"off", "remove", "clear", "disable", "none", "stop"}


class ConfigView(OwnedView):
    """$ar config panel: add roles (role picker) / remove roles / clear all."""

    def __init__(self, cog: "Autoreact", ctx: commands.Context):
        super().__init__(ctx.author.id, timeout=300)
        self.cog = cog
        self.guild = ctx.guild
        self.build()

    def build(self):
        self.clear_items()
        self._add = discord.ui.RoleSelect(placeholder="Add roles that can use  $ar setup ...",
                                          min_values=1, max_values=10, row=0)
        self._add.callback = self.on_add
        self.add_item(self._add)

        roles = self.cog.config_roles(self.guild)
        if roles:
            self._remove = discord.ui.Select(
                placeholder="Remove roles ...", min_values=1, max_values=len(roles[:25]), row=1,
                options=[discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles[:25]])
            self._remove.callback = self.on_remove
            self.add_item(self._remove)
            clear = discord.ui.Button(label="Clear all roles", style=discord.ButtonStyle.danger,
                                      emoji=I.e("cross"), row=2)
            clear.callback = self.on_clear
            self.add_item(clear)

    async def _refresh(self, interaction: discord.Interaction):
        self.build()
        await interaction.response.edit_message(embed=self.cog.config_embed(self.guild), view=self)

    async def on_add(self, interaction: discord.Interaction):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        c = self.cog.cfg(self.guild.id)
        room = MAX_ROLES - len(c["roles"])
        new = [r for r in self._add.values if not r.is_default() and r.id not in c["roles"]][:max(room, 0)]
        c["roles"].extend(r.id for r in new)
        self.cog.save(self.guild.id)
        await self._refresh(interaction)

    async def on_remove(self, interaction: discord.Interaction):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        c = self.cog.cfg(self.guild.id)
        gone = {int(v) for v in self._remove.values}
        c["roles"] = [r for r in c["roles"] if r not in gone]
        self.cog.save(self.guild.id)
        await self._refresh(interaction)

    async def on_clear(self, interaction: discord.Interaction):
        if not await ratelimit.guard_interaction(interaction):
            return
        self.cog.cfg(self.guild.id)["roles"] = []
        self.cog.save(self.guild.id)
        await self._refresh(interaction)


class Autoreact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cache: dict[int, dict] = {}

    # ---------- data ----------
    def cfg(self, gid: int) -> dict:
        c = self._cache.get(gid)
        if c is None:
            raw = self.bot.db.get_settings(gid, "autoreact")
            c = self._cache[gid] = {"roles": list(raw.get("roles", [])), "users": dict(raw.get("users", {}))}
        return c

    def save(self, gid: int):
        self.bot.db.set_settings(gid, "autoreact", self.cfg(gid))

    def config_roles(self, guild: discord.Guild) -> list[discord.Role]:
        return [r for r in (guild.get_role(i) for i in self.cfg(guild.id)["roles"]) if r]

    def can_use(self, member: discord.Member) -> bool:
        """Admins (Manage Server) always, everybody else only with a role added in $ar config."""
        if member.guild_permissions.manage_guild:
            return True
        allowed = set(self.cfg(member.guild.id)["roles"])
        return any(r.id in allowed for r in member.roles)

    def drop_emoji(self, gid: int, uid: int, emoji: str):
        c = self.cfg(gid)
        lst = c["users"].get(str(uid), [])
        if emoji in lst:
            lst.remove(emoji)
            if not lst:
                c["users"].pop(str(uid), None)
            self.save(gid)

    def config_embed(self, guild: discord.Guild) -> discord.Embed:
        roles = self.config_roles(guild)
        active = len(self.cfg(guild.id)["users"])
        e = discord.Embed(title=f"{I.s('settings')} Auto Reacter - Config", color=BLURPLE, description=(
            f"{I.s('crown')} **Roles that can use `{PREFIX}ar`:**\n"
            + (" ".join(r.mention for r in roles) if roles else "*none yet*")
            + f"\n\n{I.s('shield_check')} Admins (**Manage Server**) can always use it.\n"
              f"{I.s('users')} Active autoreacters: **{active}**"))
        e.set_footer(text="Or: $ar config add/remove @role · $ar config clear")
        return e

    # ---------- the actual reacting ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """React with a member's emoji when somebody @pings that member in the text (not on replies, not on their own messages)."""
        if message.guild is None or message.author.bot or message.webhook_id or not message.raw_mentions:
            return
        perms = message.channel.permissions_for(message.guild.me)
        if not (perms.add_reactions and perms.read_message_history):
            return
        users = self.cfg(message.guild.id)["users"]
        todo: dict[str, discord.Member] = {}               # emoji -> pinged member
        # raw_mentions = only the @pings typed in the message text. message.mentions would ALSO contain the author of a
        # replied-to message, so replying to someone (even with reply-ping on) must not trigger their autoreact.
        for uid in dict.fromkeys(message.raw_mentions):
            member = message.guild.get_member(uid)
            if member is None or member.bot or member.id == message.author.id:
                continue
            emojis = users.get(str(member.id))
            if not emojis or not self.can_use(member):     # no autoreact / lost the role -> paused
                continue
            todo.setdefault(emojis[0], member)             # ONE emoji per member
            if len(todo) >= MAX_PINGED:
                break
        for emoji, member in todo.items():
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as e:
                if e.code == 10014:                        # emoji was deleted / bot can't see it any more
                    self.drop_emoji(message.guild.id, member.id, emoji)
                    continue
                if e.code in (10008, 90001) or isinstance(e, discord.Forbidden):   # msg deleted / user blocked bot
                    return
                log.warning("autoreact failed (%s): %s", emoji, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        c = self.cfg(role.guild.id)
        if role.id in c["roles"]:
            c["roles"].remove(role.id)
            self.save(role.guild.id)

    # ---------- $ar ----------
    @commands.group(name="ar", aliases=["autoreact", "autoreacter", "au"], invoke_without_command=True)
    @commands.guild_only()
    async def autoreacter(self, ctx: commands.Context, *, emoji: str = ""):
        """$ar [emoji] - set the emoji I react with when you get pinged (no emoji = show your status)"""
        if emoji.strip():
            return await self.set_emoji(ctx, emoji)
        mine = self.cfg(ctx.guild.id)["users"].get(str(ctx.author.id), [])
        allowed = self.can_use(ctx.author)
        lines = [
            "Pick **one emoji** and I'll react with it every time someone **@mentions you**.",
            "",
            f"{I.s('heart')} **Your emoji:** {mine[0] if mine else '*not set yet*'}",
            f"{I.s('shield_check')} **Access:** " + (f"{I.s('check')} you can use it" if allowed else f"{I.s('cross')} ask an admin"),
            "",
            "**How to use**",
            f"`{PREFIX}ar <emoji>` · set your emoji",
            f"`{PREFIX}ar off` · turn it off",
        ]
        if ctx.author.guild_permissions.manage_guild:
            lines += ["", "**Admin**", f"`{PREFIX}ar config` · choose which roles can use it"]
        e = discord.Embed(title=f"{I.s('sparkles')} Auto Reacter", color=BRAND, description="\n".join(lines))
        e.set_footer(text="1 emoji each · I react when you're pinged")
        await ctx.reply(embed=e, mention_author=False)

    async def resolve_plain_emoji(self, ctx: commands.Context, text: str) -> str:
        """Members without Nitro can't send other servers' emojis - Discord sends them as plain `:name:`.
        The BOT can use every emoji of every server it is in (+ its own application emojis), so look the emoji up by
        `:name:`, plain `name` or the emoji ID and turn it into the real `<:name:id>`. Normal / already-custom emojis pass through."""
        t = text.strip()
        m = re.fullmatch(r":([A-Za-z0-9_]{2,32}):", t) or re.fullmatch(r"([A-Za-z0-9_]{2,32})", t)
        if m is None and not re.fullmatch(r"\d{15,25}", t):
            return text
        if t.lower() in OFF_WORDS:
            return text
        em = None
        if t.isdigit():
            em = self.bot.get_emoji(int(t))
            if em is None:
                em = next((e for e in await self._app_emojis() if e.id == int(t)), None)
        else:
            name = m.group(1).lower()
            pool = [e for e in self.bot.emojis if e.name.lower() == name and e.is_usable()]
            pool.sort(key=lambda e: (e.guild_id != ctx.guild.id, e.name != m.group(1)))     # this server first, exact case first
            em = pool[0] if pool else next((e for e in await self._app_emojis() if e.name.lower() == name), None)
        if em is None:
            if t.isdigit() or t.startswith(":"):
                raise VCError("I can't find that emoji. I can only use emojis from servers I'm in.")
            return text                                         # a plain word that isn't an emoji -> normal error below
        return str(em)

    async def _app_emojis(self) -> list:
        try:
            return await self.bot.fetch_application_emojis()
        except Exception:
            return []

    async def set_emoji(self, ctx: commands.Context, emojis: str):
        if not self.can_use(ctx.author):
            raise VCError("Auto Reacter isn't open to you yet. Ask an admin.")
        emojis = emojis.strip()
        c = self.cfg(ctx.guild.id)
        if emojis.lower() in OFF_WORDS:
            if c["users"].pop(str(ctx.author.id), None) is None:
                raise VCError("You haven't set an autoreact.")
            self.save(ctx.guild.id)
            return await ctx.reply(ok("Auto Reacter is **off**."), mention_author=False)

        perms = ctx.channel.permissions_for(ctx.guild.me)
        if not (perms.add_reactions and perms.read_message_history):
            raise VCError("I need **Add Reactions** and **Read Message History** here.")

        emojis = await self.resolve_plain_emoji(ctx, emojis)       # ":name:" / emoji id (members without Nitro) -> <:name:id>
        tokens = CUSTOM_EMOJI.sub(r" \1 ", emojis).split()
        tokens = list(dict.fromkeys(tokens))
        if len(tokens) > MAX_EMOJIS:
            raise VCError(f"Just **1 emoji**, like `{PREFIX}ar <emoji>`")
        tok = tokens[0]
        try:                                     # real test: Discord itself tells us if the emoji is usable
            await ctx.message.add_reaction(tok)
        except discord.HTTPException:
            if CUSTOM_EMOJI.fullmatch(tok) and not perms.external_emojis:
                raise VCError("I need **Use External Emojis** for that one.")
            raise VCError(f"I can't use {tok if CUSTOM_EMOJI.fullmatch(tok) else f'`{tok}`'}. Use a normal emoji or one from a server I'm in.")
        c["users"][str(ctx.author.id)] = [tok]
        self.save(ctx.guild.id)
        await ctx.reply(ok(f"Done. I'll react with {tok} when someone pings you. Turn off: `{PREFIX}ar off`"), mention_author=False)

    @autoreacter.command(name="setup", aliases=["set", "on"])
    @commands.guild_only()
    async def au_setup(self, ctx: commands.Context, *, emoji: str = ""):
        """$ar setup <emoji> - same as `$ar <emoji>`"""
        if not emoji.strip():
            raise VCError(f"Which emoji? Like `{PREFIX}ar <emoji>`")
        await self.set_emoji(ctx, emoji)

    @autoreacter.command(name="off", aliases=["disable", "stop"])
    @commands.guild_only()
    async def au_off(self, ctx: commands.Context):
        """$ar off - turn your autoreact off"""
        c = self.cfg(ctx.guild.id)
        if c["users"].pop(str(ctx.author.id), None) is None:
            raise VCError("You haven't set an autoreact.")
        self.save(ctx.guild.id)
        await ctx.reply(ok("Auto Reacter is **off**."), mention_author=False)

    # ---------- $ar config ----------
    @autoreacter.group(name="config", aliases=["cfg", "roles"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def au_config(self, ctx: commands.Context):
        """$ar config - pick the roles that may use `$ar setup` (admin)"""
        view = ConfigView(self, ctx)
        view.message = await ctx.reply(embed=self.config_embed(ctx.guild), view=view, mention_author=False)

    @au_config.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def au_config_add(self, ctx: commands.Context, roles: commands.Greedy[discord.Role]):
        """$ar config add @role [@role ...] - give roles access to `$ar setup`"""
        roles = [r for r in roles if not r.is_default()]
        if not roles:
            raise VCError("Mention a role: `$ar config add @Role`.")
        c = self.cfg(ctx.guild.id)
        new = [r for r in roles if r.id not in c["roles"]]
        if len(c["roles"]) + len(new) > MAX_ROLES:
            raise VCError(f"Max {MAX_ROLES} roles.")
        c["roles"].extend(r.id for r in new)
        self.save(ctx.guild.id)
        await ctx.reply(ok(f"Added {' '.join(r.mention for r in new) or 'nothing new'}. They can use `{PREFIX}ar` now."), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @au_config.command(name="remove", aliases=["delete", "del"])
    @commands.has_permissions(manage_guild=True)
    async def au_config_remove(self, ctx: commands.Context, roles: commands.Greedy[discord.Role]):
        """$ar config remove @role [@role ...]"""
        c = self.cfg(ctx.guild.id)
        gone = [r for r in roles if r.id in c["roles"]]
        if not gone:
            raise VCError("None of those roles had access.")
        c["roles"] = [i for i in c["roles"] if i not in {r.id for r in gone}]
        self.save(ctx.guild.id)
        await ctx.reply(ok(f"Removed {' '.join(r.mention for r in gone)}."), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @au_config.command(name="clear", aliases=["reset"])
    @commands.has_permissions(manage_guild=True)
    async def au_config_clear(self, ctx: commands.Context):
        """$ar config clear - remove all roles (only admins keep access)"""
        self.cfg(ctx.guild.id)["roles"] = []
        self.save(ctx.guild.id)
        await ctx.reply(ok("All roles removed. Only admins can use it now."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Autoreact(bot))
