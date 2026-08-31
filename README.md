```
                        ____  _
                       / ___|| |_   __ _   __ _   ___
                       \___ \| __| / _` | / _` | / _ \
                        ___) | |_ | (_| || (_| ||  __/
                       |____/ \__| \__,_| \__, | \___|
                                          |___/
```

<div align="center">

### Every CS internship. One command.

**Stage pulls postings from 1,400+ employers into a database on your computer,
so you can search all of them at once instead of checking thirty career pages.**

It runs locally. There is no account to make and no server involved.

<br>

![version](https://img.shields.io/static/v1?label=version&message=v1.0.0&color=7c3aed) [![license](https://img.shields.io/static/v1?label=license&message=MIT&color=db2777)](LICENSE) ![python](https://img.shields.io/static/v1?label=python&message=3.12%20%7C%203.13%20%7C%203.14&color=2563eb&logo=python&logoColor=white)

![tests](https://img.shields.io/static/v1?label=tests&message=2%2C274&color=16a34a) ![coverage](https://img.shields.io/static/v1?label=coverage&message=89%25&color=16a34a) [![ci](https://img.shields.io/github/actions/workflow/status/NicholasXydis/Stage/ci.yml?branch=main&label=ci&logo=githubactions&logoColor=white)](https://github.com/NicholasXydis/Stage/actions/workflows/ci.yml)

<br>

<p>
  <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/python/python-original.svg" alt="Python" width="42" height="42" align="middle">
  &nbsp;&nbsp;
  <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/sqlite/sqlite-original.svg" alt="SQLite" width="42" height="42" align="middle">
  &nbsp;&nbsp;
  <img src="https://avatars.githubusercontent.com/u/93378883?s=200" alt="Textual" width="42" height="42" align="middle">
  &nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/windows8/windows8-original.svg" alt="Windows" width="42" height="42" align="middle">
  &nbsp;&nbsp;
  <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/linux/linux-original.svg" alt="Linux" width="42" height="42" align="middle">
  &nbsp;&nbsp;
  <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/apple/apple-original.svg" alt="macOS" width="42" height="42" align="middle">
</p>

</div>

## Why

Good internship postings go up quietly and fill fast, so you end up refreshing
the same career pages every few days. Job boards help until you notice most of
what they show you is senior roles, PhD research posts, or work on another
continent.

Stage checks 1,400+ employer boards for you and keeps the postings a CS
undergrad in Canada or the United States can actually apply to. Everything it
rejects is saved with the reason, so you can look at what it skipped instead of
taking its word for it.

The last sync kept **1,887** postings out of **114,875**.

## Install

```bash
uv tool install stage-cli
```

<details>
<summary><b>Don't have uv?</b> Install it first, then reopen your terminal.</summary>

<br>

If you already have Python, this works everywhere:

```bash
pip install uv
```

Or use your usual package manager — `brew install uv` on macOS, `winget install
astral-sh.uv` on Windows. More options at
[docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/).

</details>

<details>
<summary><b>Other ways to install</b></summary>

<br>

```bash
brew install NicholasXydis/tap/stage    # macOS and Linux
pipx install stage-cli                  # isolates the tool, like uv
pip install stage-cli                   # installs into the active environment
```

`uv tool install`, Homebrew, and `pipx` all keep Stage away from your system
Python, which is what you want for a command line tool.

</details>

Needs Python 3.12, 3.13, or 3.14. Nothing else to set up — no database to
configure, no API key, no config file.

### First run

```bash
stage sync                    # fetch every board once (grab a coffee)
stage tui                     # browse what came back
```

The first sync visits every board and takes a few minutes. Later ones are much
quicker, since Stage remembers what it already has and only asks each board for
what changed.

```bash
stage --install-completion    # tab-complete commands and filters
```

Completion needs a fresh terminal. After that, `stage <TAB>` lists commands and
`stage list --<TAB>` lists filters.

## The basics

```bash
stage sync                            # fetch the latest postings
stage list                            # what is new, newest first
stage search "machine learning"       # search titles, employers, descriptions
stage tui                             # browse it all in a full screen
```

Rows are numbered, so you can act on what you just saw:

```bash
stage list --role swe --location montreal
stage show 3                          # read the third row in full
stage open 3 5 9                      # open those three in your browser
```

Filters work the same on `list`, `search`, and `export`:

| Filter | What it takes |
| --- | --- |
| `--role` | swe, security, data, ml-ai, quant, infra, embedded, general-cs |
| `--location` | montreal, canada, usa, unknown |
| `--term` | summer-2027, fall-2026, winter-2027, and so on |
| `--lang` | en, fr, bilingual |
| `--source` | greenhouse, lever, ashby, workday, and more |
| `--company` | one exact employer |
| `--all` | every match, not just the first page |
| `--last N` | look back N days instead of the default 14; `--last 0` removes the limit. On `search`, which already covers every row, it narrows instead |

Mistype one and Stage tells you what it accepts, rather than showing you an
empty list.

## The full-screen browser

```bash
stage tui
```

Type to search as you go. Number keys cycle the filters, `w` expands the full
description, `o` opens the posting in your browser, `e` exports what you are
looking at, and `s` saves a search for later. Press `?` for the full key list.

Same database as the CLI. The TUI cycles the three filters that are most useful
interactively — role, location, and language; `--term`, `--source`, and
`--company` stay CLI-only.

## Keeping it fresh

```bash
stage schedule enable     # sync quietly in the background
stage schedule status     # check on it
```

Opt-in, and it uses whatever scheduler your system already has: launchd,
systemd, or Task Scheduler. Nothing runs until you turn it on.

Want to hear about new postings without checking? Point Stage at a Discord
channel you manage:

```bash
stage schedule notify https://discord.com/api/webhooks/...
stage schedule test-notify        # make sure it works
```

Create the webhook in Discord under Edit Channel, Integrations, New Webhook —
in a channel you manage, on any server. Nothing to host: a webhook is just a URL
Stage posts to. After each scheduled sync, anything new lands in that channel.
`stage list --new` shows the same thing in your terminal.

Two things worth knowing. Notifications follow *scheduled* syncs, not a manual
`stage sync`. And scheduled syncs only run while the machine is awake — miss a
few, and you get one catch-up run afterwards rather than all of them.

<details>
<summary><b>Want notifications around the clock?</b></summary>

<br>

Put Stage on something that stays on — a VPS, a Raspberry Pi, an old laptop:

```bash
uv tool install stage-cli
stage sync                         # first sync, a few minutes
stage schedule notify <webhook>
stage schedule enable              # systemd, launchd, or Task Scheduler
```

That machine keeps its own database and posts to Discord on its own schedule.
Yours stays free for browsing.

</details>

## Good to know

- Everything sits in one SQLite file you own. Open it with any SQLite client,
  back it up, or delete it.
- The only network traffic is Stage reading public job boards. No account, no
  telemetry.
- Requests are rate limited per host, cached, and retried with backoff, so it
  does not hammer anyone's site.
- Four in five postings arrive with no description at all. Stage marks those
  fields `unknown` instead of guessing.
- Missing something you expected? `stage quarantine` shows which rule caught it.

## Learn more

`stage help` walks through the common workflows with real examples.
`stage help COMMAND` explains one command; `stage --help` lists them all.

[CHANGELOG.md](CHANGELOG.md) lists what changed in each release.

## Found a bug?

Open an issue — especially if a job board stopped working, since those break
without warning and I cannot watch all of them myself. I read everything.

I am not taking pull requests right now.

## Security

Stage reads public job boards and writes to a local file. It never asks for
credentials. Found a security issue? Open a
[security advisory](https://github.com/NicholasXydis/Stage/security/advisories/new)
instead of a public issue.

## License

MIT. See [LICENSE](LICENSE).

<div align="center">
<br>
<sub>Built for students who would rather be studying than refreshing career pages.</sub>
</div>
