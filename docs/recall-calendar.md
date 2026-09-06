# Google Calendar recording setup

Earwitness uses Recall Calendar V1 to connect each user's Google calendar and
schedule recording bots. Google sign-in still supplies Earwitness with calendar
access for meeting titles and attendees; it does not connect a calendar to Recall.

## Deploy

1. Set `BASE_URL` to the externally reachable HTTPS origin of Earwitness and set
   a strong `SECRET_KEY`. Keep `AUTH_DISABLED` off outside local development.
2. Configure `RECALL_API_KEY` and `RECALL_REGION` for the workspace containing the
   bot preset. Regions are `eu-central-1`, `us-east-1`, `us-west-2`, or
   `ap-northeast-1`. Configure the workspace bot preset in that region's dashboard.
3. Enable Google Calendar API in the Google Cloud project. Configure an OAuth web
   client with `userinfo.email` and `calendar.events.readonly` scopes. Add the
   exact redirect URI `https://YOUR_APP/calendar/oauth/callback`.
4. Save that client's ID and secret in the regional Recall dashboard's Calendar
   V1 configuration. Set `RECALL_GOOGLE_CLIENT_ID` in Earwitness to the same ID.
   The Calendar client secret stays in Recall. Existing
   `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` settings continue serving app login.
5. In Google consent-screen testing mode, add the permitted Google test accounts.
   External production use requires Google's verification for the sensitive
   Calendar scope. Use an app-owned callback domain for verification.
6. Start the webapp and worker as usual. Startup creates the new identity/OAuth
   tables in existing databases. Back up the database before deployment and
   retain it across deployments: calendar mappings are persistent identities.

Do not exchange the Google authorization code in the app callback. The callback
checks the user's session and one-time state, then forwards the original query
to the fixed regional Recall callback. Success and error return destinations
are fixed application URLs, and success is checked against Recall state.

### Logging and credentials

OAuth URLs contain authorization codes and a short-lived Calendar token. Exclude
query strings from proxy, load-balancer, tracing and access logs for
`/calendar/oauth/` routes. For Nginx use `$uri` rather than `$request_uri` or
`$request` in that location's access format. The app redacts these callback
queries from its Uvicorn access logs and sends no-store/no-referrer responses.
Disable request/response body capture on these routes in external monitoring.
Keep `.env`, database backups, logs and browser traces private.

## Connect and choose recording

Open **Calendar** in navigation. New users choose one of four modes before
connecting, then authorize Google and return to Earwitness:

| Choice | Automatic recording |
| --- | --- |
| Off | No automatic recording rules enabled |
| External only | At least one attendee's email domain differs from the host's |
| Internal only | No attendee's email domain differs from the host's |
| All eligible meetings | Both internal and external meetings |

Eligible events need a supported meeting link, start and end times, and must not
be cancelled or already ended. All-day events are excluded. These presets add no
restriction on host status, recurrence or invitation acceptance. **External** is
relative to the meeting host, not necessarily your company.

Explicit per-meeting overrides in Recall take precedence, including when Off is
selected. This page shows overrides but does not edit them. Other connected
users may still need a shared bot. Do not delete workspace bots to turn off one
user's automatic recording.

Existing combinations outside these presets display **Custom**. Reconnect keeps
preferences; selecting and saving a preset explicitly replaces the six recording
flags while retaining unrelated settings such as the bot name. Upcoming events
show a scheduled bot only when Recall supplies `bot_id`; connection alone does
not prove that recording is enabled. Scheduling may take time after connection
or preference changes. Refresh the page to check again. A scheduled bot may need
to be admitted to the meeting before it can record.

Disconnect removes only the Google connection in Recall. It preserves Earwitness
login, enrichment credentials and historical recordings. Reconnection uses the
same persisted Recall identity.

## Adopt an existing dashboard connection

Do this **before** connecting the same owner through Earwitness. Obtain the exact
existing Calendar V1 `external_id` from an operator-controlled Recall source;
do not guess it from the email or substitute Recall's internal user UUID.

Run on the deployment's database/configuration:

```bash
uv run python -m webapp.calendar_admin adopt \
  --user-email owner@example.com --external-id EXISTING_EXTERNAL_ID
```

The command verifies a Google connection email against the active local user's
email before storing the mapping. It rejects mismatches and conflicting local
identities and preserves the existing preferences. Repeating the same verified
adoption is safe. The browser cannot choose another user's Recall identity.

If the owner cannot establish a matching identity, use a controlled reconnection:
record the current preference choice and upcoming schedule privately, disconnect
the old Google connection in the Recall dashboard, then connect through Earwitness
and explicitly choose the intended recording mode. Verify the resulting schedule
before the next meeting. Replacing a connection can clear its old calendar data
and scheduled bots; this is not an identity merge. Keep historical local
recordings. Do not run two active owner connections as an experiment.

## Recovery

- **Setup unavailable:** check the app's Calendar client ID, regional workspace
  key, BASE_URL, dashboard client credentials and exact Google redirect URI.
- **Consent denied:** return to Calendar and retry connection when ready.
- **Connection needs authorization:** reconnect the account. Consent-screen test
  restrictions and Google authorization expiry can require renewed consent.
- **Recall unavailable or rate-limited:** retain the selected choice and retry
  later. Check Recall's service status if the failure persists.
- **No upcoming meetings:** wait for initial sync, refresh, and confirm that the
  connected account's primary calendar contains an eligible upcoming event.

## Acceptance evidence

Live acceptance must use a permitted Google account and an eligible meeting:
connect → choose recording → scheduled bot → admit bot → recording → import by
the existing sync. Also verify the owner's adopted schedule and two connected
users sharing a meeting. Store no event titles, participant information, bot IDs
or recordings in the repository or public comments.

Verification on 2026-09-06:

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 52 files formatted.
- `uv run pytest -q`: 308 passed; one pre-existing TestClient deprecation warning.
- Chromium with synthetic Recall responses: all four modes saved and survived
  reload; Custom rules remained identifiable; an upstream outage retained the
  unsaved choice and offered Retry without claiming disconnection.
- Light/night layouts at 360×640 and 1280×900: no horizontal overflow, visible
  keyboard focus, primary controls at least 44px high. Empty and expired-access
  states were inspected at 360×640. With reduced motion, submit showed
  `Saving…` and `aria-busy=true`; the button stayed 286×44px.
- A forged non-ASCII CSRF value was safely rejected on all three mutation routes.

No live deployment/account was available, so actual Google consent, bot admission
and recording import, the owner's live mapping, and shared-meeting deduplication
remain **unverified**. Automated and browser checks use synthetic data and do not
establish that a real bot joins or records. Keep these acceptance criteria open.

## Official contracts

- [Google OAuth setup](https://docs.recall.ai/docs/calendar-v1-google-calendar)
- [Recording preferences and deduplication](https://docs.recall.ai/docs/calendar-v1-recording-preferences)
- [Calendar V1 FAQ](https://docs.recall.ai/docs/calendar-v1-faq)
- [Calendar user API](https://docs.recall.ai/reference/calendar_user_retrieve)
- [Calendar users lookup](https://docs.recall.ai/reference/calendar_users_list)
