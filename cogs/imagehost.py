"""Image host - turn any image / GIF / banner into a link. ALL files live in ONE private storage channel that the BOT OWNER
sets in HIS OWN server (`$imgchannel`). Whatever any other server uploads through Kon (self-role banners, `$tolink`, ...)
is saved there too; only the LINK (+ the hosting message id) is kept in each server's data, so backups stay small.

BOT OWNER ONLY (the application owner / OWNER_IDS) - admins and $konextra owners can't use these commands.
$imgchannel [#channel]      set the storage channel (no channel = this one)      $imgchannelreset  remove it
$tolink [name] [url]        attach an image / GIF, reply to a message with one, or give a link -> saved link
                            (aliases: $link, $imglink)   up to 5 files at once
$links [page]               list the saved links
$linkget <name>             fresh link + preview of a saved image
$linkdel <name>             delete a saved image (also deletes the hosting message)

Discord's CDN links carry an expiry stamp, so a link you paste somewhere else stops working after a while. That is why
the hosting message id is kept: `$linkget <name>` (and every Kon feature that uses a saved image) asks Discord for a
FRESH link each time. Other cogs: `bot.get_cog("ImageHost").fresh_url(guild, name)` / `.resolve_image(guild, text)`.
"""
import asyncio
import io
import logging
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.config import ASSET_CHANNEL_ID, BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import OwnedView, replied_message

log = logging.getLogger("kon.imagehost")

KEY = "imagehost"                 # per server: the saved names -> links
HOST_KEY = "imagehost_host"       # ONE global setting (stored under guild 0): the owner's storage channel
HOST_GID = 0
MAX_FILES = 5
MAX_ITEMS = 300
HARD_LIMIT = 25 * 1024 * 1024
PER_PAGE = 6
UA = {"User-Agent": "Mozilla/5.0 (Kon bot)"}
URL_RE = re.compile(r"https?://[^\s<>]+")
IMG_URL = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", re.I)
NO_PING = discord.AllowedMentions.none()


def sniff_ext(data: bytes) -> Optional[str]:
    """Real image type from the file bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:32]


def fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class LinksView(OwnedView):
    """Saved images list with First / Prev / Next / Last buttons."""

    def __init__(self, cog, ctx, items: list, channel_text: str, page: int = 1):
        super().__init__(ctx.author.id, timeout=300)
        self.items, self.channel_text = items, channel_text
        self.pages = max(1, -(-len(items) // PER_PAGE))
        self.page = min(max(page, 1), self.pages) - 1
        self.sync()

    def embed(self) -> discord.Embed:
        chunk = self.items[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]
        blocks = [f"{I.s('tag')} **{k}**\n{I.s('image')} `{it['t'].upper()}` · `{fmt_size(it.get('s', 0))}`\n"
                  f"{I.s('member')} <@{it['by']}> · <t:{it['ts']}:R>" for k, it in chunk]
        e = discord.Embed(color=BLURPLE, title=f"{I.s('image')} Saved images ({len(self.items)})",
                          description="\n\n".join(blocks) + f"\n\n{I.s('folder')} **Host channel:** {self.channel_text}")
        e.set_footer(text=f"Page {self.page + 1}/{self.pages} · {PREFIX}linkget <name> · {PREFIX}linkdel <name>")
        return e

    def sync(self):
        self.first.emoji, self.prev.emoji = I.e("rewind"), I.e("prev")        # Kon icons
        self.next.emoji, self.last.emoji = I.e("next"), I.e("fast_forward")
        self.first.disabled = self.prev.disabled = self.page <= 0
        self.next.disabled = self.last.disabled = self.page >= self.pages - 1
        self.counter.label = f"{self.page + 1}/{self.pages}"

    async def go(self, interaction: discord.Interaction, page: int):
        self.page = min(max(page, 0), self.pages - 1)
        self.sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, 0)

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page + 1)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.pages - 1)


class ImageHost(commands.Cog):
    """Image / GIF / banner -> saved link."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------ data
    def cfg(self, gid: int) -> dict:
        c = self.bot.db.get_settings(gid, KEY)
        c.setdefault("channel", None)
        c.setdefault("items", {})
        return c

    def save(self, gid: int, c: dict):
        self.bot.db.set_settings(gid, KEY, c)

    def host_id(self) -> int:
        return self.bot.db.get_settings(HOST_GID, HOST_KEY).get("channel") or ASSET_CHANNEL_ID or 0

    def host_ready(self) -> bool:
        try:
            self.storage_channel()
            return True
        except VCError:
            return False

    def storage_channel(self, guild=None) -> discord.TextChannel:
        """The bot owner's image channel (in the OWNER'S server) - the same one for every server."""
        cid = self.host_id()
        ch = self.bot.get_channel(cid) if cid else None
        if not isinstance(ch, discord.TextChannel):
            raise VCError(f"The bot owner hasn't set the image host channel yet (`{PREFIX}imgchannel #channel` in the owner's server).")
        p = ch.permissions_for(ch.guild.me)
        if not (p.view_channel and p.send_messages and p.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {ch.mention}.")
        return ch

    async def host_upload(self, data: bytes, filename: str, note: str) -> discord.Message:
        """For other cogs: upload a file into the owner's image channel and return the hosting message."""
        ch = self.storage_channel()
        return await ch.send(note, file=discord.File(io.BytesIO(data), filename=filename), allowed_mentions=NO_PING)

    # ------------------------------------------------------------ API for other features
    async def fresh_url(self, guild: discord.Guild, name: str) -> Optional[str]:
        """A working link for a saved image (asks Discord for a fresh one and remembers it)."""
        c = self.cfg(guild.id)
        it = c["items"].get(slug(name))
        if not it:
            return None
        ch = self.bot.get_channel(it["c"])
        if ch is not None:
            try:
                msg = await ch.fetch_message(it["m"])
                if msg.attachments and msg.attachments[0].url != it.get("url"):
                    it["url"] = msg.attachments[0].url
                    self.save(guild.id, c)
            except discord.HTTPException:
                pass
        return it.get("url")

    async def resolve_image(self, guild: discord.Guild, text: str) -> str:
        """A link, or the NAME of a saved image -> a usable link."""
        text = (text or "").strip()
        if text.startswith(("http://", "https://")):
            return text
        url = await self.fresh_url(guild, text)
        if not url:
            raise VCError(f"`{text}` is not a link and not a saved image name (see `{PREFIX}links`).")
        return url

    # ------------------------------------------------------------ download helpers
    async def download(self, url: str, limit: int) -> bytes:
        if not url.startswith(("http://", "https://")):
            raise VCError("Links must start with `https://`.")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=UA) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        raise VCError("I couldn't open that link. It may have expired.")
                    data = bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        data += chunk
                        if len(data) > limit:
                            raise VCError(f"That image is over **{limit // (1024 * 1024)} MB**.")
                    return bytes(data)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise VCError("I couldn't download that link.")

    async def gather(self, ctx: commands.Context, image: Optional[discord.Attachment], urls: list[str]) -> list:
        """Everything that can be converted: ("att", Attachment) / ("url", str). Own message first, then the replied one."""
        out: list = []
        if image is not None:
            out.append(("att", image))
        if ctx.interaction is None:                                   # prefix: all attachments of the message
            out += [("att", a) for a in ctx.message.attachments if image is None or a.id != image.id]
        ref = await replied_message(ctx) if ctx.interaction is None else None
        if ref is not None:
            out += [("att", a) for a in ref.attachments]
            for e in ref.embeds:
                u = e.image.url or (e.thumbnail.url if e.type == "image" else None)
                if u:
                    out.append(("url", u))
            out += [("url", u) for u in URL_RE.findall(ref.content or "") if IMG_URL.search(u)]
        out += [("url", u) for u in urls]
        seen, uniq = set(), []
        for kind, v in out:
            key = v.id if kind == "att" else v
            if key not in seen:
                seen.add(key)
                uniq.append((kind, v))
        return uniq[:MAX_FILES]

    # ------------------------------------------------------------ commands
    @commands.hybrid_command(name="imgchannel", aliases=["setimg", "imagechannel"],
                             description="(Bot owner) Set the private channel that stores converted images")
    @app_commands.describe(channel="Storage channel (default: this one)")
    @checks.true_owner_only()
    async def imgchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            raise VCError("Pick a normal text channel.")
        p = channel.permissions_for(ctx.guild.me)
        if not (p.view_channel and p.send_messages and p.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {channel.mention}.")
        self.bot.db.set_settings(HOST_GID, HOST_KEY, {"channel": channel.id, "guild": ctx.guild.id})
        await ctx.reply(ok(f"Image host channel = {channel.mention}. Images that **any** server uploads through Kon are "
                           f"saved here now. Keep it private and don't delete its messages.\n"
                           f"Convert with `{PREFIX}tolink` · list with `{PREFIX}links`."),
                        mention_author=False)

    @commands.hybrid_command(name="imgchannelreset", description="(Bot owner) Remove the image storage channel setting")
    @checks.true_owner_only()
    async def imgchannelreset(self, ctx: commands.Context):
        if not self.bot.db.get_settings(HOST_GID, HOST_KEY).get("channel"):
            raise VCError("No image channel was set.")
        self.bot.db.set_settings(HOST_GID, HOST_KEY, {})
        await ctx.reply(ok("Image channel removed. Saved links keep working; new uploads need a new channel."),
                        mention_author=False)

    @commands.hybrid_command(name="tolink", aliases=["link", "imglink"],
                             description="(Bot owner) Turn an image / GIF / banner into a saved link")
    @app_commands.describe(name="Name to save it under (optional)", image="Image or GIF file", url="...or a link to an image")
    @checks.true_owner_only()
    async def tolink(self, ctx: commands.Context, name: Optional[str] = None,
                     image: Optional[discord.Attachment] = None, url: Optional[str] = None):
        """$tolink [name] + attach a file / reply to a message with an image / $tolink name https://link"""
        urls = []
        if name and name.startswith(("http://", "https://")):
            urls.append(name)
            name = None
        if url and url.startswith(("http://", "https://")):
            urls.append(url)
        sources = await self.gather(ctx, image, urls)
        if not sources:
            raise VCError(f"Attach an image / GIF, reply to a message that has one, or give a link: "
                          f"`{PREFIX}tolink banner https://...`")
        ch = self.storage_channel()
        c = self.cfg(ctx.guild.id)
        base = slug(name) if name else ""
        if name and not base:
            raise VCError("Use letters, numbers, `-` or `_` for the name.")
        limit = min(ch.guild.filesize_limit, HARD_LIMIT)

        results = []
        async with ctx.typing():
            for i, (kind, src) in enumerate(sources, 1):
                if kind == "att":
                    if src.size > limit:
                        raise VCError(f"**{src.filename}** is over **{limit // (1024 * 1024)} MB**.")
                    data = await src.read()
                else:
                    data = await self.download(src, limit)
                ext = sniff_ext(data)
                if ext is None:
                    raise VCError("Only PNG, JPG, GIF or WEBP images work.")
                key = base if base and len(sources) == 1 else (f"{base}-{i}" if base else self.auto_name(c))
                replaced = key in c["items"]
                if not replaced and len(c["items"]) >= MAX_ITEMS:
                    raise VCError(f"The list is full ({MAX_ITEMS}). Delete some with `{PREFIX}linkdel <name>`.")
                msg = await ch.send(f"`{key}` · {ctx.author} · {ctx.guild.name} (storage - don't delete)",
                                    file=discord.File(io.BytesIO(data), filename=f"{key}.{ext}"),
                                    allowed_mentions=NO_PING)
                old = c["items"].get(key)
                c["items"][key] = {"c": msg.channel.id, "m": msg.id, "url": msg.attachments[0].url, "t": ext,
                                   "s": len(data), "by": ctx.author.id, "ts": int(time.time())}
                self.save(ctx.guild.id, c)
                if old:
                    await self.forget(old)
                results.append((key, ext, len(data), msg.attachments[0].url, replaced))

        e = discord.Embed(color=BRAND, title=f"{I.s('link')} Saved link" + ("s" if len(results) > 1 else ""))
        e.description = "\n\n".join(
            f"{I.s('tag')} **Name:** `{k}`\n{I.s('image')} **Type:** `{ext.upper()}`\n{I.s('folder')} **Size:** `{fmt_size(size)}`"
            + (f"\n{I.s('refresh')} **Replaced the old one**" if rep else "") + f"\n{I.s('link')} **Link:**\n```{u}```"
            for k, ext, size, u, rep in results)
        if len(results) == 1:
            e.set_image(url=results[0][3])
        e.set_footer(text=f"Stored in #{ch.name} · {PREFIX}linkget <name> gives a fresh link any time")
        await ctx.reply(embed=e, mention_author=False)

    @staticmethod
    def auto_name(c: dict) -> str:
        n = len(c["items"]) + 1
        while f"img-{n}" in c["items"]:
            n += 1
        return f"img-{n}"

    async def forget(self, it: Optional[dict]):
        """Delete the hosting message (best effort)."""
        if not it:
            return
        ch = self.bot.get_channel(it.get("c", 0))
        if ch is not None:
            try:
                await ch.get_partial_message(it["m"]).delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="links", aliases=["imglist", "linklist"], description="(Bot owner) List the saved image links")
    @app_commands.describe(page="Page number")
    @checks.true_owner_only()
    async def links(self, ctx: commands.Context, page: int = 1):
        c = self.cfg(ctx.guild.id)
        items = sorted(c["items"].items())
        hid = self.host_id()
        ch = f"<#{hid}>" if hid else f"*not set* - `{PREFIX}imgchannel #channel`"
        if not items:
            raise VCError(f"Nothing saved in this server yet. Image host channel: {ch.replace('*', '')}. Use `{PREFIX}tolink`.")
        view = LinksView(self, ctx, items, ch, page)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

    @commands.hybrid_command(name="linkget", aliases=["getlink"], description="Fresh link + preview of a saved image")
    @app_commands.describe(name="Saved image name")
    @checks.true_owner_only()
    async def linkget(self, ctx: commands.Context, name: str):
        url = await self.fresh_url(ctx.guild, name)
        if not url:
            raise VCError(f"No saved image called `{slug(name)}`. See `{PREFIX}links`.")
        e = discord.Embed(color=BRAND, title=f"{I.s('link')} {slug(name)}",
                          description=f"{I.s('link')} **Fresh link:**\n```{url}```")
        e.set_image(url=url)
        await ctx.reply(embed=e, mention_author=False)

    @commands.hybrid_command(name="linkdel", aliases=["dellink"], description="(Bot owner) Delete a saved image")
    @app_commands.describe(name="Saved image name")
    @checks.true_owner_only()
    async def linkdel(self, ctx: commands.Context, name: str):
        c = self.cfg(ctx.guild.id)
        it = c["items"].pop(slug(name), None)
        if it is None:
            raise VCError(f"No saved image called `{slug(name)}`.")
        self.save(ctx.guild.id, c)
        await self.forget(it)
        await ctx.reply(ok(f"**{slug(name)}** deleted."), mention_author=False)


async def setup(bot):
    await bot.add_cog(ImageHost(bot))
