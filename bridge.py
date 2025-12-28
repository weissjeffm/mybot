import asyncio
import os
import json
import markdown
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 

# Config
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
        self.client = AsyncClient(MATRIX_URL, MATRIX_USER)
        self.user_cache = {} # Caches 'User ID' -> 'Display Name'

    async def start(self):
        print(f"Logging in as {MATRIX_USER}...")
        await self.client.login(MATRIX_PASS)
        
        # Load resume token
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                self.client.next_batch = f.read().strip()

        # Register Callbacks
        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        
        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        # MANUAL SYNC LOOP (Fixes 'loop_callback' bug)
        while True:
            try:
                sync_response = await self.client.sync(timeout=30000, full_state=True)
                
                # Save token
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
                print(f"DEBUG LOG: {text}") # <--- 1. See exactly what comes in

                # CHECK FOR JSON SIGNAL
                # We check if "TOPIC_CHANGE" is in there, regardless of prefix
                if "TOPIC_CHANGE" in text and "{" in text:
                    try:
                        # 2. Extract JSON part (Ignore "Ran tool. " prefix)
                        json_start = text.find('{')
                        json_str = text[json_start:] 
                        
                        data = json.loads(json_str)
                        
                        # Verify it's actually our signal
                        if data.get("signal") == "TOPIC_CHANGE":
                            subject = data.get("subject", "New Topic")
                            
                            # ACTION: Create New Thread Header
                            new_header = await self.client.room_send(
                                room_id=room.room_id,
                                message_type="m.room.message",
                                content={
                                    "msgtype": "m.text",
                                    "body": f"🧵 New Topic: {subject}",
                                    "format": "org.matrix.custom.html",
                                    "formatted_body": f"<h3>🧵 {subject}</h3>"
                                }
                            )
                            
                            state["current_root"] = new_header.event_id
                            print(f"Topic Switch! New Root: {state['current_root']}")
                            return # Swallow this log line so it doesn't print to chat
                            
                    except json.JSONDecodeError as e:
                        print(f"DEBUG: Found TOPIC_CHANGE but invalid JSON: {e}")
                    except Exception as e:
                        print(f"DEBUG: Error handling topic change: {e}")
                        # Don't silence it! Let it print to console.
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

if __name__ == "__main__":
    bot = MatrixBot()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
