import aiohttp
from nio.crypto import decrypt_attachment

async def transcribe_audio(audio_bytes, filename, api_key):
    """Sends audio bytes to LocalAI with authentication."""
    stt_url = "http://localhost:8080/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    
    data = aiohttp.FormData()
    data.add_field('file', audio_bytes, filename=filename, content_type='application/octet-stream')
    data.add_field('model', 'whisper-large')

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(stt_url, data=data, headers=headers) as resp:
                if resp.status == 200:
                    json_resp = await resp.json()
                    return json_resp.get("text", "")
                return None
    except Exception as e:
        print(f"⚠️ Transcription failure: {e}")
        return None

async def extract_audio_bytes(client, event):
    """Handles both RoomMessageAudio and RoomEncryptedAudio types."""
    try:
        file_info = None
        if hasattr(event, 'file') and event.file:
            file_info = event.file
        elif 'file' in event.source.get('content', {}):
            file_info = event.source['content']['file']

        if file_info:
            mxc_url = getattr(file_info, 'url', file_info.get('url'))
            resp = await client.download(mxc_url)
            ciphertext = resp.body
            
            key = getattr(file_info, 'key', file_info.get('key'))['k']
            iv = getattr(file_info, 'iv', file_info.get('iv'))
            hashes = getattr(file_info, 'hashes', file_info.get('hashes'))['sha256']
            
            audio_bytes = decrypt_attachment(ciphertext, key, hashes, iv)
            print(f"✅ Decryption successful. Bytes: {len(audio_bytes)}")
            return audio_bytes
        else:
            print("📂 Plain attachment download...")
            resp = await client.download(event.url)
            return resp.body
    except Exception as e:
        print(f"❌ Audio extraction failed: {e}")
        return None
    
