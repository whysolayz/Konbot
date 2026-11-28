"""$cleanbots (was $purgebot, $pb) - clean up bot messages and command messages in the current channel.

    $pb          -> removes up to 10 bot messages / command messages (newest first)
    $pb 100      -> up to 100 (max 500)

$clean (was $purgemember, $p, $pm) - delete the messages of ONE member in the current channel.

    $p @member         -> that member's last 10 messages
    $p @member 100     -> last 100 (max 500)
    (reply to a message of the member) $p        -> purges that member (10)
    (reply to a message of the member) $p 50     -> purges that member (50)
    Members without Manage Messages still get their profile with $p (that was the old $p).

Removed: messages sent by any bot (Kon's replies, other bots' replies, embeds) and messages that are commands
(start with a prefix like  $afk  !ban  ?help  .play  -kick ...) - and the no-prefix commands (plain  afk  profile ...)
of members who have no-prefix access. Normal chat of members is never touched.
Every result says WHAT was removed ("6 bot replies · 4 commands" / "31 text · 4 files · 3 links").
The `$pb` message itself is removed too. Discord can't bulk-delete messages older than 14 days, so those are skipped.
"""
import asyncio
import re
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands

from core.config import PREFIX
from core.errors import VCError
from core.icons import I
from core.ui import replied_message

DEFAULT = 10
MAX = 500
SCAN_FACTOR = 6          # look at most N*6 (max 1500) recent messages to find N matches
# "<prefix char><letter/digit>": $afk  !ban  ?help  .play  -kick  +role  ;cmd  %rank  &x  ^x  *x  =x  ~x  >x  ,x
COMMAND_LIKE = re.compile(r"^[!$?.;\-+%&^*=~>,/][A-Za-z0-9]")


def report(kinds: dict) -> str:
    """{'bot replies': 6, 'commands': 4} -> '6 bot replies · 4 commands' (the clean report under the result)"""
    return " · ".join(f"{n} {label}" for label, n in kinds.items() if n)


class Purge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def is_target(self, m: discord.Message, prefixes: tuple) -> bool:
        if m.author.bot or m.webhook_id:
            return True
        c = m.content or ""
        if not c:
            return False
        if COMMAND_LIKE.match(c) is not None or any(p and c.startswith(p) for p in prefixes):
            return True
        # no-prefix members: a plain "afk", "profile", "vclock" ... is a command too
        author = m.author
        if isinstance(author, discord.User) and m.guild:
            author = m.guild.get_member(author.id) or author
        if not self.bot.is_noprefix(author):
            return False
        word = c.split(None, 1)[0].lower()
        bot = self.bot
        if word in bot.all_commands or (m.guild and word in bot.aliases(m.guild.id)):     # plain lookup: no full get_context
            return True
        if not (word == "vc" or word.startswith("set")):                                  # only these two forms need the slow path
            return False
        try:
            ctx = await bot.get_context(m)
        except Exception:
            return False
        return bool(ctx.valid)

    @staticmethod
    async def _delete(ch, todo: list, reason: str):
        """Delete in bulk chunks of 100 - several chunks at the same time."""
        async def one(chunk):
            if len(chunk) == 1:
                await chunk[0].delete()
            else:
                await ch.delete_messages(chunk, reason=reason)
        await asyncio.gather(*(one(todo[i:i + 100]) for i in range(0, len(todo), 100)))

    @staticmethod
    def _notice(ch, text: str, **kw):
        """Send the short result without making the command wait for it."""
        async def go():
            try:
                await ch.send(text, delete_after=7, **kw)
            except discord.HTTPException:
                pass
        asyncio.create_task(go())

    @commands.command(name="purgebot", aliases=["pb"])
    @commands.has_permissions(manage_messages=True)
    @commands.guild_only()
    async def purgebot(self, ctx: commands.Context, amount: int = DEFAULT):
        """$purgebot [amount] - delete bot messages + command messages (default 10, max 500)"""
        if amount < 1:
            raise VCError("Amount must be at least 1.")
        clamped = amount > MAX
        amount = min(amount, MAX)
        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me)
        if not (perms.manage_messages and perms.read_message_history):
            raise VCError("I need **Manage Messages** and **Read Message History** here.")

        prefixes = (PREFIX,)

        limit_age = discord.utils.utcnow() - timedelta(days=14) + timedelta(minutes=5)
        found, skipped_old = [], 0
        kinds = {"bot replies": 0, "commands": 0}
        async for m in ch.history(limit=min(amount * SCAN_FACTOR + 20, 1500), before=ctx.message):
            if await self.is_target(m, prefixes):
                if m.created_at < limit_age:
                    skipped_old += 1
                    continue
                found.append(m)
                kinds["bot replies" if (m.author.bot or m.webhook_id) else "commands"] += 1
                if len(found) >= amount:
                    break

        todo = [ctx.message, *found]
        try:
            await self._delete(ch, todo, f"purgebot by {ctx.author} ({ctx.author.id})")
        except discord.HTTPException as e:
            raise VCError(f"Couldn't delete: {e.text or e.status}")

        text = f"{I.s('check')} Deleted **{len(found)}** bot message{'s' if len(found) != 1 else ''}."
        if found:
            text += f"\n-# {report(kinds)}"
        if clamped:
            text += f" (max is {MAX})"
        if skipped_old:
            text += f" {skipped_old} older than 14 days skipped."
        self._notice(ch, text)


    # ---------- $p / $purgemember ----------
    @commands.command(name="purgemember", aliases=["p", "pm", "purgeuser"])
    @commands.guild_only()
    async def purgemember(self, ctx: commands.Context, *args: str):
        """$p @member [amount] - delete a member's messages (default 10, max 500). Reply to a member + `$p` works too."""
        profile = self.bot.get_command("profile")
        if not ctx.author.guild_permissions.manage_messages:        # normal members: $p = profile like before
            if ctx.invoked_with == "clean":                         # the new name is cleanup only (no profile surprise)
                raise commands.CheckFailure("needs Manage Messages")
            target = None
            if args:
                try:
                    target = await commands.MemberConverter().convert(ctx, args[0])
                except commands.BadArgument:
                    target = None
            return await ctx.invoke(profile, member=target)

        amount, member = DEFAULT, None
        for tok in args:
            if tok.isdigit() and len(tok) <= 4:
                amount = int(tok)
                continue
            if member is None:
                try:
                    member = await commands.MemberConverter().convert(ctx, tok)
                    continue
                except commands.BadArgument:
                    pass
            raise VCError("Use `$p @member [amount]`, or reply to their message with `$p`.")
        if member is None:
            ref = await replied_message(ctx)
            if ref is not None:
                member = ctx.guild.get_member(ref.author.id)
                if member is None:
                    raise VCError("They left the server.")
        if member is None:
            return await ctx.invoke(profile, member=None)            # plain `$p` = your profile
        if amount < 1:
            raise VCError("Amount must be at least 1.")
        clamped = amount > MAX
        amount = min(amount, MAX)
        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me)
        if not (perms.manage_messages and perms.read_message_history):
            raise VCError("I need **Manage Messages** and **Read Message History** here.")

        limit_age = discord.utils.utcnow() - timedelta(days=14) + timedelta(minutes=5)
        found, skipped_old = [], 0
        kinds = {"text": 0, "files": 0, "links": 0}
        async for m in ch.history(limit=min(amount * SCAN_FACTOR + 50, 3000), before=ctx.message):
            if m.author.id != member.id:
                continue
            if m.created_at < limit_age:
                skipped_old += 1
                continue
            found.append(m)
            kinds["files" if m.attachments else "links" if "http" in (m.content or "") else "text"] += 1
            if len(found) >= amount:
                break

        todo = [ctx.message, *found]
        try:
            await self._delete(ch, todo, f"purgemember by {ctx.author} ({ctx.author.id})")
        except discord.HTTPException as e:
            raise VCError(f"Couldn't delete: {e.text or e.status}")

        text = f"{I.s('check')} Deleted **{len(found)}** message{'s' if len(found) != 1 else ''} from {member.mention}."
        if found:
            text += f"\n-# {report(kinds)}"
        if clamped:
            text += f" (max is {MAX})"
        if skipped_old:
            text += f" {skipped_old} older than 14 days skipped."
        self._notice(ch, text, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Purge(bot))
