from pathlib import Path

from PIL import Image, ImageDraw


out = Path(__file__).resolve().parent / "paws_local_monitor.ico"
size = 256
image = Image.new("RGBA", (size, size), (7, 15, 18, 255))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 248, 248), radius=48, fill=(14, 31, 35, 255), outline=(244, 182, 95, 255), width=6)
draw.ellipse((47, 47, 209, 209), outline=(98, 213, 209, 255), width=9)
draw.ellipse((82, 82, 174, 174), outline=(98, 213, 209, 150), width=5)
draw.line((128, 34, 128, 90), fill=(244, 182, 95, 255), width=8)
draw.line((128, 166, 128, 222), fill=(244, 182, 95, 255), width=8)
draw.line((34, 128, 90, 128), fill=(244, 182, 95, 255), width=8)
draw.line((166, 128, 222, 128), fill=(244, 182, 95, 255), width=8)
draw.ellipse((112, 112, 144, 144), fill=(181, 217, 111, 255))
image.save(out, sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
print(out)
