"""Bounded, unprivileged icon import; output is always a plain 72px RGBA PNG.

PNG decoding follows the W3C PNG specification's scanline/filter definitions:
https://www.w3.org/TR/png-3/#9Filtering
Optional JPEG/SVG conversion uses installed system tools, without a shell.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import struct
import subprocess
import xml.etree.ElementTree as ET
import zlib

SUPPORTED_SUFFIXES = ('.png', '.jpg', '.jpeg', '.svg')
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
SIGNATURE = b'\x89PNG\r\n\x1a\n'


class ImageError(ValueError):
    pass


def _check(condition, message):
    if not condition:
        raise ImageError(message)


def _dimensions(width, height):
    _check(1 <= width <= 4096 and 1 <= height <= 4096 and width * height <= MAX_PIXELS,
           'Choose an image up to 4096 pixels per side and 4 megapixels in total.')


def _paeth(left, up, corner):
    value = left + up - corner
    distances = (abs(value - left), abs(value - up), abs(value - corner))
    return (left, up, corner)[distances.index(min(distances))]


def decode_png(data):
    """Return (width, height, RGBA bytes), rejecting unsupported PNG features."""
    _check(isinstance(data, bytes) and len(data) <= MAX_BYTES and data.startswith(SIGNATURE),
           'Choose a PNG smaller than 8 MiB.')
    position, count = 8, 0
    header = palette = transparency = None
    compressed = []
    data_finished = ended = False
    while position + 12 <= len(data):
        count += 1
        _check(count <= 4096, 'PNG contains too many chunks.')
        length, = struct.unpack_from('>I', data, position)
        kind = data[position + 4:position + 8]
        finish = position + length + 12
        _check(finish <= len(data), 'PNG is truncated.')
        body = data[position + 8:finish - 4]
        _check(zlib.crc32(kind + body) & 0xffffffff == struct.unpack_from('>I', data, finish - 4)[0], 'PNG checksum mismatch.')
        _check(re.fullmatch(b'[A-Za-z]{4}', kind), 'Invalid PNG chunk name.')
        if header is None:
            _check(kind == b'IHDR' and length == 13, 'PNG header is missing.')
            header = struct.unpack('>IIBBBBB', body)
            width, height, depth, color, compression, filtering, interlace = header
            _dimensions(width, height)
            _check(compression == filtering == interlace == 0, 'Save this PNG without interlacing before importing it.')
            _check(color in (0, 2, 3, 4, 6) and depth in ((1, 2, 4, 8) if color in (0, 3) else (8,)),
                   'Save this PNG with 8-bit color channels before importing it.')
        elif kind == b'IHDR':
            raise ImageError('PNG contains multiple headers.')
        elif kind == b'PLTE':
            _check(not compressed and palette is None and color not in (0, 4) and 0 < length <= 768 and length % 3 == 0, 'Invalid PNG palette.')
            palette = body
            if color == 3:
                _check(length // 3 <= 2 ** depth, 'PNG palette exceeds its bit depth.')
        elif kind == b'tRNS':
            _check(not compressed and transparency is None and color in (0, 2, 3), 'Invalid PNG transparency.')
            _check((color == 0 and length == 2) or (color == 2 and length == 6) or
                   (color == 3 and palette is not None and 0 < length <= len(palette) // 3), 'Invalid PNG transparency size.')
            transparency = body
        elif kind == b'IDAT':
            _check(not data_finished and (color != 3 or palette is not None), 'Invalid PNG image-data order.')
            compressed.append(body)
        elif kind == b'IEND':
            _check(length == 0 and finish == len(data) and compressed, 'PNG end marker is invalid.')
            ended = True
            break
        else:
            _check(kind[0] & 32, 'PNG contains an unsupported critical chunk.')
            _check(kind not in (b'acTL', b'fcTL', b'fdAT'), 'Choose a still image instead of an animated PNG.')
        if compressed and kind != b'IDAT':
            data_finished = True
        position = finish
    _check(ended, 'PNG end marker is missing.')
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = (width * channels * depth + 7) // 8
    bytes_per_pixel = max(1, (channels * depth + 7) // 8)
    expected = (stride + 1) * height
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(b''.join(compressed), expected + 1)
    except zlib.error as error:
        raise ImageError('PNG compression is invalid.') from error
    _check(len(raw) == expected and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
           'PNG decompressed size does not match its dimensions.')
    result = bytearray(width * height * 4)
    previous = bytearray(stride)
    transparent = tuple(struct.unpack('>' + 'H' * (len(transparency) // 2), transparency)) if transparency is not None and color in (0, 2) else None
    for y in range(height):
        offset = y * (stride + 1)
        filter_kind = raw[offset]
        _check(filter_kind <= 4, 'Unsupported PNG scanline filter.')
        row = bytearray(raw[offset + 1:offset + stride + 1])
        if filter_kind:
            for i in range(stride):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = previous[i]
                corner = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                predictor = left if filter_kind == 1 else up if filter_kind == 2 else (left + up) // 2 if filter_kind == 3 else _paeth(left, up, corner)
                row[i] = (row[i] + predictor) & 255
        if color == 6:
            result[y * width * 4:(y + 1) * width * 4] = row
        else:
            for x in range(width):
                if color in (0, 3):
                    sample = (row[x * depth // 8] >> (8 - depth - x * depth % 8)) & ((1 << depth) - 1)
                    if color == 0:
                        gray = sample * 255 // ((1 << depth) - 1)
                        rgba = (gray, gray, gray, 0 if transparent == (sample,) else 255)
                    else:
                        _check(sample < len(palette) // 3, 'PNG refers to a missing palette color.')
                        rgba = (*palette[sample * 3:sample * 3 + 3], transparency[sample] if transparency is not None and sample < len(transparency) else 255)
                elif color == 2:
                    rgb = tuple(row[x * 3:x * 3 + 3])
                    rgba = (*rgb, 0 if transparent == rgb else 255)
                else:
                    gray, alpha = row[x * 2:x * 2 + 2]
                    rgba = (gray, gray, gray, alpha)
                result[(y * width + x) * 4:(y * width + x + 1) * 4] = bytes(rgba)
        previous = row
    return width, height, bytes(result)


def encode_png(width, height, rgba):
    _dimensions(width, height)
    _check(len(rgba) == width * height * 4, 'RGBA size does not match dimensions.')
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    raw = b''.join(b'\0' + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


def _fit_icon(width, height, pixels):
    scale = min(72 / width, 72 / height)
    target_width, target_height = max(1, round(width * scale)), max(1, round(height * scale))
    result = bytearray(72 * 72 * 4)
    left, top = (72 - target_width) // 2, (72 - target_height) // 2
    for y in range(target_height):
        sy = min(height - 1, max(0, (y + .5) * height / target_height - .5))
        y0 = int(sy)
        fy = sy - y0
        for x in range(target_width):
            sx = min(width - 1, max(0, (x + .5) * width / target_width - .5))
            x0 = int(sx)
            fx = sx - x0
            samples = [(x0, y0, (1 - fx) * (1 - fy)), (min(x0 + 1, width - 1), y0, fx * (1 - fy)),
                       (x0, min(y0 + 1, height - 1), (1 - fx) * fy), (min(x0 + 1, width - 1), min(y0 + 1, height - 1), fx * fy)]
            colors, alpha = [0., 0., 0.], 0.
            for px, py, weight in samples:
                i = (py * width + px) * 4
                weighted_alpha = pixels[i + 3] * weight
                alpha += weighted_alpha
                for channel in range(3):
                    colors[channel] += pixels[i + channel] * weighted_alpha
            rgba = [max(0, min(255, round(c / alpha))) for c in colors] + [max(0, min(255, round(alpha)))] if alpha else [0, 0, 0, 0]
            i = ((top + y) * 72 + left + x) * 4
            result[i:i + 4] = bytes(rgba)
    return encode_png(72, 72, result)


def _jpeg_header(data):
    _check(data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'), 'JPEG signature or end marker is invalid.')
    position = 2
    while position + 4 <= len(data):
        _check(data[position] == 255, 'Invalid JPEG marker.')
        while position < len(data) and data[position] == 255:
            position += 1
        _check(position < len(data), 'JPEG is truncated.')
        marker = data[position]
        position += 1
        _check(marker not in (0, 0xd8, 0xd9, 0xda), 'JPEG dimensions were not found before image data.')
        _check(position + 2 <= len(data), 'JPEG is truncated.')
        length, = struct.unpack_from('>H', data, position)
        _check(length >= 2 and position + length <= len(data), 'JPEG segment is truncated.')
        if marker in (0xc0, 0xc1, 0xc2):
            _check(length >= 8 and data[position + 2] == 8, 'Choose an 8-bit JPEG.')
            height, width = struct.unpack_from('>HH', data, position + 3)
            _dimensions(width, height)
            return
        _check(marker not in (0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf), 'Unsupported JPEG encoding.')
        position += length
    raise ImageError('JPEG dimensions are missing.')


def _svg_header(data):
    try:
        text = data.decode('utf-8')
        _check(not re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I), 'SVG document entities are not supported.')
        _check(not re.search(r'<\?(?!xml\s)', text, re.I), 'SVG processing instructions are not supported.')
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError) as error:
        raise ImageError('Choose a valid UTF-8 SVG, or save it as PNG.') from error
    _check(root.tag in ('svg', '{http://www.w3.org/2000/svg}svg'), 'SVG root element is missing.')
    elements = list(root.iter())
    _check(len(elements) <= 10000, 'SVG contains too many elements.')
    for node in elements:
        name = node.tag.rsplit('}', 1)[-1].lower()
        _check(name not in ('script', 'foreignobject', 'image', 'filter', 'animate', 'animatetransform', 'animatemotion', 'set'), 'Use a static SVG made of shapes, or save this image as PNG.')
        for key, value in node.attrib.items():
            local = key.rsplit('}', 1)[-1].lower()
            _check(not local.startswith('on'), 'SVG event handlers are not supported.')
            if local in ('href', 'base'):
                _check(local == 'href' and re.fullmatch(r'#[A-Za-z_][A-Za-z0-9_.:-]*', value), 'SVG external references are not supported.')
            _safe_svg_value(value)
        if name == 'style':
            _safe_svg_value(node.text or '')
    viewbox = root.attrib.get('viewBox')
    if viewbox:
        try:
            values = [float(x) for x in re.split(r'[\s,]+', viewbox.strip())]
        except ValueError as error:
            raise ImageError('Invalid SVG viewBox.') from error
        _check(len(values) == 4 and all(math.isfinite(x) for x in values) and 0 < values[2] <= 4096 and 0 < values[3] <= 4096, 'SVG viewBox exceeds supported limits.')


def _safe_svg_value(value):
    # Disallow CSS escapes, comments, @import/font-face and non-fragment URLs.
    # Native fragment fills such as url(#gradient) remain usable.
    _check(not any(token in value for token in ('\\', '@', '/*', '*/')), 'Complex SVG styling is not supported; save it as PNG.')
    cleaned = re.sub(r'url\(\s*[\'\"]?#[A-Za-z_][A-Za-z0-9_.:-]*[\'\"]?\s*\)', '', value, flags=re.I)
    _check(not re.search(r'url\s*\(|(?:https?|file|data|ftp):|//', cleaned, re.I), 'SVG external resources are not supported.')


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _convert(command, data):
    _check(os.geteuid() != 0, 'Image conversion must run as the desktop user, before administrator authentication.')
    executable = shutil.which(command[0], path='/usr/bin:/bin')
    _check(executable is not None, f'{command[0]} is not installed. Save the image as PNG first.')
    try:
        result = subprocess.run([executable, *command[1:]], input=data, capture_output=True, timeout=15, preexec_fn=_limits,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'HOME': os.environ.get('HOME', '/nonexistent')})
    except (OSError, subprocess.SubprocessError) as error:
        raise ImageError('Image conversion failed or exceeded its time limit. Save the image as PNG.') from error
    _check(result.returncode == 0 and len(result.stdout) <= MAX_BYTES, 'Image conversion failed. Save the image as PNG.')
    return result.stdout


def normalize_image(path):
    """Read a selected local image once; return metadata-free 72×72 PNG bytes."""
    path = Path(path)
    _check(path.suffix.lower() in SUPPORTED_SUFFIXES, 'Choose a PNG, JPEG or SVG image.')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as source:
            _check(stat.S_ISREG(os.fstat(source.fileno()).st_mode), 'Choose a regular local image file.')
            data = source.read(MAX_BYTES + 1)
    except OSError as error:
        raise ImageError('The selected image could not be read.') from error
    _check(len(data) <= MAX_BYTES, 'Choose an image smaller than 8 MiB.')
    if path.suffix.lower() in ('.jpg', '.jpeg'):
        _jpeg_header(data)
        data = _convert(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1', '-protocol_whitelist', 'pipe',
                         '-f', 'image2pipe', '-c:v', 'mjpeg', '-i', 'pipe:0', '-frames:v', '1', '-vf',
                         'format=rgba,scale=72:72:force_original_aspect_ratio=decrease:flags=lanczos', '-threads', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'], data)
    elif path.suffix.lower() == '.svg':
        _svg_header(data)
        data = _convert(['rsvg-convert', '--format=png', '--width=72', '--height=72', '--keep-aspect-ratio'], data)
    return _fit_icon(*decode_png(data))


def normalize_icon(source, destination):
    """Convenience wrapper for non-privileged staging tools."""
    data = normalize_image(source)
    Path(destination).write_bytes(data)
    return {'width': 72, 'height': 72, 'sha256': hashlib.sha256(data).hexdigest()}
