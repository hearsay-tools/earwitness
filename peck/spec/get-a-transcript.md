# Get a transcript

A recorded meeting is only useful once someone can read what was said. This feature lets a person
ask for the transcript of a finished meeting, follow the request in the queue, and cancel it again
if they asked for the wrong meeting.

## Identity
Marta Lis, a project manager, is signed in.

## Page
Marta Lis starts on the Meetings page at `/meetings`.

## Given
### People
| Name | Role | Email |
|------|------|-------|
| Marta Lis | Project manager | marta.lis@northwind-studio.com |
| Tomasz Wróbel | Recruiter | tomasz.wrobel@northwind-studio.com |
### Meetings
| Meeting | Date | Start | Minutes | Platform | Status | Participants |
|---------|------|-------|---------|----------|--------|--------------|
| Harbor kickoff | 2026-09-14 | 10:00 | 45 | Google Meet | To process | Marta Lis, Ola Nowak |
| Sprint review 14 | 2026-09-11 | 13:00 | 30 | Zoom | Ready | Marta Lis, Piotr Zielinski |
| Hiring sync | 2026-09-10 | 09:30 | 20 | Google Meet | To process | Tomasz Wróbel |
| Vendor demo | 2026-09-09 | 15:00 | 25 | Microsoft Teams | No recording | Marta Lis |
| Quarterly planning | 2026-10-20 | 11:00 | 60 | Google Meet | Upcoming | Marta Lis, Tomasz Wróbel |
### Jobs
| Meeting | Job | Status | Started by |
|---------|-----|--------|------------|
| Sprint review 14 | Download + transcription | Done | Marta Lis |
| Hiring sync | Download + transcription | Failed | Tomasz Wróbel |

## Visible
- Every finished meeting shows a status: "To process", "Processing", "Ready" or "No recording".
- A meeting page shows its date, start time, length and status in one line under the title.
- A meeting with a recording and no transcript offers a "Get transcript" button.
- A meeting page lists only the jobs run for that meeting, each with its kind, who started it, and its status.
- The Queue page counts waiting, in-progress and failed jobs in one line under its title.
- The Queue page lists every job with its meeting, who started it and its status, newest first.
- A waiting job can be cancelled; a finished, failed or cancelled job can be retried instead.
- A failed job shows the reason it failed.

## Behavior
1. Marta opens the meeting "Harbor kickoff".
2. Marta asks for the transcript of "Harbor kickoff".
3. Marta opens the Queue page.
4. Marta cancels the waiting "Download + transcription" job for "Harbor kickoff".
5. Marta opens "Harbor kickoff" from the Queue page.
6. Marta leaves and later comes back to the "Harbor kickoff" page by reloading it.

## Acceptance criteria
All criteria are mandatory. Each asks about one thing the user can see.

### Before Marta asks for anything
- **AC1:** Is "Harbor kickoff" listed on the Meetings page with the status "To process"?

### After Marta opens the meeting "Harbor kickoff"
- **AC2:** Does the page say "No transcript yet. Kick off processing with the button above."?
- **AC3:** Does the Jobs section say "Nothing has been run for this meeting yet."?
- **AC4:** Is there a "Get transcript" button?

### After Marta asks for the transcript of "Harbor kickoff"
- **AC5:** Does the line under the title show the status "Processing"?
- **AC6:** Does the Jobs section list exactly one job?
- **AC7:** Is that job a "Download + transcription" job with the status "Waiting"?
- **AC8:** Does that job show that it was started by "marta.lis@northwind-studio.com"?
- **AC9:** Does that job have a "Cancel" button?

### After Marta opens the Queue page
- **AC10:** Does the Queue page say "1 waiting · 0 in progress · 1 failed."?
- **AC11:** Is the "Harbor kickoff" job the top row of the job list, with the status "Waiting"?
- **AC12:** Is the "Hiring sync" job shown as "Failed" with the reason "Recording is not available any more"?
- **AC13:** Does the "Hiring sync" job show that it was started by "tomasz.wrobel@northwind-studio.com"?

### After Marta cancels the waiting job for "Harbor kickoff"
- **AC14:** Does the "Harbor kickoff" job show the status "Canceled"?
- **AC15:** Does the Queue page say "0 waiting · 0 in progress · 1 failed."?
- **AC16:** Does the "Harbor kickoff" job row offer a "Retry" button?
- **AC17:** Is there no "Cancel" button anywhere in the job list?

### After Marta opens "Harbor kickoff" from the Queue page
- **AC18:** Does the line under the title show the status "To process"?

### After Marta comes back to the "Harbor kickoff" page by reloading it
- **AC19:** Does the Jobs section show the "Download + transcription" job with the status "Canceled"?
- **AC20:** Is there a "Get transcript" button?
