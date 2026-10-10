"""Render original README artwork; Pillow is a documentation-only dependency.

Run from the repository root after installing Pillow: python docs/assets/render_workflow.py
The runtime and conformance environments do not need Pillow.
"""
from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1200, 500
BG, PANEL = '#0b1220', '#131e30'
INK, MUTED, LINE = '#f4f7fb', '#b7c4d9', '#33465f'
ACCENT, GREEN = '#6ee7f0', '#9ce6be'


def font(size, bold=False):
    candidates = (
        ('C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf'),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
         else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
        ('/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold
         else '/System/Library/Fonts/Supplemental/Arial.ttf'),
    )
    override = os.environ.get('TRAILFORGE_DOC_FONT_BOLD' if bold else 'TRAILFORGE_DOC_FONT')
    for path in ((override,) if override else ()) + candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError('Set TRAILFORGE_DOC_FONT and TRAILFORGE_DOC_FONT_BOLD to local TTF files')


def centered(draw, xy, text, face, fill=INK):
    draw.text(xy, text, font=face, fill=fill, anchor='mm')


STAGES = [
    ('DEFINE', 'Scope + goal', 'The host defines scope, adapters, checks and limits.'),
    ('ADMIT', 'Reserve + persist', 'Record an attempt and its reservation before executing.'),
    ('EXECUTE', 'Bounded adapter', 'Validate the output and bind it to the admitted goal.'),
    ('VERIFY', 'Named checks', 'Required verifiers establish PASS, FAIL or UNKNOWN.'),
    ('CHECKPOINT', 'Resume same goal', 'Persist progress; uncertain work needs host resolution.'),
]


def render(active=-1, progress=0):
    image = Image.new('RGB', (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((38, 32, 1162, 468), radius=22, fill=PANEL, outline=LINE, width=2)
    draw.text((67, 60), 'TRAILFORGE', font=font(43, True), fill=INK)
    draw.text((69, 120), 'Bounded work. Durable evidence. Explicit authority.', font=font(23), fill=MUTED)
    draw.rounded_rectangle((985, 65, 1127, 102), radius=12, outline=LINE, width=2)
    centered(draw, (1056, 84), 'PUBLIC ALPHA', font(15, True), ACCENT)
    for i, (title, subtitle, _) in enumerate(STAGES):
        x = 67 + i * 215
        color = ACCENT if i == active else LINE
        if i < 4:
            draw.line((x + 187, 238, x + 214, 238), fill=MUTED, width=2)
            draw.polygon([(x + 214, 238), (x + 207, 233), (x + 207, 243)], fill=MUTED)
        draw.rounded_rectangle((x, 190, x + 185, 287), radius=12, outline=color,
                               fill=BG, width=3 if i == active else 2)
        centered(draw, (x + 92, 218), title, font(18, True), ACCENT if i == active else INK)
        centered(draw, (x + 92, 257), subtitle, font(15), MUTED)
        if i < active:
            draw.ellipse((x + 166, 178, x + 190, 202), fill=GREEN)
            centered(draw, (x + 178, 190), '+', font(16, True), BG)
    caption = STAGES[active][2] if active >= 0 else 'An illustrative control loop; tasks execute in their declared order.'
    centered(draw, (600, 333), caption, font(21), INK)
    draw.line((68, 371, 1128, 371), fill=LINE, width=1)
    if active >= 0:
        draw.line((68, 371, 68 + int(1060 * (active + progress) / 5), 371), fill=ACCENT, width=3)
    centered(draw, (600, 402), 'UNKNOWN -> host decision    |    external write -> separate approval', font(18), MUTED)
    centered(draw, (600, 439), 'Provider-neutral Python kernel  /  MIT', font(15), MUTED)
    return image


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    static = render()
    static.save(ROOT / 'workflow.png', optimize=True)
    frames, durations = [static], [1800]
    for stage in range(5):
        for step in range(6):
            frames.append(render(stage, (step + 1) / 6))
            durations.append(240)
    frames.append(static)
    durations.append(1600)
    # A shared palette prevents frame-to-frame color shifts.
    palette = static.quantize(colors=96)
    encoded = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    encoded[0].save(ROOT / 'workflow.gif', save_all=True, append_images=encoded[1:],
                    duration=durations, loop=0, disposal=1, optimize=True)
    print(f'Rendered {len(frames)} frames and static fallback in {ROOT}')


if __name__ == '__main__':
    main()
