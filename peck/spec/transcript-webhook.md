# Transcript webhook

Teams want finished transcripts to land in their own tools (a CRM, a wiki) without copying them by
hand. This feature lets a person set the one webhook that receives every finished transcript: its
address, how it is called, and a secret token, while making sure the token never shows up again
and a secret is never hidden in the address.

## Identity
Marta Lis, a project manager, is signed in.

## Page
Marta Lis starts on the Settings page at `/settings`.

## Given
### People
| Name | Role | Email |
|------|------|-------|
| Marta Lis | Project manager | marta.lis@northwind-studio.com |
### Webhook
| URL | Method | Bearer token |
|-----|--------|--------------|
| https://ops.harbor-crm.com/old-hook | GET | legacy-4410 |

## Visible
- The Transcript webhook section shows "On" when a URL is saved and "Off" when it is empty.
- The saved URL and method are filled in when the page opens.
- A saved bearer token is never shown; the page only says that a token is saved.
- A URL that carries a username, password or secret is refused with a message, and the saved settings stay as they were.
- A successful save is confirmed with "Webhook settings saved."

## Behavior
1. Marta tries to save the webhook URL "https://hooks.harbor-crm.com/meetings?api_key=abc123". The URL carries a secret, so the app refuses and tells her why.
2. Marta saves the webhook URL "https://hooks.harbor-crm.com/meetings" with the method "POST" and the bearer token "hb-7Q2x-9931".
3. Marta removes the saved token and saves the webhook.
4. Marta empties the webhook URL and saves the webhook.
5. **The app:** restarts.
6. Marta leaves and later comes back to the Settings page by reloading it.

## Acceptance criteria
All criteria are mandatory. Each asks about one thing the user can see.

### Before Marta changes anything
- **AC1:** Does the Transcript webhook section show "On"?
- **AC2:** Does the Webhook URL field contain "https://ops.harbor-crm.com/old-hook"?
- **AC3:** Is the "GET" method selected?
- **AC4:** Does the page say "A token is saved. It is not shown again. Leave this blank to keep it."?
- **AC5:** Is the text "legacy-4410" absent from the page?

### When Marta tries to save the URL "https://hooks.harbor-crm.com/meetings?api_key=abc123"
- **AC6:** Does the app tell her "Webhook URL must be a valid http or https URL with a host and a usable port, and without a username, password, credential query parameter, or the bearer token."?
- **AC7:** Does the Webhook URL field contain "https://ops.harbor-crm.com/old-hook"?
- **AC8:** Is the "GET" method selected?

### After Marta saves the URL "https://hooks.harbor-crm.com/meetings"
- **AC9:** Does the page say "Webhook settings saved."?
- **AC10:** Does the Webhook URL field contain "https://hooks.harbor-crm.com/meetings"?
- **AC11:** Is the "POST" method selected?
- **AC12:** Does the page say "A token is saved. It is not shown again. Leave this blank to keep it."?
- **AC13:** Is the text "hb-7Q2x-9931" absent from the page?

### After Marta removes the saved token
- **AC14:** Does the page say "Optional. Sent as Authorization: Bearer, and never written to the queue log."?
- **AC15:** Is there no "Remove saved token" checkbox?

### After Marta empties the webhook URL
- **AC16:** Does the Transcript webhook section show "Off"?

### After the app restarts and Marta reloads the Settings page
- **AC17:** Is the Webhook URL field empty?
- **AC18:** Does the Transcript webhook section show "Off"?
- **AC19:** Is the "POST" method selected?
