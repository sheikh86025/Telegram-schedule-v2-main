# Telegram Auto Forward Scheduler Bot (Railway)

Saves every post from one source channel into a library and copies posts to your destination channels in timed batches. When a channel's queue runs out, it keeps going with **random posts from the library, without repeating until every post has been sent once**, then starts a new cycle.

## Deploy on Railway
1. Connect this GitHub repository to Railway.
2. **Add a Volume mounted at `/data` and set `DATABASE_PATH=/data/bot.db`** — otherwise the queue and channel list are lost on every redeploy.
3. Add Railway Variables (see below).
4. Add the bot to the source channel (admin recommended) and as admin (post permission) in every destination channel.

## Variables
| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | – | Token from BotFather |
| `ADMIN_IDS` | – | Comma-separated Telegram user IDs allowed to use commands |
| `SOURCE_CHANNEL_IDS` | – | Source channel IDs, comma separated (`SOURCE_CHANNEL_ID` also works) |
| `LOW_POST_THRESHOLD` | `10` | Low-post alert level (0 = off) |
| `DAILY_REPORT_HOUR` | `8` | Daily report hour, local time (-1 = off) |
| `MAX_IMPORT_RANGE` | `5000` | Max IDs per range import |
| `BACKUP_DELAY_SECONDS` | `3` | Pause between backup/restore copies |
| `AUTO_SNAPSHOT_HOURS` | `24` | Send the backup file to owners every N hours (0 = off) |
| `MAX_RESTORE_RANGE` | `20000` | Max IDs per range restore |
| `BACKUP_CHANNEL_ID` | – | Backup channel; enables automatic recovery |
| `DESTINATION_CHAT_IDS` | – | Destinations re-checked and re-added at every start |
| `DATABASE_PATH` | `./data/bot.db` | Use `/data/bot.db` with a Railway volume |
| `DEFAULT_INTERVAL_MINUTES` | `200` | Wait after each batch (new channels) |
| `BATCH_SIZE` | `12` | Posts per batch |
| `POST_DELAY_SECONDS` | `3` | Pause between posts inside a batch (avoids rate limits) |
| `POLL_SECONDS` | `30` | How often the scheduler looks for a due destination (30 is enough, fixed times are minute-exact) |
| `FAIL_ALERT_AFTER` | `3` | DM the admins after this many failed sends in a row for one destination |
| `TIMEZONE` | `Asia/Kolkata` | Timezone for quiet hours |
| `QUIET_START_HOUR` / `QUIET_END_HOUR` | `0` / `7` | No sending from 00:00 until 07:00 (set both equal to disable) |

## Destinations: channels AND groups
The source channel (`SOURCE_CHANNEL_ID`) is never a destination: the bot only collects posts from it.
Posts can go to channels, groups and supergroups (including a specific **topic** of a forum group).

**Add**
- **Easiest:** add the bot with an account listed in `ADMIN_IDS` — channel: as admin with *Post messages*; group: as a member. It registers itself and messages the admins. (If someone else adds the bot, it is ignored, so strangers cannot make it post in their groups.)
- **Many at once:** `/addmany` and paste all IDs / @usernames in one message, one per line (optional `interval=200`). The bot must already be in each chat; it reports what was added and what failed.
- **Inside a group/topic:** send `/addhere` (optional minutes). Posts go to that topic. If the command is not delivered, use `/addhere@YourBotUsername` or make the bot admin.
- **By ID/username:** `/addchannel -1001234567890`, `/addchannel @name`, optional minutes and `topic=ID` (e.g. `/addchannel -1001234567890 200 topic=7`).

**Remove**
- `/removechannel` -> tap the chat -> confirm, or `/removehere` inside a group, or `/removechannel CHAT_ID`.
- **Automatic:** if the bot is removed, demoted, loses the permission to post, or the chat/topic is gone, the destination is removed immediately. No retries; the admins get one notice. A group upgraded to a supergroup keeps its schedule (ID is moved automatically).

## Manage
- `/channels` — all destinations, interval, batch size, pending posts, next run
- `/setinterval CHAT_ID|all | MINUTES` — wait between batches
- `/setbatch CHAT_ID|all | POSTS` — posts per batch (default `BATCH_SIZE`)
- `/pause` / `/resume` `CHAT_ID|all` — stop/restart a destination without removing it
- `/sendnow CHAT_ID|all` — start a batch right away (quiet hours still apply)
- Reply to a post with `/broadcast` — send it to every destination now
In a group, `/pause`, `/resume`, `/sendnow`, `/setinterval 200` and `/setbatch 3` work without an ID.

## Automatic recovery after a reset or redeploy
Telegram does not let a bot list the chats it belongs to, so the bot needs a starting point. Set these **once** in Railway → Variables (`/railway_vars` prints the exact values):
- `BACKUP_CHANNEL_ID` — your backup channel. The bot keeps an up-to-date snapshot **pinned** there (needs the *Pin messages* admin right). After any reset, on start it reads that snapshot and restores sources, destinations, intervals, footer/button/times, admins and the post list by itself. It is still admin in the channels, so **nobody has to remove and re-add it**.
- `DESTINATION_CHAT_IDS` — optional extra safety: these chats are re-checked at every start and added automatically if the bot can post there.

Also: `/detect` runs the search on demand (`/detect -100…` to check specific IDs). If the bot sees activity in a channel/group it belongs to but that is not set up, it asks the admins with buttons (destination / source / ignore).
The bot never overwrites a snapshot it has not loaded, and never saves an empty state over a good one.

## Backup and restore (if the bot or Railway project is lost)
Two layers protect you:
1. **Backup channel** — a private channel (bot is admin) that holds a copy of every post. Telegram stores it permanently, so it survives the bot, Railway and even the source channels.
   - `/setbackup -100…` sets it. New posts are copied there automatically; `/backup` copies everything that exists now (runs in the background, ~20 posts/min; `/job` shows progress, `/stopjob` stops it, run `/backup` again to continue).
   - If an original post disappears (source channel deleted, bot removed from it), the bot silently switches to its backup copy.
2. **Backup file** — `/export_backup` sends a `scheduler-backup-….json` file with your sources, destinations, intervals, footer/button/times, admins and the post list. It is also sent **every day to the owners** (`AUTO_SNAPSHOT_HOURS`). Keep the latest one.

**Rebuild after a disaster**
1. Deploy the bot again (new token is fine) and make it admin in the backup channel, sources and destinations.
2. Send the backup file to the bot, reply to it with `/import_backup` (owner only). Everything comes back; destinations where the bot is not admin yet are listed.
3. Without a file: `/setbackup -100…` then `/restore_posts` ranges (below).

**Re-post everything into a channel** (e.g. a new, empty channel): `/restore_posts @newchannel` posts every backed-up post, oldest first. `/restore_posts @newchannel FIRST LAST` uses message IDs of the backup channel (works without a database), `/restore_posts all` goes into every destination. Restored posts are plain copies without footer/button/times.

Notes: the bot cannot read a channel's old posts by itself, so a post is only protected once it is in the backup channel. A backup file can hold about 50,000 posts (Telegram's 20 MB limit for bot downloads).

## Multiple sources and old posts
- Sources: set `SOURCE_CHANNEL_IDS` (comma separated) or use `/addsource -100…` (bot must be admin there), `/removesource`, `/sources`. New posts of every source are collected automatically.
- **Old posts** — Telegram bots cannot read a channel's history by themselves, so there are two ways:
  1. `/import_history` → forward the old posts to the bot (long-press a post, select many, Forward → bot) → `/done`. Posts forwarded from a source are matched by original ID, so duplicates are skipped.
  2. `/import_history SOURCE_ID FIRST_ID LAST_ID` — adds a whole range of post IDs (max 5000 per command). IDs that do not exist are removed automatically when the bot tries to send them.
  3. **5,000+ old posts, no manual forwarding:** run `tools/import_old_posts.py` once with your own Telegram account (it must be in `ADMIN_IDS`). It sends `/import_history`, forwards the whole channel history to the bot in small batches and sends `/done`, so the footer works on every post. The channel must already be a source (`/addsource`), forwarding must be allowed on it, and the bot must not be redeployed while it runs. New posts are collected live by the bot, so this is only for the backlog. Afterwards run `/backup` if you use a backup channel.
- Imported posts join the library and are sent in the random rotation (no repeats until all were used).
- **Undo / delete imported posts:** `/delete_history` shows buttons (per source, or ALL imported). Or type `/delete_history SOURCE_ID ID` (one post), `/delete_history SOURCE_ID FIRST LAST` (a range), `/delete_history all`. Everything except a single ID asks for confirmation. This only removes posts from the bot's library; nothing is deleted from your Telegram channels, and posts already sent stay where they are. Posts the bot collected live from a source are never touched by the per-source/all options.

## Counts and alerts
- `/stats` — library size per source, and per destination: sent in 24h, used, remaining, pending, cycles.
- **Low-post alert** when `LOW_POST_THRESHOLD` (default 10) unused posts are left; **all-posts-used alert** when every post has been sent and repeating starts.
- **Daily report** to the admins at `DAILY_REPORT_HOUR` (default 08:00, `-1` = off). Each destination is listed with its name **and chat ID**.

## Footer, button and fixed times
- `/setfooter all | text` or `/setfooter CHAT_ID | text` — added under every post (text posts and captions). Use `-` as text to switch it off for one destination. `/clearfooter`, `/branding`.
- `/setbutton all | Join Channel | https://t.me/yourchannel` — an inline button under the **last post of every batch** (the bot adds it after the batch). Up to 3 buttons: `/setbutton all | Text 1 | link 1 | Text 2 | link 2`. One destination: use its chat ID instead of `all`; switch it off there with `/setbutton CHAT_ID | -`. `/clearbutton all|CHAT_ID`. Buttons the source post already has are kept.
- `/settimes all | 09:00,14:00,21:00` — post at fixed times (24-hour, `TIMEZONE`) instead of every N minutes. At each time one batch is sent (`/setbatch`). Different times per destination: `/settimes CHAT_ID | 09:00,21:00`; back to the interval for one destination: `/settimes CHAT_ID | -`; `/cleartimes all|CHAT_ID`. In a group, `/settimes 09:00,21:00` works without an ID. If a time falls into the quiet hours, the batch is sent when they end. `/channels` shows the times.

## Send now
`/sendnow all` or `/sendnow CHAT_ID [posts]` sends immediately **even during quiet hours** and also un-pauses.

## Admins
Owners (`ADMIN_IDS`) can run `/addadmin USER_ID` (or reply to the person's message), `/removeadmin USER_ID`, `/admins`. Added admins can use everything except managing admins. A person finds their user ID by sending /start to the bot.

## How posting works
1. A new source post is given to **one** destination (true round-robin).
2. Each channel sends its pending posts first, in order.
3. When its queue is empty it picks random posts it has not sent yet in the current cycle. After every post has been sent, a new cycle starts (never the same post twice in a row).
4. After a batch, the channel waits its interval (or until its next fixed time, `/settimes`). Nothing is sent during quiet hours.
5. If a source post was deleted, it is removed from the library automatically.

## Limitations
- Albums are handled as separate messages, not as grouped albums.
- Edited source posts are not re-synced.
- Protected content cannot be copied.
- One destination per chat (a forum group can target one topic).
- Never commit bot tokens; use Railway Variables.

## Failure alerts
When a destination fails several times in a row (`FAIL_ALERT_AFTER`, default 3: network errors, rejected posts, permission checks that cannot finish), every admin gets a DM with the chat, the number of failures and the last error; a reminder follows at most every 6 hours, and the counter resets after the next successful batch. If the bot is removed or loses the right to post, the destination is dropped and the admins are told as before. Admins must have opened the bot once (send it /start), otherwise Telegram does not let it message them.
