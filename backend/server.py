# Bus Alert backend (Python / Flask rewrite)
#
# Same behaviour as the Node version:
#  - one global session ("work" or "home" or none)
#  - while active, polls tfi-gtfs every POLL_INTERVAL_SEC for the configured
#    stop, filters to ROUTE_ID, tracks the next upcoming arrival
#  - a new expected time is only "confirmed" (logged + push-notified) after
#    it has shown up on CONFIRM_AFTER_POLLS consecutive polls in a row
#  - auto-stops after SESSION_MAX_SEC
#
# Run with a SINGLE worker process (state lives in memory):
#   gunicorn -w 1 -b 0.0.0.0:$PORT server:app
# or for local testing:
#   python server.py

import os
import json
import time
import threading
from datetime import datetime

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

TFI_GTFS_URL = os.getenv("TFI_GTFS_URL", "http://localhost:7341")
ROUTE_ID = os.getenv("ROUTE_ID", "A2")
WORK_STOP_ID = os.getenv("WORK_STOP_ID")
HOME_STOP_ID = os.getenv("HOME_STOP_ID")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:example@example.com")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "10"))
CONFIRM_AFTER_POLLS = int(os.getenv("CONFIRM_AFTER_POLLS", "3"))
SESSION_MAX_SEC = int(os.getenv("SESSION_MAX_SEC", str(30 * 60)))

STOPS = {"work": WORK_STOP_ID, "home": HOME_STOP_ID}

# --- single-user in-memory state --------------------------------------------
lock = threading.Lock()
session = None            # dict, see start() for shape
push_subscription = None
stop_event = None
worker_thread = None


def now_ms():
    return int(time.time() * 1000)


def parse_iso(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_arrivals(stop_id):
    resp = requests.get(
        f"{TFI_GTFS_URL}/api/v1/arrivals",
        params={"stop": stop_id},
        headers={"Accept": "application/json"},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    stop_data = data.get(str(stop_id))
    if not stop_data:
        raise RuntimeError(f"No data returned for stop {stop_id}")
    return stop_data.get("arrivals", [])


def pick_next_relevant(arrivals):
    now = datetime.now().astimezone()
    candidates = []
    for a in arrivals:
        if a.get("route") != ROUTE_ID:
            continue
        expected = parse_iso(a.get("real_time_arrival") or a.get("scheduled_arrival"))
        scheduled = parse_iso(a.get("scheduled_arrival"))
        if expected and expected > now:
            candidates.append((expected, scheduled, a))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    expected, scheduled, a = candidates[0]
    return {
        "route": a.get("route"),
        "headsign": a.get("headsign"),
        "expected_ms": int(expected.timestamp() * 1000),
        "scheduled_ms": int(scheduled.timestamp() * 1000) if scheduled else None,
    }


def send_push(title, body):
    if not push_subscription or not VAPID_PRIVATE_KEY:
        return
    try:
        webpush(
            subscription_info=push_subscription,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
    except WebPushException as e:
        print("Push failed:", repr(e))


def poll_once():
    with lock:
        s = session
    if not s:
        return

    try:
        arrivals = fetch_arrivals(s["stop_id"])
        next_arr = pick_next_relevant(arrivals)
    except Exception as e:
        with lock:
            if session is s:
                session["last_error"] = str(e)
        return

    with lock:
        if session is not s:
            return  # session changed/stopped while we were fetching
        session["last_error"] = None

        if not next_arr:
            session["last_poll"] = None
            return

        diff_minutes = 0
        if next_arr["scheduled_ms"]:
            diff_minutes = round((next_arr["expected_ms"] - next_arr["scheduled_ms"]) / 60000)

        session["last_poll"] = {
            "route": next_arr["route"],
            "headsign": next_arr["headsign"],
            "scheduled": next_arr["scheduled_ms"],
            "expected": next_arr["expected_ms"],
            "diffMinutes": diff_minutes,
            "polledAt": now_ms(),
        }

        if session["baseline_expected"] is None:
            session["baseline_expected"] = next_arr["expected_ms"]
            session["pending_expected"] = next_arr["expected_ms"]
            session["pending_count"] = 1
            return

        if next_arr["expected_ms"] == session["pending_expected"]:
            session["pending_count"] += 1
        else:
            session["pending_expected"] = next_arr["expected_ms"]
            session["pending_count"] = 1

        if (
            session["pending_count"] >= CONFIRM_AFTER_POLLS
            and session["pending_expected"] != session["baseline_expected"]
        ):
            event = {
                "time": now_ms(),
                "previous": session["baseline_expected"],
                "updated": session["pending_expected"],
                "diffMinutes": round(
                    (session["pending_expected"] - session["baseline_expected"]) / 60000
                ),
            }
            session["history"].append(event)
            session["baseline_expected"] = session["pending_expected"]
            mode = session["mode"]

        else:
            event = None

    # send the push outside the lock (network call)
    if event:
        direction = "delayed" if event["diffMinutes"] > 0 else "earlier"
        mag = abs(event["diffMinutes"])
        prev_t = datetime.fromtimestamp(event["previous"] / 1000).strftime("%H:%M")
        new_t = datetime.fromtimestamp(event["updated"] / 1000).strftime("%H:%M")
        send_push(
            f"{ROUTE_ID} bus now {direction} {mag} min",
            f"Was {prev_t}, now {new_t} at your {mode} stop.",
        )


def worker_loop(evt):
    while not evt.is_set():
        poll_once()
        with lock:
            active = session is not None
            expires = session["expires_at"] if session else None
        if active and now_ms() >= expires:
            stop_session_internal()
            break
        evt.wait(POLL_INTERVAL_SEC)


def stop_session_internal():
    global session, stop_event, worker_thread
    with lock:
        session = None
    if stop_event:
        stop_event.set()
    stop_event = None
    worker_thread = None


# --- routes -------------------------------------------------------------

@app.route("/api/vapid-public-key")
def vapid_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})


@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    global push_subscription
    push_subscription = (request.get_json(force=True) or {}).get("subscription")
    return jsonify({"ok": True})


@app.route("/api/session/status")
def status():
    with lock:
        if not session:
            return jsonify({"active": False})
        remaining = max(0, round((session["expires_at"] - now_ms()) / 1000))
        return jsonify(
            {
                "active": True,
                "mode": session["mode"],
                "startedAt": session["started_at"],
                "expiresAt": session["expires_at"],
                "secondsRemaining": remaining,
                "lastPoll": session["last_poll"],
                "history": session["history"],
                "lastError": session["last_error"],
            }
        )


@app.route("/api/session/start", methods=["POST"])
def start():
    global session, stop_event, worker_thread

    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode not in ("work", "home"):
        return jsonify({"error": "mode must be 'work' or 'home'"}), 400

    stop_id = STOPS.get(mode)
    if not stop_id:
        return jsonify({"error": f"No stop configured for {mode}"}), 500

    stop_session_internal()  # enforce only-one-active-mode

    started = now_ms()
    with lock:
        session = {
            "mode": mode,
            "stop_id": stop_id,
            "started_at": started,
            "expires_at": started + SESSION_MAX_SEC * 1000,
            "baseline_expected": None,
            "pending_expected": None,
            "pending_count": 0,
            "history": [],
            "last_poll": None,
            "last_error": None,
        }

    stop_event = threading.Event()
    worker_thread = threading.Thread(target=worker_loop, args=(stop_event,), daemon=True)
    worker_thread.start()

    return jsonify({"active": True, "mode": mode})


@app.route("/api/session/stop", methods=["POST"])
def stop():
    stop_session_internal()
    return jsonify({"active": False})


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_ms()})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8787"))
    app.run(host="0.0.0.0", port=port)
