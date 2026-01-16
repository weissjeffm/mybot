from datetime import datetime

def current_date_time() -> str:
    """Returns the current date and time. Use this when you need to know the current date/time in order to find what you need (eg, researching current events, schedules, weather, etc)"""
    now = datetime.now()
    # Format: Thursday, January 15, 2026, 21:49
    return now.strftime("%A, %B %d, %Y, %H:%M")
