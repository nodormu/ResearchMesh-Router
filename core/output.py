"""Shared output helpers for the local tools.

Every local tool clips what it returns so a single call can't flood the
context window. The per-tool budgets differ (bash/editor output is worth more
than a page of scraped HTML), so the limit is the caller's choice.

Tools that can return pixels rather than text (the file editor's `view` on an
image, memory's `view`, every computer-use screenshot) return the marker dict
built by `image_result`. Chat._local_result_to_content turns it into a real
`image` content block; anything that doesn't understand the marker just sees a
dict, never a mojibake string.
"""

# Extensions the file-editor and memory tools advertise image support for
# (their descriptions both say "Image files (.jpg, .jpeg, or .png)").
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def clip(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"
    return text


def image_result(media_type: str, data: str, text: str) -> dict:
    """Marker for a tool result that is an image. `data` is base64 (no
    newlines); `text` is the caption sent alongside the pixels."""
    return {
        "__kind__": "image",
        "media_type": media_type,
        "data": data,
        "text": text,
    }
