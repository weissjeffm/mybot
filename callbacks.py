import asyncio
import markdown
import traceback
from langchain_core.messages import HumanMessage
from langgraph_agent import run_agent_logic 
from media_utils import extract_audio_bytes, transcribe_audio
from auth_utils import handle_verification_request
from bot_utils import get_display_name, get_structured_history

async def process_message(bot, room, event):
    if event.sender == bot.client.user_id: return

    # Fix: Get sender_name at top to prevent UnboundLocalError
    sender_name = await get_display_name(bot, event.sender)
    clean_body = ""

    # 1. Handle Audio
    is_audio = "audio" in str(type(event)).lower()
    if is_audio:
        print(f"🎙️ Audio from {sender_name}")
        audio_bytes = await extract_audio_bytes(bot.client, event)
        if audio_bytes:
            await bot.client.room_typing(room.room_id, True)
            transcript = await transcribe_audio(audio_bytes, "voice.ogg", "sk-50cf096cc7c795865e")
            if transcript:
                print(f"📝 Transcript: {transcript}")
                clean_body = f"[Transcription of voice message from {sender_name}]: {transcript}"
    
    # 2. Handle Text
    elif hasattr(event, "body"):
        clean_body = event.body.replace(bot.client.user_id, "").replace(bot.short_name, "", 1).strip()

    if not clean_body: return

    # 3. Auth Commands
    if clean_body == "!verify":
        await handle_verification_request(bot.client, room.room_id, event.sender)
        return

    # 4. Gatekeeper
    is_direct = room.member_count <= 2
    is_mentioned = (bot.short_name in clean_body.lower()) or (bot.client.user_id in str(event.source))
    if not (is_direct or is_mentioned or is_audio):
        return

    # 5. Threading
    content = event.source.get('content', {})
    relates_to = content.get('m.relates_to', {})
    thread_root_id = relates_to.get('event_id') if relates_to.get('rel_type') == 'm.thread' else event.event_id

    await run_agent_turn(bot, room, thread_root_id, sender_name, clean_body)

async def run_agent_turn(bot, room, thread_root_id, sender_name, clean_body):
    ui_state = {"log_event_id": None, "thoughts": [], "active_tools": {}}
    
    async def log_callback(text, node=None, data=None):
        async with bot.ui_lock:
            data = data or {}
            if node == "act_start":
                for action in data.get("actions", []): ui_state["active_tools"][action] = "⚙️"
            elif node == "act_finish":
                for item in data.get("results", []):
                    ui_state["active_tools"][item["action"]] = "✅" if item["status"] == "ok" else "❌"
            elif node == "reason":
                ui_state["thoughts"].append(text)

            try: await bot.client.room_typing(room.room_id, True, timeout=30000)
            except: pass

            display_lines = [f"💭 {t}" for t in ui_state["thoughts"][-2:]]
            display_lines += [f"{icon} {act}" for act, icon in ui_state["active_tools"].items()]
            if not display_lines: return

            html_body = f"<blockquote><font color='gray'>{'<br>'.join(display_lines)}</font></blockquote>"
            msg_content = {
                "msgtype": "m.notice", "body": f"* Thinking...\n" + "\n".join(display_lines),
                "format": "org.matrix.custom.html", "formatted_body": html_body,
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id}
            }
            if ui_state["log_event_id"]:
                msg_content["m.new_content"] = {"msgtype": "m.notice", "body": msg_content["body"], "format": "org.matrix.custom.html", "formatted_body": html_body}
                msg_content["m.relates_to"] = {"rel_type": "m.replace", "event_id": ui_state["log_event_id"]}

            resp = await bot.client.room_send(room.room_id, "m.room.message", content=msg_content, ignore_unverified_devices=True)
            if not ui_state["log_event_id"]: ui_state["log_event_id"] = resp.event_id

    try:
        history = await get_structured_history(bot, room.room_id, thread_root_id)
        history.append(HumanMessage(content=f"{sender_name}: {clean_body}"))

        initial_state = {
            "messages": history,
            "bot_name": await bot.display_name or bot.short_name or "Assistant",
            "log_callback": log_callback
        }
        
        response = await asyncio.wait_for(run_agent_logic(initial_state), timeout=600)
        
        html_response = markdown.markdown(response, extensions=['tables', 'fenced_code', 'nl2br'])
        await bot.client.room_send(
            room.room_id, "m.room.message",
            content={
                "msgtype": "m.text", "body": response, "format": "org.matrix.custom.html", 
                "formatted_body": html_response,
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id}
            }, ignore_unverified_devices=True
        )
    except Exception:
        traceback.print_exc()
        
