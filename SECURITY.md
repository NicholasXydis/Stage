# Security Policy

## Supported Version

| Version | Supported |
| --- | --- |
| 1.1.0 | ✅ |

Only the latest release receives security fixes.

## Reporting a Vulnerability

Do not open a public issue for suspected vulnerabilities.

Report privately through [GitHub Security Advisories](https://github.com/NicholasXydis/Stage/security/advisories/new), or email <NicholasXydis@outlook.com>, with:

- the affected command, adapter, or source file
- reproduction steps, using a recorded fixture rather than a live request
- expected impact
- relevant output, with local paths removed

I will review valid reports as quickly as possible and prioritize fixes based on severity and exploitability.

## Scope

In scope:

- terminal escape or control-character injection from posting titles and descriptions
- registry fields that build a request target reaching an unintended host
- a redirect that carries origin-bound headers to a host they were not meant for
- source payloads that escape validation, the response size ceiling, or the per-host request limit
- unsafe deserialization of source, registry, or lexicon data
- path traversal through database, capture, or configuration paths
- vulnerable dependencies

Out of scope:

- probing live job boards to demonstrate a report. Use recorded fixtures — a blocked endpoint
  harms every user of this tool more than the bug does
- vulnerabilities in the job boards themselves, which belong to the vendor operating them
- rate limiting, throttling, or circuit breaker trips, which are the intended request posture
- social engineering
- automated scanner output without a working reproduction
- reports requiring access to a machine, database, or configuration you do not own

Stage is a local command-line tool. It has no server, no network listener, no accounts, and
no user authentication. Postings are stored in a SQLite database under your own user account.

Outbound requests reach only the hosts the shipped registry names, over https, and a redirect
that changes origin drops the headers bound to the first one. The two registry rows that send
a bearer token use a board's own public key, never a credential of yours.
