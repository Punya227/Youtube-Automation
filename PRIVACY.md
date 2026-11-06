# Privacy Policy — Shorts Autopilot

Shorts Autopilot is a personal, self-hosted tool that uploads videos to the owner's own YouTube channel
through the official YouTube Data API v3. It runs only on the owner's Windows PC.

## What data it accesses
- **YouTube access (OAuth 2.0):** the scopes `youtube.upload` (upload videos) and `youtube.readonly`
  (read channel name and upload status). It does not access any other Google data.
- **Local video files** that the owner places in the app's `incoming` folder, plus optional metadata files.

## How data is stored
- The OAuth token is stored only on the owner's PC, encrypted with Windows DPAPI.
- Upload history and settings are stored in a local SQLite database on the same PC.
- Nothing is sent to any server other than Google's YouTube API endpoints. If the owner enables optional
  AI-generated titles, the video's file name, topic and metadata text (never the video itself) are sent to the
  Anthropic API.

## Sharing
No data is sold, shared, or used for advertising. There are no analytics or tracking.

## Revoking access
Use **Disconnect** in the app's YouTube Settings page, or remove access at
https://myaccount.google.com/permissions. Deleting the app's `secrets` and `youtube_shorts` folders removes all local data.

Shorts Autopilot's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact
Open an issue at https://github.com/Punya227/Youtube-Automation/issues
