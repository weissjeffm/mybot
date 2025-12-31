import asyncio
import os
import json
import markdown
import time
import uuid
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 

from nio import (
    AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent, MegolmEvent,
    LocalProtocolError, ToDeviceEvent
)

import logging
import sys

# # Configure logging to print to the terminal
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
#     stream=sys.stdout
# )

# # Force 'nio' to be verbose so we see the store failure
# logging.getLogger('nio').setLevel(logging.DEBUG)

# Config
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
        
        self.user_cache = {} # Caches 'User ID' -> 'Display Name'
        # We MUST provide a store_path. 
        # The bot will create a SQLite DB here to store encryption keys.
        # This folder MUST be persistent (backed up).
        store_folder = "./crypto_store" 
        if not os.path.exists(store_folder):
            os.makedirs(store_folder)

        self.client = AsyncClient(
            MATRIX_URL, 
            "@weissbot:matrix.org",
            store_path=store_folder
        )

        self.pending_events = {} # Stores session_id -> list of MegolmEvents

    async def start(self):
        # --- 1. PERSISTENT LOGIN LOGIC ---
        creds_file = "credentials.json"
        
        if os.path.exists(creds_file):
            print("💾 Found saved credentials. Restoring session...")
            with open(creds_file, "r") as f:
                creds = json.load(f)
            
            self.client.access_token = creds["access_token"]
            self.client.user_id = creds["user_id"]
            self.client.device_id = creds["device_id"]
            
            # This loads the keys from the database without creating a new device
            await self.client.sync(timeout=30000, full_state=True) 
            
        else:
            print(f"🆕 No credentials found. Logging in as {MATRIX_USER}...")
            resp = await self.client.login(MATRIX_PASS)
            
            if isinstance(resp, LocalProtocolError):
                print(f"❌ Login failed: {resp}")
                return

            # SAVE the credentials so we are the SAME device next time
            print(f"💾 Saving new credentials to {creds_file}...")
            with open(creds_file, "w") as f:
                json.dump({
                    "access_token": resp.access_token,
                    "user_id": resp.user_id,
                    "device_id": resp.device_id
                }, f)

        print(f"🤖 BOT DEVICE ID: {self.client.device_id}")

        # --- 2. RESUME SYNC TOKEN ---
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                self.client.next_batch = f.read().strip()

        # --- 3. REGISTER CALLBACKS ---
        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        self.client.add_event_callback(self.decryption_failure_callback, MegolmEvent)
        self.client.add_to_device_callback(self.cb_key_arrived, ToDeviceEvent)
        
        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        while True:
            try:
                sync_response = await self.client.sync(timeout=30000, full_state=True)
                
                if hasattr(sync_response, "next_batch"):
                    self.client.next_batch = sync_response.next_batch
                    with open("next_batch", "w") as f:
                        f.write(sync_response.next_batch)
                        
            except Exception as e:
                print(f"Sync error: {e}")
                await asyncio.sleep(5)
                
    async def invite_callback(self, room: MatrixRoom, event: InviteMemberEvent):
        """Auto-join rooms."""
        print(f"Invite received for room {room.room_id}!")
        await self.client.join(room.room_id)

    async def get_display_name(self, user_id):
        """Resolves User ID to Name with caching."""
        if user_id in self.user_cache:
            return self.user_cache[user_id]
        try:
            resp = await self.client.get_displayname(user_id)
            name = resp.displayname if resp.displayname else user_id
            self.user_cache[user_id] = name
            return name
        except:
            return user_id

    async def get_thread_history(self, room_id, thread_root_id, limit=30):
        """Fetches and formats thread history."""
        response = await self.client.room_messages(room_id, limit=limit)
        if not response.chunk: return ""

        thread_events = []
        for event in response.chunk:
            if not isinstance(event, RoomMessageText): continue
            
            # Check if this event belongs to the thread
            evt_id = event.event_id
            relates = event.source.get('content', {}).get('m.relates_to', {})
            parent_id = relates.get('event_id')
            
            if evt_id == thread_root_id or parent_id == thread_root_id:
                sender = "AI_Agent" if event.sender == self.client.user_id else await self.get_display_name(event.sender)
                # Skip tool logs to keep context clean
                if "⚙️" in event.body: continue
                thread_events.append(f"{sender}: {event.body}")

        thread_events.reverse()
        return "\n".join(thread_events)

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        if event.sender == self.client.user_id: return

        # --- 1. GATEKEEPER (Group Chat Logic) ---
        is_direct = room.member_count <= 2
        is_mentioned = (self.client.user_id in event.body) or ("weissbot" in event.body.lower())
        
        # Only reply if DM or Mentioned
        if not (is_direct or is_mentioned):
            return

        # Clean the prompt (remove bot name)
        clean_body = event.body.replace(self.client.user_id, "").replace("weissbot", "", 1).strip()
        sender_name = await self.get_display_name(event.sender)
        # check if the user is just trying to verify via the matrix protocol
        if clean_body == "!verify":
            print(f"🔐 Scheduling verification request for {sender_name}...")
            
            # DEFINE THE TASK
            async def send_verify():
                try:
                    # Construct the content
                    content = {
                        "body": "🔐 Verification Request",
                        "msgtype": "m.key.verification.request",
                        "to": event.sender,
                        "from_device": self.client.device_id,
                        "methods": ["m.sas.v1"],
                        "timestamp": int(time.time() * 1000),
                        "transaction_id": str(uuid.uuid4())
                    }
                    
                    # SEND IT
                    await self.client.room_send(
                        room.room_id,
                        message_type="m.room.message",
                        content=content
                    )
                    print("🔐 Request SENT successfully!")
                except Exception as e:
                    print(f"🔐 Send failed: {e}")

            # EXECUTE IN BACKGROUND (Breaks the Deadlock)
            asyncio.create_task(send_verify())
            return
       
        print(f"Processing request from {sender_name}: {clean_body}")

        # --- 2. DETERMINE ROOT ---
        content = event.source.get('content', {})
        relates_to = content.get('m.relates_to', {})
        is_in_thread = relates_to.get('rel_type') == 'm.thread'
        
        # Current Thread Root (or the message itself if new)
        thread_root_id = relates_to.get('event_id') if is_in_thread else event.event_id

        await self.client.room_typing(room.room_id, True, timeout=60000)

        # --- 3. EXECUTION STATE ---
        # We track where we should send logs/answers.
        # It starts as the current root, but might change if 'signal_topic_change' is called.
        state = {"current_root": thread_root_id}

        try:
            # --- 4. TOOL LOGGING CALLBACK ---
            async def log_callback(text):
                print(f"DEBUG LOG: {text}") 

                # CHECK FOR JSON SIGNAL
                if "TOPIC_CHANGE" in text and "{" in text:
                    try:
                        json_start = text.find('{')
                        json_str = text[json_start:] 
                        data = json.loads(json_str)
                        
                        if data.get("signal") == "TOPIC_CHANGE":
                            subject = data.get("subject", "New Topic")
                            
                            # --- LINK GENERATION ---
                            # Create a permalink to the User's original trigger message
                            original_link = f"https://matrix.to/#/{room.room_id}/{event.event_id}"
                            
                            # ACTION: Create New Thread Header with Context Link
                            new_header = await self.client.room_send(
                                room_id=room.room_id,
                                message_type="m.room.message",
                                content={
                                    "msgtype": "m.text",
                                    "body": f"🧵 New Topic: {subject} (from {sender_name})",
                                    "format": "org.matrix.custom.html",
                                    "formatted_body": (
                                        f"<h3>🧵 {subject}</h3>"
                                        f"<p><i>In response to <a href='{original_link}'>{sender_name}'s request</a></i></p>"
                                    )
                                }
                            )
                            
                            state["current_root"] = new_header.event_id
                            print(f"Topic Switch! New Root: {state['current_root']}")
                            return 
                        
                    except Exception as e:
                        print(f"DEBUG: Error handling topic change: {e}")
                        # NORMAL LOGGING (Gray Box)
                html_log = f"<blockquote><font color='gray'>⚙️ {text}</font></blockquote>"
                await self.client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": f"⚙️ {text}",
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_log,
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": state["current_root"]
                        }
                    }
                )

            # --- 5. BUILD CONTEXT & RUN ---
            history = await self.get_thread_history(room.room_id, state["current_root"])
            
            prompt = f"""
CONVERSATION HISTORY:
{history}

CURRENT REQUEST FROM {sender_name}:
{clean_body}
"""
            final_response = await run_agent_logic(prompt, log_callback=log_callback)

            # --- 6. SEND FINAL ANSWER ---
            # Convert Markdown -> HTML (with Tables support)
            html_response = markdown.markdown(
                final_response, 
                extensions=['tables', 'fenced_code', 'nl2br']
            )

            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": final_response,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_response,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": state["current_root"] # Sends to new thread if switched
                    }
                }
            )

        finally:
            await self.client.room_typing(room.room_id, False)
            
    async def decryption_failure_callback(self, room: MatrixRoom, event: MegolmEvent):
        """
        Handles messages that the bot could not decrypt.
        This usually happens immediately after joining a new encrypted room 
        before the keys have been shared.
        """
        print(f"🔒 Encrypted message received from {event.sender} (Session: {event.session_id})")
        #print("   ❌ Unable to decrypt. Waiting for keys...")

        # Add to buffer so we can retry later
        if event.session_id not in self.pending_events:
            self.pending_events[event.session_id] = []
        self.pending_events[event.session_id].append(event)
        
        # CHECK: Do we already have the key?
        if not self.client.store.get_inbound_group_session(room_id, event.session_id):
            print(f"   ❌ Key missing for this session. requesting it now...")
                                    
            # ACTION: Demand the key from the sender (your Element client)
            await self.client.request_room_key(
                event, 
                room.room_id, 
                event.sender, 
                event.session_id
            )
            print("   📤 Key request sent. Waiting for Element to reply...")
        else:
            print("   🤔 We have the key, but nio didn't decrypt it yet. It might be processed next tick.")

    async def cb_key_arrived(self, event: ToDeviceEvent):
        """
        Called when keys arrive. Checks if we have pending messages waiting for this key.
        """
        # Safety check for the 'str object has no attribute type' error
        if getattr(event, "type", "") != "m.room_key":
            return

        # The event content has the session_id
        content = event.content
        session_id = content.get("session_id")
        room_id = content.get("room_id")

        if session_id in self.pending_events:
            print(f"🔑 KEYS ARRIVED for session {session_id}! Retrying {len(self.pending_events[session_id])} messages...")
            
            # Process all waiting messages for this session
            for encrypted_event in self.pending_events[session_id]:
                try:
                    # 1. Manually decrypt the event
                    decrypted_event = self.client.decrypt_event(encrypted_event)
                    
                    # 2. Check if it's actual text (and not just metadata)
                    if isinstance(decrypted_event, RoomMessageText):
                        print(f"   🔓 RETRY SUCCESS: {decrypted_event.body}")
                        
                        # 3. Get the room object
                        room = self.client.rooms.get(room_id)
                        if room:
                            # 4. INJECT into your normal logic
                            await self.message_callback(room, decrypted_event)
                            
                except Exception as e:
                    print(f"   ❌ Retry failed: {e}")

            # clear the buffer for this session
            del self.pending_events[session_id]
            
if __name__ == "__main__":
    bot = MatrixBot()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
