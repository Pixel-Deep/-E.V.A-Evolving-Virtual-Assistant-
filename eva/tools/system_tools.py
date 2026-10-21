
"""Safe local system utilities for EVA."""

from datetime import datetime


def get_local_datetime() -> str:
    """Return the current date and time from the PC's local clock."""

    now = datetime.now().astimezone()

    result = now.strftime(
        "It is %I:%M:%S %p on %A, %d %B %Y"
    ).replace(" 0", " ")

    timezone = now.strftime("%Z")

    if timezone:
        result += f" ({timezone})"

    return result + "."
