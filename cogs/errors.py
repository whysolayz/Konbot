"""One place for every command error (prefix and slash)."""
import inspect
import logging
import time
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from core import flair, names, ratelimit
from core.config import PREFIX
from core.errors import CommandDisabled, VCError
from core.icons import I, err

log = logging.getLogger("kon.errors")

# custom wording (taken from the screenshots)
MISSING = {
    "vcpermit": "Mention someone to permit.",
    "vckick": "Mention someone to kick.",
}
VERB = {"vcban": "ban", "vcunban": "unban", "vctransfer": "transfer ownership to", "vcmute": "mute",
        "dragme": "drag you to"}


def unwrap(error):
    while hasattr(error, "original") and error.original is not None:
        error = error.original
    return error


class Errors(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._hint_at: dict = {}                    # channel id -> when the last "did you mean" was shown
        bot.tree.on_error = self.on_tree_error

    async def on_tree_error(self, interaction: discord.Interaction, error):
        error = unwrap(error)
        log.error("slash error: %r", error)
        if isinstance(error, app_commands.CheckFailure) and not isinstance(error, app_commands.CommandOnCooldown):
            text = None                                     # no rights = no reply (slash can't be fully silent, so just ack)
        else:
            text = err("Something went wrong.")
        try:
            if text is None:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                    await interaction.delete_original_response()
                return
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error):
        error = unwrap(error)
        name = ctx.command.name if ctx.command else ""

        if isinstance(error, commands.CommandNotFound):
            return await self.suggest(ctx)                     # "did you mean $lock?" (only for a close typo)
        if isinstance(error, (commands.NotOwner, commands.MissingPermissions,
                              commands.MissingRole, commands.MissingAnyRole)):
            return          # owner / admin commands used by someone without the rights: no reply at all
        if ctx.interaction is None and ctx.prefix == "":   # no-prefix user typed a normal word -> don't spam errors
            return
        if isinstance(error, ratelimit.RateLimited):
            return await self.send(ctx, ratelimit.limited_text(error.retry_after))
        if isinstance(error, CommandDisabled):
            return await self.send(ctx, err(f"`{PREFIX}{error.name}` is off here."), delete_after=6)
        if isinstance(error, VCError):
            return await self.send(ctx, err(str(error)))

        # "$vcpermit all"
        if name == "vcpermit" and ctx.interaction is None and isinstance(error, commands.BadArgument):
            parts = ctx.message.content.split()
            if len(parts) >= 2 and parts[-1].lower() == "all":
                try:
                    msg = await self.bot.vc_service.permit_all(ctx.author)
                    return await self.send(ctx, f"{I.s('check')} {msg}")
                except VCError as e:
                    return await self.send(ctx, err(str(e)))

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                              commands.BadUnionArgument)):
            if name in MISSING:
                return await self.send(ctx, err(MISSING[name]))
            if name in VERB:
                return await self.send(ctx, err(f"Mention someone to {VERB[name]}."))
            if isinstance(error, commands.MissingRequiredArgument):
                return await self.send(ctx, err(f"I need a `{error.param.name}` for that."))
            return await self.send(ctx, err(str(error)))
        if isinstance(error, commands.NoPrivateMessage):
            return await self.send(ctx, err("That only works in a server."))
        if isinstance(error, commands.CheckFailure):
            return

        log.error("Unhandled error in %s:\n%s", name,
                  "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        await self.send(ctx, err(f"I couldn't finish that (`{type(error).__name__}`) - it's been logged for the owner."))

    async def suggest(self, ctx: commands.Context):
        """A mistyped command gets a quiet hint with the closest commands THIS person may use. Rules that keep it
        from being annoying: only with the real prefix, only for a close typo, once per 15 s per channel, gone after 10 s."""
        if ctx.interaction is not None or ctx.guild is None or ctx.prefix != PREFIX:
            return
        now = time.monotonic()
        if now - self._hint_at.get(ctx.channel.id, 0) < 15:
            return
        pool = {n: c for n, c in self.bot.all_commands.items() if not c.hidden and c.name != "rolecmd"}
        picked, seen = [], set()
        for guess in flair.closest(ctx.invoked_with or "", pool, n=3):
            cmd = pool[guess]
            if cmd.qualified_name in seen or not await self.may_use(ctx, cmd):
                continue
            seen.add(cmd.qualified_name)
            picked.append(f"`{PREFIX}{names.shown(cmd.qualified_name)}`")
        if not picked:
            return
        self._hint_at[ctx.channel.id] = now
        try:
            await ctx.reply(f"{I.s('help')} Did you mean {' or '.join(picked[:2])}?", mention_author=False, delete_after=10)
        except discord.HTTPException:
            pass

    @staticmethod
    async def may_use(ctx: commands.Context, cmd: commands.Command) -> bool:
        """Only the command's own checks (and its group's), not the global rate limiter."""
        c = cmd
        while c is not None:
            for chk in c.checks:
                try:
                    r = chk(ctx)
                    if inspect.isawaitable(r):
                        r = await r
                    if not r:
                        return False
                except Exception:
                    return False
            c = c.parent
        return True

    @staticmethod
    async def send(ctx: commands.Context, text: str, delete_after: float = None):
        kw = {"delete_after": delete_after} if delete_after and ctx.interaction is None else {}
        try:
            await ctx.reply(text, mention_author=False, ephemeral=True, **kw)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Errors(bot))
