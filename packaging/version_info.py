"""Write a PyInstaller Windows version-resource file.

usage: python version_info.py <output-file> <exe-name> <description>
The version is read from handback/__init__.py (no import, so no side effects).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version():
    text = (ROOT / "handback" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def render(version, exe, description):
    nums = [int(p) for p in re.findall(r"\d+", version)[:4]]
    nums += [0] * (4 - len(nums))
    tup = tuple(nums)
    original = exe + ".exe"
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'handback'),
      StringStruct('FileDescription', {description!r}),
      StringStruct('FileVersion', {version!r}),
      StringStruct('InternalName', {exe!r}),
      StringStruct('OriginalFilename', {original!r}),
      StringStruct('ProductName', 'handback'),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


if __name__ == "__main__":
    out, exe, desc = sys.argv[1:4]
    Path(out).write_text(render(package_version(), exe, desc), encoding="utf-8")
