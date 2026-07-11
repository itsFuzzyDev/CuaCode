from PIL import ImageDraw, ImageFont

def draw_grid(img, grid_size: int, font_path: str):
    draw = ImageDraw.Draw(img)
    lw, lh = img.size
    try:
        font = ImageFont.truetype(font_path, 13)
    except Exception:
        font = ImageFont.load_default()

    minor_size = max(10, grid_size // 4)
    for lx in range(0, lw + 1, minor_size):
        if lx % grid_size != 0: draw.line([(lx, 0), (lx, lh)], fill=(255, 120, 120, 90), width=1)
    for ly in range(0, lh + 1, minor_size):
        if ly % grid_size != 0: draw.line([(0, ly), (lw, ly)], fill=(255, 120, 120, 90), width=1)
    for lx in range(0, lw + 1, grid_size): draw.line([(lx, 0), (lx, lh)], fill=(255, 50, 50, 180), width=2)
    for ly in range(0, lh + 1, grid_size): draw.line([(0, ly), (lw, ly)], fill=(255, 50, 50, 180), width=2)

    for lx in range(0, lw + 1, grid_size):
        for ly in range(0, lh + 1, grid_size):
            label = f"{lx},{ly}"
            bbox = draw.textbbox((lx + 2, ly + 1), label, font=font)
            draw.rectangle(bbox, fill=(255, 255, 255, 210))
            draw.text((lx + 2, ly + 1), label, fill=(220, 0, 0), font=font)
    return img