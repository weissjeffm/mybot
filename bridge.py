import asyncio
import os
#from nio import AsyncClient, MatrixRoom, RoomMessageText
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent # <-- Added InviteMemberEvent
from langgraph_agent import run_agent_logic  # We will build this next

# Config (Set these in your env or hardcode for testing)
MATRIX_URL = os.getenv("MATRIX_URL", "https://matrix.org")
MATRIX_USER = os.getenv("MATRIX_USER", "@weissbot:matrix.org")
MATRIX_PASS = os.getenv("MATRIX_PASS", "password")

class MatrixBot:
    def __init__(self):
        self.client = AsyncClient(MATRIX_URL, MATRIX_USER)

    async def start(self):
        print(f"Logging in as {MATRIX_USER}...")
        await self.client.login(MATRIX_PASS)
        
        # Load the 'next_batch' token if it exists
        next_batch = None
        if os.path.exists("next_batch"):
            with open("next_batch", "r") as f:
                next_batch = f.read().strip()

        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        
        # Define a callback to save the token after every sync
        async def save_token(response):
            with open("next_batch", "w") as f:
                f.write(response.next_batch)

        print("Bot is listening...")
        # Sync with 'since' token so we don't replay old history
        await self.client.sync_forever(timeout=30000, since=next_batch, full_state=True)
        
    async def invite_callback(self, room: MatrixRoom, event: InviteMemberEvent):
        """Auto-join any room we are invited to."""
        print(f"Invite received for room {room.room_id}!")
        await self.client.join(room.room_id)
        
    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        print(f"Message from {event.sender}: {event.body}")

        # 1. Send an acknowledgment (Threading starts here)
        # We reply to the user's message, creating a thread
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event.event_id,
                }
            }
        )

        # 2. Run the LangGraph Agent
        # We pass a "log_callback" so the agent can post updates to the thread
        async def log_to_thread(text):
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"⚙️ {text}", # Emoji to denote system thought
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": event.event_id,
                    }
                }
            )

        # 3. Get Final Answer
        final_response = await run_agent_logic(event.body, log_callback=log_to_thread)

        # 4. Post Final Answer to Main Channel (Reply to original message but visible)
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": final_response,
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": event.event_id}
                }
            }
        )

if __name__ == "__main__":
    bot = MatrixBot()
    asyncio.run(bot.start())
