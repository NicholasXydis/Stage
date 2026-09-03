# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.1.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.1.0
[1.0.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.0.0
