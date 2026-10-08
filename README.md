# Telegram Reposter

A small, single-purpose Telegram bot that keeps a channel active by **reposting its own
old posts** on a randomised daily schedule.

It never generates content. It picks existing posts out of an archive and re-publishes
them with Telegram's own `copyMessage` / `copyMessages` API, so media stays on Telegram's
servers — nothing is downloaded or re-uploaded, and the result looks like a normal new
channel post (no *forwarded from* header, no bot branding).

```
telegram-reposter/
├── bot/
│   ├── main.py          entry point: startup, validation, shutdown
│   ├── config.py        .env loading + validation
│   ├── settings_store.py  runtime settings stored in SQLite, layered over .env
│   ├── database.py      SQLite schema and queries
│   ├── scheduler.py     daily plan generation + posting loop
│   ├── selector.py      content classification + selection strategy
│   ├── telegram.py      Telegram API gateway, error classification
│   └── handlers.py      admin commands, live channel listener, /scan
├── data/                default SQLite location (git-ignored)
├── selfcheck.py         offline sanity checks, no Telegram needed
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
└── README.md
```

---

## 1. What it needs from you

| Value | Where to get it |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `CHANNEL_ID` | `-100…` numeric id, or `@your_channel` |
| `ADMIN_USER_ID` | your own user id, from [@userinfobot](https://t.me/userinfobot) |

`ADMIN_USER_ID` is the **only** account that can talk to the bot. Everyone else gets
"⛔️ This bot is private." No configuration is ever echoed to Telegram.

---

## 2. Quick start

```bash
git clone <this repo> telegram-reposter && cd telegram-reposter
cp .env.example .env
$EDITOR .env            # BOT_TOKEN, CHANNEL_ID, ADMIN_USER_ID at minimum
docker compose up -d --build
docker compose logs -f
```

That is the whole deployment. On a VPS you get a `restart: unless-stopped` container that
survives reboots. The SQLite database lives in the `reposter-data` named volume — the
image runs as a non-root user (uid 10001), so a `./data` bind mount would not be writable
without a `chown`.

Inspect or back it up:

```bash
docker compose exec reposter ls -la /app/data
docker compose exec reposter python -c "import sqlite3;print(sqlite3.connect('/app/data/reposter.sqlite3').execute('select count(*) from posts').fetchone())"
docker run --rm -v telegram-reposter_reposter-data:/data -v "$PWD:/backup" alpine \
    tar czf /backup/reposter-backup.tgz -C /data .
```

<details>
<summary>Prefer host-visible files in <code>./data</code>?</summary>

Replace the volume line in `docker-compose.yml`:

```yaml
    volumes:
      - ./data:/app/data
```

and hand the directory to the container's user once:

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```
</details>

### Without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
.venv/bin/python -m bot.main
```

For a systemd unit, point `ExecStart` at `.venv/bin/python -m bot.main`,
`WorkingDirectory` at the project root, and `Restart=always`.

### Checking the setup without Telegram

```bash
.venv/bin/python selfcheck.py
```

Runs the scheduler and selector offline against a temporary database: verifies that times
always land inside the window with the required gaps, that the daily mix is covered, that
fallbacks and cooldowns behave, and that an interrupted slot is never re-sent.

---

## 3. Add the bot to the channel

1. Open the channel → **Manage Channel** → **Administrators** → **Add Admin**.
2. Search for your bot's `@username` and add it.
3. Give it at least these rights:
   - ✅ **Post Messages** — required, this is how reposts are published
   - ✅ **Delete Messages** — recommended; `/scan` deletes its own probe copies
   - ☐ Edit Messages — not used
4. Add the bot as a member of the channel **before** starting it. On startup the bot calls
   `getChat` and `getChatMember` and prints any permission problem to the log and to your
   DM, so misconfiguration is visible immediately.

The bot only needs outbound internet access. No ports, no webhook, no public URL.

---

## 4. Populating the archive (read this part)

**The Telegram Bot API cannot list a channel's message history.** There is no
`getChatHistory`, and being an administrator does not change that. The bot therefore
builds its archive in three ways:

### a) Automatically, going forward
As soon as the bot is an admin, every new post in the channel arrives as a `channel_post`
update and is archived (albums stored as one entry). Reposts the bot itself publishes are
recognised and **not** re-archived.

### b) Forwarding old posts — recommended for the backfill
Open the channel in Telegram, select the old posts you want in the archive (you can select
many at once with *Select → tap*), and **forward them to the bot's private chat**. Each
forward tells the bot the original `message_id` and date, and albums arrive as one logical
post. The whole burst is confirmed with a single reply a few seconds after you stop
forwarding — not one reply per message.

### c) `/scan <from_id> <to_id>` — walking the channel by id
```
/scan 1 1500        # probe ids 1…1500
/scan status        # progress
/scan stop          # cancel (resumes from the cursor next time)
```
Because there is no *getMessage*, the bot probes each id by copying it to your private chat
(or `SCAN_CHAT_ID`) and deleting the copy immediately. Anything that still exists is
archived. Expect notification noise and roughly one id per second.

Trade-offs of `/scan` versus forwarding:

| | forwarding | `/scan` |
|---|---|---|
| Albums | kept together | split into single messages |
| Original date | preserved | unknown (copying loses it) |
| Speed | instant | ~1 id/sec |
| Noise | none | one probe per id |

Both can be mixed freely. Re-importing the same content is idempotent — it never creates a
duplicate.

**Rule of thumb for a healthy archive:** at least 40–50 usable posts per category. With
`REPOST_COOLDOWN_DAYS=7` and 4 posts/day a category needs ~28 items to avoid ever relaxing
the cooldown.

---

## 5. How the daily scheduler works

**One plan per local day, generated once and stored in SQLite.** Restarting the bot does
not create a new plan — the existing rows are reused, and already-published slots keep
their `posted` status, so nothing is republished after a restart. A slot interrupted
mid-send is marked `unknown` and deliberately **not** re-sent; the bot DMs you about it.

**Times.** The `START_HOUR → END_HOUR` window (or `START_TIME → END_TIME` for a half-hour
boundary, or whatever `/set_start_time` and `/set_end_time` last set) is split into
`POSTS_PER_DAY` equal slots.
Each post is drawn uniformly from inside its slot, with a guard band of
`RANDOMIZATION_MINUTES` (capped at 15 % of a slot) so posts never bunch up at the edges,
and a random 0–59 s nudge so timestamps do not look generated. Times are then sorted and
`MIN_INTERVAL_MINUTES` is enforced by pushing items apart. Nothing can ever fall outside
the window.

With the defaults (08:00–22:00, 4 posts) a day looks like:

```
08:31 — TEXT
12:11 — IMAGE
16:42 — VIDEO
20:18 — AUDIO
```

**Categories.** `DAILY_MIX` defines the daily counts — by default one text, one photo, one
video and one audio post. The order is shuffled per day (seeded by the date, so it is
stable across restarts), which is what randomises the "morning text / afternoon photo"
pattern while still guaranteeing variety.

**Selection.** At the moment a slot fires, a post is chosen from the archive:

1. Posts of the slot's category are fetched.
2. Anything already used today is excluded — the same original can never appear twice in
   one day.
3. Posts reposted within `REPOST_COOLDOWN_DAYS` are excluded.
4. The rest are drawn **weighted-random**: never-reposted content outranks recently
   reused content, and older content outranks newer.
5. If the category has nothing left, the cooldown is relaxed (least-recently-used wins),
   then the fallback chain is walked — e.g. `VIDEO → IMAGE → FILE → AUDIO → TEXT` — and
   finally anything enabled is accepted. A slot only fails when the archive is empty.
6. If the chosen source post turns out to be deleted, it is disabled in the archive and
   another one is picked, up to `MAX_SEND_ATTEMPTS` times.

**Catch-up.** If a slot is more than `CATCHUP_MAX_LATE_MINUTES` late (default 90), it is
marked `missed` rather than published, so a long outage cannot produce a burst of posts.
`MIN_INTERVAL_MINUTES` is also honoured at publish time.

**Failure isolation.** Every slot is executed inside its own try/except. A Telegram error,
a deleted source message, a network hiccup or a permission problem is logged and the
scheduler moves on. Transient failures leave the slot pending for the next tick (every
20 s) and are abandoned after 5 consecutive attempts.

---

## 6. Commands

| Command | Effect |
|---|---|
| `/start` | Command list |
| `/status` | State, today's counters, next post, archive size and breakdown |
| `/today` | Today's full plan with status per slot |
| `/next` | The next scheduled post with a countdown |
| `/post [category]` | Publish one extra post right now (`TEXT`, `IMAGE`, `VIDEO`, `AUDIO`, `FILE`, `random`) — see §7 |
| `/skip` | Drop the next pending slot |
| `/pause` / `/resume` | Stop / restart publishing (the plan is kept) |
| `/settings` | Show the scheduling settings and where each value comes from |
| `/set_…` | Change a scheduling setting at runtime — see §7 |
| `/scan <from> <to>` | Build the archive by probing ids (`/scan stop`, `/scan status`) |
| `/archive` | Archive statistics and recent reposts |
| `/import` | Instructions for importing old posts |
| `/list [CATEGORY]` | Sample archive entries (`TEXT`, `IMAGE`, `VIDEO`, `AUDIO`, `FILE`) |

`/status` looks like:

```
🤖 Bot: ▶️ Running
📅 Today (2026-10-07, Asia/Tehran)
   planned 4 · posted 2 · remaining 2 · skipped 0 · failed 0
⏭ Next post: 16:42 — 🎬 VIDEO (in 3h 12m)
🗂 Archive: 842 posts
   📝 TEXT 210 · 🖼 IMAGE 180 · 🎬 VIDEO 120 · 🎵 AUDIO 90 · 📎 FILE 242
🕒 Posting window: 08:00–22:00
⚙️ 4/day · min gap 120m · cooldown 7d
```

---

## 7. Configuration reference

Everything lives in `.env` (see `.env.example`). Only the first three are required.

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | **required** |
| `CHANNEL_ID` | — | **required**, `-100…` or `@username` |
| `ADMIN_USER_ID` | — | **required** |
| `TIMEZONE` | `Asia/Tehran` | IANA zone for all scheduling; the VPS clock is ignored |
| `POSTS_PER_DAY` | `4` | Slots per day |
| `START_HOUR` / `END_HOUR` | `8` / `22` | Posting window, whole hours (`END_HOUR=24` means midnight) |
| `START_TIME` / `END_TIME` | — | Same window in `HH:MM`, for a half-hour start; wins over `START_HOUR`/`END_HOUR` |
| `MIN_INTERVAL_MINUTES` | `120` | Minimum gap between two posts |
| `RANDOMIZATION_MINUTES` | `60` | Jitter around each slot, capped at 15 % of a slot |
| `DAILY_MIX` | `TEXT,IMAGE,VIDEO,AUDIO` | Daily category counts |
| `REPOST_COOLDOWN_DAYS` | `7` | Minimum age before a post may be reused |
| `MAX_SEND_ATTEMPTS` | `3` | Different archive items tried per slot |
| `CATCHUP_MAX_LATE_MINUTES` | `90` | Drop a slot later than this |
| `IMPORT_ENABLED` | `true` | Accept forwarded posts into the archive |
| `EXTRA_SOURCE_CHAT_IDS` | — | Additional channels whose forwards may be imported |
| `SCAN_CHAT_ID` | admin DM | Where `/scan` sends its temporary probe copies |
| `SCAN_INTERVAL_SECONDS` | `0.7` | Delay between probes |
| `DATABASE_PATH` | `data/reposter.sqlite3` | SQLite file |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `LOG_FILE` | `data/bot.log` | Rotating log (2 MB × 3); set `none` for stdout only |
| `POLL_TIMEOUT` | `30` | Long-polling timeout |

### Changing the schedule from Telegram

Six of the variables above can be changed at runtime — no SSH, no `.env` edit, no restart:
`POSTS_PER_DAY`, the two window bounds, `MIN_INTERVAL_MINUTES`, `RANDOMIZATION_MINUTES` and
`REPOST_COOLDOWN_DAYS`.

```
/settings                          show the current values
/set_posts_per_day 6
/set_start_time 08:30
/set_end_time 23:00
/set_min_interval 90
/set_randomization 45
/set_cooldown 7
/set_cooldown default              drop the override, go back to .env
```

`/settings` answers with:

```
⚙️ Scheduling Settings

📌 Posts per day: 4
🕐 Time window: 08:00 → 22:00
⏱ Minimum interval: 120 minutes
🎲 Randomization: ±60 minutes
🔄 Repost cooldown: 7 days
```

**Where the values live.** `.env` is only the *default*. A changed value is written to the
`state` table in the same SQLite file as the archive (key `setting.posts_per_day`, …), so it
survives a container restart and a redeploy. On startup, and on every scheduler tick, the
defaults are re-read from `.env` and the saved values are layered on top, so a `docker
compose restart` — or changing `.env` — never quietly reverts what you set from Telegram.
`/settings` tells you how many values are currently coming from Telegram.

**What a change does to the plan.** Today is never rebuilt: published slots are history and
the pending ones are what you are currently expecting, so moving them could fire a post
early or lose one. Future days that are still untouched (every slot `pending`) are re-planned
at once with the new values; a future day with any non-pending slot — one dropped with
`/skip`, say — is left exactly as it is. The next day is always planned with the values in
force at the time, so a change is never more than one day from being fully in effect. A
change can therefore neither lose a post nor duplicate one.

**Manual `/post` is separate.** `/post video` publishes an extra post immediately; it does
not consume, move, delay, regenerate or delete any scheduled slot, and it does not change the
plan for today or tomorrow. The count is additive — 4 scheduled plus 1 manual is 5 posts. It
uses exactly the same selection, cooldown and duplicate protection as the scheduler, so
anything it publishes starts its cooldown and will not be picked again for
`REPOST_COOLDOWN_DAYS`.

---

## 8. Content categories

| Category | Telegram media types |
|---|---|
| `IMAGE` | photo |
| `VIDEO` | video, animation (GIF), video note |
| `AUDIO` | audio, voice message |
| `TEXT` | text-only posts, polls |
| `FILE` | documents, stickers |

An album takes the category of its "heaviest" member: video beats photo beats audio beats
document beats text. Albums are copied with `copyMessages`, so the whole group is
republished as one album.

---

## 9. Logging

```
2026-10-07 08:31:04 INFO     Generated schedule for 2026-10-07: 08:31 TEXT, 12:11 IMAGE, 16:42 VIDEO, 20:18 AUDIO
2026-10-07 16:42:03 INFO     Reposted message 1832 as video (VIDEO)
2026-10-07 20:17:21 INFO     Reposted message 921 as audio (AUDIO, IMAGE cooldown relaxed)
2026-10-07 21:02:55 ERROR    Slot 2026-10-07#3 has no content: archive is empty or every candidate is already used today
```

Startup, shutdown, schedule generation, every selection, every success, failure and skip
are logged. The token is never logged.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `BOT_TOKEN was rejected` | Wrong token in `.env` |
| `CHANNEL_ID … is not reachable` | Bot is not a member, wrong id, or channel is private and the id is wrong |
| `⚠️ the bot is only "member"` at startup | Promote it to administrator with **Post Messages** |
| `🚫 Cannot publish to the channel` | Permission removed, or the bot was kicked |
| `⚠️ … has no content` | Archive empty or all candidates used/disabled. Run `/scan` or forward posts |
| Posts look duplicated after a crash | Should not happen — interrupted slots are marked `unknown` and skipped |
| Nothing happens at the scheduled time | Check `/status` for the paused flag, and `docker compose logs` |
| Times are wrong | `TIMEZONE` is `Asia/Tehran` by default; the container runs in UTC on purpose |

Reset a single day's plan:

```bash
sqlite3 data/reposter.sqlite3 "DELETE FROM schedule WHERE day='2026-10-07';"
docker compose restart
```

---

## 11. Notes and limits

- **No content is downloaded.** `copyMessage` / `copyMessages` keep media on Telegram's
  servers and strip the forward header. Files that Telegram refuses to copy (extremely
  large ones, or protected content) are disabled in the archive and reported.
- **Protected content**: if the channel has *Restrict saving content* enabled, Telegram may
  refuse copies. Turn it off if you hit that.
- **Deleting the original is safe.** Reposts already published stay in the channel; a
  deleted source is simply disabled the next time it is picked.
- **One instance only.** Long polling allows exactly one running instance per token.
