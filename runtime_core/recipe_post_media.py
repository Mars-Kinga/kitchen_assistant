"""Bounded image preparation for recipe posts; uses pinned public networking."""
from __future__ import annotations

import base64
import struct
from concurrent.futures import ThreadPoolExecutor

from .video_media import VideoMediaError, fetch_public_resource

MAX_POST_IMAGES = 16
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_PREPARED_BYTES = 16 * 1024 * 1024


def image_dimensions(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 4 <= len(data):
            if data[index] != 255:
                break
            while index < len(data) and data[index] == 255:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return width, height
            index += length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise VideoMediaError("图文图片格式无效，支持 JPEG、PNG、WebP。", status_code=415)


def prepare_image(data: bytes) -> bytes:
    if len(data) > MAX_IMAGE_BYTES:
        raise VideoMediaError("单张教程图片不能超过 8 MB。", status_code=413)
    width, height = image_dimensions(data)
    if min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise VideoMediaError("教程图片尺寸过大。", status_code=413)
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
        raise VideoMediaError("无法读取教程图片。", status_code=415)
    scale = min(1.0, 1600 / max(image.shape[:2]))
    if scale < 1:
        image = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise VideoMediaError("教程图片处理失败。", status_code=415)
    return encoded.tobytes()


def download_post_images(urls: list[str]) -> tuple[list[str], int]:
    if len(urls) > MAX_POST_IMAGES:
        raise VideoMediaError("图文教程最多支持 16 张图片。", status_code=413)
    def download(url: str) -> bytes:
        resource = fetch_public_resource(url, max_bytes=MAX_IMAGE_BYTES, timeout=15)
        return prepare_image(resource.data)
    with ThreadPoolExecutor(max_workers=3) as pool:
        images = list(pool.map(download, urls))
    size = sum(len(data) for data in images)
    if size > MAX_PREPARED_BYTES:
        raise VideoMediaError("教程图片总量过大。", status_code=413)
    return ["data:image/jpeg;base64," + base64.b64encode(data).decode("ascii") for data in images], size
