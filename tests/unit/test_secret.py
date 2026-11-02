"""interpose.secret: masking everywhere a value can be shown, and the secrets-directory reader."""

from __future__ import annotations

import copy
import logging
import os
import traceback
from dataclasses import dataclass
from pathlib import Path

import pytest

from interpose.secret import MASK, Secret, read_secret_dir
from interpose.settings import ConfigError

VALUE = "hunter2-very-secret"


class TestMasking:
    def test_str_and_repr(self) -> None:
        s = Secret(VALUE)
        assert str(s) == MASK
        assert repr(s) == f"Secret({MASK!r})"

    def test_f_strings_and_format(self) -> None:
        s = Secret(VALUE)
        assert f"{s}" == MASK
        assert f"{s!s} {s!r} {s!a}" == f"{MASK} Secret('{MASK}') Secret('{MASK}')"
        assert f"[{s:>10}]" == f"[  {MASK}]"
        assert f"{s}" == MASK
        assert "%s %r" % (s, s) == f"{MASK} Secret('{MASK}')"  # noqa: UP031  # %-format is a path too

    def test_containers(self) -> None:
        s = Secret(VALUE)
        assert VALUE not in str([s]) + str({"k": s}) + str((s,)) + repr({s})

    def test_dataclass_repr(self) -> None:
        @dataclass(frozen=True)
        class Config:
            proxy_auth: Secret

        assert VALUE not in repr(Config(Secret(VALUE)))

    def test_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        log = logging.getLogger("interpose.test")
        s = Secret(VALUE)
        log.info("auth=%s", s)
        log.info("auth=%r", s)
        log.info(s)
        log.info("auth", extra={"auth": s})
        log.info("%(auth)s", {"auth": s})
        assert VALUE not in caplog.text
        assert MASK in caplog.text

    def test_exceptions_and_tracebacks(self) -> None:
        s = Secret(VALUE)
        try:
            raise ValueError(s)
        except ValueError as e:
            assert VALUE not in str(e)
            assert VALUE not in repr(e)
            assert VALUE not in "".join(traceback.format_exception(e))
        empty: dict[Secret, str] = {}
        with pytest.raises(KeyError) as info:
            empty[s]  # the lookup is what raises
        assert VALUE not in str(info.value)

    def test_no_attribute_dict_to_dump(self) -> None:
        s = Secret(VALUE)
        assert not hasattr(s, "__dict__")
        with pytest.raises(TypeError):
            vars(s)

    def test_copies_stay_masked(self) -> None:
        s = Secret(VALUE)
        for c in (copy.copy(s), copy.deepcopy(s)):
            assert str(c) == MASK
            assert c.reveal() == VALUE


class TestValue:
    def test_reveal(self) -> None:
        assert Secret(VALUE).reveal() == VALUE

    def test_equality_compares_values(self) -> None:
        assert Secret("a") == Secret("a")
        assert Secret("a") != Secret("b")
        assert Secret("é") == Secret("é")  # non-ASCII still compares (compare_digest wants bytes)

    def test_never_equal_to_its_plain_value(self) -> None:
        assert Secret("a") != "a"
        assert Secret("a") != "a"

    def test_hashable(self) -> None:
        assert len({Secret("a"), Secret("a"), Secret("b")}) == 2

    def test_truthiness(self) -> None:
        assert Secret("x")
        assert not Secret("")

    @pytest.mark.parametrize("value", [b"bytes", 1, None, Secret("nested")])
    def test_only_str(self, value: object) -> None:
        with pytest.raises(TypeError, match="Secret takes a str"):
            Secret(value)  # type: ignore[arg-type]  # asserting the runtime check

    def test_not_a_str(self) -> None:
        assert not isinstance(Secret(VALUE), str)


class TestReadSecretDir:
    def test_reads_regular_files(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_text("abc\n", encoding="utf-8")
        (tmp_path / "crlf").write_text("def\r\n", encoding="utf-8", newline="")
        (tmp_path / "bare").write_text("ghi", encoding="utf-8")
        (tmp_path / "twice").write_text("jkl\n\n", encoding="utf-8")
        (tmp_path / "padded").write_text("  mno  \n", encoding="utf-8")
        secrets = read_secret_dir(tmp_path)
        assert {k: v.reveal() for k, v in secrets.items()} == {
            "bare": "ghi",
            "crlf": "def",
            "padded": "  mno  ",
            "token": "abc",
            "twice": "jkl\n",
        }
        assert all(isinstance(v, Secret) for v in secrets.values())
        assert list(secrets) == sorted(secrets)

    def test_kubernetes_layout(self, tmp_path: Path) -> None:
        # A secret volume: files are symlinks into a timestamped dir, reached through ..data.
        stamp = tmp_path / "..2026_11_01_00_00_00.000000000"
        stamp.mkdir()
        (stamp / "token").write_text("abc\n", encoding="utf-8")
        (tmp_path / "..data").symlink_to(stamp.name)
        (tmp_path / "token").symlink_to("..data/token")
        (tmp_path / ".hidden").write_text("x", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        secrets = read_secret_dir(tmp_path)
        assert list(secrets) == ["token"]
        assert secrets["token"].reveal() == "abc"

    def test_missing_dir_means_none(self, tmp_path: Path) -> None:
        assert dict(read_secret_dir(tmp_path / "missing")) == {}

    def test_read_only_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            read_secret_dir(tmp_path)["x"] = Secret("y")  # type: ignore[index]  # asserting read-only

    def test_not_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "file").write_text("x", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"^cannot read secrets directory .*file: Not a dir"):
            read_secret_dir(tmp_path / "file")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
    def test_unreadable_file(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_text(VALUE, encoding="utf-8")
        (tmp_path / "token").chmod(0)
        try:
            with pytest.raises(ConfigError, match=r"^cannot read secret 'token' in .*: Perm") as e:
                read_secret_dir(tmp_path)
            assert e.value.__cause__ is None
        finally:
            (tmp_path / "token").chmod(0o600)

    def test_not_utf8(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_bytes(b"\xff\xfe")
        with pytest.raises(ConfigError, match=r"^cannot read secret 'token' in .*: not UTF-8$"):
            read_secret_dir(tmp_path)

    def test_errors_never_contain_values(self, tmp_path: Path) -> None:
        (tmp_path / "good").write_text(VALUE, encoding="utf-8")
        (tmp_path / "zbad").write_bytes(VALUE.encode() + b"\xff")
        with pytest.raises(ConfigError) as info:
            read_secret_dir(tmp_path)
        assert VALUE not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
