import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ice_circuit import image_utils as images


def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)


def fixture(width, height, depth, color, raw, extras=b'', interlace=0):
    return images.SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace)) + extras + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


class ImageTests(unittest.TestCase):
    def test_all_five_filters_reconstruct_known_rgba_rows(self):
        rows = [bytes([10 + i * 10, 20 + i * 10, 30 + i * 10, 255, 40 + i * 10, 50 + i * 10, 60 + i * 10, 128]) for i in range(5)]
        filtered = bytes([0]) + rows[0] + bytes([1, 20, 30, 40, 255, 30, 30, 30, 129]) + bytes([2, 10, 10, 10, 0, 10, 10, 10, 0]) + bytes([3, 25, 30, 35, 128, 20, 20, 20, 193]) + bytes([4, 10, 10, 10, 0, 10, 10, 10, 0])
        self.assertEqual(images.decode_png(fixture(2, 5, 8, 6, filtered)), (2, 5, b''.join(rows)))

    def test_palette_and_packed_grayscale_transparency(self):
        extras = chunk(b'PLTE', bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])) + chunk(b'tRNS', bytes([0, 128]))
        self.assertEqual(images.decode_png(fixture(3, 1, 2, 3, b'\0\x18', extras))[2], bytes([255, 0, 0, 0, 0, 255, 0, 128, 0, 0, 255, 255]))
        data = fixture(2, 1, 1, 0, b'\0\x40', chunk(b'tRNS', b'\0\0'))
        self.assertEqual(images.decode_png(data)[2], bytes([0, 0, 0, 0, 255, 255, 255, 255]))

    def test_rgb_color_key_and_grayscale_alpha(self):
        data = fixture(2, 1, 8, 2, bytes([0, 10, 20, 30, 20, 30, 40]), chunk(b'tRNS', struct.pack('>HHH', 10, 20, 30)))
        self.assertEqual(images.decode_png(data)[2], bytes([10, 20, 30, 0, 20, 30, 40, 255]))
        self.assertEqual(images.decode_png(fixture(2, 1, 8, 4, bytes([0, 128, 50, 200, 255])))[2], bytes([128, 128, 128, 50, 200, 200, 200, 255]))

    def test_malformed_and_unsupported_png_are_rejected(self):
        good = images.encode_png(1, 1, b'\x01\x02\x03\xff')
        bad_crc = bytearray(good)
        bad_crc[-1] ^= 1
        examples = [bytes(bad_crc), good[:-1], good + b'junk', fixture(1, 1, 8, 6, b'\0\0\0\0\0', interlace=1),
                    fixture(1, 1, 16, 6, b'\0' * 9), fixture(1, 1, 8, 6, b'\5\0\0\0\0'),
                    fixture(1, 1, 8, 6, b'\0\0\0\0\0', chunk(b'acTL', b'\0' * 8)),
                    fixture(1, 1, 8, 6, b'\0\0\0\0\0', chunk(b'FAKE', b''))]
        for data in examples:
            with self.subTest(data=data[-20:]), self.assertRaises(images.ImageError):
                images.decode_png(data)

    def test_decompression_bomb_and_oversized_dimensions_are_rejected(self):
        with self.assertRaisesRegex(images.ImageError, 'decompressed size'):
            images.decode_png(fixture(1, 1, 8, 6, b'\0' * 1000000))
        with self.assertRaisesRegex(images.ImageError, 'megapixels'):
            images.decode_png(fixture(4096, 4096, 8, 6, b''))

    def test_normalization_preserves_aspect_and_transparent_margins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wide.png'
            original = images.encode_png(4, 2, bytes([20, 40, 60, 255]) * 8)
            path.write_bytes(original)
            data = images.normalize_image(path)
            width, height, pixels = images.decode_png(data)
            self.assertEqual((width, height), (72, 72))
            self.assertEqual(pixels[:72 * 18 * 4], b'\0' * (72 * 18 * 4))
            self.assertEqual(pixels[72 * 18 * 4:72 * 54 * 4], bytes([20, 40, 60, 255]) * (72 * 36))
            self.assertEqual(path.read_bytes(), original)

    def test_transparent_colored_pixel_does_not_make_a_resize_halo(self):
        normalized = images._fit_icon(2, 1, bytes([255, 0, 0, 255, 0, 255, 0, 0]))
        _, _, pixels = images.decode_png(normalized)
        for i in range(0, len(pixels), 4):
            if pixels[i + 3]:
                self.assertEqual(pixels[i:i + 3], b'\xff\0\0')

    def test_non_regular_input_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pipe.png'
            os.mkfifo(path)
            with self.assertRaisesRegex(images.ImageError, 'regular local'):
                images.normalize_image(path)

    def test_svg_external_entities_urls_styles_and_animation_are_rejected(self):
        bad = [b'<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]><svg>&e;</svg>',
               b'<?xml-stylesheet href="../private.css"?><svg/>',
               b'<svg><use href="../image.svg#x"/></svg>',
               b'<svg><image href="data:image/png;base64,AAAA"/></svg>',
               b'<svg><path fill="url(https://example.invalid/a)"/></svg>',
               b'<svg><style>@import "../private.css";</style></svg>',
               b'<svg><path style="fill:u\\72l(secret)"/></svg>',
               b'<svg><foreignObject/></svg>', b'<svg><animate/></svg>',
               b'<svg viewBox="0 0 inf 1"/>']
        for data in bad:
            with self.subTest(data=data), self.assertRaises(images.ImageError):
                images._svg_header(data)
        images._svg_header(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 72 72"><defs><linearGradient id="a"/></defs><path fill="url(#a)" d="M0 0L72 72"/></svg>')

    def test_jpeg_header_checks_size_before_a_converter_runs(self):
        header = b'\xff\xd8\xff\xc0' + struct.pack('>HBHHB', 8, 8, 1, 2, 3) + b'\xff\xd9'
        images._jpeg_header(header)
        with self.assertRaises(images.ImageError):
            images._jpeg_header(header.replace(b'\x00\x01\x00\x02', b'\xff\xff\xff\xff'))

    def test_external_conversion_is_never_run_as_root_and_is_bounded(self):
        with patch.object(images.os, 'geteuid', return_value=0), patch.object(images.subprocess, 'run') as run:
            with self.assertRaisesRegex(images.ImageError, 'desktop user'):
                images._convert(['rsvg-convert'], b'<svg/>')
            run.assert_not_called()
        with patch.object(images.os, 'geteuid', return_value=1000), patch.object(images.shutil, 'which', return_value='/usr/bin/rsvg-convert'), patch.object(images.subprocess, 'run', side_effect=subprocess.TimeoutExpired('rsvg-convert', 15)) as run:
            with self.assertRaisesRegex(images.ImageError, 'time limit'):
                images._convert(['rsvg-convert'], b'<svg/>')
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
            self.assertEqual(run.call_args.kwargs['input'], b'<svg/>')
            self.assertEqual(run.call_args.kwargs['preexec_fn'], images._limits)

    @unittest.skipUnless(shutil.which('rsvg-convert') and os.geteuid() != 0, 'Optional non-root librsvg interoperability test')
    def test_installed_svg_converter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'icon.svg'
            path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="#204060"/></svg>')
            self.assertEqual(images.decode_png(images.normalize_image(path))[:2], (72, 72))

    @unittest.skipUnless(shutil.which('ffmpeg') and os.geteuid() != 0, 'Optional non-root ffmpeg interoperability test')
    def test_installed_jpeg_converter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'icon.jpg'
            source = images.encode_png(8, 4, bytes([20, 40, 60, 255]) * 32)
            jpeg = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-i', 'pipe:0', '-frames:v', '1', '-threads', '1', '-c:v', 'mjpeg', '-f', 'image2pipe', 'pipe:1'], input=source, capture_output=True, check=True, timeout=15).stdout
            path.write_bytes(jpeg)
            self.assertEqual(images.decode_png(images.normalize_image(path))[:2], (72, 72))


if __name__ == '__main__':
    unittest.main()
