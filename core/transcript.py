"""Ticket transcripts - a single self-contained HTML file that looks like Discord (dark theme).

    html = build_html(guild, channel, messages, info)

`info` is a list of (label, value) pairs shown in the header card (ticket #, category, opened by, ...).
No JavaScript, no external CSS - the file opens in any browser. Images / attachments link to Discord's CDN.
"""
import datetime as dt
import html as _html
import re
from typing import Iterable, Optional

from .config import TIMEZONE

CDN = "https://cdn.discordapp.com"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font-family:"gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:16px;line-height:1.375}
a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
.top{position:sticky;top:0;z-index:5;background:#313338;border-bottom:1px solid #26272b;padding:12px 16px;display:flex;align-items:center;gap:12px;box-shadow:0 1px 0 rgba(0,0,0,.2)}
.top img{width:32px;height:32px;border-radius:50%}
.top .gname{font-weight:600;color:#f2f3f5}
.top .chan{color:#949ba4}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px 40px}
.card{background:#2b2d31;border-radius:8px;border-left:4px solid #5865f2;margin:16px 0 8px;padding:14px 16px}
.card h2{margin:0 0 8px;font-size:18px;color:#f2f3f5}
.card .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px 18px}
.card .k{font-size:12px;font-weight:700;color:#b5bac1;text-transform:uppercase;letter-spacing:.02em}
.card .v{color:#dbdee1;word-break:break-word}
.day{display:flex;align-items:center;gap:8px;color:#949ba4;font-size:12px;font-weight:600;margin:18px 0 6px}
.day:before,.day:after{content:"";flex:1;height:1px;background:#3f4147}
.msg{display:flex;gap:16px;padding:2px 0 2px 0;position:relative;margin-top:17px}
.msg.cont{margin-top:0}
.msg:hover{background:#2e3035}
.av{flex:0 0 40px;width:40px;height:40px;border-radius:50%;margin-top:2px}
.gutter{flex:0 0 40px;font-size:10px;color:#949ba4;text-align:right;opacity:0;padding-top:5px}
.msg:hover .gutter{opacity:1}
.body{min-width:0;flex:1}
.head{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.name{font-weight:500;color:#f2f3f5}
.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:600;border-radius:3px;padding:0 4px;line-height:15px;text-transform:uppercase}
.tag.staff{background:#248046}
.ts{font-size:12px;color:#949ba4}
.edited{font-size:10px;color:#949ba4}
.content{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.reply{display:flex;align-items:center;gap:6px;font-size:14px;color:#b5bac1;margin:0 0 2px 56px;position:relative}
.reply:before{content:"";position:absolute;left:-36px;top:50%;width:30px;height:10px;border-left:2px solid #4e5058;border-top:2px solid #4e5058;border-top-left-radius:6px}
.reply img{width:16px;height:16px;border-radius:50%}
.reply .rn{font-weight:500}
.reply .rt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60ch;opacity:.85}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.mention:hover{background:#5865f2;color:#fff}
code{background:#1e1f22;border-radius:4px;padding:1px 4px;font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",monospace;font-size:.875em}
pre{background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:8px;margin:4px 0;overflow:auto;white-space:pre}
pre code{background:none;padding:0;font-size:.875em}
blockquote{margin:2px 0;padding:0 8px 0 12px;border-left:4px solid #4e5058}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px;padding:0 2px}
.spoiler:hover{background:#2b2d31;color:inherit}
h1.md,h2.md,h3.md{margin:8px 0 4px;color:#f2f3f5;white-space:normal}
h1.md{font-size:1.5em}h2.md{font-size:1.25em}h3.md{font-size:1.1em}
.sub{font-size:12px;color:#949ba4}
ul.md{margin:2px 0;padding-left:22px}
img.emoji{width:22px;height:22px;vertical-align:-5px}
img.emoji.big{width:48px;height:48px}
.att{margin-top:4px}
.att img,.att video{max-width:min(520px,100%);max-height:350px;border-radius:6px;display:block}
.file{display:flex;align-items:center;gap:10px;background:#2b2d31;border:1px solid #1e1f22;border-radius:6px;padding:10px 12px;max-width:430px;margin-top:4px}
.file .fn{font-weight:500}.file .fs{font-size:12px;color:#949ba4}
.embed{display:grid;max-width:520px;margin-top:4px;background:#2b2d31;border-left:4px solid #1e1f22;border-radius:4px;padding:8px 14px 12px 12px}
.embed .eauthor{font-size:14px;font-weight:600;color:#f2f3f5;margin-top:4px}
.embed .etitle{font-weight:600;color:#f2f3f5;margin-top:4px}
.embed .etitle a{color:#00a8fc}
.embed .edesc{font-size:14px;margin-top:4px;white-space:pre-wrap;overflow-wrap:anywhere}
.embed .fields{display:grid;grid-template-columns:repeat(12,1fr);gap:6px 10px;margin-top:8px}
.embed .fname{font-size:14px;font-weight:600;color:#f2f3f5}
.embed .fval{font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
.embed img.eimg{max-width:100%;max-height:300px;border-radius:4px;margin-top:10px}
.embed img.ethumb{width:70px;height:70px;object-fit:cover;border-radius:4px;justify-self:end;grid-row:1/8;grid-column:2}
.embed .efoot{font-size:12px;color:#b5bac1;margin-top:8px}
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.react{background:#2b2d31;border:1px solid transparent;border-radius:8px;padding:2px 8px;font-size:14px;display:flex;align-items:center;gap:5px}
.react img{width:16px;height:16px}
.btnrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.btn{background:#4e5058;color:#fff;border-radius:3px;padding:2px 14px;font-size:14px;font-weight:500;line-height:28px}
.btn.primary{background:#5865f2}.btn.success{background:#248046}.btn.danger{background:#da373c}
.sys{color:#949ba4;font-size:14px;padding-left:56px;margin-top:10px}
.sys b{color:#dbdee1}
.foot{text-align:center;color:#949ba4;font-size:12px;margin-top:30px;padding-top:12px;border-top:1px solid #3f4147}
"""


def esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def fmt_dt(d: Optional[dt.datetime], *, seconds=False) -> str:
    if d is None:
        return "-"
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(TIMEZONE).strftime("%d %b %Y, %I:%M" + (":%S" if seconds else "") + " %p")


def fmt_day(d: dt.datetime) -> str:
    return d.astimezone(TIMEZONE).strftime("%A, %d %B %Y")


def avatar_url(user) -> str:
    try:
        return str(user.display_avatar.replace(size=64, static_format="png").url)
    except Exception:
        return f"{CDN}/embed/avatars/0.png"


# ------------------------------------------------------------------ discord markdown -> html
_CODE_BLOCK = re.compile(r"```(?:([A-Za-z0-9_+\-]+)\n)?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_URL = re.compile(r"(https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]])")
_MASKED = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_USER = re.compile(r"&lt;@!?(\d+)&gt;")
_ROLE = re.compile(r"&lt;@&amp;(\d+)&gt;")
_CHAN = re.compile(r"&lt;#(\d+)&gt;")
_EMOJI = re.compile(r"&lt;(a?):(\w+):(\d+)&gt;")
_TIME = re.compile(r"&lt;t:(-?\d+)(?::([tTdDfFR]))?&gt;")


def _lookup(guild, kind: str, id_: int, message=None) -> str:
    if kind == "user":
        m = guild.get_member(id_) if guild else None
        if m is None and message is not None:
            m = next((u for u in getattr(message, "mentions", ()) if u.id == id_), None)
        return f"@{m.display_name}" if m else "@unknown-user"
    if kind == "role":
        r = guild.get_role(id_) if guild else None
        return f"@{r.name}" if r else "@deleted-role"
    ch = guild.get_channel(id_) if guild else None
    return f"#{ch.name}" if ch else "#deleted-channel"


def _time_tag(m: re.Match) -> str:
    try:
        d = dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc).astimezone(TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return m.group(0)
    f = m.group(2) or "f"
    txt = {"t": d.strftime("%I:%M %p"), "T": d.strftime("%I:%M:%S %p"), "d": d.strftime("%d/%m/%Y"),
           "D": d.strftime("%d %B %Y"), "f": d.strftime("%d %B %Y %I:%M %p"),
           "F": d.strftime("%A, %d %B %Y %I:%M %p"), "R": d.strftime("%d %b %Y, %I:%M %p")}[f]
    return f'<span class="mention">{txt}</span>'


def md(text: str, guild=None, message=None, *, allow_big_emoji=True) -> str:
    """Discord markdown -> safe html."""
    if not text:
        return ""
    stash: list = []

    def keep(h: str) -> str:
        stash.append(h)
        return f"\x00{len(stash) - 1}\x00"

    # 1) code is cut out first, nothing inside it is formatted
    text = _CODE_BLOCK.sub(lambda m: keep(f"<pre><code>{esc(m.group(2).strip(chr(10)))}</code></pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{esc(m.group(2))}</code>"), text)
    only_emoji = allow_big_emoji and re.fullmatch(r"\s*(<a?:\w+:\d+>\s*){1,27}", text) is not None

    text = _html.escape(text, quote=False)

    # 2) masked links + plain links
    text = _MASKED.sub(lambda m: keep(f'<a href="{esc(m.group(2))}" target="_blank" rel="noopener">{m.group(1)}</a>'), text)
    text = _URL.sub(lambda m: keep(f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'), text)

    # 3) mentions / emojis / timestamps
    text = _USER.sub(lambda m: f'<span class="mention">{esc(_lookup(guild, "user", int(m.group(1)), message))}</span>', text)
    text = _ROLE.sub(lambda m: f'<span class="mention">{esc(_lookup(guild, "role", int(m.group(1))))}</span>', text)
    text = _CHAN.sub(lambda m: f'<span class="mention">{esc(_lookup(guild, "chan", int(m.group(1))))}</span>', text)
    text = _EMOJI.sub(lambda m: (
        f'<img class="emoji{" big" if only_emoji else ""}" alt=":{m.group(2)}:" title=":{m.group(2)}:" '
        f'src="{CDN}/emojis/{m.group(3)}.{"gif" if m.group(1) else "png"}?size=64">'), text)
    text = _TIME.sub(_time_tag, text)
    text = text.replace("@everyone", '<span class="mention">@everyone</span>').replace(
        "@here", '<span class="mention">@here</span>')

    # 4) inline styles
    text = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', text, flags=re.S)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)

    # 5) line based: headings, subtext, quotes, lists
    out, lines, quote, ul = [], text.split("\n"), [], False

    def flush_quote():
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for ln in lines:
        mq = re.match(r"^&gt; ?(.*)$", ln)
        if mq:
            quote.append(mq.group(1))
            continue
        flush_quote()
        mh = re.match(r"^(#{1,3}) (.+)$", ln)
        ms = re.match(r"^-# (.+)$", ln)
        ml = re.match(r"^[-*] (.+)$", ln)
        if ml:
            if not ul:
                out.append('<ul class="md">')
                ul = True
            out.append(f"<li>{ml.group(1)}</li>")
            continue
        if ul:
            out.append("</ul>")
            ul = False
        if mh:
            n = len(mh.group(1))
            out.append(f'<h{n} class="md">{mh.group(2)}</h{n}>')
        elif ms:
            out.append(f'<div class="sub">{ms.group(1)}</div>')
        else:
            out.append(ln + "\n")
    flush_quote()
    if ul:
        out.append("</ul>")
    text = "".join(out).rstrip("\n")

    # 6) put the code back (blocks may sit inside placeholders more than once -> loop)
    for _ in range(3):
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


# ------------------------------------------------------------------ pieces
def _is_image(att) -> bool:
    ct = (getattr(att, "content_type", None) or "").lower()
    n = att.filename.lower()
    return ct.startswith("image/") or n.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _is_video(att) -> bool:
    ct = (getattr(att, "content_type", None) or "").lower()
    return ct.startswith("video/") or att.filename.lower().endswith((".mp4", ".webm", ".mov"))


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def render_attachments(m) -> str:
    out = []
    for a in getattr(m, "attachments", ()):
        spoil = ' style="filter:blur(18px)"' if a.is_spoiler() else ""
        if _is_image(a):
            out.append(f'<div class="att"><a href="{esc(a.url)}" target="_blank"><img src="{esc(a.url)}" alt="{esc(a.filename)}"{spoil}></a></div>')
        elif _is_video(a):
            out.append(f'<div class="att"><video controls preload="none" src="{esc(a.url)}"{spoil}></video></div>')
        else:
            out.append(f'<div class="file"><div>📄</div><div><a class="fn" href="{esc(a.url)}" target="_blank">{esc(a.filename)}</a>'
                       f'<div class="fs">{_size(a.size)}</div></div></div>')
    for s in getattr(m, "stickers", ()):
        url = getattr(s, "url", "")
        out.append(f'<div class="att"><img src="{esc(url)}" alt="{esc(s.name)}" style="width:160px;height:160px" title="{esc(s.name)}"></div>')
    return "".join(out)


def render_embed(e, guild, message) -> str:
    try:
        color = f"#{e.color.value:06x}" if e.color else "#1e1f22"
    except Exception:
        color = "#1e1f22"
    parts = []
    if e.author and e.author.name:
        parts.append(f'<div class="eauthor">{esc(e.author.name)}</div>')
    if e.title:
        t = esc(e.title)
        parts.append(f'<div class="etitle">{f"<a href={chr(34)}{esc(e.url)}{chr(34)}>{t}</a>" if e.url else t}</div>')
    if e.description:
        parts.append(f'<div class="edesc">{md(e.description, guild, message, allow_big_emoji=False)}</div>')
    if e.fields:
        fs = []
        for f in e.fields:
            span = 4 if f.inline else 12
            fs.append(f'<div style="grid-column:span {span}"><div class="fname">{esc(f.name)}</div>'
                      f'<div class="fval">{md(f.value, guild, message, allow_big_emoji=False)}</div></div>')
        parts.append('<div class="fields">' + "".join(fs) + "</div>")
    if e.image and e.image.url:
        parts.append(f'<img class="eimg" src="{esc(e.image.url)}">')
    if e.thumbnail and e.thumbnail.url:
        parts.append(f'<img class="ethumb" src="{esc(e.thumbnail.url)}">')
    foot = []
    if e.footer and e.footer.text:
        foot.append(esc(e.footer.text))
    if e.timestamp:
        foot.append(fmt_dt(e.timestamp))
    if foot:
        parts.append(f'<div class="efoot">{" • ".join(foot)}</div>')
    if not parts:
        return ""
    return f'<div class="embed" style="border-left-color:{color}">' + "".join(parts) + "</div>"


def render_reactions(m) -> str:
    rs = []
    for r in getattr(m, "reactions", ()):
        em = r.emoji
        if isinstance(em, str):
            icon = esc(em)
        elif getattr(em, "id", None):
            icon = f'<img src="{CDN}/emojis/{em.id}.{"gif" if getattr(em, "animated", False) else "png"}?size=32" alt="">'
        else:
            icon = esc(str(em))
        rs.append(f'<div class="react">{icon}<span>{r.count}</span></div>')
    return f'<div class="reactions">{"".join(rs)}</div>' if rs else ""


def _walk_components(items):
    """Yield every component of a message (works for action rows and Components V2 containers)."""
    for c in items or ():
        yield c
        for attr in ("children", "components"):
            kids = getattr(c, attr, None)
            if kids:
                yield from _walk_components(kids)


def render_components(m, guild) -> str:
    """V2 text and the buttons of a message, as plain as possible (so panels sent with V2 are still readable)."""
    texts, buttons = [], []
    for c in _walk_components(getattr(m, "components", None)):
        t = type(c).__name__
        if t == "TextDisplay":
            texts.append(f'<div class="content">{md(getattr(c, "content", ""), guild, m)}</div>')
        elif t == "Button":
            style = {"primary": "primary", "success": "success", "danger": "danger"}.get(
                getattr(getattr(c, "style", None), "name", ""), "")
            if getattr(c, "label", None) or getattr(c, "emoji", None):
                buttons.append(f'<div class="btn {style}">{esc(c.label or "")}</div>')
        elif t in ("SelectMenu", "Select"):
            buttons.append(f'<div class="btn">{esc(getattr(c, "placeholder", None) or "Select an option")} ▾</div>')
    return "".join(texts) + (f'<div class="btnrow">{"".join(buttons)}</div>' if buttons else "")


SYSTEM_TEXT = {
    "pins_add": "pinned a message to this channel.",
    "new_member": "joined the ticket.",
    "recipient_add": "added someone to the ticket.",
    "recipient_remove": "removed someone from the ticket.",
    "channel_name_change": "changed the channel name.",
    "thread_created": "started a thread.",
}


def _role_color(author) -> str:
    try:
        c = author.color
        return f"#{c.value:06x}" if c and c.value else "#f2f3f5"
    except Exception:
        return "#f2f3f5"


# ------------------------------------------------------------------ the whole page
def build_html(guild, channel, messages: Iterable, info: list, *, title: str = "Ticket transcript",
               is_staff=lambda member: False) -> str:
    msgs = list(messages)
    guild_icon = ""
    try:
        if guild.icon:
            guild_icon = f'<img src="{esc(guild.icon.replace(size=64, format="png").url)}" alt="">'
    except Exception:
        pass

    grid = "".join(f'<div><div class="k">{esc(k)}</div><div class="v">{v}</div></div>' for k, v in info)
    head = (f'<div class="top">{guild_icon}<div><span class="gname">{esc(guild.name)}</span> '
            f'<span class="chan">· #{esc(channel.name)}</span></div></div>')
    body = [f'<div class="card"><h2>{esc(title)}</h2><div class="grid">{grid}</div></div>']

    last_day, last_author, last_ts = None, None, None
    by_id = {m.id: m for m in msgs}
    for m in msgs:
        created = m.created_at
        local = created.astimezone(TIMEZONE)
        day = local.date()
        if day != last_day:
            body.append(f'<div class="day">{esc(fmt_day(created))}</div>')
            last_day, last_author, last_ts = day, None, None

        mt = getattr(getattr(m, "type", None), "name", "default")
        if mt not in ("default", "reply") and mt in SYSTEM_TEXT:
            body.append(f'<div class="sys"><b>{esc(m.author.display_name)}</b> {SYSTEM_TEXT[mt]} '
                        f'<span class="ts">{esc(local.strftime("%I:%M %p"))}</span></div>')
            last_author = None
            continue

        author = m.author
        cont = (last_author == author.id and last_ts is not None and (created - last_ts).total_seconds() < 420
                and mt != "reply")
        last_author, last_ts = author.id, created

        ref_html = ""
        ref = getattr(m, "reference", None)
        if ref is not None and getattr(ref, "message_id", None):
            target = by_id.get(ref.message_id) or getattr(ref, "resolved", None)
            if isinstance(target, type(m)) or hasattr(target, "author"):
                snippet = (target.content or ("Click to see attachment" if getattr(target, "attachments", None) else "Embed"))
                snippet = re.sub(r"\s+", " ", snippet)[:120]
                ref_html = (f'<div class="reply"><img src="{esc(avatar_url(target.author))}" alt="">'
                            f'<span class="rn" style="color:{_role_color(target.author)}">{esc(target.author.display_name)}</span>'
                            f'<span class="rt">{esc(snippet)}</span></div>')
            else:
                ref_html = '<div class="reply"><span class="rt">Original message was deleted</span></div>'

        content = md(m.content, guild, m) if m.content else ""
        extras = render_attachments(m) + "".join(render_embed(e, guild, m) for e in m.embeds) + render_components(m, guild)
        extras += render_reactions(m)
        edited = ' <span class="edited">(edited)</span>' if getattr(m, "edited_at", None) else ""
        content_html = f'<div class="content">{content}{edited}</div>' if content else ""

        if cont:
            body.append(f'<div class="msg cont"><div class="gutter">{esc(local.strftime("%I:%M"))}</div>'
                        f'<div class="body">{content_html}{extras}</div></div>')
            continue

        tags = ""
        if author.bot:
            tags += '<span class="tag">APP</span>'
        else:
            try:
                if is_staff(author):
                    tags += '<span class="tag staff">STAFF</span>'
            except Exception:
                pass
        body.append(
            f"{ref_html}"
            f'<div class="msg"><img class="av" src="{esc(avatar_url(author))}" alt="">'
            f'<div class="body"><div class="head"><span class="name" style="color:{_role_color(author)}">{esc(author.display_name)}</span>'
            f'{tags}<span class="ts">{esc(fmt_dt(created))}</span></div>{content_html}{extras}</div></div>')

    foot = (f'<div class="foot">{len(msgs)} message{"s" if len(msgs) != 1 else ""} · '
            f'generated {esc(fmt_dt(dt.datetime.now(dt.timezone.utc)))} (IST)</div>')
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} - #{esc(channel.name)}</title><style>{CSS}</style></head>'
            f'<body>{head}<div class="wrap">{"".join(body)}{foot}</div></body></html>')
