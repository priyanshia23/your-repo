import requests
import json
from datetime import datetime, timezone, timedelta
import time

# ==============================
# CONFIG
# ==============================

SLACK_TOKEN = "xoxb-1259594035652-10740645182023-sVPgUXYxX8gRElqYO2VsIkZ9"
AIRTABLE_TOKEN = "patMhjMqmVkMf0Gpc.b42cc2035d97a186f37b0c8b0b96c008966e7fb3f9008c44771486d7721eaf85"
BASE_ID = "apphLcvA4OO7gKjl9"
TABLE_NAME = "Slack Thread Trails 2 copy"
DATABASE_TABLE = "Database"
DATABASE_VIEW = "Grid view"
BATCH_SIZE = 10

# How often (in seconds) to re-poll for new messages during the day
POLL_INTERVAL_SECONDS = 300  # every 5 minutes

SLACK_HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}"}
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

IST = timezone(timedelta(hours=5, minutes=30))

# ==============================
# GET TODAY'S MIDNIGHT IN IST → UTC UNIX TIMESTAMP
# ==============================

def get_today_start_ts():
    """
    Returns Unix timestamp for today 00:00:00 IST (converted to UTC).
    This resets every calendar day in India.
    """
    now_ist = datetime.now(IST)
    today_midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_midnight_utc = today_midnight_ist.astimezone(timezone.utc)
    return today_midnight_utc.timestamp()


# ==============================
# DEDUPLICATION: TRACK SAVED THREADS IN MEMORY
# ==============================

# Key: (channel_id, thread_ts) → True
# Prevents re-saving the same thread on repeat polls within the same run
saved_threads = set()

def is_already_saved(channel_id, thread_ts):
    return (channel_id, thread_ts) in saved_threads

def mark_as_saved(channel_id, thread_ts):
    saved_threads.add((channel_id, thread_ts))


# ==============================
# CACHE FOR USER NAMES
# ==============================

user_cache = {}

def get_user_name(user_id):
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        res = requests.get(
            "https://slack.com/api/users.info",
            headers=SLACK_HEADERS,
            params={"user": user_id}
        )
        data = res.json()
        if data.get("ok"):
            user = data["user"]
            name = (
                user.get("real_name")
                or user.get("profile", {}).get("display_name")
                or user_id
            )
            user_cache[user_id] = name
            return name
    except Exception as e:
        print(f"User fetch error for {user_id}:", e)
    user_cache[user_id] = user_id
    return user_id


# ==============================
# FETCH ALL CHANNELS FROM DATABASE TABLE
# ==============================

def fetch_all_channel_ids():
    """
    Reads ALL channel IDs from the Database table (isFetched or not).
    For continuous sync, we want to poll ALL channels repeatedly,
    not skip already-processed ones. isFetched logic is for one-time backfill.
    Returns list of channel_id strings.
    """
    results = []
    url = f"https://api.airtable.com/v0/{BASE_ID}/{DATABASE_TABLE}"
    cursor = None

    while True:
        params = {
            "pageSize": 100,
            "fields[]": ["channelId"]
        }
        if cursor:
            params["offset"] = cursor

        res = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
        data = res.json()

        if "records" not in data:
            print("Airtable fetch error:", data)
            break

        for record in data["records"]:
            channel_id = record.get("fields", {}).get("channelId", "").strip()
            if channel_id:
                results.append(channel_id)

        cursor = data.get("offset")
        if not cursor:
            break

    return results


# ==============================
# CHECK IF BOT IS MEMBER OF CHANNEL
# ==============================

def is_bot_in_channel(channel_id):
    try:
        res = requests.get(
            "https://slack.com/api/conversations.info",
            headers=SLACK_HEADERS,
            params={"channel": channel_id}
        )
        data = res.json()
        if not data.get("ok"):
            print(f"  ⚠️  conversations.info error for {channel_id}: {data.get('error')}")
            return False
        return data.get("channel", {}).get("is_member", False)
    except Exception as e:
        print(f"  ⚠️  Exception checking membership for {channel_id}: {e}")
        return False


# ==============================
# GET MESSAGES IN CHANNEL FROM A GIVEN TIMESTAMP
# ==============================

def get_channel_messages(channel_id, from_ts):
    messages = []
    cursor = None

    while True:
        params = {
            "channel": channel_id,
            "oldest": from_ts,
            "limit": 200
        }
        if cursor:
            params["cursor"] = cursor

        res = requests.get(
            "https://slack.com/api/conversations.history",
            headers=SLACK_HEADERS,
            params=params
        )
        data = res.json()

        if not data.get("ok"):
            print(f"  History error for {channel_id}:", data.get("error"))
            break

        messages.extend(data.get("messages", []))

        if not data.get("has_more"):
            break

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return messages


# ==============================
# GET FULL THREAD REPLIES
# ==============================

def get_thread_replies(channel_id, thread_ts):
    res = requests.get(
        "https://slack.com/api/conversations.replies",
        headers=SLACK_HEADERS,
        params={"channel": channel_id, "ts": thread_ts}
    )
    data = res.json()
    if data.get("ok"):
        return data.get("messages", [])
    else:
        print(f"  Thread fetch error:", data.get("error"))
        return []


# ==============================
# BUILD SLACK LINK FOR THREAD
# ==============================

def build_slack_link(channel_id, thread_ts):
    ts_formatted = thread_ts.replace(".", "")
    return f"https://slack.com/archives/{channel_id}/p{ts_formatted}?thread_ts={thread_ts}&cid={channel_id}"


# ==============================
# BUILD CLEAN THREAD TRAIL
# ==============================

def build_thread_trail(channel_id, channel_name, root_message):
    thread_ts = root_message.get("ts")

    if root_message.get("reply_count", 0) > 0:
        replies = get_thread_replies(channel_id, thread_ts)
    else:
        replies = [root_message]

    trail = []
    all_participants = set()
    has_reactions = False
    all_reactions = []

    for index, msg in enumerate(replies):
        user_id = msg.get("user", "unknown")
        user_name = get_user_name(user_id)
        all_participants.add(user_name)

        msg_ts = msg.get("ts")
        msg_dt = datetime.utcfromtimestamp(float(msg_ts))

        msg_reactions = []
        for reaction in msg.get("reactions", []):
            has_reactions = True
            for uid in reaction.get("users", []):
                reaction_entry = {
                    "emoji": reaction.get("name"),
                    "reactedBy": get_user_name(uid),
                    "reactedById": uid
                }
                msg_reactions.append(reaction_entry)
                all_reactions.append({
                    "messageIndex": index + 1,
                    "messageBy": user_name,
                    "emoji": reaction.get("name"),
                    "reactedBy": get_user_name(uid)
                })

        trail.append({
            "index": index + 1,
            "datetime": msg_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "senderId": user_id,
            "senderName": user_name,
            "text": msg.get("text", ""),
            "reactions": msg_reactions,
            "isRootMessage": index == 0
        })

    root_user_id = root_message.get("user", "unknown")
    root_user_name = get_user_name(root_user_id)
    root_ts = root_message.get("ts")
    root_dt = datetime.utcfromtimestamp(float(root_ts))

    slack_link = build_slack_link(channel_id, thread_ts)

    return {
        "channelId": channel_id,
        "channelName": channel_name,
        "threadId": thread_ts,
        "slackLink": slack_link,
        "threadDate": root_dt.strftime("%Y-%m-%d"),
        "dayOfWeek": root_dt.strftime("%A"),
        "initialMessage": root_message.get("text", ""),
        "initialSenderId": root_user_id,
        "initialSenderName": root_user_name,
        "initialMessageTs": root_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "replyCount": max(len(replies) - 1, 0),
        "fullThreadTrail": json.dumps(trail, indent=2, ensure_ascii=False),
        "participants": ", ".join(all_participants),
        "hasReactions": has_reactions,
        "reactionsDetail": json.dumps(all_reactions, indent=2, ensure_ascii=False),
        "extractedAt": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    }


# ==============================
# SAVE TO AIRTABLE
# ==============================

def save_to_airtable(record):
    url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_NAME}"
    data = {"fields": record}
    res = requests.post(url, json=data, headers=AIRTABLE_HEADERS)
    if res.status_code == 200:
        print(f"    ✅ Saved thread {record['threadId']} from #{record['channelName']}")
    else:
        print(f"    ❌ Airtable error: {res.status_code}", res.json())


# ==============================
# PROCESS ALL CHANNELS ONCE
# ==============================

def process_all_channels(channel_ids, from_ts):
    new_count = 0
    skipped_not_member = 0
    skipped_no_messages = 0

    for channel_id in channel_ids:
        print(f"\n  Checking channel {channel_id} ...")

        if not is_bot_in_channel(channel_id):
            print(f"  ⛔ Bot not in channel {channel_id} — skipping")
            skipped_not_member += 1
            time.sleep(0.3)
            continue

        messages = get_channel_messages(channel_id, from_ts)

        if not messages:
            skipped_no_messages += 1
            continue

        print(f"  Found {len(messages)} messages")

        for root_msg in messages:
            if root_msg.get("subtype"):
                continue

            thread_ts = root_msg.get("ts")

            # Skip if we already saved this thread in this run
            if is_already_saved(channel_id, thread_ts):
                continue

            trail = build_thread_trail(channel_id, channel_id, root_msg)
            if trail:
                save_to_airtable(trail)
                mark_as_saved(channel_id, thread_ts)
                new_count += 1

            time.sleep(0.5)

        time.sleep(0.5)

    return new_count, skipped_not_member, skipped_no_messages


# ==============================
# MAIN RUNNER — CONTINUOUS DAILY SYNC
# ==============================

def run():
    print("=== Slack Thread Sync — Continuous Daily Mode ===")
    print(f"Timezone: IST (UTC+5:30)")
    print(f"Poll interval: every {POLL_INTERVAL_SECONDS // 60} minutes\n")

    # Load all channel IDs once at startup (re-fetched each new day)
    print("Loading all channel IDs from Airtable Database table...")
    channel_ids = fetch_all_channel_ids()
    print(f"Loaded {len(channel_ids)} channels.\n")

    last_day = None  # track IST calendar day to reset at midnight

    poll_number = 0

    while True:
        now_ist = datetime.now(IST)
        today_ist_str = now_ist.strftime("%Y-%m-%d")

        # ─── NEW DAY: reset deduplication set + reload channels ───
        if today_ist_str != last_day:
            print(f"\n{'='*50}")
            print(f"📅 New IST day detected: {today_ist_str}")
            print(f"  Resetting deduplication cache and reloading channels...")
            saved_threads.clear()
            channel_ids = fetch_all_channel_ids()
            print(f"  Loaded {len(channel_ids)} channels.\n")
            last_day = today_ist_str

        from_ts = get_today_start_ts()
        poll_number += 1

        print(f"\n{'─'*50}")
        print(f"🔄 Poll #{poll_number} — {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
        print(f"   Fetching messages from midnight IST ({datetime.utcfromtimestamp(from_ts).strftime('%Y-%m-%d %H:%M:%S')} UTC)")
        print(f"   Channels to scan: {len(channel_ids)}")

        new_count, skip_member, skip_msgs = process_all_channels(channel_ids, from_ts)

        print(f"\n  ✅ Poll #{poll_number} complete")
        print(f"     New threads saved       : {new_count}")
        print(f"     Skipped (not member)    : {skip_member}")
        print(f"     Skipped (no messages)   : {skip_msgs}")
        print(f"     Total in-memory tracked : {len(saved_threads)}")
        print(f"\n  ⏳ Next poll in {POLL_INTERVAL_SECONDS // 60} minutes...")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
