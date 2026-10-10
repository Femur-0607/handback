# PyInstaller spec: one onedir folder with handback.exe (console) and
# handback-dashboard.exe (windowed) sharing a single _internal directory.
# Environment: HB_VERSION_DIR holds cli.txt / dashboard.txt version resources.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).resolve().parent
vdir = Path(os.environ["HB_VERSION_DIR"])
icon = root / "handback" / "assets" / "icon.ico"
icon_arg = [str(icon)] if icon.exists() else None

datas = [(str(root / "handback" / "skills" / "SKILL.md"), "handback/skills")]
for sub in ("antigravity", "codex"):
    datas.append((str(root / "handback" / "skills" / sub / "SKILL.md.in"), f"handback/skills/{sub}"))
for f in sorted((root / "handback" / "assets").iterdir()):
    if f.is_file():
        datas.append((str(f), "handback/assets"))

hidden = collect_submodules("handback") + ["tkinter", "tkinter.ttk"]
common = dict(pathex=[str(root)], datas=datas, hiddenimports=hidden, excludes=["pytest"], noarchive=False)
utf8 = [("X utf8_mode=1", None, "OPTION")]

a_cli = Analysis([str(root / "packaging" / "entry_cli.py")], **common)
a_dash = Analysis([str(root / "packaging" / "entry_dashboard.py")], **common)
MERGE((a_cli, "entry_cli", "handback"), (a_dash, "entry_dashboard", "handback-dashboard"))

pyz_cli = PYZ(a_cli.pure)
pyz_dash = PYZ(a_dash.pure)

exe_cli = EXE(pyz_cli, a_cli.scripts, [], exclude_binaries=True, name="handback", console=True,
              icon=icon_arg, version=str(vdir / "cli.txt"), options=utf8, upx=False)
exe_dash = EXE(pyz_dash, a_dash.scripts, [], exclude_binaries=True, name="handback-dashboard", console=False,
               icon=icon_arg, version=str(vdir / "dashboard.txt"), options=utf8, upx=False)

COLLECT(exe_cli, a_cli.binaries, a_cli.datas,
        exe_dash, a_dash.binaries, a_dash.datas,
        strip=False, upx=False, name="handback")
