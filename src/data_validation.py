"""Input validation helpers for the reporting notebook.

Public API
----------
- validate_today(today) -> date
"""

import warnings
from datetime import date, timedelta


def validate_today(today: date) -> date:
    """Sanity-check the user-supplied report ``today`` and adjust if needed.

    Rules:
      * ``today`` cannot be in the future — reset to ``date.today()``.
      * ``today`` cannot be a Sunday (no stand-up that day) — reset to the
        previous calendar day (Saturday).

    Each adjustment emits a ``UserWarning`` so the notebook surfaces the
    change to the user without aborting the run.
    """
    if today > date.today():
        warnings.warn(
            f"'today' cannot be in the future \u2014 changed from {today} to {date.today()}.",
            stacklevel=2,
        )
        today = date.today()

    if today.weekday() == 6:  # Sunday
        new_today = today - timedelta(days=1)
        warnings.warn(
            f"'today' cannot be Sunday \u2014 changed from {today} to {new_today}.",
            stacklevel=2,
        )
        today = new_today

    return today
