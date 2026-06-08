"""Tests for the UV watcher's pure helper (notify_watcher.topics.uv)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import uv


class DescribeTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(uv._describe(3), "moderate")
        self.assertEqual(uv._describe(6), "high")
        self.assertEqual(uv._describe(8.5), "very high")
        self.assertEqual(uv._describe(11), "extreme")


if __name__ == "__main__":
    unittest.main()
