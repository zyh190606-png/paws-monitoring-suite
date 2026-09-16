from pathlib import Path

from PIL import Image, ImageDraw


out = Path(__file__).resolve().parent / "paws_dashboard.ico"
size = 256
image = Image.new("RGBA", (size, size), (5, 20, 17, 255))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 248, 248), radius=48, fill=(8, 38, 31, 255), outline=(56, 214, 177, 255), width=5)
draw.polygon([(128, 28), (225, 96), (128, 228), (31, 96)], fill=(10, 67, 54, 255), outline=(56, 214, 177, 255))
for offset, alpha in [(0, 255), (26, 190), (52, 120)]:
    points = []
    for x in range(35, 224, 8):
        y = 111 + offset + int(10 * __import__("math").sin((x - 35) / 28))
        points.append((x, y))
    draw.line(points, fill=(56, 214, 177, alpha), width=7, joint="curve")
image.save(out, sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
print(out)

