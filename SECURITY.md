# Security Policy

## Supported Version

| Version | Supported |
| --- | --- |
| 1.2.1 | ✅ |

Only the latest release gets security fixes.

## Reporting a Vulnerability

Do not open a public issue. Report privately through [GitHub Security Advisories](https://github.com/NicholasXydis/Stage/security/advisories/new), or email <NicholasXydis@outlook.com>, with:

- the command, adapter or source file affected
- steps to reproduce, using a recorded fixture rather than a live request
- the impact you expect
- any output, with your local paths removed

I review reports quickly and fix the most severe first.

## Scope

In scope:

- posting text that injects terminal escapes or control characters, changes what the posting card shows or loads, or gets Stage to open anything but a public web page
- a registry field or redirect that sends a request, or its headers, to a host it was not meant for
- source data that gets past validation, the response size limit or the per-host request limit
- unsafe loading of source, registry or lexicon data
- path traversal through database, capture, export or settings paths
- an AI app getting more through `stage mcp` than its tools allow: writing the database beyond a schema upgrade or an allowed sync, touching other files, or acting without `--allow-actions`
- `stage connect` writing anywhere but the chosen app's settings and their backup
- vulnerable dependencies

Out of scope:

- probing live job boards to prove a report. Use recorded fixtures: a blocked endpoint hurts every user of Stage more than the bug does
- bugs in the job boards themselves, which belong to their vendors
- rate limits and circuit breaker trips, which are intended
- social engineering, and scanner output without a working reproduction
- anything that needs a machine, database or settings you do not own

Stage runs on your computer with no network listener, accounts or logins. `stage mcp` talks only over the standard input and output of the app that starts it. Stage sends https requests only to the boards in its registry or your own board list, its feeds, the platforms `stage discover` checks, and Discord if you set a webhook. Before `stage discover` adds a board, it reads that host's robots.txt and leaves the board out if robots.txt disallows its job list, apart from two job platforms whose robots.txt blocks every reader. A redirect to http or to a host Stage does not know stops the request, and a redirect to another origin drops the first origin's headers. The three registry rows that send a bearer token use the board's own public key, never one of yours.
