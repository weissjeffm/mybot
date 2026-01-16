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
