"""AFK system (same behaviour + same afk_data.json as before, now with custom icons).

Messages: the reason sits UNDER the time, every line has a Kon icon:
    ⏱ Time Away: 9s
    ℹ Reason: meow
    🔔 Mentions (Page 1/1): ...
"""
import json
import os
import re
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from core import emoji
from core.config import AFK_FILE, BLURPLE, MENTIONS_PER_PAGE, TIMEZONE
from core.icons import I


def fmt_duration(seconds: int) -> str:
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def now_footer() -> str:
    t = datetime.now(TIMEZONE)
    return f"Today at {t.hour % 12 or 12}:{t.minute:02d} {t.strftime('%p')}"


class MentionPager(discord.ui.View):
    def __init__(self, cog, member, data, owner_id):
        super().__init__(timeout=120)
        self.cog, self.member, self.data, self.owner_id, self.page = cog, member, data, owner_id, 0
        self.pages = max(1, -(-len(data["mentions"]) // MENTIONS_PER_PAGE))
        self.prev.emoji = I.e("left")
        self.next.emoji = I.e("right")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.cog.welcome_embed(self.member, self.data, self.page), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _):
        self.page = min(self.pages - 1, self.page + 1)
        await interaction.response.edit_message(embed=self.cog.welcome_embed(self.member, self.data, self.page), view=self)


AFK_MAX = 50
CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")


def afk_len(text: str) -> int:
    """Length of an AFK reason: every custom emoji counts as 1 (not the characters of its <:name:id> code)."""
    return len(CUSTOM_EMOJI.sub("\u2022", text).replace("\ufe0f", ""))


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.afk: dict[int, dict] = {}
        self.tag_enabled: dict[int, bool] = {}

    # ---------- storage ----------
    def save(self):
        tmp = str(AFK_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"afk": {str(k): v for k, v in self.afk.items()},
                       "tag": {str(k): v for k, v in self.tag_enabled.items()}}, f)
        os.replace(tmp, AFK_FILE)

    async def cog_load(self):
        if os.path.exists(AFK_FILE):
            try:
                with open(AFK_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.afk.update({int(k): v for k, v in d.get("afk", {}).items()})
                self.tag_enabled.update({int(k): v for k, v in d.get("tag", {}).items()})
            except Exception as e:
                print("AFK data load error:", e)

    # ---------- nickname tag ----------
    async def set_afk_nick(self, member: discord.Member, data: dict):
        if not self.tag_enabled.get(member.guild.id, True):
            return
        base = member.display_name
        if base.startswith("[AFK] "):
            return
        data["old_nick"] = member.nick
        try:
            await member.edit(nick=f"[AFK] {base}"[:32], reason="User went AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def remove_afk_nick(self, member: discord.Member, data: dict):
        if not member.display_name.startswith("[AFK] "):
            return
        try:
            await member.edit(nick=data.get("old_nick"), reason="User returned from AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

    def welcome_embed(self, member, data: dict, page: int = 0) -> discord.Embed:
        mentions = data.get("mentions", [])
        total = len(mentions)
        pages = max(1, -(-total // MENTIONS_PER_PAGE))
        page = max(0, min(page, pages - 1))
        away = int(time.time() - data["since"])
        start = page * MENTIONS_PER_PAGE
        chunk = mentions[start:start + MENTIONS_PER_PAGE]
        text = "\n".join(f"**#{start + i}** · {m.get('author', 'someone')} · [Jump to message]({m['url']})"
                         for i, m in enumerate(chunk, 1)) or "None"
        embed = discord.Embed(
            description=(f"{I.s('stopwatch')} **Time Away:** {fmt_duration(away)}\n"
                         f"{I.s('info')} **Reason:** {data['reason']}\n\n"
                         f"{I.s('bell_ring')} **Mentions (Page {page + 1}/{pages}):**\n{text}"),
            color=BLURPLE)
        embed.set_author(name=f"Welcome back, {member.display_name}!", icon_url=member.display_avatar.url)
        embed.set_footer(text=f"{total} total mentions | {now_footer()}")
        return embed

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        ctx = await self.bot.get_context(message)
        is_afk_cmd = ctx.valid and ctx.command and ctx.command.name == "afk"

        if message.author.id in self.afk and not is_afk_cmd:
            data = self.afk.pop(message.author.id)
            self.save()
            await self.remove_afk_nick(message.author, data)
            view = MentionPager(self, message.author, data, message.author.id) if len(data["mentions"]) > MENTIONS_PER_PAGE else None
            await message.reply(embed=self.welcome_embed(message.author, data), view=view, mention_author=False)

        if not is_afk_cmd:
            embeds, seen = [], set()
            for user in message.mentions:
                if user.id in self.afk and user.id != message.author.id and not user.bot and user.id not in seen:
                    seen.add(user.id)
                    data = self.afk[user.id]
                    data["mentions"].append({
                        "author": message.author.display_name,
                        "channel_id": message.channel.id,
                        "url": message.jump_url,
                        "content": (message.content[:100] or "[no text]").replace("\n", " "),
                    })
                    e = discord.Embed(
                        description=(f"{I.s('stopwatch')} **Away since:** <t:{int(data['since'])}:R>\n"
                                     f"{I.s('info')} **Reason:** {data['reason']}"),
                        color=BLURPLE)
                    e.set_author(name=f"{user.display_name} is AFK", icon_url=user.display_avatar.url)
                    embeds.append(e)
            if embeds:
                self.save()
                try:
                    await message.reply(embeds=embeds[:5], mention_author=False, delete_after=5)
                except discord.HTTPException:
                    pass

    # ---------- commands ----------
    @commands.hybrid_command(name="afk", aliases=['away'], description="Go AFK with an optional reason")
    @app_commands.describe(reason="Why are you away?")
    @commands.guild_only()
    async def afk_cmd(self, ctx: commands.Context, *, reason: str = "AFK"):
        # emojis stay only when they are from this server / the bot's servers / the bot's own emojis; others are removed
        reason, _ = await emoji.sanitize(self.bot, ctx.guild, reason.strip())
        reason = reason or "AFK"
        n = afk_len(reason)
        if n > AFK_MAX:
            return await ctx.reply(f"{I.s('cross')} Keep the reason under **{AFK_MAX}** characters (yours is {n}).", mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())
        data = {"reason": reason, "since": time.time(), "guild_id": ctx.guild.id, "old_nick": None, "mentions": []}
        self.afk[ctx.author.id] = data
        await self.set_afk_nick(ctx.author, data)
        self.save()
        e = discord.Embed(description=f"{I.s('check')} **You're now AFK**\n{I.s('info')} **Reason:** {reason}", color=BLURPLE)
        e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=e, mention_author=False)

    @commands.command(name="afktag")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def afktag_cmd(self, ctx: commands.Context, state: str = None):
        """$afktag [on/off] - turn the [AFK] nickname tag on/off (admin)"""
        current = self.tag_enabled.get(ctx.guild.id, True)
        if state is None:
            new = not current
        elif state.lower() in ("on", "enable", "true"):
            new = True
        elif state.lower() in ("off", "disable", "false"):
            new = False
        else:
            return await ctx.reply(f"{I.s('cross')} Use `{ctx.prefix}afktag on` or `{ctx.prefix}afktag off`.", mention_author=False)
        self.tag_enabled[ctx.guild.id] = new
        self.save()
        await ctx.reply(embed=discord.Embed(description=f"{I.s('check')} AFK tag is **{'on' if new else 'off'}**.", color=BLURPLE),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(AFK(bot))
