import base64
import io
import unittest
import wave
from speech_service import decode_audio, encode_wav


class AudioValidationTests(unittest.TestCase):
    def test_pcm_round_trip(self):
        encoded = encode_wav(b'\0\0' * 16000, 16000, 1)
        with wave.open(io.BytesIO(decode_audio(encoded)), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 16000)

    def test_rejects_invalid_or_oversized_audio(self):
        for value in (None, '%bad%', 'a' * 1280061, base64.b64encode(b'not wave').decode()):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                decode_audio(value)

    def test_rejects_format_duration_and_truncation(self):
        for encoded in (encode_wav(b'\0\0' * 100,16000,1), encode_wav(b'\0\0'*16000,22050,1), encode_wav(b'\0\0'*16000,16000,2)):
            with self.assertRaises(ValueError): decode_audio(encoded)
        valid = base64.b64decode(encode_wav(b'\0\0'*16000,16000,1))
        with self.assertRaises(ValueError): decode_audio(base64.b64encode(valid[:-8]).decode())
