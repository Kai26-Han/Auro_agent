"""不依赖模型的普通 Python 工具函数。"""


def count_characters(text: str) -> int:
    """统计非空白 Unicode 字符数；标点也计数，并非分词或 token 数。"""
    return sum(1 for character in text if not character.isspace())
