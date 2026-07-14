from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path, PurePosixPath
import re
import shutil
import sys


REQUIRED_DISTRIBUTIONS = (
    "anyio",
    "bottle",
    "certifi",
    "cffi",
    "clr-loader",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "prompt-toolkit",
    "proxy-tools",
    "pycparser",
    "pyinstaller",
    "pythonnet",
    "pywebview",
    "wcwidth",
)

OPTIONAL_DISTRIBUTIONS = (
    "altgraph",
    "colorama",
    "packaging",
    "pefile",
    "pillow",
    "pygments",
    "pyinstaller-hooks-contrib",
    "pywin32-ctypes",
    "setuptools",
    "typing-extensions",
    "websocket-client",
)

FALLBACK_LICENSES = {
    "proxy-tools": Path(__file__).resolve().parents[1] / "third_party" / "licenses" / "proxy_tools-LICENSE.txt",
}

LICENSE_OVERRIDES = {
    "proxy-tools": "BSD-3-Clause",
}


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def is_license_file(path: PurePosixPath) -> bool:
    lower_parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    in_metadata_licenses = any(
        part.endswith(".dist-info") and "licenses" in lower_parts[index + 1 :]
        for index, part in enumerate(lower_parts)
    )
    return in_metadata_licenses or name.startswith(("license", "copying", "notice", "authors"))


def license_relative_path(path: PurePosixPath) -> Path:
    lower_parts = tuple(part.casefold() for part in path.parts)
    if "licenses" in lower_parts:
        index = lower_parts.index("licenses")
        remainder = path.parts[index + 1 :]
        if remainder:
            return Path(*remainder)
    return Path(path.name)


def project_url(dist: metadata.Distribution) -> str:
    urls = dist.metadata.get_all("Project-URL") or []
    for value in urls:
        label, separator, url = value.partition(",")
        if separator and label.strip().casefold() in {"source", "repository", "homepage", "home"}:
            return url.strip()
    return str(dist.metadata.get("Home-page") or "").strip()


def collect_distribution(name: str, output_dir: Path, *, required: bool) -> list[str] | None:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        if required:
            raise RuntimeError(f"required distribution is missing: {name}") from None
        return None

    canonical_name = str(dist.metadata.get("Name") or name)
    component_dir = output_dir / f"{normalized_name(canonical_name)}-{dist.version}"
    copied: list[str] = []
    for item in dist.files or ():
        relative = PurePosixPath(str(item).replace("\\", "/"))
        if not is_license_file(relative):
            continue
        source = Path(dist.locate_file(item))
        if not source.is_file():
            continue
        destination = component_dir / license_relative_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination.relative_to(output_dir).as_posix())

    fallback = FALLBACK_LICENSES.get(normalized_name(name))
    if not copied and fallback is not None and fallback.is_file():
        destination = component_dir / "LICENSE.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fallback, destination)
        copied.append(destination.relative_to(output_dir).as_posix())
    if not copied:
        raise RuntimeError(f"no license file found for bundled distribution: {canonical_name} {dist.version}")

    license_name = LICENSE_OVERRIDES.get(normalized_name(name)) or str(
        dist.metadata.get("License-Expression") or dist.metadata.get("License") or "see included files"
    ).strip()
    lines = [
        f"{canonical_name} {dist.version}",
        f"  License: {license_name or 'see included files'}",
    ]
    url = project_url(dist)
    if url:
        lines.append(f"  Project: {url}")
    lines.append("  Files: " + ", ".join(sorted(copied)))
    return lines


def collect(output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    expected = (Path(__file__).resolve().parents[1] / "build" / "third-party-licenses").resolve()
    if output_dir != expected:
        raise RuntimeError(f"license output must be the isolated build directory: {expected}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    entries: list[list[str]] = []
    for name in REQUIRED_DISTRIBUTIONS:
        result = collect_distribution(name, output_dir, required=True)
        assert result is not None
        entries.append(result)
    for name in OPTIONAL_DISTRIBUTIONS:
        result = collect_distribution(name, output_dir, required=False)
        if result is not None:
            entries.append(result)

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python license file is missing: {python_license}")
    python_dir = output_dir / f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(python_license, python_dir / "LICENSE.txt")
    entries.append(
        [
            f"Python {sys.version.split()[0]}",
            "  License: Python Software Foundation License",
            "  Project: https://www.python.org/",
            f"  Files: {python_dir.relative_to(output_dir).as_posix()}/LICENSE.txt",
        ]
    )

    manifest = output_dir / "THIRD_PARTY_COMPONENTS.txt"
    content = [
        "DeepSeekFathom bundled third-party components",
        "",
        "The complete license texts referenced below are distributed in this directory.",
        "ReasoniX attribution is distributed separately in NOTICE.txt.",
        "",
    ]
    for entry in sorted(entries, key=lambda item: item[0].casefold()):
        content.extend(entry)
        content.append("")
    manifest.write_text("\n".join(content), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = collect(args.output)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
