import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.video._shared import upload_image_fal


class FalUploadHelperTests(unittest.TestCase):
    def test_uses_official_client_and_returns_trimmed_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "reference.png"
            image.write_bytes(b"not-a-real-png-but-upload-is-mocked")
            with patch(
                "fal_client.upload_file",
                return_value="  https://v3b.fal.media/files/reference.png  ",
            ) as upload:
                result = upload_image_fal(str(image))

        upload.assert_called_once_with(image)
        self.assertEqual(result, "https://v3b.fal.media/files/reference.png")

    def test_rejects_empty_client_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "reference.jpeg"
            image.write_bytes(b"reference")
            with patch("fal_client.upload_file", return_value=""):
                with self.assertRaisesRegex(RuntimeError, "returned no URL"):
                    upload_image_fal(str(image))

    def test_missing_file_fails_before_client_call(self) -> None:
        with patch("fal_client.upload_file") as upload:
            with self.assertRaises(FileNotFoundError):
                upload_image_fal("/definitely/missing/reference.png")
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
