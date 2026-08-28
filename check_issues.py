"""
GitHub issue watcher -> Telegram push notifications.

Polls a configured list of repos for open issues matching given labels
(or all open issues, if no labels are set), and sends a Telegram message
the first time each issue is seen. Also checks for a pending `/clear`
command from you, to bulk-delete old notification messages.

Runs on a schedule via GitHub Actions, so notifications land within
~5 minutes of an issue appearing, not instantly.

Env vars:
    GH_TOKEN             - GitHub personal access token (read-only, public repos is enough)
    TELEGRAM_BOT_TOKEN   - token for your Telegram bot (from @BotFather)
    TELEGRAM_CHAT_ID     - your personal Telegram chat id
"""

import json
import os
from datetime import datetime, timezone, timedelta

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
GITHUB_TOKEN = os.environ.get("GH_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

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


def delete_telegram_message(message_id):
    url = f"{TELEGRAM_API}/deleteMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id},
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
        print(f"Could not poll Telegram updates: {e}")
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
                if delete_telegram_message(m["message_id"]):
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


def main():
    config = load_json(CONFIG_PATH, {"repos": []})
    state = load_json(
        STATE_PATH,
        {"repos": {}, "notified": [], "sent_messages": [], "telegram_update_offset": 0},
    )

    process_commands(state)

    notified = set(state.get("notified", []))
    sent_messages = state.setdefault("sent_messages", [])
    now = datetime.now(timezone.utc)

    for entry in config["repos"]:
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
                print(f"Error fetching {repo} [{label or 'all'}]: {e}")
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
                    print(f"Failed to notify {key}: {e}")
                    continue

                notified.add(key)
                if message_id:
                    sent_messages.append({
                        "message_id": message_id,
                        "key": key,
                        "sent_at": now.isoformat(),
                    })

        # Move the checkpoint forward, with overlap so we never miss
        # an issue that landed right at the boundary of two runs.
        repo_state["last_checked"] = (now - timedelta(minutes=OVERLAP_MINUTES)).isoformat()

    state["notified"] = list(notified)[-MAX_NOTIFIED_HISTORY:]
    state["sent_messages"] = sent_messages[-MAX_SENT_MESSAGE_HISTORY:]
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()