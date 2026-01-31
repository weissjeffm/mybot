import logging
import asyncio
import markdown
import traceback
from langchain_core.messages import HumanMessage

# Suppress verbose Matrix SDK schema validation warnings
logging.getLogger("matrix").setLevel(logging.ERROR)
logging.getLogger("matrix_client").setLevel(logging.ERROR)
from langgraph_agent import run_agent_logic, set_llm_instance
import langgraph_agent
from media_utils import extract_audio_bytes, transcribe_audio, text_to_speech
from auth_utils import handle_verification_request
from bot_utils import get_display_name, get_structured_history, send_audio_message, should_send_audio, summarize_for_audio

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
            transcript = await transcribe_audio(
                audio_bytes,
                "voice.ogg",
                base_url=bot.localai_base_url,
                api_key=bot.localai_api_key
            )
            if transcript:
                print(f"📝 Transcript: {transcript}")
                clean_body = f"[Transcription of voice message from {sender_name}]: {transcript}"
    
    # 2. Handle Text
    elif hasattr(event, "body"):
        clean_body = event.body.replace(bot.client.user_id, "").replace(bot.short_name, "", 1).strip()
   
    if not clean_body: return

    print(f"📥 Message from {sender_name}: {clean_body[:120]}{'...' if len(clean_body) > 120 else ''}")
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

    await run_agent_turn(bot, room, thread_root_id, sender_name, clean_body, event.event_id)

async def run_agent_turn(bot, room, thread_root_id, sender_name, clean_body, event_id):
    ui_state = {"log_event_id": None, "thoughts": [], "active_tools": {}}
    
    # Send typing indicator immediately when processing starts
    await bot.client.room_typing(room.room_id, True, timeout=5000)
    
    async def log_callback(text, node=None, data=None):
        async with bot.ui_lock:
            data = data or {}
            if node == "act_start":
                for action in data.get("actions", []):
                    if "error" in action:
                        continue
                    tool_name = action["name"].replace('_', ' ').capitalize()
                    args_str = ", ".join(repr(arg) for arg in action["args"])
                    if action["kwargs"]:
                        kwargs_str = ", ".join(f"{k}={repr(v)}" for k, v in action["kwargs"].items())
                        args_str = f"{args_str}, {kwargs_str}" if args_str else kwargs_str
                    display_text = f"{tool_name}: {args_str}"
                    ui_state["active_tools"][action["original"]] = {
                        "display": display_text, 
                        "status": "⚙️"
                    }
            elif node == "act_finish":
                #print(f"📥 Tools finished: {data}")
                for item in data.get("results", []):
                    action = item["action"]
                    original_str = action.get("original", "")
                    if original_str in ui_state["active_tools"]:
                        ui_state["active_tools"][original_str]["status"] = "✅" if item["status"] == "ok" else "❌"
            elif node == "reason":
                ui_state["thoughts"].append(text)

            # Update typing and UI
            try: await bot.client.room_typing(room.room_id, True, timeout=30000)
            except: pass

            display_lines = [f"💭 {t}" for t in ui_state["thoughts"][-2:]]
            display_lines += [f"{tool_info['status']} {tool_info['display']}" for tool_info in ui_state["active_tools"].values()]
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
            if not ui_state["log_event_id"]: 
                ui_state["log_event_id"] = resp.event_id

    try:
        # Get history and filter out any thread migration messages
        raw_history = await get_structured_history(bot, room.room_id, thread_root_id)
        # Filter out messages that are thread migration metadata
        history = [
            msg for msg in raw_history 
            if not (isinstance(msg.content, str) and msg.content.strip().startswith("[Thread migrated to:"))
        ]
        history.append(HumanMessage(content=f"{sender_name}: {clean_body}"))

        # Ensure LLM is initialized
        if langgraph_agent.llm is None:
            set_llm_instance(bot.localai_base_url, bot.localai_api_key)
            
        initial_state = {
            "messages": history,
            "bot_name": await bot.display_name or bot.short_name or "Assistant",
            "log_callback": log_callback,
            "llm": langgraph_agent.llm
        }
        
        
        result = await asyncio.wait_for(run_agent_logic(initial_state), timeout=600)
        final_response = result["response"]
        topic_change = result["topic_change"]

        final_thread_id = thread_root_id  # Default to current thread

        if topic_change:
            topic = topic_change["topic"]
            print(f"🔄 Topic change: '{topic}'")

            # 2. Create a NEW TOP-LEVEL MESSAGE as the new thread root
            new_root_content = {
                "msgtype": "m.text",
                "body": f"[New Topic: {topic}]\n\n"
                        f"> {clean_body}\n\n"
                        f"(Started from <a href='https://matrix.to/#/{room.room_id}/{thread_root_id}?via=dumaweiss.com'>previous discussion</a>)",
                "format": "org.matrix.custom.html",
                "formatted_body": f"<i>[New Topic: {topic}]</i><br>"
                                 f"<blockquote>{clean_body}</blockquote>"
                                 f"(Started from <a href='https://matrix.to/#/{room.room_id}/{thread_root_id}?via=dumaweiss.com'>previous discussion</a>)"
            }

            new_root_resp = await bot.client.room_send(
                room.room_id,
                "m.room.message",
                content=new_root_content,
                ignore_unverified_devices=True
            )
            new_root_event_id = new_root_resp.event_id  # Capture new thread ID

            # 1. Send a notification in the OLD THREAD (reply to old thread)
            old_thread_notification = {
                "msgtype": "m.text",
                "body": f"🔄 Topic changed to: '{topic}'\n\n"
                        f"Continuing discussion in a fresh thread: "
                        f"https://matrix.to/#/{room.room_id}/{new_root_event_id}?via=dumaweiss.com",
                "format": "org.matrix.custom.html",
                "formatted_body": f"🔄 Topic changed to: '<b>{topic}</b>'<br>"
                                 f"Continuing discussion in a <a href='https://matrix.to/#/{room.room_id}/{new_root_event_id}?via=dumaweiss.com'>fresh thread</a>."
            }

            await bot.client.room_send(
                room.room_id,
                "m.room.message",
                content={
                    **old_thread_notification,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id
                    }
                },
                ignore_unverified_devices=True
            )

            final_thread_id = new_root_event_id

            # Pause briefly before sending reply into new thread
            await asyncio.sleep(0.15)

        # Send final reply into correct thread
        print(f"📤 Sending response: {final_response[:120]}{'...' if len(final_response) > 120 else ''}")
        print(f"🎯 Final message will be sent in thread: {final_thread_id}")
        # Send text reply (unchanged)
        html_response = markdown.markdown(final_response, extensions=['tables', 'fenced_code', 'nl2br'])
        await bot.client.room_send(
            room.room_id, "m.room.message",
            content={
                "msgtype": "m.text", "body": final_response, "format": "org.matrix.custom.html", 
                "formatted_body": html_response,
                "m.relates_to": {"rel_type": "m.thread", "event_id": final_thread_id}
            }, ignore_unverified_devices=True
        )

        # Conditionally send TTS audio version based on response length
        try:
            if should_send_audio(final_response):
                print("🔊 Generating TTS audio response...")
                # Create a concise summary for audio playback
                if langgraph_agent.llm is None:
                    set_llm_instance(bot.localai_base_url, bot.localai_api_key)
                audio_text = await summarize_for_audio(final_response, langgraph_agent.llm)
                print(f"🔊 Audio summary: {audio_text[:120]}{'...' if len(audio_text) > 120 else ''}")
                audio_bytes = await text_to_speech(
                    audio_text,
                    base_url=bot.localai_base_url,
                    api_key=bot.localai_api_key
                )
                await send_audio_message(bot, room.room_id, audio_bytes, "response.ogg", thread_id=final_thread_id)
            else:
                print("🔇 Skipping audio generation for short response")
        except Exception as e:
            print(f"❌ TTS or audio send failed: {e}")
    except Exception:
        traceback.print_exc()
        
