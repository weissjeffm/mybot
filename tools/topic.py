import json

def signal_topic_change(subject: str):
    """Call this when the user changes the conversation topic
    significantly. The purpose is to automatically file different
    discussions into different threads, so the user doesn't have to
    remember to do it manually. All you have to do is call this
    function when the user changes the subject, the orchestration
    program does the rest.

    Args: subject: A 3-6 word title
    for the new topic (e.g. "Server Disk Space").

    """
    # Returns a JSON signal that bridge.py will intercept
    return json.dumps({
        "signal": "TOPIC_CHANGE",
        "subject": subject
    })
