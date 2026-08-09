from PIL import ImageDraw, ImageFont

def draw_grid(img, grid_size: int, font_path: str, origin=(0, 0), scale: float = 1.0):
    # Labels are always true screen coordinates: screen = origin + image_px / scale.
    draw = ImageDraw.Draw(img)
    lw, lh = img.size
    ox, oy = origin
    try:
        font = ImageFont.truetype(font_path, 13)
    except Exception:
        font = ImageFont.load_default()

    def _screen_lines(o, length, step):
        start = (o // step) * step
        if start < o: start += step
        return range(start, o + int(length / scale) + 1, step)

    minor_size = max(10, grid_size // 4)
    for sx in _screen_lines(ox, lw, minor_size):
        if sx % grid_size != 0:
            px = round((sx - ox) * scale)
            draw.line([(px, 0), (px, lh)], fill=(255, 120, 120, 90), width=1)
    for sy in _screen_lines(oy, lh, minor_size):
        if sy % grid_size != 0:
            py = round((sy - oy) * scale)
            draw.line([(0, py), (lw, py)], fill=(255, 120, 120, 90), width=1)
    for sx in _screen_lines(ox, lw, grid_size):
        px = round((sx - ox) * scale)
        draw.line([(px, 0), (px, lh)], fill=(255, 50, 50, 180), width=2)
    for sy in _screen_lines(oy, lh, grid_size):
        py = round((sy - oy) * scale)
        draw.line([(0, py), (lw, py)], fill=(255, 50, 50, 180), width=2)

    for sx in _screen_lines(ox, lw, grid_size):
        for sy in _screen_lines(oy, lh, grid_size):
            px, py = round((sx - ox) * scale), round((sy - oy) * scale)
            label = f"{sx},{sy}"
            bbox = draw.textbbox((px + 2, py + 1), label, font=font)
            draw.rectangle(bbox, fill=(255, 255, 255, 210))
            draw.text((px + 2, py + 1), label, fill=(220, 0, 0), font=font)
    return img