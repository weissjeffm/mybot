import asyncio
import os
import json
import time
import uuid
import markdown
from nio import (
    AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent,
    KeyVerificationStart, KeyVerificationCancel, KeyVerificationKey,
    KeyVerificationMac, LocalProtocolError
)
from langgraph_agent import run_agent_logic 

# Config
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
        self.client = AsyncClient(MATRIX_URL, MATRIX_USER)
        self.user_cache = {} 

    async def start(self):
        print(f"Logging in as {MATRIX_USER}...")
        await self.client.login(MATRIX_PASS)
        print(f"🤖 Bot Device ID: {self.client.device_id}")
        
        # Load resume token
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                self.client.next_batch = f.read().strip()

        # 1. Standard Room Callbacks
        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        
        # 2. Crypto Callbacks (Only the ones that are stable)
        self.client.add_to_device_callback(self.cb_verification_start, KeyVerificationStart)
        self.client.add_to_device_callback(self.cb_verification_key, KeyVerificationKey)
        self.client.add_to_device_callback(self.cb_verification_cancel, KeyVerificationCancel)
        
        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        # Initial Sync to load keys
        await self.client.sync(timeout=30000, full_state=True)

        # 3. MANUAL SYNC LOOP
        while True:
            try:
                sync_response = await self.client.sync(timeout=30000, full_state=True)
                
                # --- MANUAL VERIFICATION REQUEST HANDLER ---
                # We check for requests here to avoid Import/Callback issues
                if sync_response.to_device_events:
                    for event in sync_response.to_device_events:
                        # Check type as a string property (safest way)
                        if getattr(event, "type", "") == "m.key.verification.request":
                            await self.handle_verification_request(event)

                # Save token
                if hasattr(sync_response, "next_batch"):
                    self.client.next_batch = sync_response.next_batch
                    with open("next_batch", "w") as f:
                        f.write(sync_response.next_batch)
                        
            except Exception as e:
                print(f"Sync error: {e}")
                import traceback
                traceback.print_exc() # Print full error so we know what's wrong
                await asyncio.sleep(5)

    # --- HANDLERS ---

    async def handle_verification_request(self, event):
        """Manually handles the 'Can we verify?' request."""
        print(f"🔐 Received verification REQUEST from {event.sender}. Sending READY.")
        
        # Extract transaction ID safely
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
            to_device_id=getattr(event, "source_device", "*")
        )

    async def cb_verification_start(self, event: KeyVerificationStart):
        print(f"🔐 Verification STARTED by {event.sender}. Accepting...")
        if "m.sas.v1" in event.content.get("method", []):
            await self.client.accept_sas_verification(event.transaction_id)
        else:
            print(f"🔐 Error: Unsupported method: {event.content.get('method')}")

    async def cb_verification_key(self, event: KeyVerificationKey):
        sas = self.client.key_verifications.get(event.transaction_id)
        if sas and sas.get_emoji():
            print("\n" + "="*40)
            print(f"🔐 VERIFICATION EMOJIS FOR {event.sender}")
            print("="*40)
            for emoji in sas.get_emoji():
                print(f"   {emoji.emoji}  ({emoji.description})")
            print("="*40 + "\n")
            await self.client.confirm_short_auth_string(event.transaction_id)
            print("🔐 Bot auto-confirmed. PLEASE CONFIRM ON YOUR DEVICE!")

    async def cb_verification_cancel(self, event: KeyVerificationCancel):
        print(f"🔐 Verification cancelled: {event.content.get('reason')}")

    async def invite_callback(self, room: MatrixRoom, event: InviteMemberEvent):
        print(f"Invite received for room {room.room_id}!")
        await self.client.join(room.room_id)

    async def get_display_name(self, user_id):
        if user_id in self.user_cache: return self.user_cache[user_id]
        try:
            resp = await self.client.get_displayname(user_id)
            name = resp.displayname if resp.displayname else user_id
            self.user_cache[user_id] = name
            return name
        except: return user_id

    async def get_thread_history(self, room_id, thread_root_id, limit=30):
        response = await self.client.room_messages(room_id, limit=limit)
        if not response.chunk: return ""
        thread_events = []
        for event in response.chunk:
            if not isinstance(event, RoomMessageText): continue
            evt_id = event.event_id
            relates = event.source.get('content', {}).get('m.relates_to', {})
            parent_id = relates.get('event_id')
            
            if evt_id == thread_root_id or parent_id == thread_root_id:
                sender = "AI_Agent" if event.sender == self.client.user_id else await self.get_display_name(event.sender)
                if "⚙️" in event.body: continue
                thread_events.append(f"{sender}: {event.body}")
        thread_events.reverse()
        return "\n".join(thread_events)

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        if event.sender == self.client.user_id: return

        # GATEKEEPER
        is_direct = room.member_count <= 2
        is_mentioned = (self.client.user_id in event.body) or ("weissbot" in event.body.lower())
        
        if not (is_direct or is_mentioned): return

        clean_body = event.body.replace(self.client.user_id, "").replace("weissbot", "", 1).strip()
        sender_name = await self.get_display_name(event.sender)
        print(f"Processing request from {sender_name}: {clean_body}")

        # --- TRIGGER VERIFICATION POPUP ---
        if clean_body == "!verify":
            print(f"🔐 Popping verification request on {sender_name}'s screen...")
            tx_id = str(uuid.uuid4())
            
            # Nested payload for to_device
            payload = {
                event.sender: {
                    "*": {
                        "from_device": self.client.device_id,
                        "methods": ["m.sas.v1"],
                        "timestamp": int(time.time() * 1000),
                        "transaction_id": tx_id
                    }
                }
            }
            
            await self.client.to_device("m.key.verification.request", payload)
            await self.client.room_send(room.room_id, "m.room.message", 
                                      {"body": "Check your screen for a popup!", "msgtype": "m.text"})
            return
        # ----------------------------------

        # Threading Logic
        content = event.source.get('content', {})
        relates_to = content.get('m.relates_to', {})
        is_in_thread = relates_to.get('rel_type') == 'm.thread'
        thread_root_id = relates_to.get('event_id') if is_in_thread else event.event_id

        await self.client.room_typing(room.room_id, True, timeout=60000)
        state = {"current_root": thread_root_id}

        try:
            async def log_callback(text):
                print(f"DEBUG LOG: {text}") 
                if "TOPIC_CHANGE" in text and "{" in text:
                    try:
                        json_start = text.find('{')
                        json_str = text[json_start:] 
                        data = json.loads(json_str)
                        if data.get("signal") == "TOPIC_CHANGE":
                            subject = data.get("subject", "New Topic")
                            original_link = f"https://matrix.to/#/{room.room_id}/{event.event_id}"
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
                            return 
                    except Exception as e:
                        print(f"DEBUG: Error handling topic change: {e}")

                html_log = f"<blockquote><font color='gray'>⚙️ {text}</font></blockquote>"
                await self.client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": f"⚙️ {text}",
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_log,
                        "m.relates_to": {"rel_type": "m.thread", "event_id": state["current_root"]}
                    }
                )

            history = await self.get_thread_history(room.room_id, state["current_root"])
            prompt = f"CONVERSATION HISTORY:\n{history}\n\nCURRENT REQUEST FROM {sender_name}:\n{clean_body}"
            
            final_response = await run_agent_logic(prompt, log_callback=log_callback)
            html_response = markdown.markdown(final_response, extensions=['tables', 'fenced_code', 'nl2br'])

            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": final_response,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_response,
                    "m.relates_to": {"rel_type": "m.thread", "event_id": state["current_root"]}
                }
            )

        finally:
            await self.client.room_typing(room.room_id, False)

if __name__ == "__main__":
    bot = MatrixBot()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
        
