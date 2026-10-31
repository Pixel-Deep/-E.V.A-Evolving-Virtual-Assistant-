
"""Safe local system utilities for EVA."""

from datetime import datetime


def get_local_datetime() -> str:
    """Return the current date and time from the PC's local clock."""
    now = datetime.now().astimezone()
    # Format time with standard 12-hour clock (hour 1-12 without irregular leading-zero string replacement)
    hour = now.hour % 12 or 12
    time_str = f"{hour}:{now.strftime('%M:%S %p')}"
    result = f"It is {time_str} on {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}"
    timezone = now.strftime("%Z")
    if timezone:
        result += f" ({timezone})"
    return result + "."
