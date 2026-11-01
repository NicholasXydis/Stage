# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-01

### Added

- `stage prefs` saves the places, roles and terms you want, and `--mine` keeps
  only the postings that fit, in `list`, `search`, `export` and the TUI.
- An application tracker. `stage save`, `applied`, `hide` and `track` set a
  status, a date and a note, and `stage tracker` lists them. In the TUI, `s`, `p`
  and `h` do the same, `z` undoes, and `l` opens your applications.
- `stage mcp` lets AI apps such as Claude, VS Code and Cursor search your postings
  and keep your tracker, and `stage connect` sets it up in about 55 of them.
  Claude Desktop, VS Code and Goose show a search as a card where you can save and
  hide postings. Opening, exporting and syncing stay off until you allow them.
- `stage show`, `open` and the tracker commands take ranges such as `1-5`.

### Changed

- `stage open` opens up to 50 tabs, up from 10.
- Output is plain ASCII, errors go to stderr, and the TUI fits an 80x24 terminal.
- Filters ignore case, so `--company google` and `--role SWE` work, and
  `stage search software intern` no longer needs quotes.

### Fixed

- Some syncs lost up to 255 Workday boards at once, because screening a large
  batch held every connection until they timed out together.
- Every sync re-added about 3,000 postings that `purge` had removed.
- Rejected postings were never removed, so the database grew by about 100,000
  rows a month. `purge` now drops those no board has listed for 30 days.
- Foreign Workday postings, such as "GB-NU-CRAMLINGTON" in England, counted as
  Canadian or American. Run `stage rescreen` once after upgrading to fix the
  postings you already have.
- Postings in several places showed "2 Locations" or lost part of the list, and
  the same Workday posting could show twice.
- Job Bank student postings past the second page were missed.
- Searching recent postings, which the TUI always does, took up to 14 seconds. It
  takes under 0.2 seconds now.
- A title with square brackets could crash the TUI, and long links wrapped, so
  copying one broke it.
- A new database's journal was readable by other accounts until it closed, and
  pointing `--db` at another program's SQLite file wrote Stage's tables into it.
- On Windows, Stage could not create its files when the account was named under
  another domain, as it is over SSH.
- A locked database got the advice to delete it.
- A scheduled sync counted as failed whenever `doctor` found anything to look at.
- Miro and Old Mission Capital work again. Beacon Software, Firetiger, Tilde
  Research, Frima and Global Partners are off.

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
