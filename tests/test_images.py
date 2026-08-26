"""Tests for storing article images inside the wiki — downloads are mocked."""

from unittest.mock import MagicMock, patch

import pytest

from subscriber.images import (
    IMAGE_NAME_RE,
    IMG_DIR,
    MAX_IMAGE_BYTES,
    save_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _response(content_type: str, body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.headers = {"content-type": content_type}
    resp.raise_for_status.return_value = None
    resp.iter_content.return_value = iter([body])
    return resp


class TestImageNameRe:
    def test_accepts_lowercase_hyphenated(self):
        assert IMAGE_NAME_RE.match("harness-interpreter-arch")

    def test_rejects_uppercase_dots_and_slashes(self):
        for bad in ("Harness", "arch.png", "a/b", "-lead", "", "图"):
            assert not IMAGE_NAME_RE.match(bad), bad


class TestSaveImage:
    @patch("subscriber.images.requests.get")
    def test_writes_under_imgs_with_the_real_extension(self, mock_get, tmp_path):
        """The model supplies a stem; the extension comes from what the server
        actually sent, so a .png name can never hold a jpeg."""
        mock_get.return_value = _response("image/jpeg", JPEG)
        name = save_image("https://cdn.example.com/x", "harness-arch", tmp_path)
        assert name == "harness-arch.jpg"
        assert (tmp_path / IMG_DIR / "harness-arch.jpg").read_bytes() == JPEG

    @patch("subscriber.images.requests.get")
    def test_png_kept_as_png(self, mock_get, tmp_path):
        mock_get.return_value = _response("image/png", PNG)
        assert save_image("https://cdn.example.com/x", "arch", tmp_path) == "arch.png"

    @patch("subscriber.images.requests.get")
    def test_bad_name_refused_before_download(self, mock_get, tmp_path):
        with pytest.raises(ValueError, match="名称"):
            save_image("https://cdn.example.com/x", "Arch.PNG", tmp_path)
        mock_get.assert_not_called()

    @patch("subscriber.images.requests.get")
    def test_non_image_refused(self, mock_get, tmp_path):
        """A link that 200s with an HTML error page must not land in imgs/."""
        mock_get.return_value = _response("text/html", b"<html>404</html>")
        with pytest.raises(ValueError, match="不是图片"):
            save_image("https://cdn.example.com/x", "arch", tmp_path)
        assert not (tmp_path / IMG_DIR).exists()

    @patch("subscriber.images.requests.get")
    def test_oversized_refused_without_writing(self, mock_get, tmp_path):
        resp = _response("image/png", b"")
        chunk = b"\x00" * 65536
        resp.iter_content.return_value = iter([chunk] * (MAX_IMAGE_BYTES // len(chunk) + 2))
        mock_get.return_value = resp
        with pytest.raises(ValueError, match="过大"):
            save_image("https://cdn.example.com/x", "arch", tmp_path)
        assert not (tmp_path / IMG_DIR / "arch.png").exists()

    @patch("subscriber.images.requests.get")
    def test_same_name_same_bytes_is_a_no_op(self, mock_get, tmp_path):
        """Recompiling a source re-saves the same figure; that must succeed
        and leave one file, not error or accumulate suffixes."""
        mock_get.side_effect = lambda *a, **k: _response("image/png", PNG)
        first = save_image("https://cdn.example.com/x", "arch", tmp_path)
        second = save_image("https://cdn.example.com/x", "arch", tmp_path)
        assert first == second == "arch.png"
        assert [p.name for p in (tmp_path / IMG_DIR).iterdir()] == ["arch.png"]

    @patch("subscriber.images.requests.get")
    def test_name_collision_with_other_content_refused(self, mock_get, tmp_path):
        mock_get.return_value = _response("image/png", PNG)
        save_image("https://cdn.example.com/x", "arch", tmp_path)
        mock_get.return_value = _response("image/png", PNG + b"different")
        with pytest.raises(ValueError, match="已存在"):
            save_image("https://cdn.example.com/y", "arch", tmp_path)

    @patch("subscriber.images.requests.get")
    def test_webp_and_gif_kept(self, mock_get, tmp_path):
        mock_get.return_value = _response("image/webp", b"RIFF....WEBP")
        assert save_image("https://cdn.example.com/w", "a", tmp_path) == "a.webp"
        mock_get.return_value = _response("image/gif", b"GIF89a")
        assert save_image("https://cdn.example.com/g", "b", tmp_path) == "b.gif"
