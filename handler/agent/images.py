"""Keeping old screenshots out of the request.

An agent loop resends the entire transcript on every round, and a computer-use
transcript is mostly screenshots. One gridded capture is around 850KB once it
is base64'd, so ten of them in the history is 8.5MB going up the wire before
the model can emit its first token -- every round, growing, and paid again for
each round after. That upload is the silence between a tool finishing and the
reply starting, and prompt caching does not touch it: cache_control saves the
server re-reading the prompt, not the client re-sending it.

Nothing is lost by dropping them. Every capture is already archived to the
session's screenshots directory by the tool itself, and a screenshot from
fifteen rounds ago is a picture of a screen that no longer looks like that --
the agent's own reason for taking a new one. What it needs is the *recent*
frames, to see what its last action did.

Eviction is permanent and in place, which is what keeps it cheap for the
prompt cache: a message is rewritten once, at the moment it falls out of the
window, and never touched again. The cached prefix ahead of that point still
matches, and the point itself is always near the tail -- so a round invalidates
the last few messages rather than the conversation.
"""

# Left where the image was, rather than deleting the block outright. A model
# that can see the gap asks for a fresh capture; one that finds a tool result
# it remembers sending mysteriously empty tends to conclude the tool is broken.
NOTE = ("[screenshot removed from the transcript -- only the most recent captures are kept. "
        "Take a new screenshot if you need to see the screen.]")

DEFAULT_KEEP = 2


def _slots(msg: dict) -> list[tuple]:
    """Every encoded image in one message, in the order it appears.

    Three dialects, three shapes, and a tool result nests one of them a level
    down. Returned as (container, key) pairs so the caller can blank one
    without needing to know which shape it came from.
    """
    slots = []
    content = msg.get("content")
    if isinstance(content, list):
        for i, block in enumerate(content):
            if not isinstance(block, dict): continue
            kind = block.get("type")
            if kind in ("image", "image_url"):
                slots.append((content, i))
            elif kind == "tool_result" and isinstance(block.get("content"), list):
                # anthropic puts the image inside the tool result itself.
                for j, inner in enumerate(block["content"]):
                    if isinstance(inner, dict) and inner.get("type") in ("image", "image_url"):
                        slots.append((block["content"], j))
    # ollama hangs a plain list of base64 strings off the message.
    if msg.get("images"):
        slots.append((msg, "images"))
    return slots


def _blank(container, key) -> None:
    if isinstance(key, int):
        container[key] = {"type": "text", "text": NOTE}
        return
    container.pop("images", None)
    text = container.get("content")
    container["content"] = f"{text}\n{NOTE}" if isinstance(text, str) and text else NOTE


def evict(messages: list[dict], keep: int = DEFAULT_KEEP) -> int:
    """Blank every image except the newest `keep`. Returns how many went.

    keep=0 is a real answer -- a blind model's transcript replayed onto a
    sighted one has images in it that nothing will ever look at again.
    """
    if keep < 0: return 0
    slots = [s for msg in messages for s in _slots(msg)]
    doomed = slots[:-keep] if keep else slots
    for container, key in doomed:
        _blank(container, key)
    return len(doomed)
