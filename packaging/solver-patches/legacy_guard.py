"""Validate the legacy puzzle again after a delayed solver API response."""
import numpy as np


def verify_legacy_puzzle(previous_canvas, previous_box, previous_button,
        screenshot, find_box, find_button, extract_canvas, error_class):
    box = find_box(screenshot)
    button = find_button(screenshot)
    if any(abs(getattr(box, key) - getattr(previous_box, key)) > 10 for key in ('left', 'top')):
        raise error_class('Legacy puzzle moved while waiting for SadCaptcha')
    if any(abs(getattr(button, key) - getattr(previous_button, key)) > 10 for key in ('left', 'top')):
        raise error_class('Legacy slider moved while waiting for SadCaptcha')
    if (box.width, box.height, button.width, button.height) != (
            previous_box.width, previous_box.height, previous_button.width, previous_button.height):
        raise error_class('Legacy puzzle resized while waiting for SadCaptcha')
    canvas = extract_canvas(screenshot.crop((box.left, box.top,
        box.left + box.width, box.top + box.height)))
    if canvas.size != previous_canvas.size:
        raise error_class('Legacy canvas resized while waiting for SadCaptcha')
    before = np.asarray(previous_canvas.convert('RGB'), dtype=np.int16)
    after = np.asarray(canvas.convert('RGB'), dtype=np.int16)
    if np.mean(np.abs(before - after)) > 5:
        raise error_class('Legacy puzzle image changed while waiting for SadCaptcha')
