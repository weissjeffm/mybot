import asyncio
import os
import json
import markdown
import time
import uuid
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 

from nio import (
    AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent,
    KeyVerificationStart, KeyVerificationCancel, KeyVerificationKey, 
    KeyVerificationMac, LocalProtocolError, ToDeviceEvent
)


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
            MATRIX_USER, 
            store_path=store_folder
        )


    async def start(self):
        print(f"Logging in as {MATRIX_USER}...")
        await self.client.login(MATRIX_PASS)
        print(f"🤖 MY DEVICE ID: {self.client.device_id}")
        
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                self.client.next_batch = f.read().strip()

        # --- STANDARD CALLBACKS ---
        self.client.add_to_device_callback(self.cb_verification_request, ToDeviceEvent)
        
        # 1. Start (These still work with specific classes)
        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        
        # --- CRYPTO CALLBACKS (The "To-Device" ones are crucial) ---
        # 1. Start Verification
        self.client.add_to_device_callback(self.cb_verification_start, KeyVerificationStart)
        self.client.add_event_callback(self.cb_verification_start, KeyVerificationStart)
        
        # 2. Key Exchange (Triggers Emoji Print)
        self.client.add_to_device_callback(self.cb_verification_key, KeyVerificationKey)
        self.client.add_event_callback(self.cb_verification_key, KeyVerificationKey)

        # 3. Cancellation
        self.client.add_to_device_callback(self.cb_verification_cancel, KeyVerificationCancel)
        self.client.add_event_callback(self.cb_verification_cancel, KeyVerificationCancel)

        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        # Initial Sync to load keys
        await self.client.sync(timeout=30000, full_state=True)

        # MANUAL LOOP
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
            print(f"🔐 Initiating verification with {sender_name}...")
            
            # We must generate a unique transaction ID
            tx_id = str(uuid.uuid4())
            
            # Construct the "In-Room" Verification Request
            # This makes a button appear in the chat stream
            content = {
                "body": "🔐 Verification Request (Please Accept)",
                "msgtype": "m.key.verification.request",
                "from_device": self.client.device_id,
                "methods": ["m.sas.v1"], # We support Emoji (SAS)
                "timestamp": int(time.time() * 1000),
                "transaction_id": tx_id
            }
            
            await self.client.room_send(
                room.room_id,
                message_type="m.room.message",
                content=content
            )
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

    # --- ENCRYPTION HELPERS ---
    async def cb_verification_request(self, event: ToDeviceEvent):
        """0. Handle the initial 'Can we verify?' request."""
        # FILTER: Only act if it's actually a verification request
        if event.type != "m.key.verification.request":
            return

        print(f"🔐 Received verification REQUEST from {event.sender}. Sending READY.")
        
        # Access content safely (generic events use a dict)
        content = event.content
        tx_id = content.get("transaction_id")
        
        await self.client.to_device(
            "m.key.verification.ready",
            {
                "transaction_id": tx_id,
                "from_device": self.client.device_id,
                "methods": ["m.sas.v1"]
            },
            to_user_id=event.sender,
            to_device_id=event.source_device
        )
    async def cb_verification_start(self, event: KeyVerificationStart):
        """1. Receive request and accept it."""
        print(f"🔐 Verification started by {event.sender}. Accepting...")
        
        # We accept the SAS method (Emoji check)
        if "m.sas.v1" in event.content.get("method", []):
            await self.client.accept_sas_verification(event.transaction_id)
        else:
            print(f"🔐 Error: Sender is using an unsupported method: {event.content.get('method')}")

    async def cb_verification_key(self, event: KeyVerificationKey):
        """2. Keys exchanged. Print emojis and auto-confirm."""
        
        # Get the SAS object for this transaction
        sas = self.client.key_verifications.get(event.transaction_id)
        
        if sas and sas.get_emoji():
            print("\n" + "="*40)
            print(f"🔐 VERIFICATION EMOJIS FOR {event.sender}")
            print("="*40)
            
            # Print the emojis to the log
            for emoji in sas.get_emoji():
                print(f"   {emoji.emoji}  ({emoji.description})")
                
            print("="*40 + "\n")

            # AUTO-CONFIRM: We assume if you see this log, you are verifying it.
            # This tells the server "Yes, the bot sees these emojis".
            # Now YOU just need to click "They Match" on your phone.
            await self.client.confirm_short_auth_string(event.transaction_id)
            print("🔐 Bot auto-confirmed match. Please confirm on your device now!")

    async def cb_verification_cancel(self, event: KeyVerificationCancel):
        """Handle cancellations."""
        print(f"🔐 Verification cancelled by {event.sender}: {event.content.get('reason')}")

if __name__ == "__main__":
    bot = MatrixBot()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
