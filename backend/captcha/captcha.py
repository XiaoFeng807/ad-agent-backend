"""验证码生成模块：用PIL生成随机验证码图片"""
import random, string
from PIL import Image, ImageDraw, ImageFont


def generate_captcha():
    """生成验证码图片，返回（图片对象, 验证码文字）"""
    # 随机生成4位验证码（数字+大写字母）
    code = "".join(random.choices(string.digits + string.ascii_uppercase, k=4))

    # 创建120x40的空白图片
    img = Image.new("RGB", (120, 40), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 画干扰线（防止机器识别）
    for _ in range(5):
        x1, y1 = random.randint(0, 120), random.randint(0, 40)
        x2, y2 = random.randint(0, 120), random.randint(0, 40)
        draw.line([(x1, y1), (x2, y2)], fill=(random.randint(100, 200),) * 3, width=2)

    # 画验证码文字（每个字符随机颜色和位置）
    for i, c in enumerate(code):
        x = 10 + i * 25 + random.randint(-3, 3)
        y = random.randint(5, 15)
        color = (random.randint(0, 100), random.randint(0, 100), random.randint(0, 100))
        draw.text((x, y), c, fill=color)

    return img, code.lower()  # 返回图片和验证码（转小写方便比对）
