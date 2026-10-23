# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `stage prefs` saves the places, roles and terms you want, the employers to
  follow or avoid, and a short note about you. `--mine` keeps only the postings
  that fit, on `list`, `search` and `export` and through a Mine switch in the
  TUI. Every list names the preferences it used, and postings with no location
  are counted rather than dropped without a word.
- An application tracker, kept in its own file. `stage save`, `applied`, `hide`
  and `track` set a status, a date and a note, `stage tracker` lists them with
  `--stats` and `--export`, and `--untracked` leaves out what you already acted
  on. A tracked posting keeps a copy of itself after its board takes it down,
  and `purge` never touches the tracker.
- The TUI saves, marks applied and hides with `s`, `p` and `h`, undoes with `z`,
  and opens a screen of your applications with `l`.
- `stage show`, `stage open` and the tracker commands take ranges such as `1-5`.
- `stage mcp` lets AI apps search and read postings and keep your tracker and
  preferences. Apps that show prompts get five ready-made ones, and their
  arguments autocomplete from your own data.
- `stage connect` sets Stage up in about 60 AI apps, through the app's own command
  where it has one and by editing its settings, with a backup, where it does not.
  `stage connect --status` and `stage doctor` show which apps are connected, and
  `stage disconnect` takes Stage out again.
- Actions, off until you allow them for an app with `stage connect
  --allow-actions`. The app can then open postings in your browser, export them
  to your Downloads folder and start a sync. Claude Code asks before each one.
- A Claude Code plugin with a skill, and one-click install buttons for Cursor,
  VS Code, LM Studio and Goose.

### Changed

- `stage open` and the TUI open up to 50 tabs at once, up from 25.

### Fixed

- Syncs failed across many boards at once, and `doctor` called it a network
  drop. Screening one large batch, such as the 31,000 rows from the custom job
  boards, held every connection for about 90 seconds, so they timed out together
  and tripped the shared Workday breaker. Large batches are screened in two
  worker processes now, and connections keep flowing meanwhile.
- A posting that `purge` had removed after its 14 days came back as new on the
  next sync whenever its board still listed it. Stage remembers when it first saw
  a purged posting and leaves it out once that is past the window.
- Postings found in the same sync were listed in id order, which after a first
  sync meant the whole list. They are listed by the date the board posted them
  now, newest first, and a posting date in the future cannot move one up.
  `stage list --new` after a single sync says that every posting is new.
- Job Bank student postings past the first two pages of a search were missed.
  Stage follows Job Bank's own job-type filters now, and a search that Job Bank
  counts as holding no student jobs is recorded as empty.
- An empty Jobvite board, one that says it has no open jobs, counted as a
  failure. It reads as empty now and closes nothing, while Jobvite's outage page
  still counts as a failure.
- Long apply links and posting ids wrapped across lines, so copying one broke it.
  They print on one line now.
- Old Mission Capital moved from Greenhouse to Ashby, where it lists 39 roles.
  Frima's site answers 404 on every page and Global Partners lists 1,845 roles
  without one internship, so both are parked until 2027-01-20.

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

[Unreleased]: https://github.com/NicholasXydis/Stage/compare/v1.1.3...HEAD
[1.1.3]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.3
[1.1.2]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.2
[1.1.1]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.1
[1.1.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.0
[1.0.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.0.0
