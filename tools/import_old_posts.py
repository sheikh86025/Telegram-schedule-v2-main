"""Import ALL old posts of a source channel into the scheduler bot - no manual forwarding.

Run once, with YOUR OWN Telegram account. It
  1. sends /import_history to the bot (import mode),
  2. forwards the source channel's old posts to the bot in small batches,
  3. sends /done and prints the bot's summary.

The bot saves them exactly like posts you forward by hand, so footer and watermark work too.
New posts that arrive later are collected by the bot by itself - this script is only for the backlog.

Before you start
  * your account must be in ADMIN_IDS of the bot
  * the channel must already be a source:  /addsource -100...   (bot is admin there)
  * do NOT redeploy or restart the bot while this runs
  * the channel must allow forwarding ("Restrict saving content" off)

Run:    pip install telethon          then      python import_old_posts.py
Colab:  !pip -q install telethon nest_asyncio     then      %run import_old_posts.py
Afterwards delete import_session.session (it is a login to your account) and never share API_HASH.
"""
import asyncio

from telethon import TelegramClient, errors

API_ID = ""          # my.telegram.org -> API development tools (leave empty to be asked)
API_HASH = ""
BOT_USERNAME = ""    # the scheduler bot's username, with or without @
SOURCE = ""          # -100xxxxxxxxxx or @username of the source channel
START_ID = 0         # resume point: only posts with ID >= START_ID (0 = from the beginning)

BATCH_SIZE = 50             # posts per forward request (Telegram allows up to 100)
PAUSE_SECONDS = 3           # pause between batches, keeps Telegram happy
FINISH_WAIT_SECONDS = 30    # let the bot finish saving before /done is sent


def ask(label, value):
    value = str(value or "").strip()
    while not value:
        value = input(f"{label}: ").strip()
    return value


async def forward_batch(client, bot, source, ids):
    """Forward one batch and wait out flood limits. True when it went through."""
    for _ in range(6):
        try:
            await client.forward_messages(bot, ids, from_peer=source)
            return True
        except errors.FloodWaitError as e:
            wait = int(e.seconds) + 2
            print(f"   Telegram asks to wait {wait}s ...")
            await asyncio.sleep(wait)
        except errors.RPCError as e:
            name = type(e).__name__
            if "forwards" in name.lower() or "FORWARDS" in str(e).upper():
                raise SystemExit("This channel has 'Restrict saving content' on, so posts cannot be forwarded.")
            print(f"   batch {ids[0]}-{ids[-1]} failed: {name}")
            return False
    return False


async def main():
    api_id = int(ask("API_ID", API_ID))
    api_hash = ask("API_HASH", API_HASH)
    bot_name = ask("Bot username", BOT_USERNAME).lstrip("@")
    source_ref = ask("Source channel (-100... or @username)", SOURCE)
    start_id = int(START_ID or 0)

    async with TelegramClient("import_session", api_id, api_hash) as client:  # asks phone + code on first run
        await client.get_dialogs()  # fills the cache so numeric IDs resolve
        source = await client.get_entity(int(source_ref) if source_ref.lstrip("-").isdigit() else source_ref)
        bot = await client.get_entity(bot_name)
        total = (await client.get_messages(source, limit=0)).total
        print(f"\nSource: {getattr(source, 'title', source_ref)} - about {total} posts")
        answer = input("Is it already added with /addsource, and is this account in ADMIN_IDS? (y/n): ")
        if answer.strip().lower() != "y":
            return print("Do that first, then run this again.")

        await client.send_message(bot, "/import_history")
        await asyncio.sleep(3)

        state = {"batch": [], "done": 0}
        failed = []

        async def flush():
            batch = state["batch"]
            if not batch:
                return
            if await forward_batch(client, bot, source, batch):
                state["done"] += len(batch)
            else:
                failed.append((batch[0], batch[-1]))
            print(f"   {state['done']} forwarded (last ID {batch[-1]})")
            state["batch"] = []
            await asyncio.sleep(PAUSE_SECONDS)

        async for msg in client.iter_messages(source, reverse=True, min_id=max(0, start_id - 1)):
            if msg.action is not None or not (msg.message or msg.media):
                continue  # service messages and empty posts
            state["batch"].append(msg.id)
            if len(state["batch"]) >= BATCH_SIZE:
                await flush()
        await flush()

        print(f"\nForwarded {state['done']} posts. Waiting for the bot to finish saving ...")
        await asyncio.sleep(FINISH_WAIT_SECONDS)
        await client.send_message(bot, "/done")
        await asyncio.sleep(5)
        async for reply in client.iter_messages(bot, limit=5):
            if not reply.out and reply.message and "Import finished" in reply.message:
                print("\nBot says:\n" + reply.message)
                break
        else:
            print("Open the bot chat and check its /done message.")
        if failed:
            print("\nThese ID ranges failed, run again with START_ID set to the first one:", failed)


def run():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(main())
    import nest_asyncio  # Colab / Jupyter already run an event loop
    nest_asyncio.apply()
    loop.run_until_complete(main())


if __name__ == "__main__":
    run()
