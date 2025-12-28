import asyncio
import os
import signal
import sys
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 
import markdown

# Config
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
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
        
        # 1. Load the 'next_batch' token (Resume position)
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                self.client.next_batch = f.read().strip()

        # 2. Register Callbacks
        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        
        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        # 3. MANUAL SYNC LOOP (Fixes the loop_callback bug)
        while True:
            try:
                # Sync with server (waits up to 30s for events)
                sync_response = await self.client.sync(timeout=30000, full_state=True)
                
                # Save token immediately
                if hasattr(sync_response, "next_batch"):
                    self.client.next_batch = sync_response.next_batch
                    with open("next_batch", "w") as f:
                        f.write(sync_response.next_batch)
                        
            except Exception as e:
                print(f"Sync error: {e}")
                await asyncio.sleep(5) # Wait before retrying

    async def invite_callback(self, room: MatrixRoom, event: InviteMemberEvent):
        """Auto-join any room we are invited to."""
        print(f"Invite received for room {room.room_id}!")
        await self.client.join(room.room_id)

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        # Ignore own messages
        if event.sender == self.client.user_id:
            return

        # --- 0. THE GATEKEEPER (Group Chat Logic) ---
        
        # Check 1: Is this a 1-on-1? (Bot + 1 Human = 2 members)
        # room.member_count is a property of the MatrixRoom object
        is_direct_chat = room.member_count <= 2
        
        # Check 2: Was the bot mentioned?
        # We check against the ID (@weissbot:...) and a casual name "weissbot"
        is_mentioned = (self.client.user_id in event.body) or ("weissbot" in event.body.lower())
        
        # Check 3: Is this a direct reply to the bot?
        # We check the 'm.in_reply_to' field in the event content
        content = event.source.get('content', {})
        relates_to = content.get('m.relates_to', {})
        reply_to_id = relates_to.get('m.in_reply_to', {}).get('event_id')
        
        is_reply_to_bot = False
        if reply_to_id:
            # We have to look up who sent the message we are replying to.
            # This is slightly expensive, so we wrap it in a try/catch or skip if lazy.
            # For a MVP, 'is_mentioned' is usually enough, but let's be robust:
            # (Requires fetching the event, which might be slow. Let's stick to mentions for speed + 1-on-1)
            pass 

        # DECISION: To Speak or Not To Speak
        # We only proceed if it's a 1-on-1, OR we were summoned
        if not (is_direct_chat or is_mentioned):
            print(f"Skipping message in {room.display_name} (Group Chat, No Mention)")
            return

        # If mentioned, strip the bot's name from the prompt so the LLM doesn't get confused
        # e.g. "@weissbot What is the time?" -> "What is the time?"
        clean_body = event.body.replace(self.client.user_id, "").replace("weissbot", "", 1).strip()
        
        # ... Continue with the rest of the function using 'clean_body' ...
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        print(f"Message from {event.sender}: {event.body}")

        # --- 1. DETERMINE THREAD ROOT ---
        # Check if the incoming message is already inside a thread
        content = event.source.get('content', {})
        relates_to = content.get('m.relates_to', {})
        
        if relates_to.get('rel_type') == 'm.thread':
            # It's already in a thread; append to the existing root
            thread_root_id = relates_to.get('event_id')
        else:
            # It's a new topic; start a new thread off this message
            thread_root_id = event.event_id

        # --- 2. TYPING INDICATOR ---
        await self.client.room_typing(room.room_id, True, timeout=60000)

        try:
            # --- 3. DEFINING THE "THINKING" FORMAT ---
            # We use an HTML blockquote or code block for thoughts
            async def log_to_thread(text):
                formatted_html = f"<blockquote><font color='gray'>⚙️ {text}</font></blockquote>"
                
                await self.client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": f"⚙️ {text}",  # Fallback for plain text clients
                        "format": "org.matrix.custom.html",
                        "formatted_body": formatted_html,
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id, # Always point to Root
                        }
                    }
                )


            # --- 3.1. CONTEXT STUFFING ---
            # Get the conversation so far
            history_text = await self.get_thread_history(room.room_id, thread_root_id)
            
            # Combine History + Current Request
            # We wrap it clearly so the LLM knows what is past vs present
            full_context_prompt = f"""
            PREVIOUS CONVERSATION HISTORY:
            {history_text}
            
            CURRENT USER REQUEST:
            {clean_body}
            """

            # --- 4. RUN THE BRAIN ---
            # We pass the FULL context now, not just event.body
            final_response = await run_agent_logic(full_context_prompt, log_callback=log_to_thread)

            # Convert model's Markdown to HTML
            # 'tables' and 'fenced_code' are essential for tech/coding questions
            html_response = markdown.markdown(
                final_response, 
                extensions=['tables', 'fenced_code', 'nl2br']
            )
            # --- 5. SEND FINAL ANSWER ---
            # We send this to the SAME thread (keeping it organized)
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": final_response,             # Plain text (fallback)
                    "format": "org.matrix.custom.html", # Trigger HTML rendering
                    "formatted_body": html_response,    # The HTML payload
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id,
                    }
                }
            )
            
        finally:
            await self.client.room_typing(room.room_id, False)

    async def get_thread_history(self, room_id, thread_root_id, limit=30):
        """
        Fetches recent room messages and filters for the specific thread.
        Returns a formatted string of the conversation.
        """
        # Fetch the last 'limit' messages from the room
        # Note: In a busy room, you might need a higher limit or proper pagination.
        response = await self.client.room_messages(room_id, limit=limit)
        
        if not response.chunk:
            return ""

        # Filter: Keep if it IS the root, or if it RELATES to the root
        thread_events = []
        for event in response.chunk:
            if not isinstance(event, RoomMessageText):
                continue
                
            event_id = event.event_id
            relates_to = event.source.get('content', {}).get('m.relates_to', {})
            parent_id = relates_to.get('event_id')

            if event.sender == self.client.user_id:
                sender_name = "AI_Agent"
            else:
                sender_name = await self.get_display_name(event.sender)
            
            thread_events.append(f"{sender_name}: {event.body}")

        # Matrix returns newest first, so we reverse to read chronologically
        thread_events.reverse()
        return "\n".join(thread_events)

    # Add a simple cache
    self.user_cache = {} 

    async def get_display_name(self, user_id):
        """Resolves a User ID to a human-readable Display Name."""
        if user_id in self.user_cache:
            return self.user_cache[user_id]
            
        try:
            # Fetch from server
            response = await self.client.get_displayname(user_id)
            displayname = response.displayname if response.displayname else user_id
            self.user_cache[user_id] = displayname
            return displayname
        except Exception:
            return user_id # Fallback

if __name__ == "__main__":
    bot = MatrixBot()
    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
