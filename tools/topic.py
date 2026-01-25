import json

def signal_topic_change(subject: str):
    """Call this when the user changes the conversation topic to
     something COMPLETELY unrelated to the previous discussion
     (example: a hard pivot from "python coding" to "gardening"). The
     purpose is to automatically file different discussions into
     different threads, so the user doesn't have to remember to do it
     manually. All you have to do is call this function, the
     orchestration program does the rest.

    Args: subject: A 3-6 word title
    for the new topic (e.g. "Server Disk Space").

    """
    # Returns a JSON signal that bridge.py will intercept
    return {
        "status": "ok",
        "event": "TOPIC_CHANGE",
        "topic": subject
        # Remove 'message' - let the AI generate the reply naturally
    }
