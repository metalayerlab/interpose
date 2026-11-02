"""interpose.settings: declaration, lookup order, coercion, provenance, checks and secrets."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from interpose.secret import MASK, Secret
from interpose.settings import (
    ConfigError,
    Resolved,
    Setting,
    Settings,
    Value,
    non_negative,
    one_of,
    port,
    positive,
)

DEFAULT = Path("/nonexistent/interpose-test/config.yaml")


def make(*settings: Setting, prefix: str = "APP_") -> Settings:
    return Settings(prefix, settings, default_path=DEFAULT)


def resolve_yaml(
    tmp_path: Path, yaml_text: str, *settings: Setting, environ: Mapping[str, str] | None = None
) -> Resolved:
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return make(*settings).resolve(path, environ or {})


# --- declaration ---------------------------------------------------------------------------------


class TestDeclaration:
    def test_env_is_prefix_and_key(self) -> None:
        settings = make(Setting("listen.port", 8080, "Port."), prefix="SLUICE_")
        assert [s.env for s in settings] == ["SLUICE_LISTEN_PORT"]

    def test_unbound_setting_has_no_prefix(self) -> None:
        assert Setting("listen.port", 8080, "Port.").env == "LISTEN_PORT"

    def test_binding_copies_and_leaves_the_original(self) -> None:
        original = Setting("a", "", "A.")
        settings = make(original)
        assert settings[0].prefix == "APP_"
        assert original.prefix == ""

    def test_sequence_protocol(self) -> None:
        a, b = Setting("a", "", "A."), Setting("b", 1, "B.")
        settings = make(a, b)
        assert len(settings) == 2
        assert [s.key for s in settings] == ["a", "b"]
        assert settings[-1].key == "b"
        assert [s.key for s in settings[0:1]] == ["a"]
        assert "APP_" in repr(settings)

    def test_public_attributes_for_consumers(self) -> None:
        # gen-wiki.py, the config CLI and mitm-proxy's server.py read exactly these.
        s = make(Setting("proxy.auth", "", "Auth.", secret=True))[0]
        assert (s.key, s.env, s.default, s.help, s.secret, s.is_list) == (
            "proxy.auth",
            "APP_PROXY_AUTH",
            "",
            "Auth.",
            True,
            False,
        )

    def test_config_env_and_default_path(self) -> None:
        settings = Settings("MITM_PROXY_", (), default_path=Path("/etc/mitm-proxy/config.yaml"))
        assert settings.config_env == "MITM_PROXY_CONFIG"
        assert settings.default_path == Path("/etc/mitm-proxy/config.yaml")
        assert settings.prefix == "MITM_PROXY_"

    def test_accepts_any_iterable(self) -> None:
        settings = Settings("APP_", (Setting(k, "", "") for k in "ab"), default_path=DEFAULT)
        assert len(settings) == 2

    def test_rejects_duplicate_keys(self) -> None:
        with pytest.raises(ValueError, match="'a': declared twice"):
            make(Setting("a", "", ""), Setting("a", 1, ""))

    @pytest.mark.parametrize("key", ["", "Listen.port", "listen..port", ".a", "a.", "a-b", "a b"])
    def test_rejects_bad_keys(self, key: str) -> None:
        with pytest.raises(ValueError, match="a key is"):
            make(Setting(key, "", ""))

    @pytest.mark.parametrize("key", ["a", "log_level", "listen.port", "a1.b_2.c3"])
    def test_accepts_good_keys(self, key: str) -> None:
        assert make(Setting(key, "", ""))[0].key == key

    def test_rejects_list_with_non_empty_default(self) -> None:
        with pytest.raises(ValueError, match="list setting's default"):
            make(Setting("hosts", "example.org", "", is_list=True))

    @pytest.mark.parametrize(
        "setting",
        [
            Setting("n", 0, "", secret=True),
            Setting("b", False, "", secret=True),
            Setting("l", "", "", secret=True, is_list=True),
        ],
    )
    def test_rejects_non_str_secret(self, setting: Setting) -> None:
        with pytest.raises(ValueError, match="secret setting must be a str"):
            make(setting)

    @pytest.mark.parametrize(
        ("keys", "name"),
        [
            (("a.b", "a_b"), "APP_A_B"),
            (("x", "x_file"), "APP_X_FILE"),
            (("x_file", "x"), "APP_X_FILE"),
            (("config",), "APP_CONFIG"),
        ],
    )
    def test_rejects_shared_variable_names(self, keys: tuple[str, ...], name: str) -> None:
        with pytest.raises(ValueError, match=rf": {name} is also "):
            make(*(Setting(k, "", "") for k in keys))

    @pytest.mark.parametrize("key", ["tls.on", "off", "a.yes", "no", "true", "null", "a.1"])
    def test_rejects_segments_yaml_does_not_read_as_strings(self, key: str) -> None:
        with pytest.raises(ValueError, match="YAML doesn't read"):
            make(Setting(key, "", ""))

    def test_setting_is_frozen(self) -> None:
        s = Setting("a", "", "")
        with pytest.raises(AttributeError):
            s.key = "b"  # type: ignore[misc]  # asserting that frozen=True refuses this


# --- lookup order and provenance -----------------------------------------------------------------

PORT = Setting("listen.port", 8080, "Port.")


class TestPrecedence:
    def env(self, tmp_path: Path, *, file: bool, env: bool) -> dict[str, str]:
        environ: dict[str, str] = {}
        if file:
            (tmp_path / "port").write_text("3\n", encoding="utf-8")
            environ["APP_LISTEN_PORT_FILE"] = str(tmp_path / "port")
        if env:
            environ["APP_LISTEN_PORT"] = "2"
        return environ

    @pytest.mark.parametrize(
        ("file", "env", "yaml", "value", "source"),
        [
            (True, True, True, 3, "APP_LISTEN_PORT_FILE"),
            (True, False, True, 3, "APP_LISTEN_PORT_FILE"),
            (True, True, False, 3, "APP_LISTEN_PORT_FILE"),
            (False, True, True, 2, "APP_LISTEN_PORT"),
            (False, True, False, 2, "APP_LISTEN_PORT"),
            (False, False, True, 1, "file"),
            (False, False, False, 8080, "default"),
        ],
    )
    def test_first_place_wins(
        self, tmp_path: Path, file: bool, env: bool, yaml: bool, value: int, source: str
    ) -> None:
        r = resolve_yaml(
            tmp_path,
            "listen:\n  port: 1\n" if yaml else "",
            PORT,
            environ=self.env(tmp_path, file=file, env=env) or {"UNRELATED": "x"},
        )
        assert r.values["listen.port"] == value
        assert r.sources["listen.port"] == source

    def test_each_setting_has_its_own_source(self, tmp_path: Path) -> None:
        r = resolve_yaml(
            tmp_path,
            "a: from-yaml\n",
            Setting("a", "", ""),
            Setting("b", "", ""),
            Setting("c", "dflt", ""),
            environ={"APP_B": "from-env"},
        )
        assert dict(r.values) == {"a": "from-yaml", "b": "from-env", "c": "dflt"}
        assert dict(r.sources) == {"a": "file", "b": "APP_B", "c": "default"}
        assert list(r.values) == ["a", "b", "c"]  # declaration order

    def test_empty_env_value_counts_as_set(self) -> None:
        r = make(Setting("a", "dflt", "")).resolve(DEFAULT, {"APP_A": ""})
        assert (r.values["a"], r.sources["a"]) == ("", "APP_A")

    def test_yaml_null_means_unset(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "a: null\nb: ~\n", Setting("a", "x", ""), Setting("b", 1, ""))
        assert dict(r.values) == {"a": "x", "b": 1}
        assert set(r.sources.values()) == {"default"}

    def test_nested_keys(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "a:\n  b:\n    c: deep\n", Setting("a.b.c", "", ""))
        assert r.values["a.b.c"] == "deep"

    def test_scalar_where_a_mapping_is_expected_means_unset(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "listen: 5\n", PORT)
        assert (r.values["listen.port"], r.sources["listen.port"]) == (8080, "default")

    def test_mapping_for_a_leaf_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"^listen: expected str"):
            resolve_yaml(tmp_path, "listen:\n  port: 1\n", Setting("listen", "", ""))

    def test_unknown_yaml_keys_are_ignored(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "other: 1\nlisten:\n  host: x\n", PORT)
        assert r.sources["listen.port"] == "default"

    def test_resolved_is_read_only(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "", PORT)
        with pytest.raises(TypeError):
            r.values["listen.port"] = 1  # type: ignore[index]  # asserting it's read-only
        with pytest.raises(TypeError):
            r.sources["listen.port"] = "x"  # type: ignore[index]  # asserting it's read-only


class TestConfigPath:
    def test_explicit_path_wins_over_config_env(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
        a.write_text("x: from-a\n", encoding="utf-8")
        b.write_text("x: from-b\n", encoding="utf-8")
        r = make(Setting("x", "", "")).resolve(a, {"APP_CONFIG": str(b)})
        assert r.values["x"] == "from-a"

    def test_config_env_names_the_file(self, tmp_path: Path) -> None:
        b = tmp_path / "b.yaml"
        b.write_text("x: from-b\n", encoding="utf-8")
        r = make(Setting("x", "", "")).resolve(None, {"APP_CONFIG": str(b)})
        assert r.values["x"] == "from-b"

    def test_default_path(self, tmp_path: Path) -> None:
        d = tmp_path / "etc.yaml"
        d.write_text("x: from-default-path\n", encoding="utf-8")
        settings = Settings("APP_", (Setting("x", "", ""),), default_path=d)
        assert settings.resolve(None, {}).values["x"] == "from-default-path"

    def test_environ_defaults_to_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_X", "from-os")
        monkeypatch.delenv("APP_CONFIG", raising=False)
        r = make(Setting("x", "", "")).resolve(tmp_path / "missing.yaml")
        assert (r.values["x"], r.sources["x"]) == ("from-os", "APP_X")
        monkeypatch.setenv("APP_CONFIG", str(tmp_path / "also-missing.yaml"))
        assert make(Setting("x", "", "")).resolve().values["x"] == "from-os"

    def test_missing_file_means_defaults(self, tmp_path: Path) -> None:
        r = make(PORT).resolve(tmp_path / "missing.yaml", {})
        assert r.values["listen.port"] == 8080

    @pytest.mark.parametrize("text", ["", "# only a comment\n", "---\n"])
    def test_empty_file_means_defaults(self, tmp_path: Path, text: str) -> None:
        assert resolve_yaml(tmp_path, text, PORT).sources["listen.port"] == "default"

    @pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
    def test_non_mapping_top_level(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match="the top level must be a mapping"):
            resolve_yaml(tmp_path, text, PORT)

    def test_unreadable_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"cannot read .*: Is a directory"):
            make(PORT).resolve(tmp_path, {})

    def test_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: \xff\xfe\n")
        with pytest.raises(ConfigError, match="not UTF-8"):
            make(PORT).resolve(path, {})

    def test_invalid_yaml_gives_position_not_content(self, tmp_path: Path) -> None:
        text = "proxy:\n  auth: hunter2-secret\n  bad: [unclosed\n"
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, text, PORT)
        message = str(info.value)
        assert message.startswith(f"cannot parse {tmp_path / 'config.yaml'} at line ")
        assert "hunter2" not in message
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__

    def test_invalid_yaml_without_a_mark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import yaml

        def boom(_: str) -> Any:
            raise yaml.YAMLError("no mark here")

        settings = make(PORT)  # declared before the patch: declaring parses key segments
        path = tmp_path / "config.yaml"
        path.write_text("a: 1\n", encoding="utf-8")
        monkeypatch.setattr(yaml, "safe_load", boom)
        with pytest.raises(ConfigError, match=r"cannot parse .*config.yaml: invalid YAML$"):
            settings.resolve(path, {})


# --- _FILE ---------------------------------------------------------------------------------------


class TestFileVariable:
    def resolve_file(self, tmp_path: Path, content: str | bytes, setting: Setting) -> Resolved:
        path = tmp_path / "value"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        settings = make(setting)
        return settings.resolve(DEFAULT, {f"{settings[0].env}_FILE": str(path)})

    @pytest.mark.parametrize(
        ("content", "value"),
        [
            ("value\n", "value"),
            ("value\r\n", "value"),
            ("value \t\n\n", "value"),
            ("  leading kept\n", "  leading kept"),
            ("two\nlines\n", "two\nlines"),
            ("", ""),
        ],
    )
    def test_trailing_whitespace_stripped(self, tmp_path: Path, content: str, value: str) -> None:
        r = self.resolve_file(tmp_path, content, Setting("a", "x", ""))
        assert r.values["a"] == value
        assert r.sources["a"] == "APP_A_FILE"

    def test_coerced_like_any_string(self, tmp_path: Path) -> None:
        assert self.resolve_file(tmp_path, "42\n", PORT).values["listen.port"] == 42
        r = self.resolve_file(tmp_path, "yes\n", Setting("enabled", False, ""))
        assert r.values["enabled"] is True
        r = self.resolve_file(tmp_path, "a, b\n", Setting("l", "", "", is_list=True))
        assert r.values["l"] == ("a", "b")

    def test_bad_value_names_the_file_variable(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"listen.port: expected int, got '0.5' \(from APP_"):
            self.resolve_file(tmp_path, "0.5\n", PORT)

    def test_missing_file_names_variable_and_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        with pytest.raises(ConfigError) as info:
            make(PORT).resolve(DEFAULT, {"APP_LISTEN_PORT_FILE": str(missing)})
        assert str(info.value) == (
            f"cannot read APP_LISTEN_PORT_FILE ({missing}): No such file or directory"
        )
        assert info.value.__cause__ is None

    def test_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"APP_LISTEN_PORT_FILE .*Is a directory"):
            make(PORT).resolve(DEFAULT, {"APP_LISTEN_PORT_FILE": str(tmp_path)})

    def test_not_utf8(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"cannot read APP_A_FILE \(.*\): not UTF-8"):
            self.resolve_file(tmp_path, b"\xff\xfe", Setting("a", "", ""))

    def test_wins_even_when_env_is_set(self, tmp_path: Path) -> None:
        (tmp_path / "v").write_text("from-file", encoding="utf-8")
        r = make(Setting("a", "", "")).resolve(
            DEFAULT, {"APP_A": "from-env", "APP_A_FILE": str(tmp_path / "v")}
        )
        assert (r.values["a"], r.sources["a"]) == ("from-file", "APP_A_FILE")


# --- coercion ------------------------------------------------------------------------------------

FLAG = Setting("flag", False, "")
COUNT = Setting("count", 0, "")
NAME = Setting("name", "", "")
LIST = Setting("hosts", "", "", is_list=True)


def yaml_value(tmp_path: Path, setting: Setting, text: str) -> Value:
    return resolve_yaml(tmp_path, f"{setting.key}: {text}\n", setting).values[setting.key]


def env_value(setting: Setting, text: str) -> Value:
    return make(setting).resolve(DEFAULT, {"APP_" + setting.key.upper(): text}).values[setting.key]


class TestBool:
    @pytest.mark.parametrize(
        "text", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", " true ", "true\n"]
    )
    def test_true_from_env(self, text: str) -> None:
        assert env_value(FLAG, text) is True

    @pytest.mark.parametrize("text", ["0", "false", "FALSE", "no", "No", "off", "Off", " off "])
    def test_false_from_env(self, text: str) -> None:
        assert env_value(FLAG, text) is False

    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("true", True),
            ("false", False),
            ("yes", True),
            ("off", False),
            ("1", True),
            ("0", False),
        ],
    )
    def test_from_yaml(self, tmp_path: Path, text: str, value: bool) -> None:
        # yes/off are YAML 1.1 bools to pyyaml; 1/0 are ints, the same spelling as "1"/"0".
        assert yaml_value(tmp_path, FLAG, text) is value

    @pytest.mark.parametrize("text", ["'yes'", "'0'", '"TRUE"'])
    def test_quoted_strings_from_yaml(self, tmp_path: Path, text: str) -> None:
        assert isinstance(yaml_value(tmp_path, FLAG, text), bool)

    @pytest.mark.parametrize("text", ["", "2", "-1", "maybe", "y", "n", "t", "enabled", "1.0"])
    def test_bad_from_env(self, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^flag: expected bool, got .* \(from APP_FLAG\)$"):
            env_value(FLAG, text)

    @pytest.mark.parametrize("text", ["2", "-1", "1.0", "0.0", "[true]", "{a: 1}", "maybe"])
    def test_bad_from_yaml(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^flag: expected bool, got .* \(from file\)$"):
            yaml_value(tmp_path, FLAG, text)

    def test_default_is_returned_as_is(self, tmp_path: Path) -> None:
        assert make(Setting("flag", True, "")).resolve(DEFAULT, {}).values["flag"] is True


class TestInt:
    @pytest.mark.parametrize(
        ("text", "value"), [("0", 0), ("42", 42), ("-7", -7), ("+7", 7), (" 12 ", 12), ("007", 7)]
    )
    def test_from_env(self, text: str, value: int) -> None:
        assert env_value(COUNT, text) == value

    @pytest.mark.parametrize(
        ("text", "value"), [("0", 0), ("42", 42), ("-7", -7), ("'12'", 12), ('"0012"', 12)]
    )
    def test_from_yaml(self, tmp_path: Path, text: str, value: int) -> None:
        result = yaml_value(tmp_path, COUNT, text)
        assert result == value
        assert type(result) is int

    @pytest.mark.parametrize(
        "text", ["", "0.5", "1e3", "1_000", "0x10", "ten", "true", "1 2", "\u0661\u0662"]
    )
    def test_bad_from_env(self, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^count: expected int, got "):
            env_value(COUNT, text)

    @pytest.mark.parametrize(
        "text",
        # The drift: 0.5 was a silent 0 in two copies, true a silent 1 in one.
        [
            "0.5",
            "1.0",
            "true",
            "false",
            "yes",
            "[1, 2]",
            "{a: 1}",
            "'1_000'",
            "'1.5'",
            "2026-11-01",
        ],
    )
    def test_bad_from_yaml(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^count: expected int, got .* \(from file\)$"):
            yaml_value(tmp_path, COUNT, text)

    def test_message_shows_the_value(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"^count: expected int, got 0.5 \(from file\)$"):
            yaml_value(tmp_path, COUNT, "0.5")


class TestStr:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("hello", "hello"),
            ("'with: colon'", "with: colon"),
            ("1", "1"),
            ("2.5", "2.5"),
            ("true", "True"),
            ("2026-11-01", "2026-11-01"),
            ("''", ""),
        ],
    )
    def test_scalars_from_yaml(self, tmp_path: Path, text: str, value: str) -> None:
        assert yaml_value(tmp_path, NAME, text) == value

    @pytest.mark.parametrize("text", ["[a, b]", "[]", "{a: 1}", "{}", "!!set {a}", "!!binary aGk="])
    def test_collections_from_yaml_are_errors_not_reprs(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^name: expected str, got "):
            yaml_value(tmp_path, NAME, text)

    @pytest.mark.parametrize("text", ["", " padded ", "[a, b]", "a,b"])
    def test_env_is_taken_verbatim(self, text: str) -> None:
        assert env_value(NAME, text) == text


class TestList:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("a", ("a",)),
            ("a,b", ("a", "b")),
            (" a , b ,", ("a", "b")),
            (",,a,,", ("a",)),
            ("", ()),
            (" , ", ()),
        ],
    )
    def test_from_env(self, text: str, value: tuple[str, ...]) -> None:
        assert env_value(LIST, text) == value

    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("[a, b]", ("a", "b")),
            ("[' a ', '', b]", ("a", "b")),
            ("[]", ()),
            ("[1, 2.5, true]", ("1", "2.5", "True")),
            ("'a, b'", ("a", "b")),
            ("example.org", ("example.org",)),
            ("['a,b']", ("a,b",)),  # a YAML item is never split again
        ],
    )
    def test_from_yaml(self, tmp_path: Path, text: str, value: tuple[str, ...]) -> None:
        assert yaml_value(tmp_path, LIST, text) == value

    def test_block_list_from_yaml(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "hosts:\n  - example.org\n  - '*.example.org'\n", LIST)
        assert r.values["hosts"] == ("example.org", "*.example.org")

    @pytest.mark.parametrize(
        "text", ["{a: 1}", "[[a], b]", "[{a: 1}]", "!!set {a}", "5", "true", "[a, null]"]
    )
    def test_bad_from_yaml(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match=r"^hosts: expected list, got "):
            yaml_value(tmp_path, LIST, text)

    def test_default_is_the_empty_tuple(self) -> None:
        r = make(LIST).resolve(DEFAULT, {})
        assert (r.values["hosts"], r.sources["hosts"]) == ((), "default")


# --- checks --------------------------------------------------------------------------------------


class TestChecks:
    @pytest.mark.parametrize("value", [1, 80, 65535])
    def test_port_ok(self, value: int) -> None:
        port(value)

    @pytest.mark.parametrize("value", [0, -1, 65536, 100000])
    def test_port_bad(self, value: int) -> None:
        with pytest.raises(ValueError, match=rf"^{value} is not a TCP port$"):
            port(value)

    @pytest.mark.parametrize("value", [1, 10**9])
    def test_positive_ok(self, value: int) -> None:
        positive(value)

    @pytest.mark.parametrize("value", [0, -5])
    def test_positive_bad(self, value: int) -> None:
        with pytest.raises(ValueError, match=rf"^must be greater than 0, got {value}$"):
            positive(value)

    @pytest.mark.parametrize("value", [0, 1])
    def test_non_negative_ok(self, value: int) -> None:
        non_negative(value)

    def test_non_negative_bad(self) -> None:
        with pytest.raises(ValueError, match=r"^must be 0 or more, got -1$"):
            non_negative(-1)

    @pytest.mark.parametrize("check", [port, positive, non_negative])
    @pytest.mark.parametrize("value", [True, "80", ("80",), Secret("80")])
    def test_int_checks_refuse_non_ints(self, check: Callable[[Value], None], value: Value) -> None:
        with pytest.raises(ValueError, match=r"^expected int$"):
            check(value)

    @pytest.mark.parametrize(
        ("value", "spelling"),
        [("INFO", None), ("info", "INFO"), ("Debug", "DEBUG"), ("error", "ERROR")],
    )
    def test_one_of_matches_any_case_and_gives_the_declared_spelling(
        self, value: str, spelling: str | None
    ) -> None:
        assert one_of("DEBUG", "INFO", "WARNING", "ERROR")(value) == spelling

    def test_one_of_keeps_an_exact_match_and_prefers_the_first_spelling(self) -> None:
        check = one_of("a", "A", "b")
        assert check("a") is None and check("A") is None
        assert check("B") == "b"

    def test_one_of_bad(self) -> None:
        with pytest.raises(ValueError, match=r"^expected one of DEBUG, INFO, got 'LOUD'$"):
            one_of("DEBUG", "INFO")("LOUD")

    @pytest.mark.parametrize("value", [1, True, ("INFO",)])
    def test_one_of_refuses_non_str(self, value: Value) -> None:
        with pytest.raises(ValueError, match=r"^expected one of DEBUG, INFO$"):
            one_of("DEBUG", "INFO")(value)

    def test_one_of_needs_a_choice(self) -> None:
        with pytest.raises(ValueError, match="at least one choice"):
            one_of()

    def test_check_failure_becomes_config_error(self) -> None:
        settings = make(Setting("listen.port", 8080, "", check=port))
        with pytest.raises(ConfigError, match=r"^listen.port: 70000 is not a TCP port$") as info:
            settings.resolve(DEFAULT, {"APP_LISTEN_PORT": "70000"})
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__

    def test_check_gets_the_coerced_value(self) -> None:
        seen: list[Value] = []
        settings = make(Setting("n", 0, "", check=seen.append))
        settings.resolve(DEFAULT, {"APP_N": "5"})
        assert seen == [5]

    def test_check_runs_on_the_default(self) -> None:
        with pytest.raises(ConfigError, match=r"^n: must be greater than 0, got 0$"):
            make(Setting("n", 0, "", check=positive)).resolve(DEFAULT, {})

    def test_check_on_a_list_gets_the_tuple(self) -> None:
        def no_dupes(value: Value) -> None:
            assert isinstance(value, tuple)
            if len(set(value)) != len(value):
                raise ValueError("listed twice")

        settings = make(Setting("l", "", "", is_list=True, check=no_dupes))
        with pytest.raises(ConfigError, match=r"^l: listed twice$"):
            settings.resolve(DEFAULT, {"APP_L": "a,a"})

    def test_one_of_resolves_to_the_declared_spelling(self) -> None:
        # Issue #8: `info` resolves to `INFO` when `INFO` is declared; the default too.
        settings = make(Setting("log_level", "info", "", check=one_of("DEBUG", "INFO")))
        assert settings.resolve(DEFAULT, {"APP_LOG_LEVEL": "debug"}).values["log_level"] == "DEBUG"
        assert settings.resolve(DEFAULT, {}).values["log_level"] == "INFO"

    @pytest.mark.parametrize(
        ("default", "replacement", "is_list"),
        [("", "x", False), (1, 2, False), (False, True, False), ("", ("a", "b"), True)],
    )
    def test_a_check_may_replace_the_value_with_its_kind(
        self, default: str | int | bool, replacement: Value, is_list: bool
    ) -> None:
        settings = make(Setting("v", default, "", is_list=is_list, check=lambda _: replacement))
        assert settings.resolve(DEFAULT, {}).values["v"] == replacement

    def test_a_check_that_returns_the_same_value_changes_nothing(self) -> None:
        settings = make(Setting("v", 1, "", check=lambda v: v))
        assert settings.resolve(DEFAULT, {}).values["v"] == 1

    @pytest.mark.parametrize(
        ("default", "replacement", "is_list", "message"),
        [
            ("", 5, False, "v: its check returned int, expected str"),
            (1, "1", False, "v: its check returned str, expected int"),
            (1, True, False, "v: its check returned bool, expected int"),
            (False, 0, False, "v: its check returned int, expected bool"),
            ("", "a", True, "v: its check returned str, expected a tuple of str"),
            ("", ("a", 1), True, "v: its check returned a tuple with a non-str item"),
            ("", 0.5, False, "v: its check returned float, expected str"),
            ("", Secret("x"), False, "v: its check returned Secret, expected str"),
            ("", ["a"], True, "v: its check returned list, expected a tuple of str"),
        ],
    )
    def test_a_replacement_of_another_kind_is_a_config_error(
        self, default: str | int | bool, replacement: Any, is_list: bool, message: str
    ) -> None:
        settings = make(Setting("v", default, "", is_list=is_list, check=lambda _: replacement))
        with pytest.raises(ConfigError) as info:
            settings.resolve(DEFAULT, {})
        assert str(info.value) == message

    def test_other_exceptions_propagate(self) -> None:
        def broken(value: Value) -> None:
            raise RuntimeError("a bug in the check")

        with pytest.raises(RuntimeError):
            make(Setting("a", "", "", check=broken)).resolve(DEFAULT, {})


# --- secrets -------------------------------------------------------------------------------------

AUTH = Setting("proxy.auth", "", "", secret=True)
VALUE = "user:hunter2-very-secret"


class TestSecretSettings:
    def test_resolves_to_secret(self, tmp_path: Path) -> None:
        (tmp_path / "auth").write_text(VALUE + "\n", encoding="utf-8")
        r = make(AUTH).resolve(DEFAULT, {"APP_PROXY_AUTH_FILE": str(tmp_path / "auth")})
        value = r.values["proxy.auth"]
        assert isinstance(value, Secret)
        assert value.reveal() == VALUE
        assert r.sources["proxy.auth"] == "APP_PROXY_AUTH_FILE"

    @pytest.mark.parametrize("source", ["env", "yaml", "default"])
    def test_every_source_gives_a_secret(self, tmp_path: Path, source: str) -> None:
        environ = {"APP_PROXY_AUTH": VALUE} if source == "env" else {}
        text = f"proxy:\n  auth: {VALUE}\n" if source == "yaml" else ""
        value = resolve_yaml(tmp_path, text, AUTH, environ=environ).values["proxy.auth"]
        assert isinstance(value, Secret)
        assert value.reveal() == ("" if source == "default" else VALUE)

    def test_unset_secret_is_falsy(self) -> None:
        assert not make(AUTH).resolve(DEFAULT, {}).values["proxy.auth"]

    def test_resolved_repr_masks(self) -> None:
        r = make(AUTH).resolve(DEFAULT, {"APP_PROXY_AUTH": VALUE})
        assert VALUE not in repr(r)
        assert VALUE not in str(dict(r.values))
        assert MASK in repr(r)

    def test_coercion_error_omits_the_value(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, f"proxy:\n  auth: [{VALUE}]\n", AUTH)
        assert str(info.value) == "proxy.auth: expected str (from file)"
        assert VALUE not in repr(info.value)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None or info.value.__suppress_context__

    def test_check_reason_is_masked(self) -> None:
        def needs_colon(value: Value) -> None:
            raise ValueError(f"{value!r} is not user:password")

        settings = make(Setting("proxy.auth", "", "", secret=True, check=needs_colon))
        with pytest.raises(ConfigError) as info:
            settings.resolve(DEFAULT, {"APP_PROXY_AUTH": VALUE})
        assert VALUE not in str(info.value)
        assert str(info.value) == f"proxy.auth: '{MASK}' is not user:password"

    @pytest.mark.parametrize(
        "value", ["C:\\pw\\x", "tab\there", 'it\'s "quoted"', "caf\u00e9-p\u00e4ss", "nl\nx"]
    )
    @pytest.mark.parametrize("fmt", ["{v}", "{v!r}", "{v!a}"])
    def test_check_reason_is_masked_in_every_form(self, value: str, fmt: str) -> None:
        def echo(v: Value) -> None:
            raise ValueError(fmt.format(v=v) + " is not user:password")

        settings = make(Setting("proxy.auth", "", "", secret=True, check=echo))
        with pytest.raises(ConfigError) as info:
            settings.resolve(DEFAULT, {"APP_PROXY_AUTH": value})
        message = str(info.value)
        for form in (value, repr(value), repr(value)[1:-1], ascii(value)[1:-1]):
            assert form not in message
        assert MASK in message

    def test_a_check_cant_replace_a_secret(self) -> None:
        settings = make(Setting("t", "", "", secret=True, check=lambda v: f"{v}-changed"))
        with pytest.raises(ConfigError) as info:
            settings.resolve(DEFAULT, {"APP_T": VALUE})
        assert str(info.value) == "t: a check can't replace a secret setting's value"
        assert VALUE not in repr(info.value) and info.value.__cause__ is None

    def test_a_check_on_a_secret_may_return_the_value_unchanged(self) -> None:
        settings = make(Setting("t", "", "", secret=True, check=lambda v: v))
        assert settings.resolve(DEFAULT, {"APP_T": VALUE}).secret("t").reveal() == VALUE

    def test_one_of_on_a_secret_keeps_the_declared_spelling_only(self) -> None:
        settings = make(Setting("t", "", "", secret=True, check=one_of("Alpha", "Beta")))
        assert settings.resolve(DEFAULT, {"APP_T": "Beta"}).secret("t") == Secret("Beta")
        with pytest.raises(ConfigError, match="can't replace a secret"):
            settings.resolve(DEFAULT, {"APP_T": "beta"})

    def test_check_gets_the_plain_value(self) -> None:
        seen: list[Value] = []
        settings = make(Setting("t", "", "", secret=True, check=seen.append))
        settings.resolve(DEFAULT, {"APP_T": VALUE})
        assert seen == [VALUE]


# --- no values in logs, no I/O before resolve ----------------------------------------------------


def test_resolve_logs_no_values(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    resolve_yaml(tmp_path, "a: plain-value\n", Setting("a", "", ""), AUTH, environ={})
    make(AUTH).resolve(DEFAULT, {"APP_PROXY_AUTH": VALUE})
    assert "plain-value" not in caplog.text
    assert VALUE not in caplog.text


def test_declaring_settings_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("I/O before resolve()")

    monkeypatch.setattr(Path, "read_text", no_io)
    monkeypatch.setattr(os, "listdir", no_io)
    settings = Settings("APP_", (AUTH, PORT), default_path=Path("/etc/app/config.yaml"))
    assert [s.env for s in settings] == ["APP_PROXY_AUTH", "APP_LISTEN_PORT"]


def test_a_consumer_config(tmp_path: Path) -> None:
    """The shape design 0001 gives a consumer's config.py, end to end."""
    levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    settings = Settings(
        "MITM_PROXY_",
        (
            Setting("log_level", "INFO", "Log level.", check=one_of(*levels)),
            Setting("proxy.port", 3128, "Port.", check=port),
            Setting("proxy.auth", "", "user:password.", secret=True),
            Setting("proxy.access_log", False, "Access log."),
            Setting("intercept.hosts", "", "Allowlist.", is_list=True),
            Setting("upstream.idle_timeout_ms", 60000, "Idle.", check=positive),
        ),
        default_path=tmp_path / "missing.yaml",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "log_level: debug\nproxy:\n  access_log: yes\nintercept:\n  hosts: [example.org]\n",
        encoding="utf-8",
    )
    (tmp_path / "auth").write_text("u:p\n", encoding="utf-8")
    r = settings.resolve(
        None,
        {
            "MITM_PROXY_CONFIG": str(config),
            "MITM_PROXY_PROXY_PORT": "8888",
            "MITM_PROXY_PROXY_AUTH_FILE": str(tmp_path / "auth"),
        },
    )
    assert dict(r.sources) == {
        "log_level": "file",
        "proxy.port": "MITM_PROXY_PROXY_PORT",
        "proxy.auth": "MITM_PROXY_PROXY_AUTH_FILE",
        "proxy.access_log": "file",
        "intercept.hosts": "file",
        "upstream.idle_timeout_ms": "default",
    }
    assert r.values["log_level"] == "DEBUG"  # the declared spelling (design 0007)
    assert r.values["proxy.port"] == 8888
    assert r.values["proxy.auth"] == Secret("u:p")
    assert r.values["proxy.access_log"] is True
    assert r.values["intercept.hosts"] == ("example.org",)
    assert r.values["upstream.idle_timeout_ms"] == 60000
    # The typed accessors give the same values with their types (design 0007).
    assert r.str("log_level") == "DEBUG"
    assert r.int("proxy.port") == 8888
    assert r.secret("proxy.auth").reveal() == "u:p"
    assert r.bool("proxy.access_log") is True
    assert r.list("intercept.hosts") == ("example.org",)


# --- typed accessors (design 0007) ---------------------------------------------------------------

KINDS = (
    Setting("s", "text", ""),
    Setting("i", 3, ""),
    Setting("b", True, ""),
    Setting("l", "", "", is_list=True),
    Setting("k", "", "", secret=True),
)


class TestTypedAccessors:
    def resolved(self) -> Resolved:
        return make(*KINDS).resolve(DEFAULT, {"APP_L": "a,b", "APP_K": VALUE})

    def test_each_accessor_gives_its_kind(self) -> None:
        r = self.resolved()
        assert r.str("s") == "text"
        assert r.int("i") == 3
        assert r.bool("b") is True
        assert r.list("l") == ("a", "b")
        assert r.secret("k").reveal() == VALUE

    @pytest.mark.parametrize(
        ("accessor", "key", "kind"),
        [
            ("str", "i", "int"),
            ("str", "b", "bool"),
            ("str", "l", "list"),
            ("int", "b", "bool"),
            ("int", "s", "str"),
            ("bool", "i", "int"),
            ("list", "s", "str"),
            ("secret", "s", "str"),
        ],
    )
    def test_another_kind_is_a_type_error_naming_the_key(
        self, accessor: str, key: str, kind: str
    ) -> None:
        with pytest.raises(TypeError) as info:
            getattr(self.resolved(), accessor)(key)
        assert str(info.value) == f"{key!r} is a {kind} setting, not {accessor}"

    @pytest.mark.parametrize("accessor", ["str", "int", "bool", "list"])
    def test_a_secret_setting_is_a_type_error_that_never_shows_the_value(
        self, accessor: str
    ) -> None:
        # Issue #13: `str()` on a secret setting would give the mask, silently.
        with pytest.raises(TypeError) as info:
            getattr(self.resolved(), accessor)("k")
        message = str(info.value)
        assert message == f"'k' is a secret setting, not {accessor}: use secret() and reveal()"
        assert VALUE not in message

    @pytest.mark.parametrize("accessor", ["str", "int", "bool", "list", "secret"])
    def test_a_missing_key_is_a_key_error(self, accessor: str) -> None:
        with pytest.raises(KeyError):
            getattr(self.resolved(), accessor)("nope")
