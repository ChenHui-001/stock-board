"""修 _main_vite_integration.py：_ASSET_RE 那行的字符串字面量错误。改用文件读写的方式。"""
from pathlib import Path

p = Path("scripts/_main_vite_integration.py")
src = p.read_text(encoding="utf-8")

# 替换有问题的两行
src = src.replace(
    "old3 = '_ASSET_RE = re.compile(r\"(/static/(?:css|js|vendor)/[^\\\"\\\\'?#\\\\s]+)\")'\n"
    "new3 = '_ASSET_RE = re.compile(r\"((?:/static/(?:css|js|vendor|dist)/|/assets/)[^\\\"\\\\'?#\\\\s]+)\")'",

    "old3 = '_ASSET_RE = re.compile(r\"(/static/(?:css|js|vendor)/[^\" + chr(92) + chr(34) + chr(63) + chr(35) + chr(92) + 's]+)\")'\n"
    "new3 = '_ASSET_RE = re.compile(r\"((?:/static/(?:css|js|vendor|dist)/|/assets/)[^\" + chr(92) + chr(34) + chr(63) + chr(35) + chr(92) + 's]+)\")'"
)

# 因为上面的替换很复杂，且 main.py 里的 _ASSET_RE 行有大量转义，
# 改用 main.py 的字符串文本替换：从源文件直接读 + 替换 + 写回

new_block = '''# 3. 改 _ASSET_RE：扩展匹配 /assets/ 和 /static/dist/
old3 = '_ASSET_RE = re.compile('
new3_marker = '_ASSET_RE = re.compile(('
main_path = Path("backend/main.py")
main_src = main_path.read_text(encoding="utf-8")
import re
m = re.search(r"_ASSET_RE = re.compile\\((.*?)\\)", main_src)
if not m:
    raise SystemExit("未找到 _ASSET_RE")
# 原：r"(/static/(?:css|js|vendor)/[^"'?#\\s]+)"
# 新：r"((?:/static/(?:css|js|vendor|dist)/|/assets/)[^"'?#\\s]+)"
old_pattern = m.group(0)
new_pattern = old_pattern.replace("/static/(?:css|js|vendor)/", "(?:/static/(?:css|js|vendor|dist)/|/assets/)")
new_pattern = new_pattern.replace("(/static", "((?:/static")
main_src = main_src.replace(old_pattern, new_pattern, 1)
main_path.write_text(main_src, encoding="utf-8")
print(f"OK _ASSET_RE 已扩展: {old_pattern} -> {new_pattern}")'''

# 直接覆盖整个文件
p.write_text(new_block, encoding="utf-8")
print("OK 已简化 _main_vite_integration.py")
