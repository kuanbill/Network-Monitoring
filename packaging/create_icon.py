"""Generate our own icon; no downloaded artwork or external assets."""
from pathlib import Path
from PIL import Image, ImageDraw

path = Path(__file__).with_name('netword.ico')
image = Image.new('RGBA', (256, 256), (15, 23, 42, 255))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((12, 12, 244, 244), radius=48, fill=(22, 33, 54, 255), outline=(56, 189, 248, 255), width=8)
draw.line([(40, 134), (80, 134), (103, 76), (132, 185), (161, 112), (179, 134), (216, 134)],
          fill=(52, 211, 153, 255), width=13, joint='curve')
image.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print('Created packaging/netword.ico')
