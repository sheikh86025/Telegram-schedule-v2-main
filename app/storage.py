import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import DATABASE_PATH, DEFAULT_INTERVAL_MINUTES, SOURCE_IDS

# In-memory copies of two small tables, so hot paths never need a database call.
_SOURCES: set = set(SOURCE_IDS)
_EXTRA_ADMINS: set = set()


def is_source(chat_id) -> bool:
    """Source channels are only read from, never posted to."""
    try:
        return int(chat_id) in _SOURCES
    except (TypeError, ValueError):
        return False


def source_ids() -> set:
    return set(_SOURCES)


def extra_admin_ids() -> set:
    return set(_EXTRA_ADMINS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _connect():
    parent = os.path.dirname(DATABASE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


@contextmanager
def connection():
    db = _connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


NOT_SOURCE = "chat_id NOT IN (SELECT chat_id FROM sources)"


def _add_missing_columns(db, table: str, columns):
    existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _refresh_caches(db):
    global _SOURCES, _EXTRA_ADMINS
    _SOURCES = {int(r["chat_id"]) for r in db.execute("SELECT chat_id FROM sources")}
    _EXTRA_ADMINS = {int(r["user_id"]) for r in db.execute("SELECT user_id FROM admins")}


def init_db():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL DEFAULT 200,
            next_run TEXT,
            kind TEXT NOT NULL DEFAULT 'channel',
            thread_id INTEGER,
            paused INTEGER NOT NULL DEFAULT 0,
            batch_size INTEGER,
            force_now INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            caption TEXT,
            entities_json TEXT,
            reply_markup_json TEXT,
            created_at TEXT NOT NULL,
            content_type TEXT,
            file_id TEXT,
            media_group_id TEXT,
            imported INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS channel_queue (
            channel_id INTEGER NOT NULL,
            queue_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY(channel_id, queue_id),
            UNIQUE(channel_id, position)
        );
        CREATE TABLE IF NOT EXISTS channel_sent (
            channel_id INTEGER NOT NULL,
            queue_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(channel_id, queue_id)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS send_log (chat_id INTEGER NOT NULL, sent_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS backups (
            queue_id INTEGER PRIMARY KEY,
            backup_chat INTEGER NOT NULL,
            backup_message_id INTEGER NOT NULL,
            backed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_send_log ON send_log(chat_id, sent_at);
        """)
        # Upgrade databases created by older versions.
        _add_missing_columns(db, "channels", (
            ("kind", "TEXT NOT NULL DEFAULT 'channel'"),
            ("thread_id", "INTEGER"),
            ("paused", "INTEGER NOT NULL DEFAULT 0"),
            ("batch_size", "INTEGER"),
            ("force_now", "INTEGER NOT NULL DEFAULT 0"),
        ))
        _add_missing_columns(db, "queue", (
            ("content_type", "TEXT"),
            ("file_id", "TEXT"),
            ("media_group_id", "TEXT"),
            ("imported", "INTEGER NOT NULL DEFAULT 0"),
        ))
        # Posts added by an ID-range import carry no content at all (older databases did not mark them).
        db.execute("UPDATE queue SET imported=1 WHERE imported=0 AND content_type IS NULL AND entities_json IS NULL")
        # The watermark feature was removed: forget any old watermark text.
        db.execute("DELETE FROM settings WHERE key LIKE 'watermark:%'")
        # Sources from the environment variables are always present.
        for cid in SOURCE_IDS:
            db.execute("INSERT OR IGNORE INTO sources(chat_id,title,added_at) VALUES(?,?,?)", (cid, str(cid), _now()))
        # A source can never also be a destination (older versions could register it by mistake).
        for r in db.execute("SELECT chat_id FROM sources").fetchall():
            _delete_destination(db, int(r["chat_id"]))
        _refresh_caches(db)


# ------------------------------------------------------------------ sources

def add_source(chat_id: int, title: str):
    with connection() as db:
        db.execute(
            "INSERT INTO sources(chat_id,title,added_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
            (chat_id, title, _now()),
        )
        _delete_destination(db, chat_id)
        _refresh_caches(db)


def remove_source(chat_id: int) -> bool:
    with connection() as db:
        cur = db.execute("DELETE FROM sources WHERE chat_id=?", (chat_id,))
        _refresh_caches(db)
        return cur.rowcount > 0


def list_sources():
    with connection() as db:
        return db.execute(
            """SELECT s.chat_id, s.title, (SELECT COUNT(*) FROM queue q WHERE q.source_chat_id=s.chat_id) AS posts
               FROM sources s ORDER BY s.title"""
        ).fetchall()


# ------------------------------------------------------------------- admins

def add_admin(user_id: int, added_by: int):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (user_id, added_by, _now()))
        _refresh_caches(db)


def remove_admin(user_id: int) -> bool:
    with connection() as db:
        cur = db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        _refresh_caches(db)
        return cur.rowcount > 0


# ----------------------------------------------- destinations (channels + groups)

def register_channel_if_missing(chat_id: int, title: str, kind: str = "channel", thread_id=None):
    if is_source(chat_id):
        return
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO channels(chat_id,title,interval_minutes,next_run,kind,thread_id) VALUES(?,?,?,?,?,?)",
            (chat_id, title or str(chat_id), DEFAULT_INTERVAL_MINUTES, _now(), kind, thread_id),
        )


def add_channel(chat_id: int, title: str, interval_minutes: int, kind: str = "channel", thread_id=None):
    if is_source(chat_id):
        return
    with connection() as db:
        db.execute(
            """INSERT INTO channels(chat_id,title,interval_minutes,next_run,kind,thread_id) VALUES(?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                   interval_minutes=excluded.interval_minutes, kind=excluded.kind, thread_id=excluded.thread_id""",
            (chat_id, title, interval_minutes, _now(), kind, thread_id),
        )


def _update_all_or_one(db, assignments: str, values: tuple, target) -> int:
    if target == "all":
        return db.execute(f"UPDATE channels SET {assignments}", values).rowcount
    return db.execute(f"UPDATE channels SET {assignments} WHERE chat_id=?", values + (int(target),)).rowcount


def set_interval(target, minutes: int) -> int:
    with connection() as db:
        return _update_all_or_one(db, "interval_minutes=?", (minutes,), target)


def set_batch_size(target, size: int) -> int:
    with connection() as db:
        return _update_all_or_one(db, "batch_size=?", (size,), target)


def set_paused(target, paused: bool) -> int:
    with connection() as db:
        return _update_all_or_one(db, "paused=?", (1 if paused else 0,), target)


def send_now(target, count=None) -> int:
    """Start a batch right away, even during quiet hours. count=None -> the normal batch size."""
    with connection() as db:
        return _update_all_or_one(
            db, "next_run=?, paused=0, force_now=?", (_now(), int(count) if count else -1), target
        )


def clear_force(chat_id: int):
    with connection() as db:
        db.execute("UPDATE channels SET force_now=0 WHERE chat_id=?", (chat_id,))


def list_channels():
    with connection() as db:
        return db.execute(
            f"""SELECT c.*, (SELECT COUNT(*) FROM channel_queue cq WHERE cq.channel_id=c.chat_id) AS pending
                FROM channels c WHERE c.{NOT_SOURCE} ORDER BY c.kind, c.title"""
        ).fetchall()


def library_size() -> int:
    with connection() as db:
        return db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]


def _delete_destination(db, chat_id: int):
    db.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM channel_queue WHERE channel_id=?", (chat_id,))
    db.execute("DELETE FROM channel_sent WHERE channel_id=?", (chat_id,))
    keys = ("cycle", "usedalert", "usedalert_at", "lowalert", "footer", "button", "times", "fail", "failalert")
    db.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(keys))})", tuple(f"{k}:{chat_id}" for k in keys))


def remove_channel(chat_id: int):
    with connection() as db:
        _delete_destination(db, chat_id)


def migrate_channel(old_id: int, new_id: int):
    """A group was upgraded to a supergroup, so Telegram gave it a new chat ID."""
    with connection() as db:
        exists = db.execute("SELECT 1 FROM channels WHERE chat_id=?", (new_id,)).fetchone()
        if exists:
            _delete_destination(db, old_id)
            return
        db.execute("UPDATE channels SET chat_id=?, kind='supergroup' WHERE chat_id=?", (new_id, old_id))
        db.execute("UPDATE channel_queue SET channel_id=? WHERE channel_id=?", (new_id, old_id))
        db.execute("UPDATE channel_sent SET channel_id=? WHERE channel_id=?", (new_id, old_id))
        db.execute("UPDATE send_log SET chat_id=? WHERE chat_id=?", (new_id, old_id))


# ------------------------------------------------------------------- posts

def _next_round_robin_channel(db):
    ids = [
        int(r["chat_id"])
        for r in db.execute(f"SELECT chat_id FROM channels WHERE paused=0 AND {NOT_SOURCE} ORDER BY chat_id")
    ]
    if not ids:
        return None
    row = db.execute("SELECT value FROM settings WHERE key='rr_last'").fetchone()
    last = int(row["value"]) if row else None
    chosen = next((c for c in ids if last is None or c > last), ids[0])
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('rr_last',?)", (str(chosen),))
    return chosen


def _append_to_channel_queue(db, channel_id: int, queue_id: int):
    pos = db.execute(
        "SELECT COALESCE(MAX(position),0)+1 AS p FROM channel_queue WHERE channel_id=?", (channel_id,)
    ).fetchone()["p"]
    db.execute(
        "INSERT OR IGNORE INTO channel_queue(channel_id,queue_id,position) VALUES(?,?,?)",
        (channel_id, queue_id, pos),
    )


def add_post(source_chat_id: int, message_id: int, caption, entities_json, reply_markup_json,
             content_type=None, file_id=None, media_group_id=None, assign: bool = True, imported: bool = False):
    """Save a post in the library. With assign=True it is also given to ONE active destination (round-robin).
    Returns the new queue id, or None if this exact post was already saved."""
    with connection() as db:
        cur = db.execute(
            """INSERT OR IGNORE INTO queue(source_chat_id,message_id,caption,entities_json,reply_markup_json,
                                           created_at,content_type,file_id,media_group_id,imported)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (source_chat_id, message_id, caption, entities_json, reply_markup_json, _now(),
             content_type, file_id, media_group_id, 1 if imported else 0),
        )
        if cur.rowcount == 0:
            return None
        queue_id = cur.lastrowid
        if assign:
            channel_id = _next_round_robin_channel(db)
            if channel_id is not None:
                _append_to_channel_queue(db, channel_id, queue_id)
        return queue_id


def add_posts_range(source_chat_id: int, first_id: int, last_id: int):
    """Add message IDs first..last of a source channel to the library.
    IDs that do not exist (deleted posts, service messages) are cleaned up automatically when a send fails.
    Returns (added, already_present)."""
    rows = [(source_chat_id, mid, _now()) for mid in range(first_id, last_id + 1)]
    with connection() as db:
        cur = db.executemany(
            "INSERT OR IGNORE INTO queue(source_chat_id,message_id,created_at,imported) VALUES(?,?,?,1)", rows
        )
        added = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return added, len(rows) - added


def delete_post(queue_id: int):
    """Remove a post everywhere (used when the source message no longer exists)."""
    with connection() as db:
        db.execute("DELETE FROM queue WHERE id=?", (queue_id,))
        db.execute("DELETE FROM channel_queue WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM channel_sent WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM backups WHERE queue_id=?", (queue_id,))


def _pick_random(db, channel_id: int, exclude_id=None):
    sql = """SELECT q.* FROM queue q
             WHERE q.id NOT IN (SELECT queue_id FROM channel_sent WHERE channel_id=?)
               AND q.id NOT IN (SELECT queue_id FROM channel_queue WHERE channel_id=?)"""
    params = [channel_id, channel_id]
    if exclude_id is not None:
        sql += " AND q.id<>?"
        params.append(exclude_id)
    sql += " ORDER BY RANDOM() LIMIT 1"
    return db.execute(sql, params).fetchone()


def _setting(db, key: str, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _put(db, key: str, value):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def get_next_post(channel_id: int):
    """Next post for a destination.
    1) its own pending queue, in order;
    2) when empty: a random post it has NOT sent yet in the current cycle;
    3) when every post has been sent: start a new cycle (never the same post twice in a row)."""
    with connection() as db:
        row = db.execute(
            """SELECT q.* FROM queue q JOIN channel_queue cq ON cq.queue_id=q.id
               WHERE cq.channel_id=? ORDER BY cq.position LIMIT 1""",
            (channel_id,),
        ).fetchone()
        if row:
            return row

        row = _pick_random(db, channel_id)
        if row is None:
            total = db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            if total == 0:
                return None
            last = db.execute(
                "SELECT queue_id FROM channel_sent WHERE channel_id=? ORDER BY sent_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            db.execute("DELETE FROM channel_sent WHERE channel_id=?", (channel_id,))
            _put(db, f"cycle:{channel_id}", int(_setting(db, f"cycle:{channel_id}", 0)) + 1)
            exclude = int(last["queue_id"]) if (last and total > 1) else None
            row = _pick_random(db, channel_id, exclude)
        if row is None:
            return None
        _append_to_channel_queue(db, channel_id, int(row["id"]))
        return row


def finish_channel_post(channel_id: int, queue_id: int, count_as_sent: bool = True):
    with connection() as db:
        db.execute("DELETE FROM channel_queue WHERE channel_id=? AND queue_id=?", (channel_id, queue_id))
        db.execute(
            "INSERT OR REPLACE INTO channel_sent(channel_id,queue_id,sent_at) VALUES(?,?,?)",
            (channel_id, queue_id, _now()),
        )
        if count_as_sent:
            db.execute("INSERT INTO send_log(chat_id,sent_at) VALUES(?,?)", (channel_id, _now()))


# --------------------------------------------------------------- scheduling

def next_due_channel(now_iso: str, quiet: bool = False):
    """A forced batch (/sendnow) goes first and ignores quiet hours; normal batches wait during quiet hours."""
    with connection() as db:
        row = db.execute(
            f"SELECT * FROM channels WHERE force_now<>0 AND {NOT_SOURCE} ORDER BY chat_id LIMIT 1"
        ).fetchone()
        if row or quiet:
            return row
        return db.execute(
            f"SELECT * FROM channels WHERE paused=0 AND {NOT_SOURCE} AND (next_run IS NULL OR next_run<=?) "
            "ORDER BY COALESCE(next_run,'') LIMIT 1",
            (now_iso,),
        ).fetchone()


def postpone_channel(channel_id: int, next_run: str):
    with connection() as db:
        db.execute("UPDATE channels SET next_run=? WHERE chat_id=?", (next_run, channel_id))


# ----------------------------------------------------- footer / button / fixed-time settings

def set_branding(kind: str, target, value: str):
    with connection() as db:
        _put(db, f"{kind}:{target}", value)


def clear_branding(kind: str, target):
    with connection() as db:
        db.execute("DELETE FROM settings WHERE key=?", (f"{kind}:{target}",))


def get_branding(kind: str, chat_id: int):
    """Per-destination text wins over the global one. A stored '-' means 'none for this destination'."""
    with connection() as db:
        for key in (f"{kind}:{chat_id}", f"{kind}:all"):
            value = _setting(db, key)
            if value is not None:
                return None if value == "-" else value
    return None


def list_branding():
    with connection() as db:
        return db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'footer:%' OR key LIKE 'button:%' OR key LIKE 'times:%' ORDER BY key"
        ).fetchall()


# -------------------------------------------------------- counts and alerts

def pop_alerts(chat_id: int, threshold: int):
    """Alerts to show now for a destination, each at most once per situation:
    ('low', remaining)  - only a few unsent posts are left in the current cycle;
    ('all_used', cycles_done) - every post has been sent once; random repeats follow."""
    out = []
    with connection() as db:
        total = db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        sent = db.execute("SELECT COUNT(*) FROM channel_sent WHERE channel_id=?", (chat_id,)).fetchone()[0]
        remaining = max(0, total - sent)

        used_key = f"usedalert:{chat_id}"
        if total > 0 and remaining == 0:
            if _setting(db, used_key) is None:
                _put(db, used_key, _now())
                last_at = _setting(db, f"usedalert_at:{chat_id}")
                if last_at is None or last_at < _ago(hours=6):  # tiny libraries cycle often; do not spam
                    _put(db, f"usedalert_at:{chat_id}", _now())
                    out.append(("all_used", int(_setting(db, f"cycle:{chat_id}", 0)) + 1))
        elif remaining > 0:
            db.execute("DELETE FROM settings WHERE key=?", (used_key,))

        low_key = f"lowalert:{chat_id}"
        if threshold > 0 and total > 0 and 0 < remaining <= threshold:
            if _setting(db, low_key) is None:
                _put(db, low_key, _now())
                out.append(("low", remaining))
        elif remaining > threshold:
            db.execute("DELETE FROM settings WHERE key=?", (low_key,))
    return out


def stats():
    with connection() as db:
        total = db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        sources = db.execute(
            """SELECT s.chat_id, s.title, (SELECT COUNT(*) FROM queue q WHERE q.source_chat_id=s.chat_id) AS posts
               FROM sources s ORDER BY s.title"""
        ).fetchall()
        manual = db.execute(
            "SELECT COUNT(*) FROM queue WHERE source_chat_id NOT IN (SELECT chat_id FROM sources)"
        ).fetchone()[0]
        cutoff = _ago(hours=24)
        destinations = []
        for c in db.execute(f"SELECT * FROM channels WHERE {NOT_SOURCE} ORDER BY kind, title").fetchall():
            cid = int(c["chat_id"])
            sent = db.execute("SELECT COUNT(*) FROM channel_sent WHERE channel_id=?", (cid,)).fetchone()[0]
            destinations.append({
                "chat_id": cid, "title": c["title"], "kind": c["kind"], "paused": bool(c["paused"]),
                "pending": db.execute("SELECT COUNT(*) FROM channel_queue WHERE channel_id=?", (cid,)).fetchone()[0],
                "used": sent, "remaining": max(0, total - sent),
                "sent_24h": db.execute(
                    "SELECT COUNT(*) FROM send_log WHERE chat_id=? AND sent_at>=?", (cid, cutoff)
                ).fetchone()[0],
                "cycles": int(_setting(db, f"cycle:{cid}", 0)),
            })
        return {"total": total, "sources": sources, "manual": manual, "destinations": destinations}


def prune_send_log():
    with connection() as db:
        db.execute("DELETE FROM send_log WHERE sent_at<?", (_ago(days=14),))


def get_setting(key: str, default=None):
    with connection() as db:
        return _setting(db, key, default)


def set_setting(key: str, value):
    with connection() as db:
        _put(db, key, value)


def delete_settings(*keys: str):
    with connection() as db:
        for key in keys:
            db.execute("DELETE FROM settings WHERE key=?", (key,))


# ------------------------------------------------------------ backup channel

def get_backup_chat():
    value = get_setting("backup_chat")
    return int(value) if value else None


def set_backup_chat(chat_id):
    with connection() as db:
        if chat_id is None:
            db.execute("DELETE FROM settings WHERE key='backup_chat'")
        else:
            _put(db, "backup_chat", chat_id)


def get_post(queue_id: int):
    with connection() as db:
        return db.execute("SELECT * FROM queue WHERE id=?", (queue_id,)).fetchone()


def posts_needing_backup(backup_chat: int):
    """Library posts that have no copy in the backup channel yet (copies already in it are skipped)."""
    with connection() as db:
        return db.execute(
            """SELECT q.* FROM queue q LEFT JOIN backups b ON b.queue_id=q.id
               WHERE b.queue_id IS NULL AND q.source_chat_id<>?
               ORDER BY q.source_chat_id, q.message_id""",
            (backup_chat,),
        ).fetchall()


def save_backup(queue_id: int, backup_chat: int, backup_message_id: int):
    with connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO backups(queue_id,backup_chat,backup_message_id,backed_at) VALUES(?,?,?,?)",
            (queue_id, backup_chat, backup_message_id, _now()),
        )


def backup_counts(backup_chat):
    """(posts with a backup copy, posts that should have one)."""
    with connection() as db:
        if backup_chat is None:
            return 0, db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM queue WHERE source_chat_id<>?", (backup_chat,)).fetchone()[0]
        done = db.execute(
            "SELECT COUNT(*) FROM queue q JOIN backups b ON b.queue_id=q.id WHERE q.source_chat_id<>?", (backup_chat,)
        ).fetchone()[0]
        return done, total


def restore_items(backup_chat: int):
    """Every post that exists in the backup channel, in order: [(message_id in backup channel, reply_markup_json)]."""
    with connection() as db:
        rows = db.execute(
            """SELECT b.backup_message_id AS mid, q.reply_markup_json AS markup
               FROM backups b JOIN queue q ON q.id=b.queue_id WHERE b.backup_chat=?
               UNION ALL
               SELECT q.message_id AS mid, q.reply_markup_json AS markup FROM queue q WHERE q.source_chat_id=?
               ORDER BY mid""",
            (backup_chat, backup_chat),
        ).fetchall()
        return [(int(r["mid"]), r["markup"]) for r in rows]


def use_backup_copy(queue_id: int) -> bool:
    """The original of a post is gone: point it at its copy in the backup channel. False if there is no copy."""
    with connection() as db:
        row = db.execute(
            "SELECT backup_chat, backup_message_id FROM backups WHERE queue_id=?", (queue_id,)
        ).fetchone()
        if not row:
            return False
        db.execute("DELETE FROM backups WHERE queue_id=?", (queue_id,))
        try:
            db.execute(
                "UPDATE queue SET source_chat_id=?, message_id=? WHERE id=?",
                (row["backup_chat"], row["backup_message_id"], queue_id),
            )
        except sqlite3.IntegrityError:  # that copy is already in the library as its own post
            db.execute("DELETE FROM queue WHERE id=?", (queue_id,))
            db.execute("DELETE FROM channel_queue WHERE queue_id=?", (queue_id,))
            db.execute("DELETE FROM channel_sent WHERE queue_id=?", (queue_id,))
        return True


# ---------------------------------------------------------- snapshot file

SNAPSHOT_FORMAT = "scheduler-backup-v1"


def export_snapshot() -> dict:
    """Everything needed to rebuild the bot on a fresh deployment (no media, no tokens)."""
    with connection() as db:
        posts = []
        for r in db.execute(
            """SELECT q.*, b.backup_chat AS bchat, b.backup_message_id AS bmid
               FROM queue q LEFT JOIN backups b ON b.queue_id=q.id ORDER BY q.id"""
        ).fetchall():
            posts.append({
                "source_chat_id": r["source_chat_id"], "message_id": r["message_id"], "caption": r["caption"],
                "entities_json": r["entities_json"], "reply_markup_json": r["reply_markup_json"],
                "content_type": r["content_type"], "media_group_id": r["media_group_id"],
                "imported": r["imported"], "backup_chat": r["bchat"], "backup_message_id": r["bmid"],
            })
        return {
            "format": SNAPSHOT_FORMAT,
            "created_at": _now(),
            "backup_chat": _setting(db, "backup_chat"),
            "sources": [dict(r) for r in db.execute("SELECT chat_id, title FROM sources")],
            "destinations": [
                dict(r) for r in db.execute(
                    "SELECT chat_id,title,kind,thread_id,interval_minutes,batch_size,paused FROM channels"
                )
            ],
            "admins": [int(r["user_id"]) for r in db.execute("SELECT user_id FROM admins")],
            "settings": {
                r["key"]: r["value"] for r in db.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'footer:%' OR key LIKE 'button:%' OR key LIKE 'times:%'"
                )
            },
            "posts": posts,
        }


def import_snapshot(data: dict) -> dict:
    """Merge a snapshot into this database (nothing existing is overwritten). Destinations are returned for the
    caller to check and add, because only the bot can tell whether it may post there."""
    if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("This is not a backup file made by this bot.")
    summary = {"posts_added": 0, "posts_existing": 0, "sources": 0, "admins": 0, "settings": 0,
               "destinations": data.get("destinations") or []}
    with connection() as db:
        for s in data.get("sources") or []:
            cur = db.execute(
                "INSERT OR IGNORE INTO sources(chat_id,title,added_at) VALUES(?,?,?)",
                (int(s["chat_id"]), str(s.get("title") or s["chat_id"]), _now()),
            )
            summary["sources"] += cur.rowcount
        for uid in data.get("admins") or []:
            summary["admins"] += db.execute(
                "INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (int(uid), None, _now())
            ).rowcount
        for key, value in (data.get("settings") or {}).items():
            if key.startswith(("footer:", "button:", "times:")):
                summary["settings"] += db.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value))
                ).rowcount
        if data.get("backup_chat") and _setting(db, "backup_chat") is None:
            _put(db, "backup_chat", int(data["backup_chat"]))

        for p in data.get("posts") or []:
            src, mid = int(p["source_chat_id"]), int(p["message_id"])
            cur = db.execute(
                """INSERT OR IGNORE INTO queue(source_chat_id,message_id,caption,entities_json,reply_markup_json,
                                               created_at,content_type,media_group_id,imported)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (src, mid, p.get("caption"), p.get("entities_json"), p.get("reply_markup_json"), _now(),
                 p.get("content_type"), p.get("media_group_id"), int(p.get("imported") or 0)),
            )
            if cur.rowcount:
                summary["posts_added"] += 1
            else:
                summary["posts_existing"] += 1
            if p.get("backup_chat") and p.get("backup_message_id"):
                row = db.execute("SELECT id FROM queue WHERE source_chat_id=? AND message_id=?", (src, mid)).fetchone()
                if row:
                    db.execute(
                        "INSERT OR IGNORE INTO backups(queue_id,backup_chat,backup_message_id,backed_at) VALUES(?,?,?,?)",
                        (row["id"], int(p["backup_chat"]), int(p["backup_message_id"]), _now()),
                    )
        for r in db.execute("SELECT chat_id FROM sources").fetchall():
            _delete_destination(db, int(r["chat_id"]))
        _refresh_caches(db)
    return summary


def state_fingerprint() -> str:
    """Cheap summary of everything a snapshot holds; changes whenever that data changes."""
    with connection() as db:
        parts = [
            tuple(db.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM queue").fetchone()),
            tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(chat_id),0), COALESCE(SUM(interval_minutes),0), COALESCE(SUM(paused),0), "
                "COALESCE(SUM(COALESCE(batch_size,0)),0), COALESCE(SUM(COALESCE(thread_id,0)),0) FROM channels"
            ).fetchone()),
            tuple(db.execute("SELECT COUNT(*), COALESCE(SUM(chat_id),0) FROM sources").fetchone()),
            tuple(db.execute("SELECT COUNT(*), COALESCE(SUM(user_id),0) FROM admins").fetchone()),
            tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(value)),0) FROM settings "
                "WHERE key LIKE 'footer:%' OR key LIKE 'button:%' OR key LIKE 'times:%'"
            ).fetchone()),
            tuple(db.execute("SELECT COUNT(*), COALESCE(MAX(backup_message_id),0) FROM backups").fetchone()),
            _setting(db, "backup_chat"),
        ]
        return repr(parts)
