"""
Kon dashboard API — copy to  cogs/dashboard_api.py  and add "cogs.dashboard_api" to EXTENSIONS in main.py.

It reads and writes the SAME data the bot's own commands use, so a change on the website is exactly like running
the command in Discord:

  module         where the bot keeps it                                   same as
  custom_vc      store.guild(g)["create"/"interface"] + db "customvc"      $customvc, $interface, $customvc config
  points         db "points"                                               $point config
  shop           db shop_items table + db "shop".per_page                  $pointshop config
  self_roles     store.data["selfroles"][g]  (+ posts/updates the panel)   $setsr / $editsr
  role_commands  db "rolecmds"                                             $setrole
  aliases        db "aliases"                                              $alias
  no_prefix      db "noprefix"                                             $noprefix
  commands       db "cmd_disable".rules                                    $disablecommand
  games          db "tictactoe"/"timetap"/"handfight".channel + BlackTea settings file   $<game> channel
  aura           db "aura"                                                 $aura config
  autoreact      db "autoreact"                                            $ar config
  afk            AFK cog tag_enabled                                       $afktag

Backup and imagehost are owner-only and are NOT exposed.

Endpoints (website sends the user's Discord OAuth2 token as  Authorization: Bearer <token>):
  GET   /dashboard/guilds               -> {"ids": [...]}
  GET   /dashboard/guilds/{gid}         -> {"channels", "roles", "config"}
  PATCH /dashboard/guilds/{gid}/{mod}   -> save one module
Every call checks the token with Discord and needs Manage Server / Administrator / owner in that server.

Env: DASHBOARD_PORT (8096), DASHBOARD_ALLOW_ORIGIN (your website URL, default *)
"""
import os
import time

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

TOKEN_CACHE_SECONDS = 60
GAMES = ("tictactoe", "timetap", "handfight", "guessanime", "guessemoji", "simonsays")


def _id(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ids(lst):
    return [i for i in (_id(x) for x in (lst or [])) if i]


def _s(v):
    return str(v) if v else ""


class DashboardAPI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.runner = None
        self.http = None
        self._tokens: dict[str, tuple[float, list]] = {}
        self.origin = os.getenv("DASHBOARD_ALLOW_ORIGIN", "https://konbot.vercel.app")

    @property
    def db(self):
        return self.bot.db

    @property
    def store(self):
        return self.bot.store

    # ---------------------------------------------------------------- server
    async def cog_load(self):
        self.http = aiohttp.ClientSession()
        app = web.Application(middlewares=[self.cors])
        app.router.add_get("/status", self.status_route)
        app.router.add_get("/", self.status_route)
        app.router.add_get("/dashboard/guilds", self.list_guilds)
        app.router.add_get("/dashboard/guilds/{gid}", self.get_guild)
        app.router.add_patch("/dashboard/guilds/{gid}/{mod}", self.patch_module)
        app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        port = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "8096"))
        await web.TCPSite(self.runner, "0.0.0.0", port).start()
        print(f"[dashboard_api] serving http://0.0.0.0:{port}/dashboard")

    async def cog_unload(self):
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()

    @web.middleware
    async def cors(self, request, handler):
        request_origin = request.headers.get("Origin")
        if request_origin and request_origin != self.origin:
            resp = web.json_response({"error": "origin not allowed"}, status=403)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = web.json_response({"error": e.reason}, status=e.status)
            except Exception as e:  # never leak a traceback to the browser
                print("[dashboard_api] request failed")
                resp = web.json_response({"error": "invalid request"}, status=400)
        resp.headers["Access-Control-Allow-Origin"] = self.origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, PATCH, OPTIONS"
        return resp

    async def status_route(self, request):
        status_cog = self.bot.get_cog("StatusAPI")
        if status_cog is None:
            return web.json_response({"online": self.bot.is_ready()}, headers={"Cache-Control": "no-store"})
        return await status_cog.status(request)

    async def user_guilds(self, request) -> list:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise web.HTTPUnauthorized(reason="missing token")
        hit = self._tokens.get(auth)
        if hit and hit[0] > time.time():
            return hit[1]
        async with self.http.get("https://discord.com/api/v10/users/@me/guilds", headers={"Authorization": auth}) as r:
            if r.status != 200:
                raise web.HTTPUnauthorized(reason="invalid token")
            guilds = await r.json()
        self._tokens[auth] = (time.time() + TOKEN_CACHE_SECONDS, guilds)
        return guilds

    async def require_manager(self, request) -> discord.Guild:
        gid = request.match_info["gid"]
        for g in await self.user_guilds(request):
            p = int(g.get("permissions", 0))
            if g["id"] == gid and (g.get("owner") or p & 0x8 or p & 0x20):
                guild = self.bot.get_guild(int(gid))
                if not guild:
                    raise web.HTTPNotFound(reason="Kon is not in this server")
                return guild
        raise web.HTTPForbidden(reason="Manage Server required")

    async def list_guilds(self, request):
        await self.user_guilds(request)
        return web.json_response({"ids": [str(g.id) for g in self.bot.guilds]})

    async def get_guild(self, request):
        guild = await self.require_manager(request)

        def kind(c):
            if isinstance(c, (discord.VoiceChannel, discord.StageChannel)):
                return "voice"
            return "category" if isinstance(c, discord.CategoryChannel) else "text"

        channels = [{"id": str(c.id), "name": c.name, "type": kind(c)} for c in guild.channels
                    if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel,
                                      discord.CategoryChannel, discord.ForumChannel))]
        roles = [{"id": str(r.id), "name": r.name, "color": r.color.value} for r in reversed(guild.roles)
                 if not r.is_default() and not r.managed]
        return web.json_response({"channels": channels, "roles": roles, "config": self.read_all(guild)})

    async def patch_module(self, request):
        guild = await self.require_manager(request)
        mod = request.match_info["mod"]
        v = await request.json()
        if not isinstance(v, dict):
            raise web.HTTPBadRequest(reason="expected an object")
        writer = getattr(self, f"w_{mod}", None)
        if writer is None:
            raise web.HTTPBadRequest(reason="unknown module")
        await writer(guild, v)
        # Keep a dashboard snapshot in the JSON store while module writers also
        # update the bot's native settings/database used by commands and cogs.
        dashboard = self.store.guild(guild.id).setdefault("dashboard", {})
        dashboard[mod] = v
        self.store.save_soon()
        return web.json_response({"ok": True})

    # ---------------------------------------------------------------- read (bot -> website)
    def read_all(self, guild) -> dict:
        gid, db = guild.id, self.db
        g = self.store.guild(gid)
        cvc = db.get_settings(gid, "customvc")
        pts = db.get_settings(gid, "points")
        sr = self.store.data.get("selfroles", {}).get(str(gid), {})
        np_ = db.get_settings(gid, "noprefix")
        aura = db.get_settings(gid, "aura")
        ar = db.get_settings(gid, "autoreact")
        bt = self.bot.get_cog("BlackTea")
        afk = self.bot.get_cog("AFK")
        color = sr.get("color")
        out = {
            "custom_vc": {"create": _s(g.get("create")), "interface": _s(g.get("interface")),
                          "admins": cvc.get("admins", True), "roles": [str(r) for r in cvc.get("roles", [])]},
            "shop": {"per_page": db.get_settings(gid, "shop").get("per_page", 4),
                     "items": [{"role": str(i["role_id"]), "price": i["price"], "stock": i["stock"], "emoji": i.get("emoji") or ""}
                               for i in db.shop_list(gid)]},
            "self_roles": {"channel": _s(sr.get("channel")), "title": sr.get("title", "Self Roles :"),
                           "title_emoji": sr.get("title_emoji") or "", "footer": sr.get("footer", ""),
                           "color": f"#{color:06x}" if isinstance(color, int) else "#ffb832",
                           "banner_url": sr.get("banner_url") or "",
                           "roles": [{"role": str(r.get("role")), "label": r.get("label") or "", "emoji": r.get("emoji") or ""}
                                     for r in sr.get("roles", [])]},
            "role_commands": {"commands": [{"name": n, "role": _s(c.get("role")), "allowed": _s(c.get("allowed"))}
                                           for n, c in db.get_settings(gid, "rolecmds").items()]},
            "aliases": {"aliases": [{"name": n, "command": c} for n, c in db.get_settings(gid, "aliases").items()]},
            "no_prefix": {"role": _s(np_.get("role")), "users": [str(u) for u in np_.get("users", [])]},
            "commands": {"rules": [{"command": k, "channels": [str(c) for c in v]}
                                   for k, v in db.get_settings(gid, "cmd_disable").get("rules", {}).items()]},
            "games": {**{k: _s(db.get_settings(gid, k).get("channel")) for k in GAMES},
                      "blacktea": _s(bt.game_channel(gid)) if bt else ""},
            "aura": {"random": aura.get("random", True),
                     "members": [{"user": str(u), "pct": p} for u, p in aura.get("members", {}).items()],
                     "roles": [{"role": str(r), "pct": p} for r, p in aura.get("roles", {}).items()],
                     "disabled": [str(u) for u in aura.get("disabled", [])]},
            "autoreact": {"roles": [str(r) for r in ar.get("roles", [])],
                          "users": [{"user": str(u), "emoji": (e[0] if e else "")} for u, e in ar.get("users", {}).items()]},
            "afk": {"tag": afk.tag_enabled.get(gid, True) if afk else True},
        }
        if pts:
            out["points"] = {**{k: v for k, v in pts.items() if k != "channel_rules"},
                             "enabled_channels": [str(c) for c in pts.get("enabled_channels", [])],
                             "disabled_channels": [str(c) for c in pts.get("disabled_channels", [])],
                             "vc_disabled_channels": [str(c) for c in pts.get("vc_disabled_channels", [])],
                             "channel_rules": [{"channel": c, "msgs": r.get("msgs", 50), "points": r.get("points", 1)}
                                               for c, r in pts.get("channel_rules", {}).items()]}
        return out

    def _drop_cache(self, cog_name: str, attr: str, gid: int):
        cog = self.bot.get_cog(cog_name)
        cache = getattr(cog, attr, None) if cog else None
        if isinstance(cache, dict):
            cache.pop(gid, None)

    # ---------------------------------------------------------------- write (website -> bot)
    async def w_custom_vc(self, guild, v):
        g = self.store.guild(guild.id)
        create = _id(v.get("create"))
        if create:
            g["create"] = create
        self.db.set_settings(guild.id, "customvc", {"roles": _ids(v.get("roles"))[:25], "admins": bool(v.get("admins", True))})
        svc = getattr(self.bot, "vc_service", None)
        if svc is not None and isinstance(getattr(svc, "_cfg", None), dict):
            svc._cfg.pop(guild.id, None)
        new_if = _id(v.get("interface"))
        if new_if and new_if != g.get("interface"):
            cog, ch = self.bot.get_cog("Interface"), guild.get_channel(new_if)
            if cog and isinstance(ch, discord.TextChannel):
                await cog.post_panel(ch)            # same as $interface #channel
        self.store.save_soon()

    async def w_points(self, guild, v):
        keys = ("chat_enabled", "chat_msgs", "chat_points", "chat_everywhere", "vc_enabled", "vc_mins", "vc_points",
                "vc_need_others", "vc_need_muted", "cooldown")
        cfg = {**self.db.get_settings(guild.id, "points"), **{k: v[k] for k in keys if k in v}}
        for k in ("disabled_channels", "enabled_channels", "vc_disabled_channels"):
            cfg[k] = _ids(v.get(k))
        cfg["channel_rules"] = {str(_id(r.get("channel"))): {"msgs": max(1, int(r.get("msgs", 50))), "points": max(1, int(r.get("points", 1)))}
                                for r in v.get("channel_rules", []) if _id(r.get("channel"))}
        self.db.set_settings(guild.id, "points", cfg)
        self._drop_cache("Points", "_cfg", guild.id)

    async def w_shop(self, guild, v):
        gid = guild.id
        s = self.db.get_settings(gid, "shop")
        s["per_page"] = max(1, min(10, int(v.get("per_page", 4))))
        self.db.set_settings(gid, "shop", s)
        wanted = {}
        for it in v.get("items", [])[:25]:
            rid = _id(it.get("role"))
            if rid and guild.get_role(rid):
                wanted[rid] = it
        for old in self.db.shop_list(gid):                 # removed on the website -> removed from the shop
            if old["role_id"] not in wanted:
                self.db.shop_remove(gid, old["id"])
        for rid, it in wanted.items():
            self.db.shop_upsert(gid, rid, max(0, int(it.get("price", 0))), int(it.get("stock", -1)), (it.get("emoji") or None))

    async def w_self_roles(self, guild, v):
        cog = self.bot.get_cog("SelfRoles")
        c = cog.cfg(guild.id) if cog else self.store.data.setdefault("selfroles", {}).setdefault(str(guild.id), {})
        c["channel"] = _id(v.get("channel"))
        for k in ("title", "footer"):
            c[k] = (v.get(k) or "")[:200] or c.get(k)
        c["title_emoji"] = v.get("title_emoji") or None
        try:
            c["color"] = int(str(v.get("color", "#ffb832")).lstrip("#"), 16)
        except ValueError:
            pass
        c["banner_url"] = v.get("banner_url") or None
        c["roles"] = [{"role": _id(r.get("role")), "label": (r.get("label") or "")[:80] or None,
                       "emoji": r.get("emoji") or None, "line": (r.get("label") or None), "line_emoji": r.get("emoji") or None}
                      for r in v.get("roles", [])[:20] if _id(r.get("role"))]
        self.store.save_soon()
        if cog and c["channel"] and c["roles"]:
            await cog.publish(guild)                       # posts or updates the panel, like $setsr

    async def w_role_commands(self, guild, v):
        cmds = {}
        for c in v.get("commands", [])[:25]:
            name = (c.get("name") or "").lower().strip().removeprefix("$").removeprefix("set")
            if name and _id(c.get("role")):
                cmds[name[:20]] = {"role": _id(c.get("role")), "allowed": _id(c.get("allowed"))}
        self.db.set_settings(guild.id, "rolecmds", cmds)
        if hasattr(self.bot, "_rolecmds"):
            self.bot._rolecmds.pop(guild.id, None)

    async def w_aliases(self, guild, v):
        a = {}
        for x in v.get("aliases", [])[:50]:
            name = (x.get("name") or "").lower().strip().lstrip("$")
            cmd = (x.get("command") or "").strip().lstrip("$")
            if name and cmd and self.bot.get_command(name) is None and self.bot.get_command(cmd.split()[0]) is not None:
                a[name[:20]] = cmd
        self.db.set_settings(guild.id, "aliases", a)
        self.bot._aliases.pop(guild.id, None)

    async def w_no_prefix(self, guild, v):
        self.db.set_settings(guild.id, "noprefix", {"role": _id(v.get("role")), "users": _ids(v.get("users"))})
        self.bot._noprefix.pop(guild.id, None)

    async def w_commands(self, guild, v):
        rules = {}
        for r in v.get("rules", []):
            key = (r.get("command") or "").strip().lstrip("$")
            if key and _ids(r.get("channels")):
                rules[key] = _ids(r.get("channels"))
        self.db.set_settings(guild.id, "cmd_disable", {"rules": rules})
        self._drop_cache("CmdControl", "_rules", guild.id)

    async def w_games(self, guild, v):
        for k in GAMES:
            d = self.db.get_settings(guild.id, k)
            cid = _id(v.get(k))
            if cid:
                d["channel"] = cid
            else:
                d.pop("channel", None)
            self.db.set_settings(guild.id, k, d)
        bt = self.bot.get_cog("BlackTea")
        if bt is not None:
            from cogs.blacktea import save_settings
            cid = _id(v.get("blacktea"))
            if cid:
                bt.settings["channels"][str(guild.id)] = cid
            else:
                bt.settings["channels"].pop(str(guild.id), None)
            save_settings(bt.settings)

    async def w_aura(self, guild, v):
        cfg = {"random": bool(v.get("random", True)),
               "members": {str(_id(m.get("user"))): max(0, min(100, int(m.get("pct", 0)))) for m in v.get("members", []) if _id(m.get("user"))},
               "roles": {str(_id(r.get("role"))): max(0, min(100, int(r.get("pct", 0)))) for r in v.get("roles", []) if _id(r.get("role"))},
               "disabled": _ids(v.get("disabled"))}
        self.db.set_settings(guild.id, "aura", cfg)
        self._drop_cache("Aura", "_cfg", guild.id)

    async def w_autoreact(self, guild, v):
        cfg = {"roles": _ids(v.get("roles"))[:25],
               "users": {str(_id(u.get("user"))): [u.get("emoji")] for u in v.get("users", []) if _id(u.get("user")) and u.get("emoji")}}
        self.db.set_settings(guild.id, "autoreact", cfg)
        self._drop_cache("Autoreact", "_cache", guild.id)

    async def w_afk(self, guild, v):
        afk = self.bot.get_cog("AFK")
        if afk:
            afk.tag_enabled[guild.id] = bool(v.get("tag", True))
            afk.save()


async def setup(bot):
    await bot.add_cog(DashboardAPI(bot))
