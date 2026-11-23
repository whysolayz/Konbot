"""Readable exports that are added to every backup (next to the raw files in data/).

  export/SERVER_CONFIG.md    every setting of every server, IDs resolved to channel / role / user names
  export/SERVER_CONFIG.json  the same, machine readable
  export/db_dump.json        EVERY row of kon.db (players, progress, badges, shop, purchases, settings, point log)
  export/players_<guild>.csv one line per player: points, chat / VC totals, badges, rank
  export/LINKS.md            every saved link / banner / image and where it is used
  export/afk.json            AFK users + AFK-tag setting
  export/custom_vc_channels.json  join-to-create channel + panel channel of every server (id, name, category, bitrate...)
  export/servers/<guild>/    ONE FOLDER PER SERVER with everything of that server only:
        config.md / config.json   readable settings (owners, VC, self roles, points, shop, no-prefix, aliases, $setrole, autoreact, AFK)
        data.json                 raw saved data of the server (vc_data.json part + AFK + mutes + extra owners)
        database.json             every database row of the server (players, progress, badges, shop, purchases, settings, point log)

The raw files (data/kon.db, data/vc_data.json, data/afk_data.json, data/selfrole/*) stay the source of truth for a
restore; these files exist so nothing is hidden inside a binary and the server setup can be re-created by hand.
"""
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

import discord

SNOWFLAKE_MIN = 10 ** 15
TABLES = ("users", "channel_progress", "badges", "shop_items", "purchases", "settings", "point_log")
MAX_LOG_ROWS_IN_JSON = 50000        # point_log is always complete inside data/kon.db


# ------------------------------------------------------------------ name lookup
class Resolver:
    def __init__(self, bot, guild):
        self.bot, self.guild = bot, guild

    def name(self, n: int):
        g = self.guild
        if g is not None:
            if g.id == n:
                return f"server {g.name}"
            ch = g.get_channel(n) or g.get_thread(n)
            if ch is not None:
                return f"#{ch.name}"
            r = g.get_role(n)
            if r is not None:
                return f"@{r.name}"
            m = g.get_member(n)
            if m is not None:
                return f"{m.display_name} ({m})"
        u = self.bot.get_user(n)
        return str(u) if u else None

    def ref(self, n):
        """{'id':..,'name':..,'found':..} for JSON output."""
        if not n:
            return None
        nm = self.name(int(n))
        return {"id": int(n), "name": nm, "found": nm is not None}


def _is_id(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= SNOWFLAKE_MIN or \
        (isinstance(v, str) and v.isdigit() and len(v) >= 16)


def md(v, res: Resolver, key: str = "", ind: int = 0) -> list:
    """YAML-ish markdown lines of any json value; every ID gets its name next to it."""
    pad = "  " * ind

    def scalar(x, k=""):
        if _is_id(x):
            nm = res.name(int(x))
            if nm:
                return f"`{x}` → {nm}"
            if any(w in k.lower() for w in ("channel", "role", "user", "owner", "create", "interface", "banned")):
                return f"`{x}` → *not found (deleted / bot not in server)*"
            return f"`{x}`"
        if isinstance(x, bool):
            return "yes" if x else "no"
        if x is None:
            return "*none*"
        if isinstance(x, float) and x > 1e9:                     # unix time
            return f"{x:.0f} ({datetime.fromtimestamp(x, timezone.utc):%Y-%m-%d %H:%M} UTC)"
        return f"`{x}`" if isinstance(x, str) and ("<:" in x or "<a:" in x) else str(x)

    out = []
    if isinstance(v, dict):
        if not v:
            return [f"{pad}- *(empty)*"]
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                out.append(f"{pad}- **{k}**" + (f" → {res.name(int(k))}" if _is_id(k) and res.name(int(k)) else "") + ":")
                out += md(x, res, str(k), ind + 1)
            else:
                label = f"**{k}**" + (f" → {res.name(int(k))}" if _is_id(k) and res.name(int(k)) else "")
                out.append(f"{pad}- {label}: {scalar(x, str(k)) if not isinstance(x, (dict, list)) else '*(empty)*'}")
    elif isinstance(v, list):
        if not v:
            return [f"{pad}- *(empty)*"]
        for n, x in enumerate(v, 1):
            if isinstance(x, (dict, list)):
                out.append(f"{pad}- #{n}")
                out += md(x, res, key, ind + 1)
            else:
                out.append(f"{pad}- {scalar(x, key)}")
    else:
        out.append(f"{pad}- {scalar(v, key)}")
    return out


# ------------------------------------------------------------------ custom VC setup (join-to-create + panel channel)
def vc_setup_snapshot(bot, gid: int, g, gcfg: dict) -> dict:
    """Where the custom-VC channels of one server are (id + name + category + settings).

    The live settings (`vc_data.json -> guilds`) are dropped when someone deletes the channel, so the last known
    state is also kept in `vc_data.json -> vc_setup` (never removed) and is part of every backup.
    """
    saved = bot.store.data.setdefault("vc_setup", {}).setdefault(str(gid), {})
    changed = False
    for key in ("create", "interface"):
        cid = gcfg.get(key)
        ch = g.get_channel(cid) if (g is not None and cid) else None
        if ch is not None:
            rec = {"id": ch.id, "name": ch.name, "type": str(ch.type),
                   "category_id": ch.category_id, "category": ch.category.name if ch.category else None,
                   "position": ch.position}
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                rec.update(bitrate=ch.bitrate, user_limit=ch.user_limit)
            if saved.get(key) != rec:
                saved[key] = rec
                changed = True
        elif cid and (key not in saved or saved[key].get("id") != cid):
            saved[key] = {"id": cid, "name": None}
            changed = True
    if gcfg.get("panel_msg") and saved.get("panel_message_id") != gcfg["panel_msg"]:
        saved["panel_message_id"] = gcfg["panel_msg"]
        changed = True
    if changed:
        bot.store.save_soon()
    out = {}
    for key in ("create", "interface"):
        rec = dict(saved.get(key) or {})
        if rec:
            rec["currently_set"] = bool(gcfg.get(key)) and gcfg.get(key) == rec.get("id")
            rec["exists_now"] = bool(g is not None and g.get_channel(rec.get("id", 0)))
        out["join_to_create" if key == "create" else "interface"] = rec or None
    out["panel_message_id"] = saved.get("panel_message_id")
    return out


# ------------------------------------------------------------------ config report (main thread: needs the discord cache)
def _guild_ids(bot) -> list:
    ids = {g.id for g in bot.guilds}
    d = bot.store.data
    ids |= {int(k) for k in d.get("guilds", {})} | {int(k) for k in d.get("selfroles", {})}
    ids |= {int(v["guild"]) for v in d.get("vcs", {}).values() if "guild" in v}
    for (gid,) in bot.db.conn.execute("SELECT DISTINCT guild_id FROM settings UNION SELECT DISTINCT guild_id FROM users "
                                      "UNION SELECT DISTINCT guild_id FROM shop_items"):
        ids.add(gid)
    return sorted(ids)


def slim_settings(raw: dict) -> dict:
    """Giveaway records hold every entry / counted message - in the readable report only their sizes are shown
    (the full data is in data/kon.db, export/db_dump.json and export/servers/<id>/database.json)."""
    out = dict(raw)
    g = out.get("giveaway")
    if isinstance(g, dict):
        g = dict(g)
        for pool in ("active", "ended"):
            recs = g.get(pool)
            if isinstance(recs, dict):
                g[pool] = {mid: {**{k: v for k, v in r.items() if k not in ("entries", "counts", "inv", "task")},
                                 "entries": len(r.get("entries") or []), "users_with_counted_messages": len(r.get("counts") or {}),
                                 "inviters": len(r.get("inv") or {}), "task_done": len(r.get("task") or [])}
                           if isinstance(r, dict) else r for mid, r in recs.items()}
        out["giveaway"] = g
    return out


def collect(bot, afk_data: dict) -> dict:
    """-> {'json': bytes, 'md': bytes, 'names': {'gid:uid': name}, 'summary': [..lines for the embed..], 'files': {..}}"""
    st, db = bot.store.data, bot.db
    guilds_out, md_parts, summary, per_server = {}, [], [], {}
    vc_all, vc_lines = {}, []
    names = {}
    afk_all = afk_data.get("afk", {})
    for gid in _guild_ids(bot):
        g = bot.get_guild(gid)
        res = Resolver(bot, g)
        gcfg = st.get("guilds", {}).get(str(gid), {})
        sr = st.get("selfroles", {}).get(str(gid), {})
        vcs = {cid: rec for cid, rec in st.get("vcs", {}).items() if rec.get("guild") == gid}
        raw_settings = {}
        for key, val in db.conn.execute("SELECT key,value FROM settings WHERE guild_id=?", (gid,)):
            try:
                raw_settings[key] = json.loads(val)
            except Exception:
                raw_settings[key] = val
        shop_items = db.shop_list(gid)
        afk = {u: v for u, v in afk_all.items() if str(v.get("guild_id")) == str(gid)}
        afk_view = {u: {"reason": v.get("reason"), "since": v.get("since"), "old_nick": v.get("old_nick"),
                        "mentions_waiting": len(v.get("mentions", []))} for u, v in afk.items()}
        players = db.conn.execute("SELECT COUNT(*) FROM users WHERE guild_id=?", (gid,)).fetchone()[0]
        for (uid,) in db.conn.execute("SELECT user_id FROM users WHERE guild_id=?", (gid,)):
            nm = res.name(uid)
            if nm:
                names[f"{gid}:{uid}"] = nm

        vc_snap = vc_setup_snapshot(bot, gid, g, gcfg)
        vc_all[str(gid)] = {"server_name": g.name if g else None, **vc_snap,
                            "active_custom_vcs": {cid: {k: v for k, v in rec.items() if k != "guild"} for cid, rec in vcs.items()}}
        cfg = {
            "server": {"id": gid, "name": g.name if g else None, "bot_is_in_server": g is not None,
                       "owner_id": g.owner_id if g else None, "members": g.member_count if g else None},
            "custom_vc": {"join_to_create_channel": gcfg.get("create"), "interface_channel": gcfg.get("interface"),
                          "interface_message_id": gcfg.get("panel_msg"),
                          "saved_setup (last known, survives channel deletion)": vc_snap,
                          "other_keys": {k: v for k, v in gcfg.items() if k not in ("create", "interface", "panel_msg")},
                          "active_custom_vcs": {cid: {k: v for k, v in rec.items() if k != "guild"} for cid, rec in vcs.items()}},
            "self_roles": sr or None,
            "points_settings": raw_settings.get("points"),
            "shop": {"settings": raw_settings.get("shop"), "items": shop_items},
            "no_prefix": raw_settings.get("noprefix"),
            "aliases": raw_settings.get("aliases"),
            "role_commands": raw_settings.get("rolecmds"),
            "autoreact": raw_settings.get("autoreact"),
            "aura": raw_settings.get("aura"),
            "custom_vc_rules": raw_settings.get("customvc"),
            "tictactoe": raw_settings.get("tictactoe"),
            "game_channels (Tic Tac Toe, BlackTea, TimeTap, Handfight)": {
                "tictactoe": (raw_settings.get("tictactoe") or {}).get("channel"),
                "blacktea": ((bot.get_cog("BlackTea").settings.get("channels", {}) if bot.get_cog("BlackTea") else {}).get(str(gid))),
                "timetap": (raw_settings.get("timetap") or {}).get("channel"),
                "handfight": (raw_settings.get("handfight") or {}).get("channel")},
            "game_stats (TimeTap, Handfight)": {"timetap": raw_settings.get("timetap_stats"),
                                                "handfight": raw_settings.get("handfight_stats")},
            "webhook_sender (name, avatar, embed, images)": raw_settings.get("webhook"),
            "saved_images ($tolink names -> links)": raw_settings.get("imagehost"),
            "command_disable ($disablecommand)": raw_settings.get("cmd_disable"),
            "extra_owners": [res.ref(i) for i in st.get("guild_owners", {}).get(str(gid), [])],
            "afk": {"tag_enabled": afk_data.get("tag", {}).get(str(gid), True), "users": afk_view},
            "tickets (panels, categories, open tickets)": raw_settings.get("tickets"),
            "giveaways (templates, running, ended)": slim_settings({"giveaway": raw_settings.get("giveaway")}).get("giveaway"),
            "all_settings_raw": slim_settings(raw_settings),
            "players_in_database": players,
        }
        guilds_out[str(gid)] = cfg

        # ----- markdown
        title = g.name if g else f"(server {gid} - bot not in it)"
        lines = [f"## {title}  (`{gid}`)", ""]
        for head, key in (("Custom VC (join-to-create + control panel + live VCs)", "custom_vc"),
                          ("Custom VC protection ($customvc config)", "custom_vc_rules"),
                          ("Self roles ($setsr / $editsr)", "self_roles"),
                          ("Points ($point config)", "points_settings"), ("Point shop", "shop"),
                          ("No-prefix", "no_prefix"), ("Aliases", "aliases"), ("Role commands ($setrole)", "role_commands"),
                          ("Tickets ($ticket)", "tickets (panels, categories, open tickets)"),
                          ("Giveaways ($giveaway)", "giveaways (templates, running, ended)"),
                          ("Autoreact", "autoreact"), ("Aura ($aura config)", "aura"), ("Tic Tac Toe channel", "tictactoe"),
                          ("Game channels (Tic Tac Toe, BlackTea, TimeTap, Handfight)", "game_channels (Tic Tac Toe, BlackTea, TimeTap, Handfight)"),
                          ("Game stats (TimeTap, Handfight)", "game_stats (TimeTap, Handfight)"),
                          ("Webhook sender (name, avatar, embed, images)", "webhook_sender (name, avatar, embed, images)"),
                          ("Saved images ($tolink)", "saved_images ($tolink names -> links)"),
                          ("Disabled commands", "command_disable ($disablecommand)"), ("Extra owners ($konextra - this server only)", "extra_owners"),
                          ("AFK", "afk"),
                          ("EVERYTHING saved for this server (raw settings, nothing left out)", "all_settings_raw")):
            lines += [f"### {head}", ""] + (md(cfg[key], res) if cfg[key] not in (None, {}) else ["- *not configured*"]) + [""]
        lines += [f"### Players", "", f"- {players} players stored in `data/kon.db` "
                  f"(full list: `export/players_{gid}.csv`)", ""]
        md_parts.append("\n".join(lines))
        raw_data = {
            "server_id": gid,
            "vc_data.guilds": gcfg,
            "vc_data.selfroles": sr,
            "vc_data.vcs": vcs,
            "vc_data.mutes": {k: v for k, v in st.get("mutes", {}).items() if k.startswith(f"{gid}:")},
            "vc_data.guild_owners": st.get("guild_owners", {}).get(str(gid), []),
            "vc_data.vc_setup": st.get("vc_setup", {}).get(str(gid), {}),
            "afk_data.afk": afk,
            "afk_data.tag": afk_data.get("tag", {}).get(str(gid), True),
        }
        per_server[str(gid)] = {
            "md": "\n".join(["# Kon - server configuration", ""] + lines).encode(),
            "json": json.dumps(cfg, indent=2, ensure_ascii=False, default=str).encode(),
            "data": json.dumps(raw_data, indent=2, ensure_ascii=False, default=str).encode(),
        }

        def nm(i):
            return res.name(i) if i else None
        jc, itf = vc_snap.get("join_to_create"), vc_snap.get("interface")
        vc_lines.append(
            f"**{title}** · join-to-create: " + (f"#{jc.get('name')} (`{jc['id']}`)" + (f" in {jc['category']}" if jc.get("category") else "")
                                                if jc else "—")
            + " · panel: " + (f"#{itf.get('name')} (`{itf['id']}`)" if itf else "—"))
        summary.append(
            f"**{title}** · join-to-create: {nm(gcfg.get('create')) or '—'} · panel: {nm(gcfg.get('interface')) or '—'} · "
            f"self-role: {nm(sr.get('channel')) or '—'} · players: {players}")

    links_md = links_report(bot, guilds_out)
    bot_res = Resolver(bot, None)
    glob = {"bot": {"name": str(bot.user), "id": bot.user.id if bot.user else None},
            "voice_mutes_running": st.get("mutes", {}), "slash_sync_hash": st.get("sync_hash")}
    header = ["# Kon - server configuration", "",
              "Every channel / role / user ID below is followed by its name (as the bot sees it right now).", "",
              "## Bot-wide", ""] + md(glob, bot_res) + [""]
    doc = "\n".join(header) + "\n" + "\n".join(md_parts)
    payload = {"bot": glob, "servers": guilds_out}
    return {"json": json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode(),
            "md": doc.encode(), "names": names, "summary": summary, "per_server": per_server,
            "vc_json": json.dumps(vc_all, indent=2, ensure_ascii=False, default=str).encode(), "vc_lines": vc_lines,
            "links_md": links_md.encode(),
            "afk": json.dumps(afk_data, indent=2, ensure_ascii=False).encode()}


def _item_lines(label: str, it) -> list:
    if isinstance(it, dict) and it.get("url"):
        ref = it.get("ref") or ({"c": it["c"], "m": it["m"]} if it.get("c") and it.get("m") else None)
        host = f" · hosted in channel `{ref['c']}` message `{ref['m']}`" if isinstance(ref, dict) else " · plain link"
        return [f"- **{label}**: {it['url']}{host}"]
    if isinstance(it, str) and it.startswith("http"):
        return [f"- **{label}**: {it}"]
    return []


def links_report(bot, guilds_out: dict) -> str:
    """export/LINKS.md - every link / banner / image the bot has saved, and WHERE it is used."""
    out = ["# Kon - saved links, banners and images", "",
           "Uploaded images live in the bot owner's image channel (`$imgchannel`); the hosting message id is kept so a fresh link "
           "can always be fetched.", ""]
    for gid, cfg in guilds_out.items():
        lines = []
        sr = cfg.get("self_roles") or {}
        lines += _item_lines("Self-role banner", {"url": sr.get("banner_url"), "ref": sr.get("banner_ref")} if sr.get("banner_url") else None)
        shop = (cfg.get("shop") or {}).get("settings") or {}
        if shop.get("thumbnail"):
            lines += _item_lines("Shop thumbnail", {"url": shop["thumbnail"], "ref": ({"c": shop["thumbnail_ref"]["c"], "m": shop["thumbnail_ref"]["m"]}
                                                                                     if shop.get("thumbnail_ref") else None)})
        wh = cfg.get("webhook_sender (name, avatar, embed, images)") or {}
        for key, label in (("avatar", "Webhook avatar"), ("image", "Webhook embed image"), ("thumb", "Webhook embed thumbnail")):
            lines += _item_lines(label, wh.get(key))
        imgs = (cfg.get("saved_images ($tolink names -> links)") or {}).get("items") or {}
        for name, it in imgs.items():
            lines += _item_lines(f"Saved image `{name}`", it)
        if lines:
            nm = (cfg.get("server") or {}).get("name") or "unknown server"
            out += [f"## {nm} (`{gid}`)", ""] + lines + [""]
    return "\n".join(out)


# ------------------------------------------------------------------ database exports (worker thread, from the snapshot)
def db_extras(snapshot: str, names: dict) -> dict:
    out = {}
    c = sqlite3.connect(snapshot)
    c.row_factory = sqlite3.Row
    try:
        dump = {}
        for t in TABLES:
            try:
                rows = [dict(r) for r in c.execute(f"SELECT * FROM {t}")]
            except sqlite3.Error:
                continue
            if t == "point_log" and len(rows) > MAX_LOG_ROWS_IN_JSON:
                dump["point_log_note"] = f"{len(rows)} rows - too many for json, they are all inside data/kon.db"
                rows = rows[-MAX_LOG_ROWS_IN_JSON:]
            if t == "settings":
                for r in rows:
                    try:
                        r["value"] = json.loads(r["value"])
                    except Exception:
                        pass
            dump[t] = rows
        out["export/db_dump.json"] = json.dumps(dump, indent=1, ensure_ascii=False).encode()

        # one database file per server (everything that belongs to that server and nothing else)
        gids = {r["guild_id"] for rows in dump.values() if isinstance(rows, list) for r in rows if "guild_id" in r}
        for gid in sorted(gids):
            out[f"export/servers/{gid}/database.json"] = json.dumps(
                {"server_id": gid, **{t: [r for r in rows if r.get("guild_id") == gid]
                                      for t, rows in dump.items() if isinstance(rows, list)}},
                indent=1, ensure_ascii=False).encode()

        badges = {}
        for r in dump.get("badges", []):
            badges.setdefault((r["guild_id"], r["user_id"]), []).append(r["badge"])
        by_guild = {}
        for r in dump.get("users", []):
            by_guild.setdefault(r["guild_id"], []).append(r)
        for gid, rows in by_guild.items():
            rows.sort(key=lambda r: (-r["points"], -r["chat_total"]))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["rank", "user_id", "name", "points", "chat_messages_total", "chat_progress",
                        "vc_seconds_total", "vc_hours", "vc_progress_seconds", "badges"])
            for n, r in enumerate(rows, 1):
                w.writerow([n, r["user_id"], names.get(f"{gid}:{r['user_id']}", ""), r["points"], r["chat_total"],
                            r["chat_progress"], r["vc_total"], round(r["vc_total"] / 3600, 2), r["vc_progress"],
                            "; ".join(badges.get((gid, r["user_id"]), []))])
            out[f"export/players_{gid}.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")      # BOM: opens fine in Excel
    finally:
        c.close()
    return out
