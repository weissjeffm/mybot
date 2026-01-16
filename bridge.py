import logging
import sys
import asyncio
import os
import json
import markdown
import time
import uuid
import traceback
from langgraph_agent import run_agent_logic 
from langchain_core.messages import HumanMessage, AIMessage
from nio import (
    AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent, Event, MegolmEvent,
    LocalProtocolError, ToDeviceEvent, RoomMessageAudio, RoomEncryptedAudio
)
from nio.crypto import decrypt_attachment # Necessary for encrypted voice notes
import aiohttp

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
        self.ui_lock = asyncio.Lock()
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
        #self.client.add_event_callback(self.message_callback, MegolmEvent)
        self.client.add_event_callback(self.message_callback, RoomMessageAudio)
        self.client.add_event_callback(self.message_callback, RoomEncryptedAudio)
        self.client.add_event_callback(self.invite_callback, InviteMemberEvent)
        self.client.add_event_callback(self.decryption_failure_callback, MegolmEvent)
        self.client.add_to_device_callback(self.cb_key_arrived, ToDeviceEvent)
        # debugger callback
        self.client.add_event_callback(self.universal_debug_callback, Event)
        
        print(f"Bot is listening... (Resuming from: {self.client.next_batch})")

        while True:
            try:
                sync_response = await self.client.sync(timeout=30000, full_state=True)
                
                if hasattr(sync_response, "next_batch"):
                    self.client.next_batch = sync_response.next_batch
                    with open("next_batch", "w") as f:
                        f.write(sync_response.next_batch)
            except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as e:
                print(f"📡 Network error during sync: {e}. Retrying in 10s...")
                await asyncio.sleep(10)
            except aiohttp.ClientPayloadError:
                print("⚠️ Matrix payload truncated. Forcing session reset...")
                # This closes the underlying connector without logging out
                await self.client.close() 
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Sync error: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)

    async def universal_debug_callback(self, room: MatrixRoom, event: Event):
        # This will print for EVERY single thing (typing, receipts, messages)
        print(f"📡 [RAW EVENT] Type: {type(event).__name__} | Sender: {event.sender}")

        # Check if it's an encrypted message that hasn't been handled
        if isinstance(event, MegolmEvent):
            print(f"  └─ 🔒 Encrypted MegolmEvent (Session: {event.session_id})")

        # Check if it has a body (most messages do)
        if hasattr(event, "body"):
            print(f"  └─ 💬 Body Snippet: {event.body[:50]}")

        # Check for audio msgtype in the raw source
        msgtype = event.source.get("content", {}).get("msgtype")
        if msgtype:
            print(f"  └─ 🏷️ MsgType: {msgtype}")
            
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
        # if it's the bot's own message, nothing to do
        if event.sender == self.client.user_id: return
        
        #print(f"DEBUG: Received event type {type(event)} from {event.sender}")
        clean_body = ""
        if isinstance(event, (RoomMessageAudio, RoomEncryptedAudio)):
            print(f"🎙️ [AUDIO DETECTED] Type: {type(event).__name__}")

            # RoomEncryptedAudio has the 'file' dict directly on the event object
            # whereas RoomMessageAudio might have it in 'source'
            audio_bytes = None
            try:
                # 1. Check for Encrypted Metadata (multiple possible nio locations)
                file_info = None
                if hasattr(event, 'file') and event.file:
                    # Type: nio.events.room_events.EncryptedFile
                    file_info = event.file
                elif 'file' in event.source.get('content', {}):
                    # Type: dictionary (fallback)
                    file_info = event.source['content']['file']

                if file_info:
                    print(f"🔐 Found encryption metadata. Downloading from {getattr(file_info, 'url', file_info.get('url'))}")

                    # Handle both object and dict access for the URL
                    mxc_url = getattr(file_info, 'url', file_info.get('url'))
                    resp = await self.client.download(mxc_url)
                    ciphertext = resp.body

                    # Extract keys (handle both object and dict)
                    key = getattr(file_info, 'key', file_info.get('key'))['k']
                    iv = getattr(file_info, 'iv', file_info.get('iv'))
                    hashes = getattr(file_info, 'hashes', file_info.get('hashes'))['sha256']

                    from nio.crypto import decrypt_attachment
                    audio_bytes = decrypt_attachment(ciphertext, key, hashes, iv)
                    print(f"✅ Decryption successful. Bytes: {len(audio_bytes)}")
                else:
                    # 2. Plain Attachment Branch
                    print("📂 No encryption metadata found. Treating as plain audio.")
                    resp = await self.client.download(event.url)
                    audio_bytes = resp.body

                # 3. Validation
                if not audio_bytes or len(audio_bytes) < 500:
                    print(f"❌ Audio extraction failed. Byte count: {len(audio_bytes)}")
                    return
            
                # 3. TRANSCRIBE
                if audio_bytes:
                    await self.client.room_typing(room.room_id, True)
                    transcript = await self.transcribe_audio(audio_bytes, "voice_note.ogg")
                    if transcript:
                        print(f"📝 Transcript: {transcript}")
                        clean_body = f"[Transcription of a voice message from {sender_name}]: {transcript}"

                    else:
                        return
            except Exception as e:
                print(f"❌ Error processing audio: {e}")
                return

        elif isinstance(event, RoomMessageText):
            # Clean the prompt (remove bot name)
            clean_body = event.body.replace(self.client.user_id, "").replace("weissbot", "", 1).strip()
            

        sender_name = await self.get_display_name(event.sender)
        
        # If it's not text or audio (like a file or image), we ignore it
        if not clean_body:
            return
        # --- 1. GATEKEEPER (Group Chat Logic) ---
        is_direct = room.member_count <= 2
        is_mentioned = (self.client.user_id in event.body) or ("weissbot" in event.body.lower())
        
        # Only reply if DM or Mentioned
        if not (is_direct or is_mentioned):
            return

        # !verify check
        if clean_body == "!verify":
            async def send_verify():
                try:
                    content = {
                        "body": "🔐 Verification Request",
                        "msgtype": "m.key.verification.request",
                        "to": event.sender,
                        "from_device": self.client.device_id,
                        "methods": ["m.sas.v1"],
                        "timestamp": int(time.time() * 1000),
                        "transaction_id": str(uuid.uuid4())
                    }
                    await self.client.room_send(
                        room.room_id,
                        message_type="m.room.message",
                        content=content,
                        ignore_unverified_devices=True
                    )
                except Exception as e:
                    print(f"🔐 Send failed: {e}")

            asyncio.create_task(send_verify())
            return
       
        print(f"Processing request from {sender_name}: {clean_body}")

        # --- 2. DETERMINE THREAD ROOT ---
        content = event.source.get('content', {})
        relates_to = content.get('m.relates_to', {})
        is_in_thread = relates_to.get('rel_type') == 'm.thread'
        thread_root_id = relates_to.get('event_id') if is_in_thread else event.event_id

        await self.client.room_typing(room.room_id, True, timeout=60000)

        # --- 3. UI/UX EXECUTION STATE ---
        # This tracks the "Thinking..." board for this specific request
        ui_state = {
            "log_event_id": None,    # The ID of the notice we edit
            "thoughts": [],          # Reasoning steps
            "active_tools": {}       # Map of 'action_str' -> 'icon'
        }
        
        ui_lock = asyncio.Lock()

        try:
            # --- 4. THE LOGGING CALLBACK (Handles the Status Board) ---
            async def log_callback(text, node=None, data=None):
                async with self.ui_lock:
                    data = data or {}

                    # Update UI state based on Graph signals
                    if node == "act_start":
                        for action in data.get("actions", []):
                            ui_state["active_tools"][action] = "⚙️"

                    elif node == "act_finish":
                        for item in data.get("results", []):
                            # item is {"action": str, "status": "ok"|"error"}
                            icon = "✅" if item["status"] == "ok" else "❌"
                            ui_state["active_tools"][item["action"]] = icon

                    elif node == "reason":
                        ui_state["thoughts"].append(text)

                    # REFRESH TYPING INDICATOR
                    # Every time a log comes in, we tell the server we are still working.
                    # We use a shorter timeout (30s) and just call it more often.
                    try:
                        await self.client.room_typing(room.room_id, True, timeout=30000)
                    except:
                        pass

                    # Format the status board
                    display_lines = []
                    for t in ui_state["thoughts"][-2:]: # Show last 2 thoughts
                        display_lines.append(f"💭 {t}")
                    for action, icon in ui_state["active_tools"].items():
                        display_lines.append(f"{icon} {action}")

                    if not display_lines: return

                    full_body = "\n".join(display_lines)
                    html_body = f"<blockquote><font color='gray'>{'<br>'.join(display_lines)}</font></blockquote>"

                    msg_content = {
                        "msgtype": "m.notice",
                        "body": f"* Thinking...\n{full_body}",
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_body,
                        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id}
                    }

                    if ui_state["log_event_id"]:
                        # Edit the existing Thinking block
                        msg_content["m.new_content"] = {
                            "msgtype": "m.notice",
                            "body": f"Thinking...\n{full_body}",
                            "format": "org.matrix.custom.html",
                            "formatted_body": html_body
                        }
                        msg_content["m.relates_to"] = {"rel_type": "m.replace", "event_id": ui_state["log_event_id"]}

                    resp = await self.client.room_send(
                        room.room_id, "m.room.message", 
                        content=msg_content, 
                        ignore_unverified_devices=True
                    )

                    if not ui_state["log_event_id"]:
                        ui_state["log_event_id"] = resp.event_id

            # --- 5. RUN AGENT ---
            # Fetch structured history
            try:
                full_history = await self.get_structured_history(room.room_id, thread_root_id)

                # Ensure current prompt is at the end
                if not full_history or full_history[-1].content != f"{sender_name}: {clean_body}":
                    full_history.append(HumanMessage(content=f"{sender_name}: {clean_body}"))

                # Wrap the logic in a timeout to prevent "spinning forever"
                response = await asyncio.wait_for(
                    run_agent_logic(full_history, log_callback=log_callback),
                    timeout=300 # 5-minute total cap for the whole agent process
                )  

                if is_mentioned:
                    response = f"{sender_name}: {response}"

                # --- 6. SEND FINAL ANSWER ---
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
                            "event_id": thread_root_id
                        },
                        "m.mentions": {"user_ids": [event.sender]}
                    }
                )
            except asyncio.TimeoutError:
                await log_callback("⚠️ The request took too long and timed out.", node="error")
                response = "I'm sorry, that research task was too complex and timed out."
            except Exception as e:
                print(f"Error in message_callback: {e}")
                traceback.print_exc()
                await log_callback(f"❌ System Error: {str(e)}", node="error")
                response = f"I encountered a technical error: {str(e)}"
            finally:
                await self.client.room_typing(room.room_id, False)

        except Exception as e:
            print(f"Error in message_callback: {e}")
            traceback.print_exc()
        finally:
            await self.client.room_typing(room.room_id, False)    
            
    async def transcribe_audio(self, audio_bytes, filename):
        """Sends audio bytes to LocalAI with authentication."""
        stt_url = "http://localhost:8080/v1/audio/transcriptions"

        # Use the same key as your LLM config
        api_key = "sk-50cf096cc7c795865e" 

        headers = {
            "Authorization": f"Bearer {api_key}"
        }

        data = aiohttp.FormData()
        data.add_field('file', audio_bytes, filename=filename, content_type='application/octet-stream')
        data.add_field('model', 'whisper-large')

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(stt_url, data=data, headers=headers) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        return json_resp.get("text", "")
                    elif resp.status == 401:
                        print("❌ STT Error: Unauthorized. Check your LocalAI API key.")
                        return None
                    else:
                        print(f"❌ STT Error: {resp.status} - {await resp.text()}")
                        return None
        except Exception as e:
            print(f"⚠️ Transcription failure: {e}")
            return None
        
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
