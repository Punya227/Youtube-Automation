# Shorts Autopilot — YouTube Shorts auto-uploader for Windows 11

A personal, local automation app that uploads **one YouTube Short per day** (configurable) from a folder on your PC and **schedules its publication** on your single channel, using only the official **YouTube Data API v3** and **Google OAuth 2.0**.

- A background **worker** watches `youtube_shorts/incoming/`, validates new videos with FFprobe/FFmpeg, generates metadata, and at the configured upload time uploads the oldest eligible video as **private** with `status.publishAt` set to your publication time. YouTube makes it public automatically at that moment.
- A **Streamlit dashboard** (`http://localhost:8501`) shows the queue, history, logs and settings and lets you edit/approve metadata, upload manually, connect your channel and change times.
- **Mock mode** (the default) runs the whole workflow without contacting YouTube.

No browser automation, Selenium, or unofficial endpoints are used.

---

## 1. Project structure

```
youtube-shorts-automation/
├── app.py                    Streamlit dashboard entry point (navigation + password gate)
├── ui.py                     Shared dashboard helpers (cached services, badges, formatting)
├── pages/
│   ├── overview.py           A. Overview
│   ├── queue.py              B. Video Queue (upload now, remove, edit/approve metadata, preview)
│   ├── history.py            C. Upload History (filter, search, re-queue)
│   ├── settings.py           D. Scheduler Settings
│   ├── youtube_settings.py   E. YouTube Settings (connect / disconnect / API test)
│   └── logs.py               F. Logs (events, upload jobs, worker log file)
├── scheduler.py              Background worker (APScheduler) - runs independently of the dashboard
├── worker_supervisor.py      Restarts the worker after crashes (used by the Windows task)
├── config.py                 .env / environment configuration and folder paths
├── database.py               SQLite schema, transactions and unique constraints
├── logging_setup.py          Rotating log files with secret redaction
├── video_processor.py        Copy-completion detection, SHA-256 hashing, FFprobe validation, integrity check
├── queue_manager.py          Folder monitoring, safe file moves, queue selection, path validation
├── metadata_generator.py     Anthropic (optional) + local template metadata, sidecar JSON overrides
├── youtube_auth.py           OAuth Desktop flow, DPAPI-encrypted token storage, channel lookup
├── youtube_uploader.py       Resumable uploads (LiveUploader) + MockUploader
├── recovery.py               Restart recovery / reconciliation of interrupted uploads
├── services/
│   ├── upload_service.py     Upload orchestration, retries, daily limit, verification
│   ├── metadata_service.py   Metadata generation/editing/approval persistence
│   └── scheduling_service.py Pure timezone-aware run/publish-slot calculations
├── tests/                    pytest suite (no real uploads, ever)
├── youtube_shorts/           Created automatically (gitignored)
│   ├── incoming/   processing/   uploaded/   failed/
│   ├── logs/       thumbnails/   metadata/
│   └── shorts.db             SQLite database
├── secrets/                  client_secret.json + encrypted token (gitignored)
├── .streamlit/config.toml    Dashboard bound to 127.0.0.1, follows light/dark theme
├── .env.example  .gitignore  requirements.txt  pytest.ini
├── setup.bat  start.bat  stop.bat  install_windows_task.ps1
└── README.md
```

---

## 2. Installation

Requirements: Windows 11, **Python 3.11+** (tested on 3.14), **FFmpeg** (for FFprobe), internet access.

1. Install Python from <https://www.python.org/downloads/> (tick **Add python.exe to PATH**) or `winget install Python.Python.3.12`.
2. Install FFmpeg: `winget install Gyan.FFmpeg`, then open a new terminal.
3. Double-click **`setup.bat`**. It:
   - finds Python 3.11+, creates `.venv`, installs `requirements.txt`,
   - creates `.env` from `.env.example` (mock mode),
   - creates the `youtube_shorts/` folders and initialises the SQLite database,
   - checks that FFmpeg is on PATH.
4. Optional: open `.env` and set `DASHBOARD_PASSWORD=` to protect the dashboard, and `ANTHROPIC_API_KEY=` for AI metadata.

---

## 3. Google Cloud Console setup

You do this once. It creates the OAuth "Desktop app" credentials the app uses to act on **your** channel.

1. Go to <https://console.cloud.google.com/> and sign in with the Google account that owns the YouTube channel.
2. **Create a project**: project picker (top bar) → **New Project** → name it e.g. `Shorts Autopilot` → **Create**, then select it.
3. **Enable the API**: **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
4. **Configure the consent screen** (shown as **Google Auth Platform** / **OAuth consent screen**):
   - **Branding**: App name `Shorts Autopilot`, user support email = your email, developer contact = your email → Save.
   - **Audience**: User type **External**. Under **Test users** add your own Google account.
   - **Data access** → **Add or remove scopes** → add
     `https://www.googleapis.com/auth/youtube.upload` and
     `https://www.googleapis.com/auth/youtube.readonly` → Save.
     (`youtube.upload` performs the uploads. `youtube.readonly` is used to show your channel name/ID, verify that YouTube accepted the scheduled publish time, and reconcile interrupted uploads so a crash never causes a duplicate.)
5. **Create the client**: **Clients** (or **Credentials → Create credentials → OAuth client ID**) → Application type **Desktop app** → name `Shorts Autopilot desktop` → **Create** → **Download JSON**.
6. Save the downloaded file as **`secrets\client_secret.json`** inside the project folder (create `secrets` if needed). Never commit or share it; `.gitignore` already excludes it.
7. **Keep the connection alive:** while the app's publishing status is **Testing**, Google expires refresh tokens after **7 days**. On **Audience**, click **Publish app** (status *In production*). For personal use you don't need Google's app verification; you will see a "Google hasn't verified this app" screen when connecting — click **Advanced → Go to Shorts Autopilot (unsafe)** since it is your own app.

### Important: YouTube API compliance audit (public videos)

Google locks videos uploaded through `videos.insert` by **unverified API projects created after 28 July 2020** to **private** viewing. Such videos stay private even with `publishAt`, so they never go public on schedule. To lift this, submit the **YouTube API Services audit / quota extension form** (<https://support.google.com/youtube/contact/yt_api_form>) describing this personal tool. Until it is approved:

- Uploads, scheduling metadata and everything in this app still work, but YouTube Studio may show the video as *Private (locked)*.
- You can publish manually in YouTube Studio.

### Quota

The default project quota is **10,000 units/day**. An upload costs about **1,600 units** (check the current costs in the [quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost)), so one upload/day leaves ample headroom. Quota resets at midnight Pacific Time; the app automatically waits for the reset if it hits `quotaExceeded`/`uploadLimitExceeded`.

---

## 4. OAuth configuration

`.env` settings:

```ini
UPLOAD_MODE=live                                   # mock = simulate, live = real uploads
GOOGLE_CLIENT_SECRET_FILE=secrets/client_secret.json
YOUTUBE_TOKEN_FILE=secrets/youtube_token.bin
```

- The app uses the **installed-app (Desktop) flow**: sign-in happens on Google's own page in your browser; the app receives an authorization code on a temporary `localhost` port.
- Your **Google password is never seen or stored**. Only the OAuth token is stored, **encrypted with Windows DPAPI** (only your Windows user account can decrypt it).
- Access tokens are refreshed automatically. If refreshing fails (revoked access, password change, expired Testing token), uploads stop with an **Authentication** log entry and a red banner on the Overview page until you reconnect. The affected video stays queued; no retries are wasted.
- Tokens are never shown in the dashboard and are redacted from all logs.

---

## 5. Connect your YouTube channel

1. Start the app (`start.bat`) and open <http://localhost:8501>.
2. Go to **YouTube Settings** → **Connect YouTube account**.
3. Your browser opens Google sign-in → choose the channel's Google account → (unverified app screen: **Advanced → Go to …**) → tick both YouTube permissions → **Continue**.
4. The tab says "YouTube account connected". Back in the dashboard you'll see the **channel name and channel ID**.
5. Click **Test API connectivity** to confirm.

*Reconnect / switch account* repeats the flow; *Disconnect* revokes the token at Google and deletes it locally (videos already scheduled on YouTube are not affected). The app manages exactly one channel — connecting another account replaces the previous one.

---

## 6. Adding videos

- Copy or export **MP4** files (H.264/AAC recommended; `.mov`/`.m4v` also accepted) into **`youtube_shorts\incoming\`**, or use **Video Queue → Add videos from this browser**.
- The worker only processes a file after its size has been unchanged for `FILE_STABLE_SECONDS` (default 10 s) across two scans and it is no longer locked by the copying program. `.partial`, `.part`, `.crdownload`, `.tmp` files are ignored.
- Validation (FFprobe + a decode test of the start and end of the file):
  - must be **vertical or square** (rotation metadata respected); **9:16 is preferred** (other ratios get a warning),
  - duration **≤ 180 s** (YouTube's current Shorts limit; adjust `SHORTS_MAX_DURATION_SECONDS` if YouTube changes it),
  - must be readable and not truncated/corrupt.
- Invalid files and exact duplicates (same SHA-256 hash as an earlier video, even if renamed) are **moved to `failed/`** with a clear reason. Files are **never deleted**.
- The oldest eligible video is uploaded first. Successful uploads move to `uploaded/`.

### Metadata

Generated when a video is queued, based only on the **filename**, an optional **topic** and an optional **sidecar file** — nothing analyses the picture or sound, and the AI prompt explicitly forbids describing footage it has not seen.

- **Anthropic** (when `ANTHROPIC_API_KEY` is set; default model `claude-opus-5-5` at low effort, override with `ANTHROPIC_MODEL`) returns title, description, tags, hashtags, category and language as schema-validated JSON.
- **Local template** fallback whenever no key is set or the API call fails or is declined.
- **Sidecar override**: create `youtube_shorts\metadata\<video name>.json` (or put it next to the video in `incoming\`). Any field you set wins; missing fields are generated:

```json
{
  "title": "Amazing Hidden Places in Kashmir",
  "description": "Explore the breathtaking beauty of Kashmir.",
  "tags": ["Kashmir", "Travel", "Nature"],
  "hashtags": ["#Kashmir", "#Travel", "#Shorts"],
  "language": "en",
  "category_id": "19",
  "topic": "Kashmir travel",
  "contains_synthetic_media": false
}
```

YouTube limits are enforced automatically (title ≤ 100 chars, no `<`/`>`, tags ≤ 500 chars, ≤ 5 hashtags incl. `#Shorts`, description ≤ 5,000 bytes). Note: `#Shorts` does **not** make a video a Short — the vertical/square format and ≤ 3 min duration do.

**Manual approval mode** (Scheduler Settings): new videos wait as *Needs approval* until you click **Save & approve** in the Video Queue. Edited metadata is never overwritten automatically.

---

## 7. Configure upload and publication times

**Scheduler Settings** page (stored in SQLite; the worker applies changes within ~15 s, no restart):

| Setting | Default | Meaning |
|---|---|---|
| Daily upload time | 08:00 | When the worker uploads the day's video(s) |
| Daily publication time | 18:00 | When YouTube makes the video public |
| Timezone | Asia/Kolkata | All times above are in this zone (DST handled) |
| Videos per day | 1 | Hard daily limit, enforced from the database |
| Minutes between publications | 60 | Spacing when more than one video per day |
| Minimum lead time | 60 min | Publication is never sooner than this after upload |
| Scheduler enabled | on | Off = only manual uploads |
| Upload & publish on weekends | on | Off = Monday–Friday only |
| Manual approval mode | off | Require metadata approval |
| Catch up missed runs | on | If the PC was off at upload time, run when the worker starts (same day only) |
| Max retries | 3 | Temporary failures retry after 5, 15, 45 … min |

The **Preview** box shows the next upload run and exactly when that video would publish. Rules: a publish time is never in the past — if today's slot has passed (or is within the lead time) the next eligible day is used, and two videos never share a slot.

---

## 8. Start the application

- **`start.bat`** — starts the worker in its own minimised window and the dashboard in the current window, then open <http://localhost:8501>.
  - Closing the browser or stopping the dashboard (Ctrl+C) does **not** stop the worker.
- **`stop.bat`** — asks the worker to stop gracefully (an upload in progress pauses at a chunk boundary and resumes on next start).
- Command line:
  ```
  .venv\Scripts\python.exe scheduler.py            # worker
  .venv\Scripts\python.exe scheduler.py --status   # show next run / publish slot
  .venv\Scripts\python.exe scheduler.py --once     # run one upload batch now and exit
  .venv\Scripts\python.exe -m streamlit run app.py # dashboard only
  ```

Only one worker can run at a time (OS file lock); extra instances exit immediately.

**Your PC must be powered on, awake, and online at the upload time.** Windows sleep stops the worker. Use *Settings → System → Power & battery → Screen and sleep → "When plugged in, put my device to sleep after: Never"*. If the PC was asleep/off at upload time, the run is caught up when the worker next starts the same day (publication is then scheduled for the next valid slot). Once uploaded, publication itself is done by YouTube — your PC does not need to be on at publish time.

---

## 9. Automatic startup with Windows

Run in PowerShell from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows_task.ps1
```

This registers the Task Scheduler task **ShortsAutopilotWorker** that starts the worker **when you sign in**, using the project's `.venv\Scripts\pythonw.exe` (no console window) and `worker_supervisor.py`, which restarts the worker after a crash with backoff. Task Scheduler is also configured to restart the task after failures (every minute, unlimited), run on battery and never time out.

- `-AtStartup` (run PowerShell **as Administrator**): starts at boot even before you sign in. Windows asks for your account password in its own credential dialog; Task Scheduler stores it so the worker can decrypt the DPAPI-protected YouTube token.
- `-Uninstall`: removes the task.

The dashboard isn't needed for uploads; start it with `start.bat` (the second worker it launches exits immediately because one is already running) or `.venv\Scripts\python.exe -m streamlit run app.py`.

---

## 10. Testing with mock mode

`UPLOAD_MODE=mock` (default) replaces YouTube with `MockUploader`, which stores "uploads" in `youtube_shorts\mock_youtube.json`. Everything else is real: folder monitoring, FFprobe validation, hashing, metadata, scheduling, SQLite, retries, recovery, dashboard. Mock video IDs start with `MOCK`, and the sidebar shows a **MOCK MODE** badge.

Try it:

1. `start.bat`, then drop a vertical MP4 into `youtube_shorts\incoming\`.
2. Within ~25 s it appears in **Video Queue** with metadata. Click **Upload now** (tick *Ignore today's daily limit* if needed).
3. Check **Upload History** and **Logs**; the file moves to `uploaded\`.

Failure injection — set `MOCK_FAIL_MODE` in `.env` and restart the worker: `transient_once`, `transient_always`, `permanent`, `auth`, `quota`, `drop_publish_at`, `crash_after_upload`, `interrupt_midway`.

Automated tests (they never upload anything to YouTube; videos are generated with FFmpeg into temp folders):

```
.venv\Scripts\python.exe -m pytest
```

Covered: video validation (orientation, rotation, duration, corrupt/truncated files), copy-completion detection, metadata generation/fallback/overrides/limits, database constraints and transactions, timezone & DST maths, never-in-the-past scheduling, slot allocation, duplicate detection by hash, daily-limit enforcement across restarts, concurrent-upload prevention, retry/backoff, permanent/auth/quota handling, crash-after-upload reconciliation, resumable-session recovery, token encryption, error classification and secret redaction.

When you switch to `UPLOAD_MODE=live`, restart both the worker and the dashboard.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Sidebar shows **Worker offline** | Start it with `start.bat` or the Windows task. Check `youtube_shorts\logs\worker.log`. |
| Video never leaves "new file detected" | The file is still being written/locked or the worker is off. Close the editor/export, wait 10–20 s. |
| "FFprobe was not found" | `winget install Gyan.FFmpeg`, reopen the terminal, or set `FFPROBE_PATH`/`FFMPEG_PATH` in `.env`. |
| "Horizontal video" / "exceeds the Shorts limit" | Export as 1080×1920 (9:16) and ≤ 180 s. The original stays in `failed\`; drop the corrected export into `incoming\`. |
| "Duplicate of …" | The same content was already queued/uploaded. Intentional re-uploads need a re-export (different file content). |
| "OAuth client file not found" | Save the Desktop-app JSON as `secrets\client_secret.json` (section 3). |
| "… is a 'web' client" | Create an OAuth client of type **Desktop app**, not Web application. |
| `access_denied` / app blocked during sign-in | Add your Google account under **Audience → Test users**, or publish the app. |
| Reconnect needed every 7 days | The consent screen is in *Testing*; click **Publish app** (section 3, step 7). |
| Red "authorization problem" banner | Token revoked/expired. YouTube Settings → Reconnect. Queued videos continue afterwards. |
| `quotaExceeded` / `uploadLimitExceeded` | The app retries automatically after midnight Pacific Time. Lower videos per day or request more quota. |
| Uploaded video is **Private (locked)** | Your API project hasn't passed the YouTube API audit (section 3). Publish manually meanwhile. |
| Status **Needs review** | The upload may have succeeded but scheduling couldn't be confirmed, or recovery couldn't reach YouTube. Check the video in YouTube Studio, then click **Mark as OK** or **Re-queue** in Upload History. |
| Nothing uploaded at 08:00 | PC asleep/off or worker not running. With *Catch up missed runs* on, it runs when the worker starts the same day. |
| Daily limit seems wrong after changing timezone | The limit counts uploads per local calendar day in the configured timezone. |
| Dashboard password | Set `DASHBOARD_PASSWORD` in `.env` and restart the dashboard. The dashboard only listens on 127.0.0.1. |
| Start over with a clean slate | Stop the worker, then move/rename `youtube_shorts\shorts.db` (videos in the folders are untouched). |

### How crash recovery works

Before uploading, the app records an upload job, the publication slot and (as soon as YouTube creates it) the resumable upload session URI. After a crash, power cut or forced shutdown, the worker's startup recovery:

1. asks YouTube for the state of the saved upload session — if YouTube already finished it, the existing video ID is recorded (**no second upload**); if it was partial, the **same session is resumed** from the confirmed byte;
2. if the session expired, searches the channel's latest uploads for the exact title after the job start time;
3. only if YouTube has no trace of it, re-queues the video; if YouTube can't be reached, marks it **Needs review** instead of guessing.

### Security notes

- `.env`, `secrets\`, tokens, the database and logs are gitignored.
- OAuth token encrypted with Windows DPAPI; the Google password is never handled.
- Secrets (access/refresh tokens, client secrets, API keys, upload session IDs) are redacted from log files and dashboard event logs.
- Uploaded filenames are sanitised and every file path is checked to stay inside `youtube_shorts\`.
- The dashboard binds to `127.0.0.1` only and can require `DASHBOARD_PASSWORD`.
