"""
图片验证码生成函数（对应教程 14.1 生成图片）

PDF 里的做法：用 Pillow 画一张 120x30 的图，随机写 5 个字母，
再加干扰点、干扰弧线、干扰线，最后做一次边缘增强，返回 (图片对象, 验证码字符串)。

验证码绘制工具：Pillow 生成图片，同时返回对应字母串给登录视图保存。
图片内容不是登录凭证；有效期、一次性校验及密码检查在 views/account.py 完成。
"""
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def check_code(width=120, height=30, char_length=5, font_file='arial.ttf', font_size=28):
    code = []
    # 创建一张白色底图
    img = Image.new(mode='RGB', size=(width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img, mode='RGB')

    def rndChar():
        """
        生成随机字母
        :return:
        """
        return chr(random.randint(65, 90))

    def rndColor():
        """
        生成随机颜色
        :return:
        """
        return (random.randint(0, 255), random.randint(10, 255), random.randint(64, 255))

    # 写文字
    # font_file 用相对路径时 Pillow 会自动去系统字体目录找；
    # 万一找不到（比如换了机器），退回到 Pillow 自带字体，保证验证码一定能生成。
    try:
        font = ImageFont.truetype(font_file, font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)

    for i in range(char_length):
        char = rndChar()
        code.append(char)
        h = random.randint(0, 4)
        draw.text((i * width / char_length, h), char, font=font, fill=rndColor())

    # 写干扰点
    for i in range(40):
        draw.point((random.randint(0, width), random.randint(0, height)), fill=rndColor())

    # 写干扰圆圈
    for i in range(40):
        draw.point((random.randint(0, width), random.randint(0, height)), fill=rndColor())
        x = random.randint(0, width)
        y = random.randint(0, height)
        draw.arc((x, y, x + 4, y + 4), 0, 90, fill=rndColor())

    # 画干扰线
    for i in range(5):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=rndColor())

    # 边缘增强，让字母更难被机器识别
    img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)

    return img, ''.join(code)


if __name__ == '__main__':
    img, code_str = check_code()
    print(code_str)
    with open('code.png', 'wb') as f:
        img.save(f, format='png')