from io import BytesIO
from nio import RoomMessageText
from langchain_core.messages import HumanMessage, AIMessage

async def get_display_name(bot, user_id):
    """Resolves User ID to Name with caching."""
    if user_id in bot.user_cache:
        return bot.user_cache[user_id]
    try:
        resp = await bot.client.get_displayname(user_id)
        name = resp.displayname if resp.displayname else user_id
        bot.user_cache[user_id] = name
        return name
    except:
        return user_id

async def get_structured_history(bot, room_id, thread_root_id, limit=30):
    """Fetches history as LangChain message objects."""
    response = await bot.client.room_messages(room_id, limit=limit)
    if not response.chunk: 
        return []

    messages = []
    for event in response.chunk:
        if not isinstance(event, RoomMessageText): 
            continue

        relates = event.source.get('content', {}).get('m.relates_to', {})
        parent_id = relates.get('event_id')

        if event.event_id == thread_root_id or parent_id == thread_root_id:
            # Filter out tool logs and status notices
            if "⚙️" in event.body or event.source.get("msgtype") == "m.notice":
                continue

            if event.sender == bot.client.user_id:
                messages.append(AIMessage(content=event.body))
            else:
                sender_name = await get_display_name(bot, event.sender)
                messages.append(HumanMessage(content=f"{sender_name}: {event.body}"))

    messages.reverse() 
    return messages

async def send_audio_message(bot, room_id: str, audio_bytes: bytes, filename: str = "response.wav"):
    """Upload and send audio as a Matrix m.audio event."""
    try:
        # Upload audio file
        response = await bot.client.upload(
            io=BytesIO(audio_bytes),
            content_type="audio/wav",
            filename=filename
        )
        mxc_uri = response.content_uri

        # Estimate duration (simplified; use pydub for accuracy)
        duration_ms = int(len(audio_bytes) / 2 * 8 / 16000 * 1000)  # rough estimate for 16kHz mono

        # Send audio message
        content = {
            "body": filename,
            "msgtype": "m.audio",
            "url": mxc_uri,
            "info": {
                "mimetype": "audio/wav",
                "size": len(audio_bytes),
                "duration": duration_ms,
                "waveform": [0, 10, 20, 30, 20, 10, 0] * 10  # minimal placeholder
            },
            "file": {"url": mxc_uri, "content_type": "audio/wav", "filename": filename}
        }

        await bot.client.room_send(room_id, "m.room.message", content=content, ignore_unverified_devices=True)
    except Exception as e:
        print(f"❌ Failed to send audio message: {e}")
