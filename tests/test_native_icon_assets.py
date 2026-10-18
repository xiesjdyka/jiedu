"""The fixed v1 ICO/tray and explicit window pixels share the approved artwork."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('native_icon_asset_fixture',ROOT/'desktop/icon_assets.py')
ASSETS=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(ASSETS)

class NativeIconTests(unittest.TestCase):
    def test_approved_artwork_and_all_sizes_are_preserved(self):
        report=ASSETS.verify_assets(ROOT)
        self.assertEqual([16,20,24,32,40,48,64,96,128,256],report['ico_sizes'])
        self.assertTrue(report['window_exact_ico_frame'])
        self.assertFalse(report['artwork_replaced_or_resized'])

    def test_explicit_window_pixels_are_exact_32px_ico_frame(self):
        frames=ASSETS.ico_frames((ROOT/'desktop/icons/icon.ico').read_bytes())
        pixels=ASSETS.frame_rgba(frames[(32,32)],(32,32))
        self.assertEqual(4096,len(pixels));self.assertEqual(pixels,(ROOT/'desktop/icons/window.rgba').read_bytes())
        self.assertEqual('1815e5c7392c2b3ca03b35fd66fd5bd737948e41b15d00966f8f9839c35d94d1',hashlib.sha256(pixels).hexdigest())

    def test_corrupt_ico_offsets_and_png_crc_cannot_silently_change_artwork(self):
        ico=bytearray((ROOT/'desktop/icons/icon.ico').read_bytes());ico[18:22]=b'\xff'*4
        with self.assertRaises(ValueError):ASSETS.ico_frames(ico)
        png=bytearray((ROOT/'static/brand/jiedu-32.png').read_bytes());png[-5]^=1
        with self.assertRaises(ValueError):ASSETS.png_rgba(png)

if __name__=='__main__':unittest.main()
