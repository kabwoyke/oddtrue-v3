from pathlib import Path

import pytest
from scripts.scan_secrets import main, scan_text


def _fake_key() -> str:
    # Built at runtime so this file itself never contains a secret-shaped literal.
    return "a1B2" * 8


def test_blocks_fake_api_secret(tmp_path: Path) -> None:
    f = tmp_path / "settings.py"
    f.write_text(f'API_SECRET = "{_fake_key()}"\n', encoding="utf-8")
    assert main([str(f)]) == 1


def test_blocks_private_key_block() -> None:
    assert scan_text("-----BEGIN " + "RSA PRIVATE KEY-----") == [(1, "private key block")]


def test_blocks_bitget_style_key() -> None:
    assert scan_text("x = bg_" + "0123456789abcdef" * 2) != []


def test_allows_clean_file_and_empty_env_example(tmp_path: Path) -> None:
    f = tmp_path / ".env.example"
    f.write_text("DATABASE_URL=\nODDTRUE_MODE=\n", encoding="utf-8")
    assert main([str(f)]) == 0


def test_allow_marker_suppresses() -> None:
    assert scan_text(f'secret = "{_fake_key()}"  # scan-secrets: allow') == []


def test_output_never_contains_the_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "a.py"
    f.write_text(f'token = "{_fake_key()}"\n', encoding="utf-8")
    main([str(f)])
    assert _fake_key() not in capsys.readouterr().err
