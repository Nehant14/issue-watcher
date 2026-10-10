"""
GitHub issue watcher -> Telegram push notifications  (standard + special repos)

STANDARD repos (the original behaviour, unchanged):
  {"repo": "owner/name", "labels": ["good first issue"]}
  Polls open issues matching the labels (or all open issues if no labels) and
  notifies once per issue. Also catches old issues that just got the label.

SPECIAL repos (opt-in per repo, for rare/contested issues such as Zulip help-wanted):
  {"repo": "zulip/zulip", "preset": "zulip"}
  or inline: {"repo": "x/y", "special": {"labels": ["help wanted"], ...}}
  Watches the repo's label-EVENT stream, so an old issue relabeled today alerts exactly
  like a brand-new one. Only alerts for unassigned issues, also when a claim is dropped,
  and uses a loud banner card (+ optional Pushover emergency alert). With "repeat": 3 the
  same issue is sent as 3 messages in a row so your phone buzzes three times.

Both kinds live in one config.json / state.json / workflow. A failure in one repo never
stops the others. `/clear` in Telegram removes alerts of both kinds.

Usage:
  python check_issues.py              one pass (GitHub Actions / cron)
  python check_issues.py --loop 45    always-on host: poll every 45 s
  python check_issues.py --digest     list every open unclaimed special-repo issue

Env vars:
  GH_TOKEN             GitHub token (read-only public repos is enough)
  TELEGRAM_BOT_TOKEN   Telegram bot token (from @BotFather)
  TELEGRAM_CHAT_ID     your personal Telegram chat id
  SPECIAL_CHAT_ID      optional: send SPECIAL alerts to a different chat/group your bot is in
                       (lets you give them their own ringtone)
  PUSHOVER_TOKEN, PUSHOVER_USER   optional: loud repeating alert for presets with "pushover": true
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
GITHUB_TOKEN = os.environ.get("GH_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
SPECIAL_CHAT_ID = os.environ.get("SPECIAL_CHAT_ID") or TELEGRAM_CHAT_ID
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")

# How far back to re-check on every run, to safely cover any gap
# between runs (covers Actions scheduling jitter/delays).
OVERLAP_MINUTES = 15
# How many notified-issue keys / sent-message records to remember.
MAX_NOTIFIED_HISTORY = 2000
MAX_SENT_MESSAGE_HISTORY = 2000

# Emoji shown per GitHub label, matched by substring (case-insensitive).
# Falls back to a generic tag emoji if nothing matches.
LABEL_EMOJI = [
    ("good first issue", "🌱"),
    ("help wanted", "🙋"),
    ("bug", "🐞"),
    ("triage", "🚦"),
    ("feature", "✨"),
    ("enhancement", "✨"),
    ("documentation", "📄"),
    ("doc", "📄"),
    ("question", "❓"),
    ("duplicate", "♻️"),
    ("wontfix", "🚫"),
    ("security", "🔒"),
]


def label_emoji(name):
    lower = name.lower()
    for keyword, emoji in LABEL_EMOJI:
        if keyword in lower:
            return emoji
    return "🏷️"


def parse_iso(ts):
    """Parse a GitHub/Telegram ISO 8601 timestamp (handles trailing 'Z')."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def fetch_issues(repo, since, label=None):
    """Fetch open issues in `repo`, updated at/after `since`.

    If `label` is None, no label filter is applied (i.e. ALL open issues).
    """
    url = f"https://api.github.com/repos/{repo}/issues"
    params = {
        "state": "open",
        "sort": "updated",
        "direction": "desc",
        "since": since,
        "per_page": 50,
    }
    if label:
        params["labels"] = label
    resp = requests.get(url, headers=github_headers(), params=params, timeout=15)
    resp.raise_for_status()
    # The issues endpoint also returns pull requests; filter those out.
    return [i for i in resp.json() if "pull_request" not in i]


def label_recently_added(repo, issue_number, label, since_dt):
    """Check whether `label` was attached to this issue at/after since_dt.

    Used to catch OLD issues that just got labeled (e.g. a maintainer
    tags a years-old issue as 'good first issue' for contributors),
    which the created_at check alone would otherwise miss.
    """
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/events"
    try:
        resp = requests.get(
            url, headers=github_headers(), params={"per_page": 100}, timeout=15
        )
        resp.raise_for_status()
    except requests.HTTPError:
        return False
    for event in resp.json():
        if event.get("event") != "labeled":
            continue
        event_label = (event.get("label") or {}).get("name")
        if event_label != label:
            continue
        if parse_iso(event["created_at"]) >= since_dt:
            return True
    return False


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(repo, issue, reason):
    """Build a clean, card-like Telegram message (HTML parse mode)."""
    header_emoji = "🆕" if reason == "new" else "🏷️"
    header_text = "New Issue" if reason == "new" else "Older Issue, Just Labeled"

    title = escape_html(issue["title"])
    issue_labels = [l["name"] for l in issue.get("labels", [])]

    if issue_labels:
        tags = "  ".join(f"{label_emoji(n)} {escape_html(n)}" for n in issue_labels)
        tags_line = f"\n{tags}"
    else:
        tags_line = "\n<i>No labels</i>"

    text = (
        f"{header_emoji} <b>{header_text}</b>\n"
        f"<code>{escape_html(repo)}</code> · #{issue['number']}\n\n"
        f"<b>{title}</b>"
        f"{tags_line}"
    )
    return text


def send_telegram(repo, issue, reason="new"):
    """Send the notification. Returns the sent message_id, or None."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping notification")
        return None
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": build_message(repo, issue, reason),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Open Issue →", "url": issue["html_url"]}
            ]]
        },
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def delete_telegram_message(message_id, chat_id=None):
    url = f"{TELEGRAM_API}/deleteMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id or TELEGRAM_CHAT_ID, "message_id": message_id},
            timeout=15,
        )
        data = resp.json()
        return bool(data.get("ok"))
    except requests.RequestException:
        return False


def process_commands(state):
    """Poll for any pending /clear command and act on it.

    Usage:
      /clear          -> deletes ALL tracked notification messages
      (reply) /clear  -> deletes all tracked messages sent BEFORE the
                          message you replied to (that one and anything
                          newer is kept)

    Note: Telegram only allows bots to delete their own messages that
    are less than 48 hours old; older ones will just be skipped.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    offset = state.get("telegram_update_offset", 0)
    try:
        resp = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=15,
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except requests.RequestException as e:
        print(f"Could not poll Telegram updates: {safe_err(e)}")
        return

    sent_messages = state.setdefault("sent_messages", [])

    for update in updates:
        state["telegram_update_offset"] = update["update_id"] + 1
        message = update.get("message")
        if not message:
            continue
        if str(message.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue  # ignore anyone other than you
        text = (message.get("text") or "").strip()
        if not text.startswith("/clear"):
            continue

        reply_to = message.get("reply_to_message")
        cutoff_id = reply_to["message_id"] if reply_to else None

        to_delete = [
            m for m in sent_messages
            if cutoff_id is None or m["message_id"] < cutoff_id
        ]

        deleted = 0
        still_present = []
        for m in sent_messages:
            if m in to_delete:
                if delete_telegram_message(m["message_id"], m.get("chat_id")):
                    deleted += 1
                else:
                    still_present.append(m)  # too old (>48h) or already gone
            else:
                still_present.append(m)
        state["sent_messages"] = still_present

        # Clean up the /clear command message itself.
        delete_telegram_message(message["message_id"])

        confirm_text = (
            f"🧹 Cleared {deleted} message(s)."
            if deleted
            else "Nothing to clear (or messages were too old to delete)."
        )
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": confirm_text},
            timeout=15,
        )


# =====================================================================
#  STANDARD-REPO MODE  (original logic, moved out of main() unchanged)
# =====================================================================
def run_standard_repo(entry, state, notified, notified_list, sent_messages, now):
    repo = entry["repo"]
    labels = entry.get("labels", [])
    repo_state = state["repos"].setdefault(repo, {})
    last_checked = repo_state.get("last_checked")

    # First-ever run for a repo: only look back 24h so you don't get
    # flooded with every old matching issue.
    since = last_checked or (now - timedelta(hours=24)).isoformat()
    since_dt = parse_iso(since)

    # If no labels are configured, watch ALL open issues (one pass,
    # label=None). Otherwise, run one pass per label.
    label_passes = labels if labels else [None]

    for label in label_passes:
        try:
            issues = fetch_issues(repo, since, label=label)
        except requests.HTTPError as e:
            print(f"Error fetching {repo} [{label or 'all'}]: {safe_err(e)}")
            continue

        for issue in issues:
            key = f"{repo}#{issue['number']}"
            if key in notified:
                continue

            is_new = parse_iso(issue["created_at"]) >= since_dt
            reason = "new"

            if not is_new:
                # Not brand-new. If we're filtering by a specific
                # label, it might still be worth flagging if that
                # label was JUST added to this older issue.
                if label and label_recently_added(repo, issue["number"], label, since_dt):
                    reason = "labeled"
                else:
                    continue

            try:
                message_id = send_telegram(repo, issue, reason)
                print(f"Notified ({reason}): {key} - {issue['title']}")
            except requests.HTTPError as e:
                print(f"Failed to notify {key}: {safe_err(e)}")
                continue

            notified.add(key)
            notified_list.append(key)
            if message_id:
                sent_messages.append({
                    "message_id": message_id,
                    "key": key,
                    "sent_at": now.isoformat(),
                })

    # Move the checkpoint forward, with overlap so we never miss
    # an issue that landed right at the boundary of two runs.
    repo_state["last_checked"] = (now - timedelta(minutes=OVERLAP_MINUTES)).isoformat()


# =====================================================================
#  SPECIAL-REPO MODE
#  A repo entry with "preset": "<name>" or "special": {...} is handled
#  here instead of by the standard path above. Everything else in
#  config.json keeps working exactly as before.
# =====================================================================
SPECIAL_MAX_EVENT_PAGES = 5


def safe_err(e):
    """Describe an error WITHOUT printing URLs (Telegram URLs contain the bot token)."""
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"{type(e).__name__} (HTTP {resp.status_code})"
    return type(e).__name__


def special_opts(entry, config):
    """Return the options dict for a special repo, or None for a standard repo."""
    preset = entry.get("preset")
    inline = entry.get("special")
    if not preset and not inline:
        return None
    opts = {}
    if preset:
        presets = config.get("presets", {})
        if preset not in presets:
            raise ValueError(f"unknown preset '{preset}'")
        opts.update(presets[preset])
    if isinstance(inline, dict):
        opts.update(inline)
    return opts


def sp_labels(opts):
    return [l.lower() for l in opts.get("labels", ["help wanted"])]


def sp_claimable(issue, labels, unassigned_only):
    """Open issue (not a PR) that still carries one of the labels and, optionally, has no assignee."""
    names = {l["name"].lower() for l in issue.get("labels", [])}
    if "pull_request" in issue or issue.get("state") != "open":
        return False
    if not names & set(labels):
        return False
    if unassigned_only and (issue.get("assignee") or issue.get("assignees")):
        return False
    return True


def sp_fetch_candidates(repo, opts, since_dt, etag):
    """Return (candidates, new_etag); candidates = [(reason, event_id, issue)], one per issue.

    Reads the repo's issue-EVENT stream, so "old issue, label added today" is the same
    thing as "new issue with the label": both are a `labeled` event inside the window.
    Raises requests.RequestException on failure.
    """
    labels = sp_labels(opts)
    unassigned_only = opts.get("unassigned_only", True)
    notify_freed = opts.get("notify_freed", True)
    url = f"https://api.github.com/repos/{repo}/issues/events"
    found, new_etag = {}, etag

    for page in range(1, SPECIAL_MAX_EVENT_PAGES + 1):
        headers = github_headers()
        if page == 1 and etag:
            headers["If-None-Match"] = etag
        r = requests.get(url, headers=headers, params={"per_page": 100, "page": page}, timeout=20)
        if page == 1 and r.status_code == 304:
            return [], etag
        r.raise_for_status()
        if page == 1:
            new_etag = r.headers.get("ETag", etag)

        events = r.json()
        if not events:
            break
        newest_first = parse_iso(events[0]["created_at"]) >= parse_iso(events[-1]["created_at"])
        if not newest_first:
            print(f"[warn] {repo}: events came back oldest-first; may miss recent ones")

        for e in events:
            if parse_iso(e["created_at"]) < since_dt:
                continue
            kind = e.get("event")
            issue = e.get("issue") or {}
            if kind == "labeled":
                if (e.get("label") or {}).get("name", "").lower() not in labels:
                    continue
                reason = "labeled"
            elif kind == "unassigned" and notify_freed:
                reason = "freed"
            else:
                continue
            if not sp_claimable(issue, labels, unassigned_only):
                continue
            found.setdefault(issue["number"], (reason, e["id"], issue))

        if newest_first and parse_iso(events[-1]["created_at"]) < since_dt:
            break
        if len(events) < 100:
            break

    return list(found.values()), new_etag


def sp_build_message(repo, issue, reason, opts):
    name = esc_html_safe(opts.get("banner") or repo.split("/")[0].upper())
    label_txt = esc_html_safe(" / ".join(opts.get("labels", ["help wanted"])).upper())
    if reason == "labeled":
        banner = f"🚨🚨 <b>{name} · {label_txt}</b> 🚨🚨"
        sub = "Just labeled — unclaimed" if opts.get("unassigned_only", True) else "Just labeled"
    else:
        banner = f"♻️🚨 <b>{name} · {label_txt} IS FREE AGAIN</b> 🚨♻️"
        sub = "A claim was dropped — unclaimed"

    body = re.sub(r"<!--.*?-->", "", issue.get("body") or "", flags=re.S)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > 300:
        body = body[:297] + "…"

    tags = " ".join(f"{label_emoji(l['name'])} {esc_html_safe(l['name'])}" for l in issue.get("labels", []))
    lines = [
        banner,
        f"<i>{sub}</i>",
        "",
        f"<code>{esc_html_safe(repo)}</code> · #{issue['number']}",
        f"<b>{esc_html_safe(issue['title'])}</b>",
        "",
        tags or "<i>No labels</i>",
        f"🕒 opened {human_age(issue['created_at'])} · 💬 {issue.get('comments', 0)} comments",
    ]
    if body:
        lines += ["", f"<i>{esc_html_safe(body)}</i>"]
    if opts.get("claim_text"):
        lines += ["", "Tap to copy, then paste as your comment 👇",
                  f"<code>{esc_html_safe(opts['claim_text'])}</code>"]
    return "\n".join(lines)


def esc_html_safe(text):
    return html.escape(text or "", quote=False)


def human_age(ts):
    secs = int((datetime.now(timezone.utc) - parse_iso(ts)).total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 1)} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def sp_send_pushover(repo, issue, reason, opts):
    if not (opts.get("pushover") and PUSHOVER_TOKEN and PUSHOVER_USER):
        return False
    name = opts.get("banner") or repo.split("/")[0].upper()
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                "title": f"🚨 {name}" + (" (freed)" if reason == "freed" else ""),
                "message": f"{repo}#{issue['number']}: {issue['title']}",
                "url": issue["html_url"], "url_title": "Open issue",
                "priority": 2, "retry": 30, "expire": 1800, "sound": "siren",
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Pushover failed: {safe_err(e)}")
        return False


def sp_send_telegram(repo, issue, reason, opts, text=None):
    """Returns (message_id, chat_id). Raises on failure."""
    chat_id = SPECIAL_CHAT_ID
    text = text or sp_build_message(repo, issue, reason, opts)
    url = issue["html_url"]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "🔥 Open issue", "url": url},
            {"text": "💬 Comment box", "url": url + "#issuecomment-new"},
        ]]},
    }
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
    if r.status_code == 400:   # formatting problem: resend plain instead of losing the alert
        payload.pop("parse_mode")
        payload["text"] = re.sub(r"<[^>]+>", "", html.unescape(text))
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["result"]["message_id"], chat_id


def sp_build_reminder(repo, issue, reason, opts, n, total):
    """Short follow-up message so the phone buzzes again: '2/3 ... still unclaimed'."""
    name = esc_html_safe(opts.get("banner") or repo.split("/")[0].upper())
    lines = [
        f"🔔🔔 <b>{n}/{total} · {name}</b> — still unclaimed",
        f"<code>{esc_html_safe(repo)}</code> · #{issue['number']}",
        f"<b>{esc_html_safe(issue['title'])}</b>",
    ]
    if opts.get("claim_text"):
        lines += ["", f"<code>{esc_html_safe(opts['claim_text'])}</code>"]
    return "\n".join(lines)


def sp_alert(repo, issue, reason, opts):
    """Send the full alert, then (if "repeat" > 1) short reminders one after another.

    Returns (delivered, [(message_id, chat_id), ...]). The alert counts as delivered as soon
    as the FIRST message (or the Pushover alert) got through; a failed reminder is just
    logged, so it can never cause the whole alert to be re-sent.
    """
    delivered, sent = False, []
    if sp_send_pushover(repo, issue, reason, opts):
        delivered = True
    if not (TELEGRAM_BOT_TOKEN and SPECIAL_CHAT_ID):
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping notification")
        return delivered, sent

    total = max(1, int(opts.get("repeat", 1)))
    delay = opts.get("repeat_delay_seconds", 3)        # Telegram allows ~1 msg/sec per chat
    for n in range(1, total + 1):
        try:
            text = None if n == 1 else sp_build_reminder(repo, issue, reason, opts, n, total)
            sent.append(sp_send_telegram(repo, issue, reason, opts, text))
            delivered = True
        except requests.RequestException as e:
            print(f"Telegram failed ({n}/{total}): {safe_err(e)}")
            if n == 1:
                break                                   # nothing got through; stop here
        if n < total:
            time.sleep(delay)
    return delivered, sent


def run_special_repo(entry, opts, state, notified, notified_list, sent_messages, now):
    repo = entry["repo"]
    rs = state["repos"].setdefault(repo, {})
    lookback = timedelta(hours=opts.get("lookback_hours", 24))
    overlap = timedelta(minutes=opts.get("overlap_minutes", 10))
    since_dt = parse_iso(rs["last_checked"]) if rs.get("last_checked") else now - lookback

    try:
        candidates, new_etag = sp_fetch_candidates(repo, opts, since_dt, rs.get("events_etag"))
    except requests.RequestException as e:
        print(f"Error fetching {repo} [special]: {safe_err(e)}")
        return                                  # checkpoint NOT advanced -> retried next run

    ok = True
    for reason, event_id, issue in candidates:
        key = f"{repo}#{issue['number']}:{reason}:{event_id}"   # never collides with standard keys
        if key in notified:
            continue
        delivered, sent_ids = sp_alert(repo, issue, reason, opts)
        if not delivered:
            ok = False                          # retry next run
            continue
        print(f"Notified (special {reason}): {repo}#{issue['number']} - {issue['title']}")
        notified.add(key)
        notified_list.append(key)
        for message_id, chat_id in sent_ids:           # all of them, so /clear removes every one
            sent_messages.append({"message_id": message_id, "chat_id": chat_id,
                                  "key": key, "sent_at": now.isoformat()})

    if ok:
        rs["last_checked"] = (now - overlap).isoformat()
        if new_etag:
            rs["events_etag"] = new_etag
    else:
        rs.pop("events_etag", None)             # force a full re-read next time


def sp_fetch_open_unclaimed(repo, opts):
    out = {}
    for label in opts.get("labels", ["help wanted"]):
        for page in range(1, 6):
            params = {"state": "open", "labels": label, "sort": "created",
                      "direction": "desc", "per_page": 100, "page": page}
            if opts.get("unassigned_only", True):
                params["assignee"] = "none"
            r = requests.get(f"https://api.github.com/repos/{repo}/issues",
                             headers=github_headers(), params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            for i in data:
                if "pull_request" not in i:
                    out[i["number"]] = i
            if len(data) < 100:
                break
    return list(out.values())


def run_digest(config):
    lines = []
    for entry in config["repos"]:
        try:
            opts = special_opts(entry, config)
            if opts is None:
                continue
            for i in sp_fetch_open_unclaimed(entry["repo"], opts):
                lines.append(f'• <a href="{i["html_url"]}">{esc_html_safe(entry["repo"])}#{i["number"]}</a> '
                             f'{esc_html_safe(i["title"])} <i>({human_age(i["created_at"])})</i>')
        except Exception as e:
            print(f"Digest error {entry.get('repo')}: {safe_err(e)}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n".join(re.sub(r"<[^>]+>", "", l) for l in lines))
        return
    text = f"📋 <b>Unclaimed special-repo issues: {len(lines)}</b>\n\n"
    text += "\n".join(lines) if lines else "None right now."
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": SPECIAL_CHAT_ID, "text": text[:3900], "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": True,
    }, timeout=20).raise_for_status()


# =====================================================================
#  MAIN
# =====================================================================
def run_once(config):
    state = load_json(
        STATE_PATH,
        {"repos": {}, "notified": [], "sent_messages": [], "telegram_update_offset": 0},
    )
    state.setdefault("repos", {})

    process_commands(state)

    notified_list = list(state.get("notified", []))
    notified = set(notified_list)
    sent_messages = state.setdefault("sent_messages", [])
    now = datetime.now(timezone.utc)

    for entry in config["repos"]:
        # One repo failing must never stop the others.
        try:
            opts = special_opts(entry, config)
            if opts is None:
                run_standard_repo(entry, state, notified, notified_list, sent_messages, now)
            else:
                run_special_repo(entry, opts, state, notified, notified_list, sent_messages, now)
        except Exception as e:
            print(f"Skipped {entry.get('repo', '?')}: {safe_err(e)} {e if isinstance(e, ValueError) else ''}")

    state["notified"] = notified_list[-MAX_NOTIFIED_HISTORY:]
    state["sent_messages"] = sent_messages[-MAX_SENT_MESSAGE_HISTORY:]
    save_json(STATE_PATH, state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true",
                    help="send a list of all open, unclaimed issues for special repos")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="poll forever every N seconds (always-on host)")
    args = ap.parse_args()

    config = load_json(CONFIG_PATH, {"repos": []})
    if args.digest:
        run_digest(config)
        return

    while True:
        try:
            run_once(config)
        except Exception as e:
            print(f"Run failed: {safe_err(e)}")
            if not args.loop:
                raise
        if not args.loop:
            break
        time.sleep(max(args.loop, 20))


if __name__ == "__main__":
    sys.exit(main())