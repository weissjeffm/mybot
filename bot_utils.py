from io import BytesIO
from nio import RoomMessageText, UploadResponse
from langchain_core.messages import HumanMessage, AIMessage

def should_send_audio(response_text: str) -> bool:
    """
    Determine if an audio version should be sent based on response length.
    Returns True for responses over 100 characters.
    """
    return len(response_text.strip()) > 100

async def summarize_for_audio(response_text: str, llm) -> str:
    """
    Create a concise summary of the response for audio playback.
    Targets 2-3 sentences that capture the key points.
    """
    if len(response_text) <= 100:
        return response_text
        
    if not llm:
        return response_text[:200] + "..." if len(response_text) > 200 else response_text
        
    summary_prompt = f"""Create a concise spoken summary of the following message in no more than 200 words. If the response is already short and suitable for speech, leave it unchanged. When summarizing, stay in first person (you're summarizing your own message), focus on the key and actionable information. Omit headings, code blocks, tables, and markdown formatting. Make it sound natural when spoken aloud.

Example:
Response: "The server temperatures are within normal limits: CPU normal, GPU normal, Inlet normal. No cooling issues detected."
Spoken summary: The server temperatures are normal.

Do NOT include phrases like: "The message states that", or "the message goes on to say", that's not first person.
Original response:
```    
{response_text}
```
Concise spoken summary:"""
    
    try:
        response = await llm.ainvoke([{"role": "user", "content": summary_prompt}])
        return response.content.strip()
    except Exception as e:
        print(f"⚠️ Failed to generate audio summary: {e}")
        # Return first 200 chars if summarization fails
        return response_text[:200] + "..." if len(response_text) > 200 else response_text

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

from nio import UploadResponse

import subprocess
import asyncio
from nio import UploadResponse
import wave

async def send_audio_message(bot, room_id: str, audio_bytes: bytes, filename: str = "voice.ogg", thread_id: str = None):
    """
    Transcodes audio to Ogg Opus (Matrix standard) and sends it.
    """
    print(f"🎤 Received {len(audio_bytes)} bytes. Transcoding to Ogg Opus...")

    try:
        with wave.open(BytesIO(audio_bytes), 'rb') as w:
            # frames / rate * 1000 = duration in ms
            duration_ms = int((w.getnframes() / w.getframerate()) * 1000)
    except Exception as e:
        print(f"⚠️ Could not read WAV duration, using sane default: {e}")
        duration_ms = 5000
        
    # 1. Transcode to Ogg Opus using FFmpeg
    # We force the codec to 'libopus' to satisfy Element.
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', 
            '-i', 'pipe:0',       # Read from stdin
            '-c:a', 'libopus',    # FORCE Opus codec
            '-b:a', '24k',        # 24k bitrate is plenty for voice
            '-ar', '48000',       # Opus likes 48kHz sample rate
            '-application', 'voip',# Optimization: Tells Opus to tune for voice, not music
            '-ar', '48000',        # Sample Rate: 48kHz (Native Opus rate)
            '-f', 'ogg',          # Ogg container
            'pipe:1',             # Write to stdout
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )
        
        # Communicate sends the input bytes and reads the output
        opus_bytes, stderr = await process.communicate(input=audio_bytes)
        
        if process.returncode != 0:
            print(f"❌ Transcoding failed: {stderr.decode()}")
            return

        print(f"✅ Transcoded! Size: {len(audio_bytes)} -> {len(opus_bytes)} bytes")

    except FileNotFoundError:
        print("❌ FFmpeg is not installed. Install it with: sudo dnf install ffmpeg")
        return

    # 2. Upload the Opus bytes
    try:
        resp, maybe_keys = await bot.client.upload(
            lambda retry, timeout: opus_bytes,
            content_type="audio/ogg", 
            filename=filename,
            filesize=len(opus_bytes)
        )

        if not isinstance(resp, UploadResponse):
            print(f"❌ Upload failed: {resp}")
            return

        # 3. Construct the Event
        # Note: We rely on Element estimating duration from the file size/header now that it's valid Opus.
        content = {
            "body": filename,
            "msgtype": "m.audio",
            "url": resp.content_uri,
            "info": {
                "mimetype": "audio/ogg",
                "size": len(opus_bytes),
                "duration": duration_ms
            },
            "org.matrix.msc3245.voice": {}
        }

        if thread_id:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}

        await bot.client.room_send(room_id, "m.room.message", content=content)

    except Exception as e:
        print(f"❌ Failed to send audio: {e}")
        
