"""Cloudinary video handling, with a fake Cloudinary: what it asks for, and that every failure falls back."""
import pytest

from cooking import media


class FakeUploader:
    def __init__(self, fail=False):
        self.fail, self.uploads, self.destroyed = fail, [], []

    def upload(self, path, **options):
        if self.fail:
            raise RuntimeError("upload refused")
        self.uploads.append((path, options))

    def destroy(self, public_id, **options):
        self.destroyed.append(public_id)


class FakeUtils:
    @staticmethod
    def cloudinary_url(public_id, **options):
        return f"https://res.example/{public_id}/{options.get('format', 'mp4')}", options


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "tiktok.mp4"
    path.write_bytes(b"x" * 4096)
    return str(path)


def test_without_cloudinary_the_original_video_is_used(video, tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)
    with media.open_video(video) as v:
        assert isinstance(v, media.LocalVideo) and v.for_model(str(tmp_path)) == video


def test_upload_asks_for_the_small_copy_and_the_upload_is_deleted_after(video, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_download", lambda url, path: open(path, "wb").write(b"y" * 1024))
    uploader = FakeUploader()
    v = media.CloudVideo(video, uploader, FakeUtils)
    small = v.for_model(str(tmp_path))
    assert small != video and open(small, "rb").read() == b"y" * 1024
    assert uploader.uploads[0][1]["eager"] == [{**media.FOR_MODEL, "format": "mp4"}] and uploader.uploads[0][1]["resource_type"] == "video"
    v.close()
    assert uploader.destroyed == [v.public_id]


def test_a_failing_download_falls_back_to_the_original_and_to_ffmpeg(video, tmp_path, monkeypatch):
    def broken(url, path):
        raise OSError("network down")
    monkeypatch.setattr(media, "_download", broken)
    monkeypatch.setattr(media, "ffmpeg_frame", lambda *args: "ffmpeg")
    v = media.CloudVideo(video, FakeUploader(), FakeUtils)
    assert v.for_model(str(tmp_path)) == video
    assert media.LocalVideo.frame(v, 3.0, str(tmp_path / "dish.jpg")) == "ffmpeg"
    assert v.frame(3.0, str(tmp_path / "dish.jpg")) == "ffmpeg"


def test_a_failing_upload_means_working_locally(video, monkeypatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://key:secret@demo")
    monkeypatch.setattr(media, "CloudVideo", lambda *args: (_ for _ in ()).throw(RuntimeError("no account")))
    with media.open_video(video) as v:
        assert type(v) is media.LocalVideo
