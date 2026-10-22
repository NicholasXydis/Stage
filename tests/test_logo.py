import base64
import struct
from pathlib import Path

import pytest

from stage.ai.server import icons
from stage.paths import logo_dir

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "logo"


def _size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def test_the_packaged_logo_is_the_one_in_assets() -> None:
    packaged = (logo_dir() / "stage.svg").read_bytes()

    assert packaged == (ASSETS / "stage.svg").read_bytes()
    assert packaged.startswith(b"<svg") and b'viewBox="0 0 64 64"' in packaged


@pytest.mark.parametrize("size", [16, 32, 180, 512])
def test_every_png_in_assets_has_the_size_its_name_says(size: int) -> None:
    assert _size((ASSETS / f"stage-{size}.png").read_bytes()) == (size, size)


def test_the_icons_stage_mcp_sends_decode_to_the_logo_files() -> None:
    sent = icons()

    assert [icon.mime_type for icon in sent] == ["image/svg+xml", "image/png"]
    svg, png = (base64.b64decode(icon.src.split(",", 1)[1]) for icon in sent)
    assert svg == (logo_dir() / "stage.svg").read_bytes()
    assert _size(png) == (128, 128)
    assert all(icon.src.startswith(f"data:{icon.mime_type};base64,") for icon in sent)


def test_a_missing_logo_file_is_left_out_instead_of_breaking_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage import paths

    (tmp_path / "stage.svg").write_bytes((logo_dir() / "stage.svg").read_bytes())
    monkeypatch.setattr(paths, "logo_dir", lambda: tmp_path)

    assert [icon.mime_type for icon in icons()] == ["image/svg+xml"]
