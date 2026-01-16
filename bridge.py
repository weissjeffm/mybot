import asyncio
import os
import json
import traceback
import aiohttp
from nio import AsyncClient, RoomMessageText, RoomMessageAudio, RoomEncryptedAudio, InviteMemberEvent, MegolmEvent, ToDeviceEvent, Event
from callbacks import process_message

class MatrixBot:
    def __init__(self):
        self.user_cache = {}
        self.pending_events = {}
        self.ui_lock = asyncio.Lock()
        
        store_folder = "./crypto_store"
        if not os.path.exists(store_folder): os.makedirs(store_folder)

        self.client = AsyncClient(
            os.getenv("MATRIX_URL", "https://matrix.org"),
            "@weissbot:matrix.org",
            store_path=store_folder
        )

    async def start(self):
        creds_file = "credentials.json"
        if os.path.exists(creds_file):
            with open(creds_file, "r") as f:
                creds = json.load(f)
            self.client.access_token, self.client.user_id, self.client.device_id = creds["access_token"], creds["user_id"], creds["device_id"]
            self.client.load_store()
            await self.client.sync(timeout=30000, full_state=True)
        else:
            resp = await self.client.login(os.getenv("MATRIX_PASS", "password"))
            with open(creds_file, "w") as f:
                json.dump({"access_token": resp.access_token, "user_id": resp.user_id, "device_id": resp.device_id}, f)

        try: await self.client.keys_upload()
        except: pass

        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f: self.client.next_batch = f.read().strip()

        # Callbacks
        self.client.add_event_callback(self.msg_handler, RoomMessageText)
        self.client.add_event_callback(self.msg_handler, RoomMessageAudio)
        self.client.add_event_callback(self.msg_handler, RoomEncryptedAudio)
        self.client.add_event_callback(self.invite_handler, InviteMemberEvent)
        self.client.add_event_callback(self.decrypt_fail_handler, MegolmEvent)
        
        print("🤖 Bot is live.")
        while True:
            try:
                sync_resp = await self.client.sync(timeout=30000, full_state=True)
                if hasattr(sync_resp, "next_batch"):
                    self.client.next_batch = sync_resp.next_batch
                    with open("next_batch", "w") as f: f.write(sync_resp.next_batch)
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(5)

    async def msg_handler(self, room, event): await process_message(self, room, event)
    async def invite_handler(self, room, event): await self.client.join(room.room_id)
    async def decrypt_fail_handler(self, room, event): await self.client.request_room_key(event)

if __name__ == "__main__":
    bot = MatrixBot()
    asyncio.run(bot.start())
    
