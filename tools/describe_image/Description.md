---
name: describe_image
output:
  description: str
  text: str
  provider: str
active: True
require_permissions: False
blind_only: True
---
Borrowed eyes. This model cannot accept images, so this tool captures the
image, hands it to a model that can see, and returns what that model saw in
words.

**It takes the screenshot for you.** Pass source "screen" and it captures the
current screen itself. You do not need a screenshot tool, you do not have one,
and you do not need one — you never handle the image at all. You send a
question and a source; you get back text. So the screen is readable to you,
just not viewable.

source is "screen" for the current screen, or a file path or image URL for
anything already on disk or online.

This is a description, not a look. It is one model's account of what another
model saw, so it is slower than seeing, it costs an extra call, and it will
miss whatever your question did not ask about. Coordinates it reports are
guesses — do not click on them.

Ask one specific question. "What does the error dialog say" gets you the text;
"describe the screen" gets you a paragraph that is mostly furniture. The `text`
field is transcribed verbatim, which is what you want for error messages,
values and labels.

What this does not make possible is operating the interface. Clicking needs
coordinates read off a grid, and there is no grid in a description. If the user
wants you to drive a GUI, say plainly that it needs a vision-capable model —
but do not tell them you cannot see the screen at all, because you can read it
through this.
