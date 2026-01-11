import asyncio
import os
import json
import markdown
import time
import uuid
import traceback
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 
from langchain_core.messages import HumanMessage, AIMessage
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

            # load the existing store
            self.client.load_store()

            # diagnostic
            #print(f"🕵️ STORE TYPE: {type(self.client.store)}")
            #print(f"🕵️ STORE MRO: {type(self.client.store).mro()}")
            #print(f"🕵️ STORE dir: {dir(self.client.store)}")
            
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

        print("📤 Uploading encryption keys to server...")
        try:
            await self.client.keys_upload()
        except:
            None
            
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
                traceback.print_exc()
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

    async def get_structured_history(self, room_id, thread_root_id, limit=30):
        """Fetches history as LangChain message objects."""
        response = await self.client.room_messages(room_id, limit=limit)
        if not response.chunk: 
            return []

        messages = []
        for event in response.chunk:
            if not isinstance(event, RoomMessageText): 
                continue

            # Determine if it's in our thread
            relates = event.source.get('content', {}).get('m.relates_to', {})
            parent_id = relates.get('event_id')

            if event.event_id == thread_root_id or parent_id == thread_root_id:
                # 1. Filter out tool logs and notices to keep context clean
                if "⚙️" in event.body or event.source.get("msgtype") == "m.notice":
                    continue

                # 2. Assign the correct Role
                if event.sender == self.client.user_id:
                    messages.append(AIMessage(content=event.body))
                else:
                    sender_name = await self.get_display_name(event.sender)
                    # We still prefix the name in the content so the AI knows WHO is talking
                    messages.append(HumanMessage(content=f"{sender_name}: {event.body}"))

        # Matrix gives us the most recent first; LangGraph/LLMs need chronological
        messages.reverse() 
        return messages

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
                        content=content,
                        ignore_unverified_devices=True
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
        state = {
            "current_root": thread_root_id,
            "log_event_id": None,  # Track the ID of the 'thinking' message
            "accumulated_logs": [],
            "thoughts": []
        }

        try:
            # --- 4. TOOL LOGGING CALLBACK (Optimized for Notifications) ---
            async def log_callback(text, node=None, data=None):
                print(f"log_callback for {node}: {data}")
                data = data or {}
                status = data.get("status", "error")
                msg = data.get("message", text) # Fallback to 'text' if data is empty

                # handle topic changes
                # 1. Handle Topic Changes (Existing logic)
                
                if data.get("event") == "TOPIC_CHANGE":
                    try:
                        topic = data.get("topic", "")
                        original_link = f"https://matrix.to/#/{room.room_id}/{event.event_id}"
                        new_header = await self.client.room_send(
                            room_id=room.room_id,
                            message_type="m.room.message",
                            ignore_unverified_devices=True,
                            content={
                                "msgtype": "m.text",
                                "body": f"🧵 New Topic: {topic}",
                                "format": "org.matrix.custom.html",
                                "formatted_body": f"<h3>🧵 {topic}</h3><p><i>Context: <a href='{original_link}'>Original Request</a></i></p>"
                                }
                            )
                        state["current_root"] = new_header.event_id
                        state["log_event_id"] = None # Reset log ID for new thread
                        state["accumulated_logs"] = []
                        state["thoughts"] = []
                    except Exception as e:
                        print(f"Topic change error: {e}")
                # Simple logic-based emojis
                if node == "reason":
                    emoji = "⚙️"
                else:
                    emoji = "✅" if status == "ok" else "❌"

                clean_line = f"{emoji} {msg}"
                state["thoughts"].append(clean_line)                    
                # 2. UI Formatting (Matrix Notice + Edit)
                # We only show the last 8 thoughts to keep the 'thinking box' tidy
                display = state["thoughts"][-8:]
                html_body = f"<blockquote><font color='gray'>{'<br>'.join(display)}</font></blockquote>"

                content = {
                    "msgtype": "m.notice",
                    "body": f"* Thinking...\n" + "\n".join(display),
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_body,
                    "m.relates_to": {"rel_type": "m.thread", "event_id": state["current_root"]}
                }

                # 3. Perform the 'Silent' Edit
                if state["log_event_id"]:
                    content["m.new_content"] = {
                        "msgtype": "m.notice",
                        "body": "Thinking...\n" + "\n".join(display),
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_body
                    }
                    content["m.relates_to"] = {"rel_type": "m.replace", "event_id": state["log_event_id"]}

                resp = await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    content=content,
                    ignore_unverified_devices=True
                )

                if not state["log_event_id"]:
                    state["log_event_id"] = resp.event_id

            # --- 5. BUILD CONTEXT & RUN ---
            # Fetch the structured list of past messages
            full_history = await self.get_structured_history(room.room_id, state["current_root"])

            # Note: The 'current_message' from 'event.body' is already included in 
            # full_history if the sync happened quickly, but to be safe and ensure 
            # the prompt is exactly what was just typed:
            if not full_history or full_history[-1].content != f"{sender_name}: {clean_body}":
                full_history.append(HumanMessage(content=f"{sender_name}: {clean_body}"))

            # Run the agent with the list
            response = await run_agent_logic(full_history, log_callback=log_callback)

            # address whoever addressed us
            if is_mentioned:
                response = f"{event.sender}: {response}"

                # --- 6. SEND FINAL ANSWER (As a fresh message) ---
            html_response = markdown.markdown(response, extensions=['tables', 'fenced_code', 'nl2br'])

            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                ignore_unverified_devices=True,
                content={
                    "msgtype": "m.text",
                    "body": response,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_response,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": state["current_root"]
                    },
                    "m.mentions": {
                        "user_ids": [event.sender]
                    }
                }
            )

        finally:
            await self.client.room_typing(room.room_id, False)
            
    async def decryption_failure_callback(self, room: MatrixRoom, event: MegolmEvent):
        print(f"🔒 Encrypted message (Session: {event.session_id}) from {event.sender}")
        
        # 1. Buffer the event so we can retry later
        if event.session_id not in self.pending_events:
            self.pending_events[event.session_id] = []
        self.pending_events[event.session_id].append(event)

        # request key
        print(f"   ❌ Key missing in RAM. Requesting from {event.sender}...")
        await self.client.request_room_key(event)
            
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
