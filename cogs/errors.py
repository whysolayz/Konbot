"""One place for every command error (prefix and slash)."""
import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
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

        if isinstance(error, (commands.CommandNotFound, commands.NotOwner, commands.MissingPermissions,
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

    @staticmethod
    async def send(ctx: commands.Context, text: str, delete_after: float = None):
        kw = {"delete_after": delete_after} if delete_after and ctx.interaction is None else {}
        try:
            await ctx.reply(text, mention_author=False, ephemeral=True, **kw)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Errors(bot))
