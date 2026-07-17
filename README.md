# A2 Bus Alert

Two buttons — Work / Home — that watch the real Bus Éireann A2 arrival at
whichever stop matters right now, and push a phone notification the moment
a delay (or an early bus) is confirmed. Auto-stops after 30 minutes.

## How it actually works (read this first)

iOS pauses JavaScript in background browser tabs, so "keep the tab open and
poll from the phone" would stop working the second she locks the screen.
So the real design is:

```
[ iPhone: PWA (index.html) ]  --tap Work/Home-->  [ backend/server.js ]
        ^                                                |
        |                                          every 10s
   phone notification  <--- web push ---  polls tfi-gtfs, checks route A2
                                                           |
                                                  [ tfi-gtfs (self-hosted) ]
                                                           |
                                                 NTA GTFS-Realtime API
```

- **tfi-gtfs**: a small open-source server (Python) that turns the National
  Transport Authority's real-time feed into a simple
  `/api/v1/arrivals?stop=1234` JSON endpoint. https://github.com/seanblanchfield/tfi-gtfs
- **backend/** (Python / Flask): polls that endpoint every 10s while a
  session is active, waits for the new arrival time to be confirmed on 3
  consecutive polls, then sends a push notification. Auto-stops after 30
  minutes. State lives in memory, so it must run as a single process/worker.
- **frontend/**: the installable iPhone app (technically a PWA — a website
  that installs like an app via Safari's "Add to Home Screen"). Two buttons,
  a live status card, a history log, a sound toggle.

Everything needs to keep running even when the phone is locked, so the
backend and tfi-gtfs both need to live somewhere always-on (a free-tier
cloud service, or a Raspberry Pi at home).

## Step-by-step setup

### 1. Get an NTA API key
Sign up (free) at https://developer.nationaltransport.ie/ and subscribe to
the GTFS-Realtime product to get an API key.

### 2. Find the two stop numbers and confirm the route code
Every physical bus stop pole has a stop number printed on it. You can also
look them up on https://www.transportforireland.ie/plan-a-journey/ — click a
stop to see its number. Get the stop number for:
- the stop she waits at near work
- the stop she waits at near home

### 3. Deploy the backend to Render (do this first, before tfi-gtfs)
It's worth proving the backend works on the real internet before wiring up
the live bus data, so this step uses placeholder stop numbers — that's
expected and fine.

**a. Push this project to GitHub.** From the unzipped project folder:
```
git init
git add .
git commit -m "Bus alert backend"
```
Create a new empty repo on https://github.com/new (don't initialize it with
a README), then:
```
git remote add origin https://github.com/YOUR-USERNAME/bus-alert.git
git branch -M main
git push -u origin main
```

**b. Create the Render service from the blueprint.**
1. Sign up / log in at https://render.com (free, no card required for this).
2. Dashboard → **New +** → **Blueprint**.
3. Connect your GitHub account and select the `bus-alert` repo. Render will
   read `render.yaml` at the repo root and configure the service
   automatically (Python runtime, build/start commands, env vars).
4. It will ask you to fill in the two blank secret values
   (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`) — leave them blank for now,
   we'll generate and add those in step 5 (push notifications) rather than
   this connectivity test.
5. Click **Apply** / **Create**. Render builds and deploys — takes a
   couple of minutes the first time.
6. Once deployed, copy the public URL Render gives you, something like
   `https://bus-alert-backend.onrender.com`.

**c. Test it from your laptop.** Open a terminal and run (swap in your real
URL):
```
curl https://bus-alert-backend.onrender.com/api/health
```
You should get back `{"ok":true,"time":...}`. Note: Render's free tier
spins the service down after 15 minutes of no traffic, so the very first
request after idling can take 30-50 seconds to respond while it wakes up —
that's normal, not a bug.

Then exercise the full session lifecycle exactly like a real button press:
```
curl https://bus-alert-backend.onrender.com/api/session/status
curl -X POST https://bus-alert-backend.onrender.com/api/session/start \
  -H "Content-Type: application/json" -d '{"mode":"work"}'
curl https://bus-alert-backend.onrender.com/api/session/status
curl -X POST https://bus-alert-backend.onrender.com/api/session/stop
```
The `status` call right after starting should show `"active": true` and a
`lastError` complaining it can't reach `TFI_GTFS_URL` — that's expected,
since tfi-gtfs isn't deployed yet (next step). Everything else (start,
stop, mode-switching, timers) is now proven to work on the real internet.

### 4. Run tfi-gtfs somewhere always-on
Easiest with Docker:
```
docker run -p 7341:7341 -e API_KEY=your_nta_key_here seanblanchfield/tfi-gtfs
```
(or use the published image `vche/tfi-gtfs` mentioned in its README). Run
this on a Raspberry Pi at home, or another small always-on host — Render's
free tier doesn't support persistent Docker services well for this one, so
a home Pi or a cheap always-on VPS is the better fit here.

Once it's running, sanity-check a stop and confirm what the A2 route code
actually looks like in the data (it should be `"A2"`, but confirm rather
than assume):
```
curl "http://<host>:7341/api/v1/arrivals?stop=<your_stop_number>"
```
Look at the `route` field in the returned `arrivals` list. Update
`ROUTE_ID` in Render's environment variables if it differs.

Then, in the Render dashboard, update `TFI_GTFS_URL`, `WORK_STOP_ID`, and
`HOME_STOP_ID` to the real values (Environment tab → edit → Render
redeploys automatically), and re-run the `curl` session-start test above —
this time `lastPoll` should populate with real data after ~10 seconds.

### 5. Add push notifications
Generate a VAPID key pair (these authenticate your server to push services
like Apple's). Easiest with the `py-vapid` package:
```
pip install py-vapid --break-system-packages
vapid --gen
```
This writes `private_key.pem` and `public_key.pem`. Use
`vapid --applicationServerKey` to print the base64 public key. Add the
public key as `VAPID_PUBLIC_KEY` and the private key's contents as
`VAPID_PRIVATE_KEY` in Render's Environment tab.
(Alternative if you have Node installed: `npx web-push generate-vapid-keys`
prints both in the base64 format `pywebpush` expects directly.)

### 6. Configure and host the frontend
Open `frontend/index.html` and change this line near the top of the
`<script>` block:
```js
const API_BASE = "https://YOUR-BACKEND-URL.example.com";
```
to the backend URL from step 4. Then host the `frontend/` folder anywhere
that serves static files over HTTPS (required for push notifications to
work) — GitHub Pages, Netlify, Vercel, or Cloudflare Pages are all free and
simple. Push the `frontend/` folder to that host.

### 7. Install it on her iPhone
1. Open the frontend URL in **Safari** (must be Safari, not Chrome, for
   iOS home screen installs).
2. Tap the Share icon → **Add to Home Screen**.
3. Open the app from the new home screen icon (not from a Safari tab) at
   least once — this is what makes push notifications work properly on iOS.
4. When prompted, allow notifications.
5. That's it — tap **Work** when she's about to leave work, or **Home**
   when about to leave for work. Phone can be locked; the alert will still
   arrive.

Optional but nice: in **Settings → Accessibility → Spoken Content → Speak
Notifications**, she can have iOS read notifications aloud automatically,
even with the app closed — useful alongside (or instead of) the in-app
sound toggle, which only speaks while the app is open in the foreground.

## Notes on the delay-detection logic
- Polls every 10s while a mode is active.
- Compares the live "expected" time to the last *confirmed* time.
- A new time only gets confirmed (logged + notified) once it's been the
  same for 3 polls in a row (30s) — this avoids false alarms from noisy
  single readings in the real-time feed.
- Session hard-stops after 30 minutes regardless.
- Only one of Work/Home can be active — starting one automatically stops
  the other, both in the UI and on the backend.
