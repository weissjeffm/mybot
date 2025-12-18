import asyncio
import os
import signal
import sys
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from langgraph_agent import run_agent_logic 

# Config
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
        self.client = AsyncClient(MATRIX_URL, MATRIX_USER)

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
            {event.body}
            """

            # --- 4. RUN THE BRAIN ---
            # We pass the FULL context now, not just event.body
            final_response = await run_agent_logic(full_context_prompt, log_callback=log_to_thread)

            # --- 5. SEND FINAL ANSWER ---
            # We send this to the SAME thread (keeping it organized)
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": final_response,
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
            
            # Check strictly for this thread
            if event_id == thread_root_id or parent_id == thread_root_id:
                sender = "AI" if event.sender == self.client.user_id else "User"
                # Skip "Thinking..." log messages (optional, keeps context clean)
                if "⚙️" in event.body:
                    continue
                thread_events.append(f"{sender}: {event.body}")

        # Matrix returns newest first, so we reverse to read chronologically
        thread_events.reverse()
        return "\n".join(thread_events)

if __name__ == "__main__":
    bot = MatrixBot()
    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        print("Received exit signal")
