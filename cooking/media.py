"""The cooking video a bot downloads, prepared for the model and for the dish photo.

With CLOUDINARY_URL set, the video is uploaded to Cloudinary once and two things come back from it:
a smaller copy for Gemini (at most 640 px, compressed, audio kept for the narration) and the photo of the
finished dish, taken from the video at the model's timestamp and auto-improved. The upload is deleted
when the bot is done with it.

Without Cloudinary, or if any Cloudinary call fails, it falls back to what the bots did before: the
original video goes to Gemini and ffmpeg takes the photo. A bot never fails because of this module.

    with open_video(path) as video:
        recipe = analyze(video.for_model(tmpdir))
        has_photo = video.frame(recipe.final_dish_timestamp_seconds, photo_path)
"""
from __future__ import annotations

import os
import subprocess
import uuid

import httpx

FOLDER = "cooking-assistant"
# What Gemini needs from a cooking video: what happens and what is said, not full resolution.
FOR_MODEL = {"width": 640, "height": 640, "crop": "limit", "quality": "auto:low",
             "video_codec": "h264", "audio_codec": "aac"}
# The dish photo: a still from the video, sized for a chat message and auto-improved.
FRAME = {"width": 1080, "crop": "limit", "effect": "improve", "quality": "auto"}
DOWNLOAD_TIMEOUT_S = 120


def _download(url: str, path: str) -> None:
    with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def ffmpeg_frame(video_path: str, seconds: float, out_path: str) -> bool:
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", str(max(0.0, seconds)), "-i", video_path, "-frames:v", "1",
                        "-q:v", "2", out_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(out_path)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


class LocalVideo:
    """No Cloudinary: the original file for the model, ffmpeg for the photo."""

    def __init__(self, path: str):
        self.path = path

    def for_model(self, out_dir: str) -> str:
        return self.path

    def frame(self, seconds: float, out_path: str) -> bool:
        return ffmpeg_frame(self.path, seconds, out_path)

    def close(self) -> None:
        pass


class CloudVideo(LocalVideo):
    """Uploaded to Cloudinary; every method falls back to `LocalVideo` if Cloudinary fails."""

    def __init__(self, path: str, uploader, utils):
        super().__init__(path)
        self._uploader, self._utils = uploader, utils
        self.public_id = f"{FOLDER}/{uuid.uuid4().hex}"
        # Made at upload, so the copy is ready when the model needs it.
        self._uploader.upload(path, resource_type="video", public_id=self.public_id,
                              eager=[{**FOR_MODEL, "format": "mp4"}])
        self._uploaded = True

    def for_model(self, out_dir: str) -> str:
        try:
            url, _ = self._utils.cloudinary_url(self.public_id, resource_type="video", secure=True, format="mp4",
                                                transformation=[FOR_MODEL])
            out = os.path.join(out_dir, "for_model.mp4")
            _download(url, out)
            print(f"[media] video for the model: {os.path.getsize(self.path) // 1024} KB -> "
                  f"{os.path.getsize(out) // 1024} KB", flush=True)
            return out
        except Exception as e:                   # noqa: BLE001  any Cloudinary or network failure
            print(f"[media] Cloudinary video failed, sending the original: {e}", flush=True)
            return self.path

    def frame(self, seconds: float, out_path: str) -> bool:
        try:
            url, _ = self._utils.cloudinary_url(self.public_id, resource_type="video", secure=True, format="jpg",
                                                transformation=[{"start_offset": max(0.0, seconds)}, FRAME])
            _download(url, out_path)
            return os.path.getsize(out_path) > 0
        except Exception as e:                   # noqa: BLE001
            print(f"[media] Cloudinary photo failed, using ffmpeg: {e}", flush=True)
            return super().frame(seconds, out_path)

    def close(self) -> None:
        if self._uploaded:
            try:
                self._uploader.destroy(self.public_id, resource_type="video", invalidate=True)
            except Exception as e:               # noqa: BLE001  a leftover upload is not worth failing for
                print(f"[media] could not delete {self.public_id}: {e}", flush=True)
            self._uploaded = False


class open_video:
    """Context manager: `CloudVideo` when CLOUDINARY_URL is set and the upload works, else `LocalVideo`."""

    def __init__(self, path: str):
        self.path = path
        self.video: LocalVideo | None = None

    def __enter__(self) -> LocalVideo:
        self.video = LocalVideo(self.path)
        if os.environ.get("CLOUDINARY_URL"):
            try:
                import cloudinary
                import cloudinary.uploader
                import cloudinary.utils
                cloudinary.reset_config()        # read CLOUDINARY_URL now, after the bot loaded its .env
                self.video = CloudVideo(self.path, cloudinary.uploader, cloudinary.utils)
            except Exception as e:               # noqa: BLE001
                print(f"[media] Cloudinary upload failed, working locally: {e}", flush=True)
        return self.video

    def __exit__(self, *exc) -> None:
        self.video.close()
