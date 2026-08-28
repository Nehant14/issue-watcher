"""
GitHub issue watcher -> ntfy push notifications.

Polls a configured list of repos for open issues matching given labels,
and sends a push notification (via ntfy.sh) the first time each issue
is seen. Designed to be run on a schedule by GitHub Actions.

Env vars:
    GH_TOKEN     - GitHub personal access token (read-only, public repos is enough)
    NTFY_TOPIC   - your private ntfy.sh topic name
"""

import json
import os
from datetime import datetime, timezone, timedelta

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
GITHUB_TOKEN = os.environ.get("GH_TOKEN")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# How far back to re-check on every run, to safely cover any gap
# between runs (covers Actions scheduling jitter/delays).
OVERLAP_MINUTES = 15
# How many notified-issue keys to remember, so state.json doesn't grow forever.
MAX_NOTIFIED_HISTORY = 2000


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


def send_ntfy(repo, issue, label=None):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, skipping notification")
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    title = f"New issue in {repo}"
    body = f"[{label}] {issue['title']}" if label else issue["title"]
    headers = {
        "Title": title,
        "Click": issue["html_url"],
        "Priority": "high",
        "Tags": "rotating_light",
    }
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=15)
    r.raise_for_status()


def main():
    config = load_json(CONFIG_PATH, {"repos": []})
    state = load_json(STATE_PATH, {"repos": {}, "notified": []})
    notified = set(state.get("notified", []))
    now = datetime.now(timezone.utc)

    for entry in config["repos"]:
        repo = entry["repo"]
        labels = entry.get("labels", [])
        repo_state = state["repos"].setdefault(repo, {})
        last_checked = repo_state.get("last_checked")
        # First-ever run for a repo: only look back 24h so you don't get
        # flooded with every old matching issue.
        since = last_checked or (now - timedelta(hours=24)).isoformat()

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
                try:
                    send_ntfy(repo, issue, label)
                    print(f"Notified: {key} - {issue['title']}")
                except requests.HTTPError as e:
                    print(f"Failed to notify {key}: {e}")
                    continue
                notified.add(key)

        # Move the checkpoint forward, with overlap so we never miss
        # an issue that landed right at the boundary of two runs.
        repo_state["last_checked"] = (now - timedelta(minutes=OVERLAP_MINUTES)).isoformat()

    state["notified"] = list(notified)[-MAX_NOTIFIED_HISTORY:]
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
