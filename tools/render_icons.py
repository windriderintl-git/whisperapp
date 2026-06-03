"""Generate the Whisper 2 app icon as a multi-resolution .ico.

Outputs to two paths (one used by PyInstaller for the EXE, one by Inno Setup).
Run from project root: `python tools/render_icons.py`
"""
from pathlib import Path
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
OUT_PATHS = [
    PROJECT / "assets" / "icons" / "app.ico",
    PROJECT / "installer" / "app.ico",
]
SIZES = [16, 32, 48, 64, 128, 256]
BG_COLOR = (109, 76, 165, 255)        # deep purple
FG_COLOR = (255, 255, 255, 255)       # white mic


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square background (nicer than full circle on small sizes).
    margin = max(1, size // 12)
    radius = max(2, size // 5)
    d.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius,
        fill=BG_COLOR,
    )

    # Mic capsule: tall pill in the middle.
    cap_w = size * 7 // 24
    cap_h = size * 12 // 24
    cap_x = (size - cap_w) // 2
    cap_y = size * 4 // 24
    d.rounded_rectangle(
        (cap_x, cap_y, cap_x + cap_w, cap_y + cap_h),
        radius=cap_w // 2,
        fill=FG_COLOR,
    )

    # Stand: U-shape yoke under the capsule, then a short post and base line.
    stroke = max(2, size // 16)
    stand_bottom = size - size * 5 // 24            # baseline y
    yoke_w = cap_w + size * 5 // 24                 # wider than capsule
    yoke_h = size * 6 // 24                         # arc height
    yoke_top = cap_y + cap_h - yoke_h // 2          # overlap top half with capsule
    yoke_x = (size - yoke_w) // 2
    # Draw the arc spanning a full ellipse, but only bottom half (start=0..180).
    d.arc(
        (yoke_x, yoke_top, yoke_x + yoke_w, yoke_top + yoke_h),
        start=0, end=180, fill=FG_COLOR, width=stroke,
    )
    # Mask the part of the arc that overlaps the capsule by redrawing the
    # capsule bottom — not needed since both are white; arc just extends past
    # the capsule edges to form the yoke arms.

    # Post under the capsule: from the bottom of the arc down to the base.
    post_top = yoke_top + yoke_h // 2
    post_bottom = stand_bottom - max(2, size // 32)
    if post_bottom > post_top:
        post_w = max(2, size // 16)
        post_x = (size - post_w) // 2
        d.rectangle(
            (post_x, post_top, post_x + post_w, post_bottom),
            fill=FG_COLOR,
        )
    # Base line.
    base_w = cap_w + size // 4
    base_x = (size - base_w) // 2
    base_h = max(2, size // 24)
    d.rounded_rectangle(
        (base_x, stand_bottom - base_h, base_x + base_w, stand_bottom),
        radius=base_h // 2,
        fill=FG_COLOR,
    )
    return img


def main() -> None:
    base = render(256)
    # Build a list of resized variants for the .ico (PIL handles multi-res .ico).
    frames = []
    for s in SIZES:
        if s == 256:
            frames.append(base)
        else:
            frames.append(render(s))
    for out in OUT_PATHS:
        out.parent.mkdir(parents=True, exist_ok=True)
        # Pillow writes a multi-size .ico when given `sizes=[(w,h), ...]`.
        # We pass the largest as the base and let PIL handle the rest.
        base.save(out, format="ICO", sizes=[(s, s) for s in SIZES],
                  append_images=frames[:-1])
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
