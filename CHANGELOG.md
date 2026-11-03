# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-02

### Added

- `stage prefs` saves the places, roles and terms you want, and `--mine` keeps
  only the postings that fit, in `list`, `search`, `export` and the TUI.
- `stage save`, `applied` and `hide` keep a tracker, listed by `stage tracker`.
- `stage mcp` lets AI apps such as Claude, Codex, VS Code and Cursor search your
  postings and keep your tracker, and `stage connect` sets it up in about 55 apps.
  Opening, exporting and syncing stay off until you allow them.
- About 580 more employer boards, among them Montreal and other Canadian
  employers, startups, and 55 insurers and health plans, plus Zapply's lists.
- Your own board list. Every Monday Stage adds boards found through community
  lists, and every Wednesday it retires dead ones.
- `stage open` and the TUI check a posting is still up after opening it and close
  it if the employer took it down. Syncs check the oldest ones too.

### Changed

- `stage schedule enable` syncs every 30 minutes (`--every 6h` remains). A board
  is read hourly after a new internship this week, at most 5.5 hours apart when
  quiet, and right after a community list shows a new posting from it.
- A host that pushed back or slowed down gets fewer requests. Unchanged answers
  do not count against the daily allowance.
- Big Workday boards with no internship filter are searched for internships on
  every read and read in full once a week, which closes what they took down.
- Cleaner place labels and stricter screening: graduate-only, plain IT and non-CS
  roles stay out. The first sync after an upgrade checks stored postings again.
- Errors are plain sentences everywhere. Ctrl-C stops cleanly and says what it
  kept or left unchanged. A scheduled run fails only on a real database problem.
- Discord puts Montreal and software first, names every posting over up to four
  messages, links each one, cannot ping anyone and skips a new board's backlog.
- `quarantine --all` is ten times faster and `rescreen` takes seconds.
- Québec emploi is off until Stage can read its new site.

### Fixed

- Half the Greenhouse boards went unread each run, and up to 255 Workday boards
  could fail at once. A full read keeps about 510 Greenhouse internships, not 190.
- One opening could show twice, and one title in two cities as one.
- A mistyped `--db` made an empty database; reading commands now refuse it.
- Postings a board took down came back from lists days later, a list dropping
  its copy hid the other copies, and every sync re-added 3,000 purged postings.
- Foreign postings counted as Canadian or American, and NVIDIA, Bosch and eight
  other boards stopped before the end of their lists.
- A TUI sync, `purge` or `rescreen` could run alongside a scheduled sync, and
  stopping a scheduled run on Windows left its sync running.
- A sync offline or behind a Wi-Fi login page marked boards as failing, and
  `stage tui` without a terminal spun a CPU core.
## [1.1.3] - 2026-09-09

### Fixed

- `doctor` said greenhouse stored nothing on a run that stored 138. A source
  left out of a run writes a row of zeroes, and the volume check read those as
  real while discarding every run that deferred a board. Greenhouse defers on
  all of them. A run counts now only if the source ran, and a deferred run is
  measured against other deferred runs.
- Haize Labs and Odin Dynamics are off. Both boards 404 with nowhere to move to.
  The two internships they published were already quarantined as non-CS.
- The PyPI page showed no author. A name and an email in one metadata field get
  folded into a single header, leaving the author line blank.

## [1.1.2] - 2026-09-09

### Fixed

- Job Bank has returned nothing since 31 August. Its server drops the connection
  on any client offering TLS 1.3, so no request ever reached the board. That one
  host is capped at TLS 1.2 now. Certificates are still checked and every other
  board still gets TLS 1.3.
- A connection that died before the server answered was logged as `ConnectError:`
  with nothing after the colon, which is why Job Bank looked fine for eight days.
  The cause is carried through now:
  `ConnectError: ConnectionResetError: [Errno 54] Connection reset by peer`.

## [1.1.1] - 2026-09-07

### Fixed

- A posting that lists several cities is no longer read as foreign when a foreign
  city happens to be listed first. "London, New York" and "New York, London, or
  Paris" are US postings. 15 rows come back, among them quant and software
  internships at Point72, Squarepoint, Xantium, GSA and Booz Allen. Rome, NY and
  Vienna, VA come back too. A comma still binds a city to its region, so
  "Toronto, ON", "Berlin, DE" and "San Jose, Costa Rica" read as before. All
  21,270 stored location strings were replayed against the change; 32 moved, all
  of them from international to domestic.
- `doctor` listed boards you had already switched off, and kept listing them for
  as long as their old failures sat in the database. It now reads the registry the
  way sync does. Sync itself still sees every row, disabled ones included, because
  that is how it tells an orphaned posting from a live one.
- `doctor` and `sync` told you to clear a blocked bucket with
  `stage sources --clear`. That flag does not exist. Both print
  `--reset-rate-limit` now.
- Process engineering, category management, talent acquisition, environmental
  engineering, construction management and investor relations are named as non-CS
  work instead of landing in the unknown pile. 109 quarantined rows, no change to
  what Stage keeps. A software role inside one of those areas still counts as
  software.

## [1.1.0] - 2026-09-03

### Added

- Discord notifications now lead with Montreal postings, then the rest of Canada,
  and put software engineering ahead of quant inside each place. Nothing is
  filtered out; the whole batch is still there, in the order you care about.
- 43 more employers, taking the registry to 1,530. Every one was reached in a
  live sync before it was added, and none was found by guessing a slug.

### Fixed

- ClickHouse moved off Greenhouse, which now answers 404 for that slug; the row
  points at its Ashby board, which returns 177 postings. Marqeta, Gloss Genius and
  Veeda AI answer 404 on every platform and are disabled rather than removed, so
  discovery does not re-add them.
- Searching in the TUI no longer stalls. Each keystroke used to queue a query on
  the single database thread, and cancelling the worker did not cancel the query
  already running, so typing three letters took about ten seconds. Keystrokes are
  now collected for 180ms before a search runs.

### Notes

- A Discord message is bounded by the 6,000 characters Discord allows, not by a
  posting count, so a batch of long titles and URLs shows fewer rows than a batch
  of short ones.

## [1.0.0] - 2026-09-01

First release. Stage collects CS internship postings from company job boards
and community feeds, keeps the ones a CS undergraduate can apply to, and stores
them in a SQLite database on your own machine.

### What it does

- **Reads 1,450+ employers** across 15 ATS platforms — Greenhouse, Lever, Ashby,
  Workday, SmartRecruiters, Workable and more — through 13 adapters, plus 11
  community feeds.
- **Filters hard.** It has kept 4,165 postings and rejected 134,368. Senior
  roles, graduate-only research posts and work outside Canada and the United
  States are all rejected. A posting whose location cannot be read is kept,
  since a board that publishes no place is missing data rather than advertising
  a foreign one. Every rejection records the rule that caught it, so
  `stage quarantine` shows what was skipped and why.
- **Classifies each posting** by role, internship scope, degree eligibility,
  location and language, in English and French. Filter with `--role`,
  `--location`, `--term`, `--lang`, `--source` and `--company`, and narrow by
  date with `--last`.
- **Browses from the terminal.** `stage list`, `stage search`, and a full-screen
  `stage tui`. Rows are numbered, so `stage show 3` and `stage open 3 5 9` act
  on what you just saw. The TUI keeps its filters in one panel, marks rows with
  `space` to open them together, and switches theme with `ctrl+t`.
- **Exports** to CSV, JSON, Markdown or PDF.
- **Runs on a schedule** through launchd, systemd or Task Scheduler, and can
  post new postings to a Discord channel through a webhook.
- **Stays polite.** Requests are rate limited per host, cached with validators,
  and backed off when a board pushes back. A cooldown longer than the retry
  ceiling stops the run for that host rather than hammering it.
- **Stays local.** No account, no server, no telemetry. The only network traffic
  is Stage reading public job boards.

### Requirements

Python 3.12, 3.13 or 3.14 on macOS, Linux or Windows.

[1.2.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.2.0
[1.1.3]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.3
[1.1.2]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.2
[1.1.1]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.1
[1.1.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.0
[1.0.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.0.0
