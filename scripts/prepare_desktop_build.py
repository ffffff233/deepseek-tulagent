from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def render_version_info(version: str) -> str:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"desktop version must be X.Y.Z: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'DeepSeekFathom'),
          StringStruct('FileDescription', 'DeepSeekFathom 桌面应用'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'DeepSeekFathom'),
          StringStruct('LegalCopyright', 'Copyright (c) DeepSeekFathom contributors'),
          StringStruct('OriginalFilename', 'DeepSeekFathom.exe'),
          StringStruct('ProductName', 'DeepSeekFathom'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def prepare(version: str, output_dir: Path) -> Path:
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"desktop version must be X.Y.Z: {version!r}")
    root = Path(__file__).resolve().parents[1]
    expected = (root / "build" / "desktop-release").resolve()
    output_dir = output_dir.resolve()
    if output_dir != expected:
        raise RuntimeError(f"desktop build output must be the isolated build directory: {expected}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    assets_dir = output_dir / "assets"
    shutil.copytree(root / "src" / "deepseekfathom" / "_core" / "desktop" / "assets", assets_dir)

    index_path = assets_dir / "index.html"
    index = index_path.read_text(encoding="utf-8")
    if index.count("__DESKTOP_VERSION__") < 2:
        raise RuntimeError("desktop index is missing version cache placeholders")
    index_path.write_text(index.replace("__DESKTOP_VERSION__", version), encoding="utf-8")
    (output_dir / "windows-version-info.txt").write_text(render_version_info(version), encoding="utf-8")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.version, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
