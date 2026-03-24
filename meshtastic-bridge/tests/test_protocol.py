"""Tests for the message chunking protocol."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    chunk_message,
    reassemble_message,
    unpack_header,
    pack_header,
    EFFECTIVE_PAYLOAD,
    MAX_LORA_PAYLOAD,
    HEADER_SIZE,
    FLAG_COMPRESSED,
    FLAG_FINAL,
    FLAG_ERROR,
)


class TestProtocol(unittest.TestCase):
    def test_short_message_single_chunk(self):
        """A short message should fit in a single chunk."""
        chunks = chunk_message("Hello", compression_enabled=False)
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0]), MAX_LORA_PAYLOAD)

        header = unpack_header(chunks[0])
        self.assertEqual(header["seq"], 0)
        self.assertEqual(header["total"], 1)
        self.assertTrue(header["final"])

    def test_long_message_multiple_chunks(self):
        """A message longer than EFFECTIVE_PAYLOAD should be split."""
        text = "A" * 1000
        chunks = chunk_message(text, compression_enabled=False)
        self.assertGreater(len(chunks), 1)

        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_LORA_PAYLOAD)

    def test_reassembly(self):
        """Chunked messages should reassemble correctly."""
        original = "The quick brown fox jumps over the lazy dog. " * 20
        chunks = chunk_message(original, compression_enabled=False)
        reassembled = reassemble_message(chunks)
        self.assertEqual(reassembled, original)

    def test_compression_reduces_chunks(self):
        """Compression should reduce chunk count for repetitive text."""
        text = "AAAA" * 500  # Highly compressible
        uncompressed_chunks = chunk_message(text, compression_enabled=False)
        compressed_chunks = chunk_message(text, compression_enabled=True)

        self.assertLessEqual(len(compressed_chunks), len(uncompressed_chunks))

        # Verify compressed flag
        if len(compressed_chunks) < len(uncompressed_chunks):
            header = unpack_header(compressed_chunks[0])
            self.assertTrue(header["compressed"])

    def test_compressed_reassembly(self):
        """Compressed messages should reassemble correctly."""
        original = "Hello World! " * 100
        chunks = chunk_message(original, compression_enabled=True)
        reassembled = reassemble_message(chunks)
        self.assertEqual(reassembled, original)

    def test_error_flag(self):
        """Error messages should have the ERROR flag set."""
        chunks = chunk_message("Error occurred", is_error=True)
        header = unpack_header(chunks[0])
        self.assertTrue(header["error"])

    def test_header_packing(self):
        """Header should pack and unpack correctly."""
        header_bytes = pack_header(12345, 3, 10, FLAG_COMPRESSED | FLAG_FINAL)
        parsed = unpack_header(header_bytes)

        self.assertEqual(parsed["msg_id"], 12345)
        self.assertEqual(parsed["seq"], 3)
        self.assertEqual(parsed["total"], 10)
        self.assertTrue(parsed["compressed"])
        self.assertTrue(parsed["final"])
        self.assertFalse(parsed["error"])

    def test_header_size(self):
        """Header should be exactly 8 bytes."""
        header = pack_header(0, 0, 1, 0)
        self.assertEqual(len(header), HEADER_SIZE)

    def test_chunk_size_limit(self):
        """No chunk should exceed MAX_LORA_PAYLOAD."""
        text = "X" * 5000
        chunks = chunk_message(text, compression_enabled=False)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_LORA_PAYLOAD)

    def test_empty_message(self):
        """Empty string should produce one chunk."""
        chunks = chunk_message("")
        self.assertEqual(len(chunks), 1)

    def test_unicode_message(self):
        """Unicode characters should be handled correctly."""
        original = "Hola! Esto es una prueba. Temperatura: 25°C"
        chunks = chunk_message(original, compression_enabled=False)
        reassembled = reassemble_message(chunks)
        self.assertEqual(reassembled, original)

    def test_exactly_effective_payload(self):
        """A message exactly EFFECTIVE_PAYLOAD bytes should be one chunk."""
        text = "A" * EFFECTIVE_PAYLOAD
        chunks = chunk_message(text, compression_enabled=False)
        self.assertEqual(len(chunks), 1)

    def test_sequence_numbers(self):
        """Chunks should have sequential sequence numbers."""
        text = "B" * 1000
        chunks = chunk_message(text, compression_enabled=False)
        for i, chunk in enumerate(chunks):
            header = unpack_header(chunk)
            self.assertEqual(header["seq"], i)

    def test_final_flag_only_on_last(self):
        """Only the last chunk should have the FINAL flag."""
        text = "C" * 1000
        chunks = chunk_message(text, compression_enabled=False)
        for i, chunk in enumerate(chunks):
            header = unpack_header(chunk)
            if i == len(chunks) - 1:
                self.assertTrue(header["final"])
            else:
                self.assertFalse(header["final"])


if __name__ == "__main__":
    unittest.main()
