""" Utility for alive_progress."""

from pyqenc.constants import WARNING_SYMBOL


def update_bar(bar, increment: int = 1, failed: int = 0):
    # We target parallel running, no reason to add specific text
    # Let's use this place to show number of failed chunks
    if bar is not None:
        bar.text = ""  if failed<=0 else f"{WARNING_SYMBOL} {failed} failed"
        if increment != 0:
            bar(increment)
