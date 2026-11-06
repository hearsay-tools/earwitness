# Find a meeting

After a few weeks the meetings list holds dozens of calls, and people remember a meeting by who was
in it rather than by its date. This feature lets a person narrow the list with a free-text search
and a status filter, see when nothing matches, and get back to the full list in one step.

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

## Visible
- By default the list shows finished meetings only; meetings that have not happened yet are hidden.
- A line under the title counts the meetings shown: "N finished meetings." without filters, "N meetings after filtering." with them.
- Meetings are listed newest first, each with its title, date and start time, length, participants and status.
- The search matches titles, participant names and emails; every word has to match.
- The Status filter shows, next to each status, how many meetings with that status match the search.
- When nothing matches, the page says so and offers a way to clear the filters.

## Behavior
1. Marta searches the meetings for "tomasz".
2. Marta also ticks the "Upcoming" status and filters again.
3. Marta replaces the search with "budget" and filters again.
4. Marta clears the filters.

## Acceptance criteria
All criteria are mandatory. Each asks about one thing the user can see.

### Before Marta searches anything
- **AC1:** Does the page say "4 finished meetings."?
- **AC2:** Is "Harbor kickoff" the top row of the list?
- **AC3:** Is "Vendor demo" the bottom row of the list, with the status "No recording"?
- **AC4:** Is "Quarterly planning" absent from the list?

### After Marta searches the meetings for "tomasz"
- **AC5:** Does the page say "1 meeting after filtering."?
- **AC6:** Is "Hiring sync" listed, with the participant "Tomasz Wróbel"?
- **AC7:** Is "Harbor kickoff" absent from the list?
- **AC8:** Does the Status filter show the count 1 next to "Upcoming"?

### After Marta also ticks the "Upcoming" status
- **AC9:** Does the page say "2 meetings after filtering."?
- **AC10:** Is "Quarterly planning" listed with the status "Upcoming"?
- **AC11:** Is "Quarterly planning" listed above "Hiring sync"?

### After Marta replaces the search with "budget"
- **AC12:** Does the page say "Nothing matches the filters"?
- **AC13:** Does the page say "0 meetings after filtering."?
- **AC14:** Is there a "Clear filters" link?

### After Marta clears the filters
- **AC15:** Does the page say "4 finished meetings."?
- **AC16:** Is the search box empty?
- **AC17:** Is "Sprint review 14" listed with the status "Ready"?
