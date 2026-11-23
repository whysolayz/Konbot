"""$ae ($addemoji) and $as ($addsticker) - steal emojis / stickers from any message.

    reply to a message that has custom emojis  ->  $ae       (adds them to this server)
    reply to a message that has a sticker      ->  $as [new name]

    NEW - images work too:
    $ae name  + attach an image / GIF          ->  that image becomes an emoji
    $ae name  as a reply to a message with an image / GIF (attachment, embed or image link)  ->  same
    $as name  + attach an image / GIF, or reply to one  ->  that image becomes a sticker
    (needs Pillow - big images are shrunk to fit Discord's size limits automatically)

    $ei [search | here | <emoji id>]  -> list animated emojis with their IDs (all servers you share with Kon), or look up one ID

After adding, the message has three buttons: **Undo** (delete everything that command just added), **Remove**
(pick which of them to delete) and **Rename** (pick one, type a new name). Text versions:
    $ae undo  |  $ae remove <:emoji:> or name ...        $as undo  |  $as remove <sticker name>
    $ae rename <:emoji:> or name <new name>             $as rename <sticker name> <new name>
"""
import asyncio
import io
import logging
import re
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import emoji as emojis
from core.config import BLURPLE
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, replied_message

log = logging.getLogger("kon.expressions")

CUSTOM = re.compile(r"<(a?):(\w{2,32}):(\d{15,25})>")
MAX_EMOJIS = 20                  # per command
EMOJI_LIMIT = 256 * 1024         # Discord's size limit for emojis
STICKER_LIMIT = 512 * 1024       # ... and for stickers


async def download(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                return await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


def safe_name(name: str, lo: int, hi: int) -> str:
    name = re.sub(r"\W", "_", name)[:hi]
    return name if len(name) >= lo else (name + "_" * lo)[:lo]


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
URL_RE = re.compile(r"https?://[^\s<>]+")
MAX_IMAGES = 10
MAX_SRC = 12 * 1024 * 1024


def _frames_resized(img, size, square):
    from PIL import Image, ImageSequence
    out, durations = [], []
    for fr in ImageSequence.Iterator(img):
        f = fr.convert("RGBA")
        f.thumbnail((size, size), Image.LANCZOS)
        if square:
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(f, ((size - f.width) // 2, (size - f.height) // 2))
            f = canvas
        out.append(f)
        durations.append(fr.info.get("duration", 60))
    return out, durations


def fit_image(data: bytes, limit: int, sizes: tuple, square: bool) -> tuple[bytes, str]:
    """-> (bytes, extension) that fits Discord's limits. Shrinks / converts with Pillow when needed (png / gif only out)."""
    try:
        from PIL import Image
    except ImportError:
        raise VCError("Install Pillow (`pip install pillow`) so I can shrink / convert images.")
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
        img.load() if not getattr(img, "is_animated", False) else None
    except Exception:
        raise VCError("That file isn't an image I can read.")
    animated = bool(getattr(img, "is_animated", False)) and img.format in ("GIF", "WEBP", "PNG")
    fmt = img.format
    if fmt in ("PNG", "GIF") and len(data) <= limit and (not square or img.size == (sizes[0], sizes[0])) and \
            max(img.size) <= max(sizes[0], 128 if not square else sizes[0]):
        return data, "gif" if fmt == "GIF" else "png"
    for size in sizes:
        buf = _io.BytesIO()
        if animated:
            frames, dur = _frames_resized(img, size, square)
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=dur, loop=0,
                           disposal=2, optimize=True, transparency=0)
            ext = "gif"
        else:
            f = img.convert("RGBA")
            from PIL import Image as _I
            f.thumbnail((size, size), _I.LANCZOS)
            if square:
                canvas = _I.new("RGBA", (size, size), (0, 0, 0, 0))
                canvas.paste(f, ((size - f.width) // 2, (size - f.height) // 2))
                f = canvas
            f.save(buf, format="PNG", optimize=True)
            ext = "png"
        if buf.tell() <= limit:
            return buf.getvalue(), ext
    raise VCError(f"Even shrunk it is bigger than {limit // 1024} KB. Try a smaller / shorter image.")


async def gather_images(ctx: commands.Context, msg) -> list[tuple[str, bytes]]:
    """Images from: the command message, then the replied message (attachments, embed images, image links)."""
    sources: list[tuple[str, object]] = []
    for m in (ctx.message, msg):
        if m is None:
            continue
        for a in m.attachments:
            if (a.content_type or "").startswith("image/") or a.filename.lower().endswith(IMG_EXT):
                sources.append((a.filename, a))
        if m is msg:
            for e in m.embeds:
                for u in (e.image.url, e.thumbnail.url if e.type in ("image", "gifv") else None):
                    if u:
                        sources.append((Path(u.split("?")[0]).name or "image", u))
            for u in URL_RE.findall(m.content or ""):
                if u.split("?")[0].lower().endswith(IMG_EXT):
                    sources.append((Path(u.split("?")[0]).name, u))
    out, seen = [], set()
    for name, src in sources:
        key = src.id if isinstance(src, discord.Attachment) else src
        if key in seen:
            continue
        seen.add(key)
        try:
            if isinstance(src, discord.Attachment):
                if src.size > MAX_SRC:
                    continue
                data = await src.read()
            else:
                data = await download(src)
        except discord.HTTPException:
            data = None
        if data and len(data) <= MAX_SRC:
            out.append((name, data))
        if len(out) >= MAX_IMAGES:
            break
    return out


async def delete_items(items, reason: str) -> int:
    n = 0
    for it in items:
        try:
            await it.delete(reason=reason)
            n += 1
        except discord.NotFound:
            pass
        except discord.HTTPException:
            continue
        await asyncio.sleep(0.3)
    return n


class UndoView(OwnedView):
    """Undo / Remove buttons under the "Added ..." message."""

    def __init__(self, cog: "Expressions", ctx: commands.Context, kind: str, items: list, text: str):
        super().__init__(ctx.author.id, timeout=180)
        self.cog, self.ctx, self.kind, self.items, self.text = cog, ctx, kind, list(items), text
        self.undo_btn.emoji = I.e("refresh")
        self.remove_btn.emoji = I.e("minus")
        self.rename_btn.emoji = I.e("edit")

    def alive(self):
        g = self.ctx.guild
        pool = {e.id for e in (g.emojis if self.kind == "emoji" else g.stickers)}
        self.items = [i for i in self.items if i.id in pool]
        return self.items

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.danger)
    async def undo_btn(self, interaction: discord.Interaction, _):
        items = self.alive()
        self.stop()
        await interaction.response.edit_message(content=f"{I.s('hourglass')} Removing...", view=None)
        n = await delete_items(items, f"undo by {interaction.user} ({interaction.user.id})")
        self.cog.last.pop((self.ctx.guild.id, self.kind), None)
        await interaction.edit_original_response(content=ok(f"Undone. Removed **{n}** {self.kind}{'s' if n != 1 else ''}."))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.secondary)
    async def remove_btn(self, interaction: discord.Interaction, _):
        items = self.alive()[:25]
        if not items:
            return await interaction.response.send_message(err("Nothing left to remove."), ephemeral=True)
        opts = [discord.SelectOption(label=i.name[:100], value=str(i.id),
                                     emoji=i if self.kind == "emoji" else None) for i in items]
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder=f"Select {self.kind}s to remove...", options=opts, min_values=1, max_values=len(opts))

        async def picked(i: discord.Interaction):
            ids = {int(v) for v in sel.values}
            chosen = [x for x in items if x.id in ids]
            await i.response.edit_message(content=f"{I.s('hourglass')} Removing...", view=None)
            n = await delete_items(chosen, f"remove by {i.user} ({i.user.id})")
            self.items = [x for x in self.items if x.id not in ids]
            await i.edit_original_response(content=ok(f"Removed **{n}** {self.kind}{'s' if n != 1 else ''}."))
            if not self.alive() and self.message:
                self.stop()
                try:
                    await self.message.edit(view=None)
                except discord.HTTPException:
                    pass
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message(f"Remove which {self.kind}s?", view=view, ephemeral=True)

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.primary)
    async def rename_btn(self, interaction: discord.Interaction, _):
        items = self.alive()[:25]
        if not items:
            return await interaction.response.send_message(err("Nothing left to rename."), ephemeral=True)
        if len(items) == 1:
            return await interaction.response.send_modal(self.rename_modal(items[0]))
        opts = [discord.SelectOption(label=i.name[:100], value=str(i.id),
                                     emoji=i if self.kind == "emoji" else None) for i in items]
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder=f"Select the {self.kind} to rename...", options=opts)

        async def picked(i: discord.Interaction):
            chosen = next((x for x in items if x.id == int(sel.values[0])), None)
            if chosen is None:
                return await i.response.send_message(err("That one is gone."), ephemeral=True)
            await i.response.send_modal(self.rename_modal(chosen))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message(f"Rename which {self.kind}?", view=view, ephemeral=True)

    def rename_modal(self, item) -> FieldsModal:
        hi = 32 if self.kind == "emoji" else 30

        async def done(i: discord.Interaction, v):
            old = item.name
            new = await self.cog.rename_item(self.ctx.guild, self.kind, item, v[0], f"rename by {i.user} ({i.user.id})")
            await i.response.send_message(ok(f"Renamed **{old}** to **{new.name}**."), ephemeral=True)
            self.items = [new if x.id == new.id else x for x in self.items]
            self.text = self.text.replace(f":{old}:", f":{new.name}:").replace(f"**{old}**", f"**{new.name}**")
            if self.message:
                try:
                    await self.message.edit(content=self.text)
                except discord.HTTPException:
                    pass
        return FieldsModal(f"Rename {self.kind}"[:45], [("New name", item.name, hi, True)], done)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


EI_PER_PAGE = 10


class EmojiPages(OwnedView):
    """Previous / Next over the pages of $ei."""

    def __init__(self, invoker_id: int, pages: list):
        super().__init__(invoker_id, timeout=180)
        self.pages, self.i = pages, 0
        self.prev.emoji, self.nxt.emoji = I.e("left"), I.e("right")
        self.sync()

    def sync(self):
        self.prev.disabled = self.i == 0
        self.nxt.disabled = self.i >= len(self.pages) - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _):
        self.i -= 1
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.i], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def nxt(self, interaction: discord.Interaction, _):
        self.i += 1
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.i], view=self)


class Expressions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last: dict[tuple, list] = {}        # (guild id, "emoji" / "sticker") -> what the last command added

    async def text_remove(self, ctx: commands.Context, kind: str, words: str, undo: bool):
        """$ae undo / $ae remove ... / $as undo / $as remove ..."""
        pool = ctx.guild.emojis if kind == "emoji" else ctx.guild.stickers
        reason = f"${'ae' if kind == 'emoji' else 'as'} {'undo' if undo else 'remove'} by {ctx.author} ({ctx.author.id})"
        if undo:
            alive = {e.id for e in pool}
            targets = [i for i in self.last.get((ctx.guild.id, kind), []) if i.id in alive]
            if not targets:
                raise VCError(f"Nothing to undo. I only remember {kind}s added since I started.")
        else:
            ids = {int(i) for _, _, i in CUSTOM.findall(words)}
            names = {w.lower().strip(":") for w in words.split() if not CUSTOM.fullmatch(w)}
            targets = [e for e in pool if e.id in ids or e.name.lower() in names]
            if not targets:
                raise VCError(f"That {kind} isn't in this server.\nUse `${'ae' if kind == 'emoji' else 'as'} remove "
                              + ("<emoji or name>`" if kind == "emoji" else "<name>`"))
        async with ctx.typing():
            n = await delete_items(targets, reason)
        self.last.pop((ctx.guild.id, kind), None) if undo else None
        await ctx.reply(ok(f"{'Undone. ' if undo else ''}Removed **{n}** {kind}{'s' if n != 1 else ''}."), mention_author=False)

    async def rename_item(self, guild: discord.Guild, kind: str, item, new_name: str, reason: str):
        """Rename one emoji / sticker of this server. Returns the updated object."""
        name = safe_name(new_name.strip().strip(":"), 2, 32 if kind == "emoji" else 30)
        try:
            new = await item.edit(name=name, reason=reason)
        except discord.Forbidden:
            raise VCError("I need **Manage Expressions** to rename that.")
        except discord.HTTPException as e:
            raise VCError(f"Discord said no: {e.text or 'rename failed'}")
        for key in ((guild.id, kind),):                                # keep the "last added" list ($ae undo) pointing at the new object
            self.last[key] = [new if x.id == item.id else x for x in self.last.get(key, [])]
        return new if new is not None else item

    async def text_rename(self, ctx: commands.Context, kind: str, words: str):
        """$ae rename <:emoji:> or name <new name>   |   $as rename <sticker name> <new name>"""
        pool = ctx.guild.emojis if kind == "emoji" else ctx.guild.stickers
        cmd = "ae" if kind == "emoji" else "as"
        parts = words.split()
        usage = f"Use `${cmd} rename " + ("<emoji or name> <new name>`" if kind == "emoji" else "<sticker name> <new name>`")
        if len(parts) < 2:
            raise VCError(usage)
        m = CUSTOM.fullmatch(parts[0])
        target = next((e for e in pool if (e.id == int(m.group(3)) if m else e.name.lower() == parts[0].lower().strip(":"))), None)
        if target is None:
            raise VCError(f"That {kind} isn't in this server.\n{usage}")
        old = target.name
        new = await self.rename_item(ctx.guild, kind, target, parts[1], f"${cmd} rename by {ctx.author} ({ctx.author.id})")
        await ctx.reply(ok(f"Renamed **{old}** to **{new.name}**" + (f" {new}" if kind == "emoji" else "") + "."), mention_author=False)

    @staticmethod
    def check_perms(ctx: commands.Context):
        me = ctx.guild.me.guild_permissions
        if not (me.manage_expressions or me.manage_emojis_and_stickers):
            raise VCError("I need **Manage Expressions**.")

    # ---------- $ae ----------
    @commands.command(name="addemoji", aliases=["ae", "addemoj"])
    @commands.has_permissions(manage_expressions=True)
    @commands.guild_only()
    async def addemoji(self, ctx: commands.Context, *, text: str = ""):
        """Reply to a message with custom emojis and type `$ae` to add them to this server."""
        self.check_perms(ctx)
        head, _, rest = text.strip().partition(" ")
        if head.lower() in ("undo", "remove", "delete", "del"):
            return await self.text_remove(ctx, "emoji", rest, undo=head.lower() == "undo")
        if head.lower() in ("rename", "rn"):
            return await self.text_rename(ctx, "emoji", rest)
        found: dict[int, tuple[str, bool]] = {}              # id -> (name, animated)
        sources = [text]
        msg = await replied_message(ctx)
        if msg is not None:
            sources.append(msg.content)
            for e in msg.embeds:                              # emojis inside embeds too
                sources += [e.title or "", e.description or ""]
            for r in msg.reactions:                           # ... and the reactions on it
                if isinstance(r.emoji, (discord.PartialEmoji, discord.Emoji)) and r.emoji.id:
                    found.setdefault(r.emoji.id, (r.emoji.name, r.emoji.animated))
        for src in sources:
            for anim, name, eid in CUSTOM.findall(src or ""):
                found.setdefault(int(eid), (name, bool(anim)))
        images = await gather_images(ctx, msg)
        if not found and not images:
            raise VCError("No emoji or image found. Reply to a message with emojis / an image, or attach an image: `$ae name`.")
        todo = list(found.items())[:MAX_EMOJIS]

        added, failed, added_obj = [], [], []
        async with ctx.typing():
            for eid, (name, animated) in todo:
                ext = "gif" if animated else "png"
                data = None
                for size in (128, 96, 64):                    # shrink until it fits Discord's 256 KB limit
                    data = await download(f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size={size}&quality=lossless")
                    if data is None or len(data) <= EMOJI_LIMIT:
                        break
                if data is None:
                    failed.append(f"`{name}` - couldn't download")
                    continue
                if len(data) > EMOJI_LIMIT:
                    failed.append(f"`{name}` - file too big")
                    continue
                try:
                    emoji = await ctx.guild.create_custom_emoji(
                        name=safe_name(name, 2, 32), image=data, reason=f"$ae by {ctx.author} ({ctx.author.id})")
                    added.append(str(emoji))
                    added_obj.append(emoji)
                except discord.HTTPException as e:
                    failed.append(f"`{name}` - {e.text or 'failed'}")
                    if e.code == 30008:                       # no free emoji slots left
                        failed.append("This server has no free emoji slots left.")
                        break
                await asyncio.sleep(0.4)

        # ---- images (attached to the command or in the message you replied to) ----
        words = [w for w in text.split() if not CUSTOM.fullmatch(w)]
        async with ctx.typing():
            for n, (fname, raw) in enumerate(images):
                base = words[0] if words else Path(fname).stem
                label = base if n == 0 else f"{base}_{n + 1}"
                try:
                    data, _ext = fit_image(raw, EMOJI_LIMIT, (128, 96, 64), square=False)
                    emoji = await ctx.guild.create_custom_emoji(
                        name=safe_name(label, 2, 32), image=data, reason=f"$ae image by {ctx.author} ({ctx.author.id})")
                    added.append(str(emoji))
                    added_obj.append(emoji)
                except VCError as e:
                    failed.append(f"`{label}` - {e}")
                except discord.HTTPException as e:
                    failed.append(f"`{label}` - {e.text or 'failed'}")
                    if e.code == 30008:
                        failed.append("This server has no free emoji slots left.")
                        break
                await asyncio.sleep(0.4)

        lines = []
        if added:
            lines.append(ok(f"Added **{len(added)}** emoji{'s' if len(added) != 1 else ''}: {' '.join(added)}"))
        if failed:
            lines.append(err("Couldn't add:\n" + "\n".join(f"· {f}" for f in failed)))
        text_out = "\n".join(lines)
        if added_obj:
            self.last[(ctx.guild.id, "emoji")] = added_obj
            view = UndoView(self, ctx, "emoji", added_obj, text_out)
            view.message = await ctx.reply(text_out, mention_author=False, view=view)
        else:
            await ctx.reply(text_out, mention_author=False)

    # ---------- $as ----------
    @commands.command(name="addsticker", aliases=["as"])
    @commands.has_permissions(manage_expressions=True)
    @commands.guild_only()
    async def addsticker(self, ctx: commands.Context, *, name: str = ""):
        """Reply to a message with a sticker and type `$as [new name]` to add it to this server."""
        self.check_perms(ctx)
        head, _, rest = name.strip().partition(" ")
        if head.lower() in ("undo", "remove", "delete", "del"):
            return await self.text_remove(ctx, "sticker", rest, undo=head.lower() == "undo")
        if head.lower() in ("rename", "rn"):
            return await self.text_rename(ctx, "sticker", rest)
        msg = await replied_message(ctx)
        stickers = list(msg.stickers) if msg is not None else []
        stickers = stickers or list(ctx.message.stickers)
        images = [] if stickers else await gather_images(ctx, msg)
        if not stickers and not images:
            raise VCError("No sticker or image found. Reply to a sticker / image, or attach an image: `$as name`.")

        added, failed = [], []
        async with ctx.typing():
            for st in stickers[:3]:
                label = st.name
                if st.format is discord.StickerFormatType.lottie:
                    failed.append(f"`{label}` - Lottie stickers can't be added (Discord only allows this for partnered servers)")
                    continue
                data = await download(st.url)
                if data is None:
                    failed.append(f"`{label}` - couldn't download")
                    continue
                if len(data) > STICKER_LIMIT:
                    failed.append(f"`{label}` - file is bigger than 512 KB")
                    continue
                try:
                    full = await st.fetch()
                except discord.HTTPException:
                    full = None
                desc = (getattr(full, "description", None) or label)[:100]
                tag = getattr(full, "emoji", None) or "slight_smile"
                new_name = (name.strip() if name.strip() and len(stickers) == 1 else label)
                ext = "gif" if st.format is discord.StickerFormatType.gif else "png"
                try:
                    sticker = await ctx.guild.create_sticker(
                        name=safe_name(new_name, 2, 30), description=desc, emoji=str(tag),
                        file=discord.File(io.BytesIO(data), filename=f"sticker.{ext}"),
                        reason=f"$as by {ctx.author} ({ctx.author.id})")
                    added.append(sticker)
                except discord.HTTPException as e:
                    failed.append(f"`{label}` - {e.text or 'failed'}")
                    if e.code == 30039:                       # no free sticker slots
                        break

        if images:
            async with ctx.typing():
                for n, (fname, raw) in enumerate(images[:3]):
                    base = name.strip() or Path(fname).stem
                    label = base if n == 0 else f"{base}_{n + 1}"
                    try:
                        data, ext = fit_image(raw, STICKER_LIMIT, (320, 256, 192), square=True)
                        sticker = await ctx.guild.create_sticker(
                            name=safe_name(label, 2, 30), description=label[:100], emoji="slight_smile",
                            file=discord.File(io.BytesIO(data), filename=f"sticker.{ext}"),
                            reason=f"$as image by {ctx.author} ({ctx.author.id})")
                        added.append(sticker)
                    except VCError as e:
                        failed.append(f"`{label}` - {e}")
                    except discord.HTTPException as e:
                        failed.append(f"`{label}` - {e.text or 'failed'}")
                        if e.code == 30039:
                            break

        lines = []
        if added:
            lines.append(ok("Added sticker " + ", ".join(f"**{s.name}**" for s in added) + "."))
        if failed:
            lines.append(err("Couldn't add:\n" + "\n".join(f"· {f}" for f in failed)))
        text_out = "\n".join(lines)
        if added:
            self.last[(ctx.guild.id, "sticker")] = list(added)
            view = UndoView(self, ctx, "sticker", added, text_out)
            view.message = await ctx.reply(text_out, mention_author=False, view=view)
        else:
            await ctx.reply(text_out, mention_author=False)

    # ---------- $ei ----------
    @commands.hybrid_command(name="ei", aliases=["emojiids", "animated"],
                             description="List animated emojis with their IDs (or look up one emoji ID)")
    @app_commands.describe(search="Part of a name, `here` (this server only) or an emoji ID")
    @commands.guild_only()
    async def ei(self, ctx: commands.Context, *, search: str = ""):
        """$ei [search | here | <emoji id>] - animated emojis with their IDs."""
        search = search.strip()
        if search.isdigit():                                       # $ei <id> -> what is it?
            hit = await emojis.find_emoji(self.bot, int(search))
            if hit is None:
                raise VCError("I can't find an emoji with that ID.")
            name, animated = hit
            e = discord.Embed(color=BLURPLE, title=f"{I.s('sparkles')} {name}",
                              description=f"{emojis.code(name, animated, int(search))}\n"
                                          f"**{'Animated' if animated else 'Static'}** · ID `{search}`")
            return await ctx.reply(embed=e, mention_author=False)

        here_only = search.lower() == "here"
        needle = "" if here_only else search.lower()
        groups = []
        guilds = [ctx.guild] if here_only else [ctx.guild, *[g for g in self.bot.guilds
                                                          if g.id != ctx.guild.id and g.get_member(ctx.author.id)]]
        for g in guilds:
            ems = sorted((x for x in g.emojis if x.animated and needle in x.name.lower()), key=lambda x: x.name.lower())
            if ems:
                groups.append((g.name, ems))
        if not here_only:
            own = sorted((x for x in await emojis.app_emojis(self.bot) if x.animated and needle in x.name.lower()),
                         key=lambda x: x.name.lower())
            if own:
                groups.append(("Kon's own emojis", own))
        total = sum(len(ems) for _, ems in groups)
        if not total:
            raise VCError("No animated emojis found." + ("" if here_only else " (Only servers you and Kon share are listed.)"))

        pages_txt, cur, n = [], [], 0
        for title, ems in groups:
            new_group = True
            for em in ems:
                if new_group:
                    cur.append(f"**{title}**")
                    new_group = False
                cur.append(f"{em} `{em.id}` · {em.name}")
                n += 1
                if n == EI_PER_PAGE:
                    pages_txt.append("\n".join(cur))
                    cur, n, new_group = [], 0, True
        if cur:
            pages_txt.append("\n".join(cur))
        pages = []
        for i, body in enumerate(pages_txt, 1):
            e = discord.Embed(color=BLURPLE, title=f"{I.s('sparkles')} Animated emojis ({total})", description=body)
            e.set_footer(text=f"Page {i}/{len(pages_txt)}")
            pages.append(e)
        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)
        view = EmojiPages(ctx.author.id, pages)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Expressions(bot))
