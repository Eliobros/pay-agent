"""Evidence validation: content sniffing and video duration."""

import struct

import pytest

from payagent import media


def mp4(seconds: float, timescale: int = 1000, version: int = 0) -> bytes:
    if version == 1:
        payload = (
            bytes([1, 0, 0, 0])
            + (0).to_bytes(8, "big")
            + (0).to_bytes(8, "big")
            + timescale.to_bytes(4, "big")
            + int(seconds * timescale).to_bytes(8, "big")
            + b"\x00" * 76
        )
    else:
        payload = (
            bytes([0, 0, 0, 0])
            + (0).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
            + timescale.to_bytes(4, "big")
            + int(seconds * timescale).to_bytes(4, "big")
            + b"\x00" * 76
        )
    mvhd = (len(payload) + 8).to_bytes(4, "big") + b"mvhd" + payload
    moov = (len(mvhd) + 8).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom" + b"\x00\x00\x00\x00"
    return ftyp + moov


def elem(element_id: int, payload: bytes) -> bytes:
    id_len = (element_id.bit_length() + 7) // 8
    return element_id.to_bytes(id_len, "big") + b"\x01" + len(payload).to_bytes(7, "big") + payload


def webm(seconds: float) -> bytes:
    # Duration is expressed in TimecodeScale ticks: scale 1 ms means 1000 ticks
    # per second, exactly like a real recorder writes it.
    info = elem(0x2AD7B1, (1_000_000).to_bytes(3, "big")) + elem(
        0x4489, struct.pack(">f", seconds * 1000)
    )
    segment = elem(0x18538067, elem(0x1549A966, info))
    header = elem(0x1A45DFA3, elem(0x4282, b"webm"))
    return header + segment


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF" + (50).to_bytes(4, "little") + b"WEBP" + b"\x00" * 100


def test_detects_the_real_content():
    assert media.detect_mime(JPEG) == "image/jpeg"
    assert media.detect_mime(PNG) == "image/png"
    assert media.detect_mime(WEBP) == "image/webp"
    assert media.detect_mime(mp4(10)) == "video/mp4"
    assert media.detect_mime(webm(10)) == "video/webm"


def test_rejects_files_that_only_look_like_media():
    assert media.detect_mime(b"<html><script>alert(1)</script>") is None
    assert media.detect_mime(b"%PDF-1.7\n") is None
    assert media.detect_mime(b"PK\x03\x04zip") is None
    with pytest.raises(media.EvidenceError):
        media.validate(b"<?php echo 1; ?>", "photo.jpg")


def test_images_are_accepted_without_a_duration():
    assert media.validate(JPEG, "photo.jpg") == ("image/jpeg", None)
    assert media.validate(PNG, "photo.png")[0] == "image/png"


def test_mp4_and_webm_durations_are_read():
    assert media.validate(mp4(42), "clip.mp4") == ("video/mp4", 42.0)
    assert media.validate(mp4(7, version=1), "clip.mp4")[1] == 7.0
    assert media.validate(webm(30), "clip.webm") == ("video/webm", 30.0)


def test_video_longer_than_the_limit_is_refused():
    with pytest.raises(media.EvidenceError) as error:
        media.validate(mp4(90), "clip.mp4")
    assert "60" in str(error.value)
    assert media.validate(mp4(60), "clip.mp4")[1] == 60.0


def test_unreadable_video_duration_is_refused():
    # ftyp header but no moov/mvhd: the duration cannot be established.
    broken = (16).to_bytes(4, "big") + b"ftyp" + b"isom" + b"\x00\x00\x00\x00"
    with pytest.raises(media.EvidenceError, match="duration"):
        media.validate(broken, "clip.mp4")


def test_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(media.config, "MAX_EVIDENCE_MB", 0)
    with pytest.raises(media.EvidenceError, match="larger"):
        media.validate(JPEG, "photo.jpg")


def test_empty_file_is_refused():
    with pytest.raises(media.EvidenceError, match="empty"):
        media.validate(b"", "photo.jpg")
