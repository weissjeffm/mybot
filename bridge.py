import asyncio
import os
import json
import traceback
import aiohttp
from nio import AsyncClient, RoomMessageText, RoomMessageAudio, RoomEncryptedAudio, InviteMemberEvent, MegolmEvent, ToDeviceEvent, Event, LoginError
from callbacks import process_message

class MatrixBot:
    def __init__(self):
# 1. Identity & Credentials Logic
        self.matrix_url = os.getenv("MATRIX_URL", "https://matrix.dumaweiss.com")
        self.matrix_user = os.getenv("MATRIX_USER", "@anton:dumaweiss.com")
        self.matrix_pass = os.getenv("MATRIX_PASS", "password")
        self._display_name_cache = None
        # Derive short_name: @weissbot:matrix.org -> weissbot
        try:
            self._short_name = self.matrix_user.split(":")[0].lstrip("@")
        except Exception:
            self._short_name = "bot"

        # 2. Storage & State
        self.user_cache = {}
        self.pending_events = {}
        self.ui_lock = asyncio.Lock()
        
        store_folder = "./crypto_store"
        if not os.path.exists(store_folder):
            os.makedirs(store_folder)

        self.client = AsyncClient(
            self.matrix_url,
            self.matrix_user,
            store_path=store_folder
        )

    @property
    def short_name(self):
        return self._short_name

    @property
    async def display_name(self):
        """
        Retrieves the bot's current display name from the server.
        Memoized to prevent redundant network calls.
        """
        if self._display_name_cache:
            return self._display_name_cache

        try:
            # Fetch the actual profile display name from Matrix
            resp = await self.client.get_displayname(self.client.user_id)
            if resp.displayname:
                self._display_name_cache = resp.displayname
                return self._display_name_cache
            return "Noname"
        except Exception as e:
            print(f"⚠️ Could not fetch display name: {e}")
        
        # Fallback to the localpart of the MXID if no display name is set
        return self._mxid_localpart or "Noname"

    async def start(self):
        creds_file = "credentials.json"
        
        # --- 1. LOGIN / AUTHENTICATION ---
        if os.path.exists(creds_file):
            with open(creds_file, "r") as f:
                creds = json.load(f)
            self.client.access_token = creds["access_token"]
            self.client.user_id = creds["user_id"]
            self.client.device_id = creds["device_id"]
            self.client.load_store()
        else:
            resp = await self.client.login(os.getenv("MATRIX_PASS", "password"))
            if isinstance(resp, LoginError):
                print(f"❌ Login failed: {resp.message}")
                raise RuntimeError("Failed to log in to Matrix.")
            else:
                print(f"✅ Logged in as {resp.user_id}")
                with open(creds_file, "w") as f:
                    json.dump({
                        "access_token": resp.access_token,
                        "user_id": resp.user_id,
                        "device_id": resp.device_id
                    }, f)
                self.client.access_token = resp.access_token
                self.client.user_id = resp.user_id
                self.client.device_id = resp.device_id

        # --- 2. CRYPTO SETUP ---
        try: await self.client.keys_upload()
        except: pass

        # --- 3. TOKEN RESTORATION ---
        # Initialize next_batch to None or load from file
        self.client.next_batch = None
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f: 
                self.client.next_batch = f.read().strip()

        # --- 4. CALLBACKS ---
        self.client.add_event_callback(self.msg_handler, RoomMessageText)
        self.client.add_event_callback(self.msg_handler, RoomMessageAudio)
        self.client.add_event_callback(self.msg_handler, RoomEncryptedAudio)
        self.client.add_event_callback(self.invite_handler, InviteMemberEvent)
        self.client.add_event_callback(self.decrypt_fail_handler, MegolmEvent)
        
        # --- 5. INITIAL SYNC (Optional but recommended) ---
        # If we have no token, do one heavy sync to get the current state and a token.
        if not self.client.next_batch:
            print("🚀 Performing initial sync...")
            try:
                # We use full_state=True ONLY here, once.
                first_sync = await self.client.sync(timeout=30000, full_state=True)
                if isinstance(first_sync, SyncResponse):
                    self.client.next_batch = first_sync.next_batch
                    with open("next_batch", "w") as f: f.write(first_sync.next_batch)
            except Exception as e:
                print(f"⚠️ Initial sync failed: {e}")

        print(f"🤖 Bot is live. Listening from token: {self.client.next_batch}")

        # --- 6. THE MAIN LOOP (Fixed) ---
        while True:
            try:
                sync_resp = await self.client.sync(
                    timeout=30000, 
                    since=self.client.next_batch,
                    full_state=False             
                )
                
                # Update token for the next iteration
                if hasattr(sync_resp, "next_batch"):
                    self.client.next_batch = sync_resp.next_batch
                    with open("next_batch", "w") as f: f.write(sync_resp.next_batch)
            
            except Exception:
                traceback.print_exc()
                # Keep this sleep! It prevents hammering if the server goes down (502s)
                await asyncio.sleep(5)

    async def msg_handler(self, room, event): await process_message(self, room, event)
    async def invite_handler(self, room, event): await self.client.join(room.room_id)
    async def decrypt_fail_handler(self, room, event): await self.client.request_room_key(event)
    
if __name__ == "__main__":
    bot = MatrixBot()
    asyncio.run(bot.start())
    
