"""lcd.py unit tests: T15 device identity + profile resolution + encode."""

import io
import unittest

from thermallcd import lcd


class DeviceIdTest(unittest.TestCase):
    def test_serial_is_the_identity(self):
        self.assertEqual(lcd._device_id("2025ACFC9E03", 3, 7), "2025ACFC9E03")

    def test_missing_serial_falls_back_to_bus_path(self):
        self.assertEqual(lcd._device_id(None, 3, 7), "bus3.addr7")
        self.assertEqual(lcd._device_id("", 1, 2), "bus1.addr2")

    def test_unsafe_characters_sanitized(self):
        # the id keys layouts/<id>.json — it must stay a filename component
        self.assertEqual(lcd._device_id("a/b\\c d", 1, 2), "a_b_c_d")
        self.assertEqual(lcd._device_id("../etc", 1, 2), "_etc")  # no traversal

    def test_reserved_default_key_never_collides(self):
        # a panel claiming the serial "DEFAULT" must not shadow the shared
        # fallback layout's own key
        self.assertEqual(lcd._device_id("DEFAULT", 1, 2), "DEFAULT_panel")
        self.assertEqual(lcd._device_id("default", 1, 2), "default_panel")


class ProfileTest(unittest.TestCase):
    def test_known_pm_resolves(self):
        # pmSub 3/5: Trofeo Vision 9.16"
        self.assertEqual(lcd.resolve_profile(65, 3), (1920, 462))
        self.assertEqual(lcd.resolve_profile(65, 5), (1920, 462))

    def test_unknown_pm_falls_back_to_family_default(self):
        self.assertEqual(lcd.resolve_profile(99, 1), (1920, 462))


class EncodeSizeTest(unittest.TestCase):
    def test_encode_resizes_to_profile(self):
        from PIL import Image

        image = Image.new("RGB", (100, 50), (10, 200, 30))
        out = lcd.encode_jpeg(image, quality=85, size=(640, 200))
        decoded = Image.open(io.BytesIO(out))
        self.assertEqual(decoded.size, (640, 200))
        # default size keeps the 1920x462 contract
        out = lcd.encode_jpeg(image)
        self.assertEqual(Image.open(io.BytesIO(out)).size, (1920, 462))


if __name__ == "__main__":
    unittest.main()
