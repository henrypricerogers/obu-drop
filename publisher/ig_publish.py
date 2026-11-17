#!/usr/bin/env python3
"""Instagram reel publisher for Ocean Below Us (runs on GitHub Actions).

Reads publisher/schedule.json. For each entry that is due within the next
LEAD_MIN minutes (or overdue by up to LATE_MIN minutes) and not yet in
publisher/posted.json, it:
  1. joins the video parts back into the original file,
  2. creates an Instagram reel container (resumable upload) with caption + cover,
  3. uploads the bytes and waits until Instagram has processed them,
  4. waits until the publish time, then publishes,
  5. records the result in posted.json (committed back by the workflow).

Access keys come from the OBU_PAGES secret (JSON: {account: {ig_id, page_token}}).
Nothing secret is ever printed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GRAPH = "https://graph.facebook.com/v23.0"
LEAD_MIN = int(os.environ.get("LEAD_MIN", "75"))
LATE_MIN = int(os.environ.get("LATE_MIN", "90"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHED = os.path.join(ROOT, "publisher", "schedule.json")
POSTED = os.path.join(ROOT, "publisher", "posted.json")
DRY = os.environ.get("DRY_RUN") == "1"
PREP = os.environ.get("DRY_RUN") == "prepare"  # upload + process, but never publish


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def req(method, url, data=None, headers=None, body=None, timeout=600):
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="ignore")
        try:
            return json.loads(txt)
        except Exception:
            return {"error": {"message": f"HTTP {e.code}: {txt[:300]}"}}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main():
    pages = json.loads(os.environ["OBU_PAGES"])
    sched = load(SCHED, [])
    posted = load(POSTED, {})
    now = time.time()
    only = os.environ.get("ONLY_ID")
    due = []
    for e in sched:
        if e["id"] in posted and posted[e["id"]].get("media_id"):
            continue
        if only and e["id"] != only:
            continue
        t = datetime.fromisoformat(e["publish_at"].replace("Z", "+00:00")).timestamp()
        if only or (-LATE_MIN * 60 <= t - now <= LEAD_MIN * 60):
            due.append((t, e))
    if not due:
        log("Nothing due.")
        return 0
    rc = 0
    for t, e in sorted(due, key=lambda x: x[0]):
        acct = pages[e["account"]]
        tok = acct["page_token"]
        ig = acct["ig_id"]
        log(f"Preparing {e['id']} for {e['account']} at {e['publish_at']}")
        path = f"/tmp/{e['id']}.mp4"
        with open(path, "wb") as out:
            for p in e["parts"]:
                # Parts live outside the sparse checkout; stream each blob straight from git.
                proc = subprocess.Popen(["git", "-C", ROOT, "cat-file", "blob", f"HEAD:{p}"], stdout=subprocess.PIPE)
                while chunk := proc.stdout.read(8 << 20):
                    out.write(chunk)
                if proc.wait() != 0:
                    raise RuntimeError(f"could not read {p} from the repo")
        size = os.path.getsize(path)
        if e.get("size") and size != e["size"]:
            log(f"Size mismatch for {e['id']}: {size} != {e['size']}")
            rc = 1
            continue
        if DRY:
            who = req("GET", f"{GRAPH}/{ig}?fields=username&access_token={urllib.parse.quote(tok)}")
            log("Dry run OK, account:", who.get("username"), "file bytes:", size)
            continue
        cover = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/{os.environ.get('GITHUB_SHA','main')}/{e['cover']}"
        c = req("POST", f"{GRAPH}/{ig}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": e["caption"],
            "share_to_feed": "true", "cover_url": cover, "access_token": tok})
        if "id" not in c:
            log("Container failed:", c.get("error", {}).get("message"))
            rc = 1
            continue
        cid, uri = c["id"], c["uri"]
        with open(path, "rb") as f:
            data = f.read()
        u = req("POST", uri, body=data, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}, timeout=1800)
        if not u.get("success"):
            log("Upload failed:", json.dumps(u)[:300])
            rc = 1
            continue
        status = None
        for _ in range(90):
            time.sleep(10)
            s = req("GET", f"{GRAPH}/{cid}?fields=status_code,status&access_token={urllib.parse.quote(tok)}")
            status = s.get("status_code")
            if status in ("FINISHED", "ERROR", "EXPIRED"):
                break
        log("Processing status:", status)
        if status != "FINISHED":
            rc = 1
            continue
        if PREP:
            log("Prepare-only test passed: Instagram processed the full file. Not publishing.")
            continue
        wait = t - time.time()
        if wait > 0:
            log(f"Waiting {int(wait)}s until publish time")
            time.sleep(wait)
        p = req("POST", f"{GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok})
        if "id" not in p:
            log("Publish failed:", p.get("error", {}).get("message"))
            rc = 1
            continue
        posted[e["id"]] = {"media_id": p["id"], "published": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with open(POSTED, "w") as f:
            json.dump(posted, f, indent=1)
        log(f"Published {e['id']} as {p['id']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
