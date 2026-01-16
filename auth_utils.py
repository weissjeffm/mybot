import uuid
import time
import asyncio

async def handle_verification_request(client, room_id, sender_id):
    """Sends a standard Matrix verification request."""
    try:
        content = {
            "body": "🔐 Verification Request",
            "msgtype": "m.key.verification.request",
            "to": sender_id,
            "from_device": client.device_id,
            "methods": ["m.sas.v1"],
            "timestamp": int(time.time() * 1000),
            "transaction_id": str(uuid.uuid4())
        }
        await client.room_send(
            room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True
        )
        print(f"🔐 Verification request sent to {sender_id}")
    except Exception as e:
        print(f"🔐 Verification failed: {e}")
