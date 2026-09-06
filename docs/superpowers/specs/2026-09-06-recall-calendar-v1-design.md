# Recall Calendar V1 management

Approved design for issue #21, including the four recording choices confirmed by the user.

## Architecture and identity

Add an authenticated `/calendar` page and a focused Calendar V1 service and
router. Keep Google app login and its enrichment credentials separate from
Recall authorization. Recall remains responsible for scheduling bots; the
existing sync continues importing recordings.

Use a new calendar identity table with a unique local user foreign key and a
unique Recall external identity. Creating a new table lets `init_db()` upgrade
existing databases without changing existing user columns. Allocate and commit
one random identity before issuing a Recall token; concurrent attempts resolve
to the persisted winner. Retain the mapping after disconnect and across logins.
Persist unfinished setup and its selected mode on that identity independently
of expiring OAuth attempts, so cleanup cannot turn a first-setup retry into a reconnect.
Store expiring OAuth attempts separately with `UtcDateTime` timestamps and
atomic consumption. Browser inputs never select a Recall identity.

## Connection and security

Generate Calendar V1 tokens on the server using the workspace key. A separate
configured Google Calendar client initiates offline consent with Recall's
required scopes and JSON state. Bind each attempt to the authenticated user,
session, exact state and expiry. The app-domain callback validates the attempt
before forwarding all original query parameters to the fixed regional Recall
callback; it does not exchange the Google code. Use separate one-time checks
for the bridge and final return, and fixed success/error destinations. Re-read
Recall's connection before reporting success.

Protect mutation forms against CSRF. Do not log OAuth query strings, tokens or
raw upstream error bodies; document matching proxy/access-log configuration.
Refresh expired Calendar tokens for the same identity with a bounded retry.
Translate configuration errors, consent denial, expired access, rate limits and
timeouts into actionable messages. Disconnect only the Google platform, leaving
the app session, Google enrichment tokens and recording history intact.

## Recording and page behavior

Show connection account/health, recording choice, and upcoming events in three
sections using existing Apptension macros, labels and CSS tokens. Link the page
from navigation and make it reachable directly after signup.

New connections require an explicit Off, External only, Internal only, or All
eligible meetings choice before connection proceeds. External means at least
one attendee differs from the host’s email domain; Internal means none do.
Show that explanation beside the selector. Existing connections retain their preferences; unsupported
combinations display Custom until explicitly replaced. Only change the six
documented automatic-recording flags, preserving unrelated fields. Read back
saved preferences and retain the submitted choice on recoverable failures.

Read paginated, time-filtered Calendar meetings through user-scoped requests.
Use `bot_id` for scheduled status and show manual recording overrides. Off
explains that existing overrides can still schedule recordings. Empty/syncing
states offer a refresh action without claiming that connection implies a bot.

Reuse existing illustrations where relevant; no new artwork. Preserve layout
during loading and provide visible keyboard focus, targets of at least 44px,
360×640 layout, both themes and reduced-motion feedback.

## Existing owner connection

Prefer operator-only adoption before minting any new identity. The operator
supplies a known dashboard external ID and local user; the command reads Recall
connection details, verifies ownership against the local account, refuses
conflicting mappings, and preserves preferences. Never derive an external ID
from an email. If ownership cannot be established through available API data,
refuse adoption and document controlled dashboard disconnect followed by app
connection. Reconnection is a fallback because replacement can remove calendar
data and scheduled bots. Verify the owner's schedule after either path.

## Verification and delivery

Automated tests cover identity races/retries, user isolation, existing database
upgrade, forged/replayed callbacks, consent denial, expired tokens, failures,
preferences/custom combinations/overrides, pagination and disconnect preserving
login and history. Mock Recall; validate fixtures against official schemas.

Run all bindings: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run pytest`. Inspect the UI in both themes at desktop and 360×640, including
keyboard and reduced motion. Document deployment OAuth settings and redaction.

Live acceptance requires a permitted Google account and deployed callback:
connect, enable recording, observe a scheduled bot, admit it to an eligible
meeting, and verify recording import. Also check owner adoption and two users
sharing a meeting. Record outcomes without personal data or bot IDs. No live
test has run yet; absent live access remains an explicit acceptance blocker.

After approved design and implementation planning, implement, verify, commit,
push and open a draft PR closing #21. Monitor CI/review per dev-flow. Human
review and merge remain outside this task's automation.

## Sources

- https://docs.recall.ai/docs/calendar-v1-google-calendar
- https://docs.recall.ai/docs/calendar-v1-recording-preferences
- https://docs.recall.ai/docs/calendar-v1-faq
- https://docs.recall.ai/reference/calendar_user_retrieve
