"""Guess the Anime - a multiplayer quiz (same style as BlackTea / Handfight).

A random anime CHARACTER IMAGE is shown every round - tap the anime the character comes from.
Easy = main characters of famous shows · Medium (default) = side characters of popular shows · Hard = deep cuts.
Images come from AniList (cached in data/anime_chars.json + prefetched during the previous round, so rounds start at once).
If AniList is unreachable the round falls back to the old text clue, so a game never breaks.

$guessanime [easy|medium|hard]   open a lobby (alias $ga) - works only in the Guess the Anime channel
$guessanime set                  (admin) pick the channel from a menu
$guessanime stats [@member]      wins, points and best game
$guessanime top                  best players of this server
$guessanime stop                 stop the running game (host or admin)

2-10 players · 10 rounds · 15 seconds per question · 4 answer buttons.
Points: a right answer gives 100 / 150 / 200 (Easy / Medium / Hard) + up to 100 for speed. After every round the scoreboard is shown.
Questions live in assets/guess_anime.json (easy / medium / hard) - add your own lines there, no code needed.
"""
import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
from core.config import BASE, DATA_DIR
from core.icons import I, err
from core.party import (LEVELS, ROUND_SECONDS, Party, PartyCog, speed_points)

log = logging.getLogger("kon.guessanime")
QUESTIONS_FILE = BASE / "assets" / "guess_anime.json"
CHAR_CACHE_FILE = DATA_DIR / "anime_chars.json"
ANILIST = "https://graphql.anilist.co"
Q_SEARCH = "query($s:String){Page(perPage:8){media(search:$s,type:ANIME,sort:POPULARITY_DESC){id title{romaji english}}}}"
Q_CHARS = ("query($id:Int){Media(id:$id){characters(sort:FAVOURITES_DESC,perPage:30)"
           "{nodes{name{full} image{large}}}}}")
# which characters (rank by popularity inside the show) each mode shows: easy = the faces of the show, hard = deep cuts
TIERS = {"easy": (0, 4), "medium": (3, 12), "hard": (10, 30)}
LETTERS = "ABCD"
GOLDEN = 0xFFB832
GOOD, BAD = 0x57F287, 0xED4245


def load_questions() -> dict:
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: [x for x in data.get(k, []) if x.get("q") and x.get("a")] for k in LEVELS}
    except (OSError, ValueError):
        return {k: [] for k in LEVELS}


class AnswerView(discord.ui.View):
    def __init__(self, game: "AnimeGame", options: list):
        super().__init__(timeout=None)
        self.game = game
        for i, name in enumerate(options):
            b = discord.ui.Button(label=name[:80], style=discord.ButtonStyle.primary, row=i // 2)
            b.callback = (lambda inter, n=name: self.pick(inter, n))
            self.add_item(b)

    async def pick(self, interaction: discord.Interaction, name: str):
        g, uid = self.game, interaction.user.id
        if uid not in g.players:
            if await ratelimit.guard_interaction(interaction, "button"):
                await interaction.response.send_message(f"{I.s('show')} You're **spectating** - join the next game!", ephemeral=True)
            return
        if g.open is False:
            return await interaction.response.defer()
        if uid in g.answers:
            return await interaction.response.send_message(err(f"You already locked **{g.answers[uid][0]}**."), ephemeral=True)
        g.answers[uid] = (name, time.monotonic() - g.round_start)
        await interaction.response.send_message(f"{I.s('check')} Locked in **{name}**.", ephemeral=True)
        if len(g.answers) >= len(g.players):
            g.all_in.set()
        else:
            asyncio.create_task(g.refresh())


class AnimeGame(Party):
    key = "guessanime"
    title = "Guess the Anime"
    icon = "star_fill"
    color = GOLDEN
    round_options = (10,)

    def __init__(self, cog, channel, host, difficulty):
        super().__init__(cog, channel, host, difficulty)
        self.message: Optional[discord.Message] = None
        self.question: Optional[dict] = None
        self.options: list = []
        self.answers: dict = {}
        self.open = False
        self.round_no = 0
        self.round_start = 0.0
        self.deadline = 0
        self.all_in = asyncio.Event()

    def how_to(self) -> str:
        return (f"A **character picture** appears - **tap the anime** the character is from before the timer runs out.\n\n"
                f"**How to play**\n"
                f"• **{self.rounds} rounds**, **{ROUND_SECONDS}s** each, one answer per picture.\n"
                f"• Right answer = base points + **speed bonus** (up to +100).\n"
                f"• The scoreboard is shown after every round {I.s('trophy')}")

    def level_info(self, level: str) -> str:
        who = {"easy": "main characters of famous shows", "medium": "side characters of popular shows", "hard": "deep cuts, rare faces"}[level]
        return f"{LEVELS[level]['base']} pts per answer · {who}"

    # ----- one round -----
    def round_embed(self) -> discord.Embed:
        q = self.question
        ask = "### Which anime is this character from?" if q.get("img") else f"### {q['q']}"
        e = discord.Embed(color=GOLDEN, title=f"{I.s(self.icon)} Guess the Anime · Round {self.round_no}/{self.rounds}",
                          description=f"{ask}\n\n{I.s('hourglass')} ends <t:{self.deadline}:R>")
        if q.get("img"):
            e.set_image(url=q["img"])
        e.add_field(name="Answered", value=f"**{len(self.answers)}/{len(self.players)}**", inline=True)
        e.add_field(name="Worth", value=f"**{self.level['base']}** + speed", inline=True)
        e.set_footer(text=f"{self.level['label']} · tap one answer below")
        return e

    async def refresh(self):
        if self.message and self.open:
            try:
                await self.message.edit(embed=self.round_embed())
            except discord.HTTPException:
                pass

    def pick_question(self, used: set) -> dict:
        pool = self.cog.questions.get(self.difficulty) or []
        fresh = [q for q in pool if q["a"] not in used]
        if len(fresh) < 1:                                           # pool too small: allow repeats from other levels
            fresh = [q for lv in LEVELS for q in self.cog.questions.get(lv, []) if q["a"] not in used] or pool
        return random.choice(fresh)

    def make_options(self, q: dict) -> list:
        same = [x["a"] for x in self.cog.questions.get(self.difficulty, []) if x["a"] != q["a"]]
        other = [x["a"] for lv in LEVELS if lv != self.difficulty for x in self.cog.questions.get(lv, []) if x["a"] != q["a"]]
        random.shuffle(same), random.shuffle(other)
        wrong = (same + other)[:3]
        opts = wrong + [q["a"]]
        random.shuffle(opts)
        return opts

    async def prepare(self, used: set) -> dict:
        """Pick the next anime + one of its characters (runs in the background during the previous round)."""
        q = dict(self.pick_question(used))
        char = await self.cog.character(q["a"], self.difficulty)
        q["img"], q["who"] = (char["i"], char["n"]) if char else (None, None)    # no image -> the old text clue
        return q

    async def play(self):
        used: set = set()
        nxt: Optional[asyncio.Task] = None
        try:
            cur = await self.prepare(used)
            for n in range(1, self.rounds + 1):
                self.round_no = n
                self.question = cur
                used.add(cur["a"])
                nxt = asyncio.create_task(self.prepare(used)) if n < self.rounds else None   # next picture loads while this round runs
                self.options = self.make_options(cur)
                self.answers, self.open = {}, True
                self.all_in = asyncio.Event()
                self.deadline = int(time.time()) + ROUND_SECONDS
                view = AnswerView(self, self.options)
                self.message = await self.channel.send(content=" ".join(f"<@{u}>" for u in self.players) if n == 1 else None,
                                                       embed=self.round_embed(), view=view)
                self.round_start = time.monotonic()
                await self.wait_any(self.all_in, ROUND_SECONDS)
                self.open = False
                view.stop()
                gained = {}
                right = cur["a"]
                lines = []
                for uid in self.players:
                    pick = self.answers.get(uid)
                    if pick is None:
                        lines.append(f"{I.s('hourglass')} <@{uid}> · no answer")
                    elif pick[0] == right:
                        pts = speed_points(self.level["base"], pick[1], ROUND_SECONDS)
                        gained[uid] = pts
                        self.scores[uid] = self.scores.get(uid, 0) + pts
                        self.correct[uid] = self.correct.get(uid, 0) + 1
                        lines.append(f"{I.s('check')} <@{uid}> · **+{pts}** · {pick[1]:.1f}s")
                    else:
                        lines.append(f"{I.s('cross')} <@{uid}> · {pick[0]}")
                who = f"{I.s('user_star')} **{cur['who']}**\n\n" if cur.get("who") else f"> {cur['q']}\n\n"
                e = discord.Embed(color=GOOD if gained else BAD,
                                  title=f"{I.s(self.icon)} Round {n}/{self.rounds} · {right}",
                                  description=who + "\n".join(lines))
                if cur.get("img"):
                    e.set_thumbnail(url=cur["img"])
                e.add_field(name=f"{I.s('trophy')} Scoreboard", value=self.board(gained), inline=False)
                e.set_footer(text="Next round coming up..." if n < self.rounds else "That was the last round!")
                try:
                    await self.message.edit(embed=e, view=None, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
                if n < self.rounds:
                    await self.pause(4)
                    cur = await nxt
        finally:
            if nxt and not nxt.done():
                nxt.cancel()


class GuessAnime(PartyCog, commands.Cog):
    """Guess the Anime."""
    game_cls = AnimeGame
    label = "Guess the Anime"
    icon = "star_fill"
    command = "guessanime"

    def __init__(self, bot):
        self.bot = bot
        self._init_party(bot)
        self.questions = load_questions()
        if not any(self.questions.values()):
            log.warning("assets/guess_anime.json is missing or empty - Guess the Anime has no questions")
        self.chars: dict = self._load_chars()          # anime title -> [{"n": name, "i": image url}, ...] best known first
        self._session: Optional[aiohttp.ClientSession] = None
        self._gate = asyncio.Semaphore(2)              # at most 2 AniList requests at once
        self._pause_until = 0.0                        # AniList said "slow down" -> no requests until then
        self._warm_task = asyncio.create_task(self._warm())

    # ----- character images (AniList) -----
    @staticmethod
    def _load_chars() -> dict:
        try:
            with open(CHAR_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_chars(self):
        try:
            tmp = CHAR_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.chars, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CHAR_CACHE_FILE)
        except OSError as e:
            log.warning("could not save the character cache: %r", e)

    async def _post(self, query: str, variables: dict) -> Optional[dict]:
        if time.monotonic() < self._pause_until:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": "KonBot/1.0"})
        async with self._gate:
            try:
                async with self._session.post(ANILIST, json={"query": query, "variables": variables}) as r:
                    if r.status == 429:
                        self._pause_until = time.monotonic() + min(int(r.headers.get("Retry-After", "30") or 30), 90)
                        return None
                    if r.status != 200:
                        return None
                    return (await r.json()).get("data")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return None

    @staticmethod
    def _norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (t or "").lower().replace("é", "e"))

    async def _fetch(self, title: str) -> Optional[list]:
        data = await self._post(Q_SEARCH, {"s": title})
        media = ((data or {}).get("Page") or {}).get("media") or []
        if not media:
            return None
        want = self._norm(title)
        hit = next((m for m in media if want in (self._norm(m["title"].get("romaji")), self._norm(m["title"].get("english")))), media[0])
        data = await self._post(Q_CHARS, {"id": hit["id"]})
        nodes = (((data or {}).get("Media") or {}).get("characters") or {}).get("nodes") or []
        out = [{"n": n["name"]["full"], "i": n["image"]["large"]} for n in nodes
               if n.get("name", {}).get("full") and (n.get("image") or {}).get("large") and "default" not in n["image"]["large"]]
        return out or None

    async def character(self, title: str, level: str) -> Optional[dict]:
        """A character picture for `title` that fits the mode - None if AniList can't help (the round then uses a text clue)."""
        chars = self.chars.get(title)
        if chars is None:
            try:
                chars = await asyncio.wait_for(self._fetch(title), 9)
            except asyncio.TimeoutError:
                chars = None
            if not chars:
                return None
            self.chars[title] = chars
            await asyncio.to_thread(self._save_chars)
        lo, hi = TIERS.get(level, TIERS["medium"])
        pool = chars[lo:hi] or chars[-4:]
        return random.choice(pool)

    async def _warm(self):
        """Quietly download the characters of every anime in the background, so rounds start instantly."""
        try:
            await self.bot.wait_until_ready()
            titles = [q["a"] for lv in LEVELS for q in self.questions.get(lv, []) if q["a"] not in self.chars]
            for t in dict.fromkeys(titles):
                if t in self.chars:
                    continue
                chars = await self._fetch(t)
                if chars:
                    self.chars[t] = chars
                    await asyncio.to_thread(self._save_chars)
                await asyncio.sleep(1.5)                 # stays far below AniList's limit
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("character warm-up stopped")

    async def cog_unload(self):
        for g in list(self.games.values()):
            g.stop()
        self._warm_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    @commands.hybrid_group(name="guessanime", aliases=["ga"], fallback="play", description="Play Guess the Anime with friends")
    @app_commands.describe(difficulty="easy, medium (default) or hard")
    @app_commands.choices(difficulty=[app_commands.Choice(name=v["label"], value=k) for k, v in LEVELS.items()])
    @commands.guild_only()
    async def guessanime(self, ctx: commands.Context, difficulty: str = "medium"):
        if not any(self.questions.values()):
            return await ctx.reply(err("The question list (`assets/guess_anime.json`) is missing - tell the bot owner."), mention_author=False)
        await self.open(ctx, difficulty)

    @guessanime.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Guess the Anime works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ga_channel(self, ctx: commands.Context):
        await self.pick_channel(ctx)

    @guessanime.command(name="stop", description="Stop the running Guess the Anime game (host or admin)")
    @commands.guild_only()
    async def ga_stop(self, ctx: commands.Context):
        await self.stop_game(ctx)

    @guessanime.command(name="stats", description="Your Guess the Anime record")
    @app_commands.describe(member="Whose stats (default: yours)")
    @commands.guild_only()
    async def ga_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        await self.show_stats(ctx, member)

    @guessanime.command(name="top", description="Best Guess the Anime players of this server")
    @commands.guild_only()
    async def ga_top(self, ctx: commands.Context):
        await self.show_top(ctx)


async def setup(bot):
    await bot.add_cog(GuessAnime(bot))
