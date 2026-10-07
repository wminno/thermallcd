#!/usr/bin/env python3
"""First-contact test: push a static test card to the Trofeo Vision LCD.

Usage: python3 test_card.py [--frames N] [--fps F]
Sends the card continuously until Ctrl+C by default, so you can also see
whether the panel holds the image or falls back to the vendor logo.
"""

import argparse
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from lcd import TrofeoLCD

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

BAR_COLORS = [
    (255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 255, 0),
    (0, 255, 255), (0, 0, 255), (255, 0, 255), (255, 255, 255),
]


def build_card() -> Image.Image:
    W, H = 1920, 462
    img = Image.new("RGB", (W, H), (10, 13, 18))
    d = ImageDraw.Draw(img)

    # color bar strip
    bar_y, bar_h = 380, 60
    bar_w = W / len(BAR_COLORS)
    for i, c in enumerate(BAR_COLORS):
        d.rectangle([i * bar_w, bar_y, (i + 1) * bar_w, bar_y + bar_h], fill=c)

    big = ImageFont.truetype(FONT_BOLD, 110)
    med = ImageFont.truetype(FONT_BOLD, 44)
    mono = ImageFont.truetype(FONT_MONO, 30)

    d.text((60, 40), "THERMALLCD", font=big, fill=(70, 220, 160))
    d.text((62, 170), "Thermalright Trofeo Vision 9.16\"  -  1920x462  -  0416:5408",
           font=med, fill=(200, 205, 215))
    d.text((62, 240), time.strftime("%Y-%m-%d  %H:%M:%S"), font=mono, fill=(120, 190, 255))
    d.text((62, 290), "if you can read this, the LCD works", font=mono, fill=(180, 180, 180))
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=0, help="0 = loop until Ctrl+C")
    ap.add_argument("--fps", type=float, default=1.0)
    args = ap.parse_args()

    card = build_card()

    lcd = TrofeoLCD(debug=True)
    lcd.open()
    info = lcd.init()
    print(f"panel info: {info}")

    n = 0
    interval = 1.0 / args.fps
    try:
        while args.frames == 0 or n < args.frames:
            t0 = time.monotonic()
            packets = lcd.send_image(card)
            dt = time.monotonic() - t0
            n += 1
            print(f"frame {n:3d}: {packets} packets, {dt * 1000:.0f} ms")
            time.sleep(max(0.0, interval - dt))
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        lcd.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
