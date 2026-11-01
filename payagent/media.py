"""Evidence file validation: real content, not the file extension.

Accepted: JPEG, PNG, WebP images and MP4, WebM videos. Videos must be readable
and at most ``MAX_VIDEO_SECONDS`` long. A video whose duration cannot be read is
refused, so a long clip can never slip through an unparseable container.
"""

import struct

from . import config

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/webm"}
ALLOWED_TYPES = IMAGE_TYPES | VIDEO_TYPES

SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def max_bytes() -> int:
    return config.MAX_EVIDENCE_MB * 1024 * 1024


def detect_mime(data: bytes) -> str | None:
    """Identify the type from the bytes themselves (magic numbers)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return None


# ---------------------------------------------------------------- MP4 (ISO BMFF)
def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _mvhd_seconds(payload: bytes) -> float | None:
    if len(payload) < 20:
        return None
    version = payload[0]
    if version == 1:
        if len(payload) < 32:
            return None
        timescale = int.from_bytes(payload[20:24], "big")
        duration = int.from_bytes(payload[24:32], "big")
    else:
        timescale = int.from_bytes(payload[12:16], "big")
        duration = int.from_bytes(payload[16:20], "big")
    if not timescale:
        return None
    return duration / timescale


def mp4_duration(data: bytes) -> float | None:
    for kind, start, end in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for inner, inner_start, inner_end in _boxes(data, start, end):
            if inner == b"mvhd":
                return _mvhd_seconds(data[inner_start:inner_end])
    return None


# ---------------------------------------------------------------- WebM (EBML)
SEGMENT_ID = 0x18538067
INFO_ID = 0x1549A966
TIMECODE_SCALE_ID = 0x2AD7B1
DURATION_ID = 0x4489


def _vint_length(first: int) -> int | None:
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length
    return None


def _elements(data: bytes, start: int, end: int):
    pos = start
    while pos < end:
        id_len = _vint_length(data[pos])
        if not id_len or pos + id_len >= end:
            return
        element_id = int.from_bytes(data[pos : pos + id_len], "big")
        size_pos = pos + id_len
        size_len = _vint_length(data[size_pos])
        if not size_len or size_pos + size_len > end:
            return
        size = int.from_bytes(data[size_pos : size_pos + size_len], "big")
        size &= (1 << (7 * size_len)) - 1  # drop the length marker bits
        body_start = size_pos + size_len
        body_end = body_start + size
        if body_start > end or body_end > end:
            return
        yield element_id, body_start, body_end
        pos = body_end


def webm_duration(data: bytes) -> float | None:
    timecode_scale = 1_000_000
    duration = None
    for element_id, start, end in _elements(data, 0, len(data)):
        if element_id != SEGMENT_ID:
            continue
        for info_id, info_start, info_end in _elements(data, start, end):
            if info_id != INFO_ID:
                continue
            for child_id, child_start, child_end in _elements(data, info_start, info_end):
                raw = data[child_start:child_end]
                if child_id == TIMECODE_SCALE_ID and raw:
                    timecode_scale = int.from_bytes(raw, "big")
                elif child_id == DURATION_ID and len(raw) in (4, 8):
                    duration = struct.unpack(">f" if len(raw) == 4 else ">d", raw)[0]
    if duration is None:
        return None
    return duration * timecode_scale / 1e9


def video_duration(data: bytes, mime: str) -> float | None:
    if mime == "video/mp4":
        return mp4_duration(data)
    if mime == "video/webm":
        return webm_duration(data)
    return None


# ---------------------------------------------------------------- validation
class EvidenceError(ValueError):
    """The uploaded file is not acceptable as refund evidence."""


def validate(data: bytes, filename: str = "") -> tuple[str, float | None]:
    """Return (mime, duration_seconds) or raise EvidenceError."""
    if not data:
        raise EvidenceError("The file is empty")
    if len(data) > max_bytes():
        raise EvidenceError(f"The file is larger than {config.MAX_EVIDENCE_MB} MB")

    mime = detect_mime(data)
    if mime is None or mime not in ALLOWED_TYPES:
        raise EvidenceError("Only JPEG, PNG and WebP images or MP4 and WebM videos are accepted")

    if mime in IMAGE_TYPES:
        return mime, None

    duration = video_duration(data, mime)
    if duration is None:
        raise EvidenceError(
            "We could not read the video duration. Please upload an MP4 or WebM "
            "video that is at most 60 seconds long."
        )
    if duration > config.MAX_VIDEO_SECONDS + 0.5:
        raise EvidenceError(
            f"The video is {duration:.0f} seconds long; the limit is {config.MAX_VIDEO_SECONDS} seconds"
        )
    return mime, round(duration, 2)


def kind_of(mime: str) -> str:
    return "video" if mime in VIDEO_TYPES else "image"
