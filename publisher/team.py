"""Marketing-team helper, run by .github/workflows/team.yml.

Modes (TEAM_MODE):
  probe    - check which Graph API reads/writes the page tokens are allowed (no data saved).
  collect  - pull Facebook + Instagram stats for every account, and new comments on
             Ocean Below Us posts only (Coached by HPR comments and messages are never read).
             Writes out/stats.json and out/comments.json, uploaded as a run artifact.
  reply    - post replies Henry approved on the morning page. TEAM_REPLIES is a JSON list of
             {"platform": "instagram"|"facebook", "comment_id": "...", "message": "..."}.
             Only Ocean Below Us comments can be replied to.
Tokens come from the OBU_PAGES secret and are never printed.
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from ig_publish import GRAPH, log, req  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out")
COMMENT_BRANDS = {"Ocean Below Us"}          # never read or answer comments for any other brand
NOW = datetime.now(timezone.utc)


def get(path, tok, **params):
    params["access_token"] = tok
    return req("GET", f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}", timeout=60)


def err(r):
    return (r.get("error") or {}).get("message") if isinstance(r, dict) else None


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00"))


def ig_media(acct):
    r = get(f"{acct['ig_id']}/media", acct["page_token"], fields="id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count", limit=30)
    if err(r):
        return None, err(r)
    out = []
    for m in r.get("data", []):
        item = {k: m.get(k) for k in ("id", "timestamp", "permalink", "like_count", "comments_count", "media_product_type")}
        item["caption"] = (m.get("caption") or "")[:140]
        ins = get(f"{m['id']}/insights", acct["page_token"], metric="views,reach,saved,shares,total_interactions")
        if not err(ins):
            for d in ins.get("data", []):
                item[d["name"]] = (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
        else:
            item["insights_error"] = err(ins)
        out.append(item)
    return out, None


def fb_reels(acct):
    r = get(f"{acct['page_id']}/video_reels", acct["page_token"], fields="id,description,created_time,permalink_url", limit=30)
    if err(r):
        return None, err(r)
    out = []
    for v in r.get("data", []):
        item = {"id": v["id"], "timestamp": v.get("created_time"), "permalink": v.get("permalink_url"), "caption": (v.get("description") or "")[:140]}
        ins = get(f"{v['id']}/video_insights", acct["page_token"], metric="blue_reels_play_count,post_impressions_unique,post_video_likes_by_reaction_type")
        if not err(ins):
            for d in ins.get("data", []):
                val = (d.get("values") or [{}])[0].get("value")
                item[{"blue_reels_play_count": "views", "post_impressions_unique": "reach", "post_video_likes_by_reaction_type": "reactions"}.get(d["name"], d["name"])] = val
        else:
            item["insights_error"] = err(ins)
        out.append(item)
    return out, None


def ig_comments(acct, media):
    out = []
    for m in media[:15]:
        if not m.get("comments_count"):
            continue
        r = get(f"{m['id']}/comments", acct["page_token"], fields="id,text,username,timestamp,replies{id,username}", limit=50)
        if err(r):
            return out, err(r)
        for c in r.get("data", []):
            mine = any(rep.get("username", "").lower() == acct.get("ig_username", "").lower() for rep in (c.get("replies") or {}).get("data", []))
            if mine:
                continue
            out.append({"platform": "instagram", "comment_id": c["id"], "post": m.get("permalink"), "post_caption": m.get("caption"),
                        "user": c.get("username"), "text": c.get("text"), "time": c.get("timestamp")})
    return out, None


def fb_comments(acct, reels):
    out = []
    for v in reels[:15]:
        r = get(f"{v['id']}/comments", acct["page_token"], fields="id,message,from,created_time,comments.limit(20){from}", limit=50)
        if err(r):
            return out, err(r)
        for c in r.get("data", []):
            replied = any((x.get("from") or {}).get("id") == acct["page_id"] for x in (c.get("comments") or {}).get("data", []))
            if replied or (c.get("from") or {}).get("id") == acct["page_id"]:
                continue
            out.append({"platform": "facebook", "comment_id": c["id"], "post": v.get("permalink"), "post_caption": v.get("caption"),
                        "user": (c.get("from") or {}).get("name", "someone"), "text": c.get("message"), "time": c.get("created_time")})
    return out, None


def collect(pages, probe=False):
    os.makedirs(OUT, exist_ok=True)
    stats, comments, report = {"collected": NOW.isoformat(timespec="seconds"), "accounts": {}}, [], {}
    for name, acct in pages.items():
        if not acct.get("ig_id") and not acct.get("page_id"):
            continue
        me = get(acct["ig_id"], acct["page_token"], fields="username,followers_count,media_count") if acct.get("ig_id") else {}
        acct["ig_username"] = me.get("username", "")
        a = {"instagram": {"followers": me.get("followers_count"), "username": me.get("username")}, "facebook": {}}
        rep = {"ig_profile": err(me) or "ok"}
        if acct.get("ig_id"):
            media, e = ig_media(acct)
            rep["ig_media"] = e or "ok"
            if media:
                rep["ig_insights"] = media[0].get("insights_error", "ok")
                a["instagram"]["posts"] = media
                if name in COMMENT_BRANDS:
                    cs, e2 = ig_comments(acct, media)
                    rep["ig_comments"] = e2 or "ok"
                    comments += [dict(c, brand=name) for c in cs]
        if acct.get("page_id"):
            fan = get(acct["page_id"], acct["page_token"], fields="followers_count,fan_count")
            a["facebook"]["followers"] = fan.get("followers_count")
            reels, e = fb_reels(acct)
            rep["fb_reels"] = e or "ok"
            if reels:
                rep["fb_insights"] = reels[0].get("insights_error", "ok")
                a["facebook"]["posts"] = reels
                if name in COMMENT_BRANDS:
                    cs, e2 = fb_comments(acct, reels)
                    rep["fb_comments"] = e2 or "ok"
                    comments += [dict(c, brand=name) for c in cs]
        stats["accounts"][name] = a
        report[name] = rep
    log("Access check:", json.dumps(report))
    if probe:
        return 0
    json.dump(stats, open(os.path.join(OUT, "stats.json"), "w"), indent=1)
    json.dump({"collected": stats["collected"], "comments": comments}, open(os.path.join(OUT, "comments.json"), "w"), indent=1)
    log(f"Saved stats for {len(stats['accounts'])} accounts and {len(comments)} unanswered Ocean Below Us comments.")
    return 0


def reply(pages):
    items = json.loads(os.environ.get("TEAM_REPLIES") or "[]")
    acct = pages["Ocean Below Us"]
    rc, done = 0, []
    for it in items:
        msg = (it.get("message") or "").strip()
        if not msg or not it.get("comment_id"):
            continue
        if it.get("platform") == "instagram":
            r = req("POST", f"{GRAPH}/{it['comment_id']}/replies", data={"message": msg, "access_token": acct["page_token"]}, timeout=60)
        elif it.get("platform") == "facebook":
            r = req("POST", f"{GRAPH}/{it['comment_id']}/comments", data={"message": msg, "access_token": acct["page_token"]}, timeout=60)
        else:
            continue
        if err(r):
            log("Reply failed on", it["platform"], it["comment_id"], ":", err(r))
            rc = 1
        else:
            done.append({"comment_id": it["comment_id"], "reply_id": r.get("id")})
            log("Replied on", it["platform"], it["comment_id"])
        time.sleep(2)
    os.makedirs(OUT, exist_ok=True)
    json.dump({"sent": done, "at": NOW.isoformat(timespec="seconds")}, open(os.path.join(OUT, "replies.json"), "w"))
    return rc


if __name__ == "__main__":
    pages = json.loads(os.environ["OBU_PAGES"])
    mode = os.environ.get("TEAM_MODE") or "collect"
    sys.exit(reply(pages) if mode == "reply" else collect(pages, probe=(mode == "probe")))
