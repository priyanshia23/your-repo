import os
import json
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==============================
# CONFIG — set these as env vars on Render
# ==============================

SLACK_TOKEN    = os.environ.get("SLACK_TOKEN", "xoxb-1259594035652-10740645182023-sVPgUXYxX8gRElqYO2VsIkZ9")
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "patMhjMqmVkMf0Gpc.b42cc2035d97a186f37b0c8b0b96c008966e7fb3f9008c44771486d7721eaf85")
BASE_ID        = os.environ.get("AIRTABLE_BASE_ID", "apphLcvA4OO7gKjl9")
TABLE_NAME     = os.environ.get("AIRTABLE_TABLE_NAME", "Slack Thread Trails 2 copy copy")

SLACK_HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}"}
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

IST = timezone(timedelta(hours=5, minutes=30))

# ==============================
# USER NAME CACHE
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
# GET CHANNEL NAME
# ==============================

def get_channel_name(channel_id):
    try:
        res = requests.get(
            "https://slack.com/api/conversations.info",
            headers=SLACK_HEADERS,
            params={"channel": channel_id}
        )
        data = res.json()
        if data.get("ok"):
            return data["channel"].get("name", channel_id)
    except Exception as e:
        print(f"Channel fetch error for {channel_id}:", e)
    return channel_id


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
    print(f"Thread fetch error: {data.get('error')}")
    return []


# ==============================
# BUILD SLACK LINK
# ==============================

def build_slack_link(channel_id, thread_ts):
    ts_formatted = thread_ts.replace(".", "")
    return f"https://slack.com/archives/{channel_id}/p{ts_formatted}?thread_ts={thread_ts}&cid={channel_id}"


# ==============================
# BUILD THREAD TRAIL RECORD
# ==============================

def build_thread_trail(channel_id, channel_name, thread_ts):
    replies = get_thread_replies(channel_id, thread_ts)

    if not replies:
        return None

    root_message = replies[0]
    trail = []
    all_participants = set()
    has_reactions = False
    all_reactions = []

    for index, msg in enumerate(replies):
        user_id = msg.get("user", "unknown")
        user_name = get_user_name(user_id)
        all_participants.add(user_name)

        msg_ts = msg.get("ts")
        msg_dt = datetime.fromtimestamp(float(msg_ts), tz=IST)

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
            "datetime": msg_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
            "senderId": user_id,
            "senderName": user_name,
            "text": msg.get("text", ""),
            "reactions": msg_reactions,
            "isRootMessage": index == 0
        })

    root_user_id = root_message.get("user", "unknown")
    root_user_name = get_user_name(root_user_id)
    root_dt = datetime.fromtimestamp(float(thread_ts), tz=IST)

    return {
        "channelId": channel_id,
        "channelName": channel_name,
        "threadId": thread_ts,
        "slackLink": build_slack_link(channel_id, thread_ts),
        "threadDate": root_dt.strftime("%Y-%m-%d"),
        "dayOfWeek": root_dt.strftime("%A"),
        "initialMessage": root_message.get("text", ""),
        "initialSenderId": root_user_id,
        "initialSenderName": root_user_name,
        "initialMessageTs": root_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
        "replyCount": max(len(replies) - 1, 0),
        "fullThreadTrail": json.dumps(trail, indent=2, ensure_ascii=False),
        "participants": ", ".join(all_participants),
        "hasReactions": has_reactions,
        "reactionsDetail": json.dumps(all_reactions, indent=2, ensure_ascii=False),
        "extractedAt": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    }


# ==============================
# FIND EXISTING AIRTABLE RECORD BY threadId
# ==============================

def find_airtable_record(thread_id):
    url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_NAME}"
    params = {
        "filterByFormula": f'{{threadId}}="{thread_id}"',
        "maxRecords": 1
    }
    res = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
    data = res.json()
    records = data.get("records", [])
    if records:
        return records[0]["id"]
    return None


# ==============================
# UPSERT TO AIRTABLE (create or update)
# ==============================

def upsert_to_airtable(record):
    thread_id = record["threadId"]
    existing_record_id = find_airtable_record(thread_id)

    if existing_record_id:
        url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_NAME}/{existing_record_id}"
        res = requests.patch(url, json={"fields": record}, headers=AIRTABLE_HEADERS)
        if res.status_code == 200:
            print(f"  🔄 Updated thread {thread_id} in #{record['channelName']}")
        else:
            print(f"  ❌ Update error: {res.status_code}", res.json())
    else:
        url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_NAME}"
        res = requests.post(url, json={"fields": record}, headers=AIRTABLE_HEADERS)
        if res.status_code == 200:
            print(f"  ✅ Created thread {thread_id} in #{record['channelName']}")
        else:
            print(f"  ❌ Create error: {res.status_code}", res.json())


# ==============================
# SLACK WEBHOOK ENDPOINT
# ==============================

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json

    # Slack URL verification challenge (one-time on setup)
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    event_type = event.get("type")

    # Only handle message events
    if event_type not in ("message", "message_replied"):
        return jsonify({"status": "ignored"})

    # Skip bot messages, edits, deletes
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_changed", "message_deleted"):
        return jsonify({"status": "ignored"})

    channel_id = event.get("channel")
    if not channel_id:
        return jsonify({"status": "no channel"})

    # For replies, thread_ts points to root. For root messages, use ts.
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not thread_ts:
        return jsonify({"status": "no ts"})

    print(f"\n📨 Event: {event_type} | Channel: {channel_id} | Thread: {thread_ts}")

    channel_name = get_channel_name(channel_id)
    trail = build_thread_trail(channel_id, channel_name, thread_ts)

    if trail:
        upsert_to_airtable(trail)
    else:
        print(f"  ⚠️  Could not build thread trail for {thread_ts}")

    return jsonify({"status": "ok"})


# ==============================
# HEALTH CHECK
# ==============================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


# ==============================
# ENTRY POINT
# ==============================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
