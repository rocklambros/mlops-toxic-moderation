import hashlib

from model.data.provenance import sha256_file


def test_sha256_file_matches_hashlib(tmp_path):
    target = tmp_path / "blob.bin"
    payload = b"jigsaw" * 100_000
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_is_chunk_size_independent(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(bytes(range(256)) * 4096)
    assert sha256_file(target, chunk_bytes=7) == sha256_file(target, chunk_bytes=1 << 20)


def test_sha256_file_handles_an_empty_file(tmp_path):
    target = tmp_path / "empty.bin"
    target.write_bytes(b"")
    assert sha256_file(target) == hashlib.sha256(b"").hexdigest()
