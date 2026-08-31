# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-31

First release. Stage collects CS internship postings from company job boards
and community feeds, keeps the ones a CS undergraduate can apply to, and stores
them in a SQLite database on your own machine.

### What it does

- **Fetches from 1,465 employers** across 15 ATS platforms — Greenhouse, Lever,
  Ashby, Workday, SmartRecruiters, Workable and more — plus 11 community feeds.
- **Filters hard.** A recent sync kept 5,111 postings and rejected 91,523.
  Every rejection records the rule that caught it, so `stage quarantine` shows
  you what was skipped and why rather than asking you to trust it.
- **Classifies each posting** by role, internship scope, degree eligibility,
  location and language, in English and French.
- **Browses from the terminal.** `stage list`, `stage search`, and a full-screen
  `stage tui`. Rows are numbered, so `stage show 3` and `stage open 3 5 9` act
  on what you just saw.
- **Exports** to CSV, JSON, Markdown or PDF.
- **Runs on a schedule** through launchd, systemd or Task Scheduler, and can
  post new postings to a Discord channel through a webhook.
- **Stays local.** No account, no server, no telemetry. The only network traffic
  is Stage reading public job boards, rate limited per host, cached with
  validators, and backed off when a board pushes back.

### Requirements

Python 3.12, 3.13 or 3.14 on macOS, Linux or Windows.

[1.0.0]: https://github.com/NicholasXydis/Stage/releases/tag/v1.0.0
