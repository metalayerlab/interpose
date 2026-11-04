"""interpose.settings: declaration, lookup order, coercion, provenance, checks and secrets."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import math
import os
import pickle
import re
import struct
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from interpose.secret import MASK, Secret
from interpose.settings import (
    ConfigError,
    ConfigWarning,
    Layer,
    Resolved,
    Section,
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


# --- layers (design 0009, step 1) ---------------------------------------------------------------

ETC = "/etc/app/config.yaml"


def layer(data: Mapping[str, Any], where: str = ETC, **kwargs: Any) -> Layer:
    return Layer(data=data, source=kwargs.pop("source", "file"), where=where, **kwargs)


class TestLayers:
    def test_first_layer_that_has_the_key_wins(self) -> None:
        first = Layer({"a": "a0"}, source="control plane", where="control plane (config v12)")
        second = Layer({"a": "a1", "b": "b1"}, source="file", where=ETC)
        settings = make(Setting("a", "", ""), Setting("b", "", ""), Setting("c", "dflt", ""))
        r = settings.resolve(environ={}, layers=[first, second])
        assert dict(r.values) == {"a": "a0", "b": "b1", "c": "dflt"}
        assert dict(r.sources) == {"a": "control plane", "b": "file", "c": "default"}

    def test_a_null_in_a_layer_falls_through_to_the_next(self) -> None:
        r = make(PORT).resolve(
            environ={},
            layers=[
                layer({"listen": {"port": None}}),
                layer({"listen": {"port": 2}}, source="second"),
            ],
        )
        assert (r.values["listen.port"], r.sources["listen.port"]) == (2, "second")

    def test_file_variable_and_variable_beat_every_layer(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_text("from-file-var\n", encoding="utf-8")
        environ = {"APP_A_FILE": str(tmp_path / "a"), "APP_B": "from-var"}
        top = layer({"a": "layer", "b": "layer"}, source="control plane")
        r = make(Setting("a", "", ""), Setting("b", "", "")).resolve(environ=environ, layers=[top])
        assert dict(r.values) == {"a": "from-file-var", "b": "from-var"}
        assert dict(r.sources) == {"a": "APP_A_FILE", "b": "APP_B"}

    def test_path_with_layers_is_a_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="path or layers, not both"):
            make(PORT).resolve(tmp_path / "config.yaml", {}, layers=[layer({})])

    def test_no_layers_means_defaults_and_environment_only(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("a: from-file\nb: from-file\n", encoding="utf-8")
        environ = {"APP_CONFIG": str(config), "APP_B": "from-var"}
        settings = Settings(
            "APP_", (Setting("a", "dflt", ""), Setting("b", "", "")), default_path=config
        )
        r = settings.resolve(environ=environ, layers=[])
        assert dict(r.values) == {"a": "dflt", "b": "from-var"}
        assert dict(r.sources) == {"a": "default", "b": "APP_B"}

    def test_read_file_is_the_implicit_file_layer(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("listen:\n  port: 1\n", encoding="utf-8")
        settings = make(PORT, Setting("a", "dflt", ""))
        environ = {"APP_CONFIG": str(config), "APP_A": "x"}
        file_layer = settings.read_file(environ=environ)
        assert file_layer == Layer(
            {"listen": {"port": 1}}, source="file", where=str(config), secret_files=True
        )
        assert settings.resolve(environ=environ, layers=[file_layer]) == settings.resolve(
            environ=environ
        )
        assert settings.read_file(config, {}) == file_layer

    def test_read_file_of_a_missing_file_is_an_empty_layer(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.yaml"
        assert make(PORT).read_file(missing, {}) == Layer(
            {}, source="file", where=str(missing), secret_files=True
        )
        assert make(PORT).read_file(environ={}).where == str(DEFAULT)

    def test_read_file_has_resolves_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("- a\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: the top level must be"):
            make(PORT).read_file(path, {})

    def test_read_file_defaults_to_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_CONFIG", str(tmp_path / "from-os.yaml"))
        assert make(PORT).read_file().where == str(tmp_path / "from-os.yaml")

    def test_sources_say_file_for_the_file_layer(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("a: x\n", encoding="utf-8")
        settings = make(Setting("a", "", ""))
        assert settings.resolve(config, {}).sources["a"] == "file"
        assert settings.resolve(environ={}, layers=[settings.read_file(config, {})]).sources == {
            "a": "file"
        }

    def test_coercion_error_names_the_layers_where(self) -> None:
        with pytest.raises(ConfigError) as info:
            make(PORT).resolve(environ={}, layers=[layer({"listen": {"port": "x"}})])
        assert str(info.value) == f"listen.port: expected int, got 'x' (from {ETC})"

    def test_origins_beat_where(self) -> None:
        top = layer(
            {"listen": {"port": "x"}, "a": "ok"},
            origins={"listen.port": f"{ETC}, line 3", "a": "elsewhere"},
        )
        with pytest.raises(ConfigError) as info:
            make(PORT, Setting("a", "", "")).resolve(environ={}, layers=[top])
        assert str(info.value) == f"listen.port: expected int, got 'x' (from {ETC}, line 3)"

    def test_check_error_names_the_layer(self) -> None:
        settings = make(Setting("listen.port", 8080, "", check=port))
        with pytest.raises(ConfigError) as info:
            settings.resolve(environ={}, layers=[layer({"listen": {"port": 70000}})])
        assert str(info.value) == f"listen.port: 70000 is not a TCP port (from {ETC})"
        top = layer({"listen": {"port": 70000}}, origins={"listen.port": "payload.listen"})
        with pytest.raises(ConfigError, match=r"TCP port \(from payload.listen\)$"):
            settings.resolve(environ={}, layers=[top])

    def test_check_error_names_the_config_file(self, tmp_path: Path) -> None:
        settings = make(Setting("n", 1, "", check=positive))
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, "n: 0\n", *settings)
        assert (
            str(info.value) == f"n: must be greater than 0, got 0 (from {tmp_path / 'config.yaml'})"
        )

    def test_check_errors_for_a_variable_or_default_still_name_no_source(self) -> None:
        settings = make(Setting("n", 0, "", check=positive))
        with pytest.raises(ConfigError, match=r"^n: must be greater than 0, got 0$"):
            settings.resolve(environ={}, layers=[layer({"other": 1})])
        with pytest.raises(ConfigError, match=r"^n: must be greater than 0, got -1$"):
            settings.resolve(environ={"APP_N": "-1"}, layers=[layer({"n": 5})])

    def test_a_secrets_value_is_in_no_message(self) -> None:
        def echo(value: Value) -> None:
            raise ValueError(f"{value!r} is not user:password")

        checked = Setting("proxy.auth", "", "", secret=True, check=echo)
        where = layer({"proxy": {"auth": VALUE}}, origins={"proxy.auth": "payload.proxy.auth"})
        with pytest.raises(ConfigError) as info:
            make(checked).resolve(environ={}, layers=[where])
        assert (
            str(info.value)
            == f"proxy.auth: '{MASK}' is not user:password (from payload.proxy.auth)"
        )
        with pytest.raises(ConfigError) as info:
            make(AUTH).resolve(environ={}, layers=[layer({"proxy": {"auth": [VALUE]}})])
        assert str(info.value) == f"proxy.auth: expected str (from {ETC})"
        assert VALUE not in repr(info.value)

    def test_data_is_neither_changed_nor_kept(self) -> None:
        data: dict[str, Any] = {"hosts": ["a", " b "], "n": "5"}
        snapshot = {"hosts": ["a", " b "], "n": "5"}
        r = make(LIST_SETTING, Setting("n", 0, "")).resolve(environ={}, layers=[layer(data)])
        assert data == snapshot
        assert dict(r.values) == {"hosts": ("a", "b"), "n": 5}
        data["n"] = "6"
        assert r.values["n"] == 5

    def test_layer_is_frozen_and_origins_default_to_empty(self) -> None:
        top = Layer({}, source="file", where=ETC)
        assert top.origins == {}
        with pytest.raises(AttributeError):
            top.where = "x"  # type: ignore[misc]  # asserting that frozen=True refuses this


LIST_SETTING = Setting("hosts", "", "", is_list=True)


# --- unknown keys and variables (design 0009, step 2) ------------------------------------------


def warnings_of(
    settings: Settings, *layers: Layer, environ: Mapping[str, str] | None = None
) -> list[str]:
    return [str(w) for w in settings.resolve(environ=environ or {}, layers=list(layers)).warnings]


class TestUnknownKeys:
    def test_a_clean_config_has_no_warnings(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "listen:\n  port: 1\n", PORT, environ={"APP_LISTEN_PORT": "2"})
        assert r.warnings == ()
        assert make(PORT).resolve(DEFAULT, {}).warnings == ()

    def test_unknown_top_level_key(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "listen:\n  port: 1\nverbose: true\n", PORT)
        assert r.values["listen.port"] == 1
        path = tmp_path / "config.yaml"
        assert r.warnings == (
            ConfigWarning("unknown", "verbose", f"unknown key 'verbose' in {path}"),
        )

    def test_nested_typo_is_reported_with_a_suggestion(self, tmp_path: Path) -> None:
        settings = Settings("SLUICE_", (PORT,), default_path=DEFAULT)
        r = settings.resolve(
            environ={}, layers=[layer({"listen": {"prot": 1}}, where="/etc/sluice/config.yaml")]
        )
        assert r.values["listen.port"] == 8080  # still ignored for resolution
        assert len(r.warnings) == 1
        (w,) = r.warnings
        assert (w.kind, w.name) == ("unknown", "listen.prot")
        assert str(w) == (
            "unknown key 'listen.prot' in /etc/sluice/config.yaml: did you mean 'listen.port'?"
        )

    def test_reported_once_at_the_first_unknown_level(self) -> None:
        data = {"listne": {"port": 1, "host": {"name": "x"}}, "listen": {"backlog": {"n": 1}}}
        assert warnings_of(make(PORT), layer(data)) == [
            f"unknown key 'listne' in {ETC}: did you mean 'listen'?",
            f"unknown key 'listen.backlog' in {ETC}",
        ]

    def test_parents_of_declared_keys_are_known(self) -> None:
        deep = Setting("a.b.c", "", "")
        assert warnings_of(make(deep), layer({"a": {"b": {"c": "x"}}})) == []
        assert warnings_of(make(deep), layer({"a": {"b": None}, "a_": 1})) == [
            f"unknown key 'a_' in {ETC}"
        ]

    def test_nothing_below_a_declared_key_or_a_scalar_parent_is_checked(self) -> None:
        # A mapping under a declared key is a coercion error if read, never an unknown key; a
        # scalar where a parent mapping belongs means unset, as before.
        deep = make(Setting("a.b", "", ""), Setting("n", 0, ""))
        top = layer({"a": "scalar"})
        lower = layer({"a": {"b": {"stray": 1}}, "zz": 1}, source="lower")
        r = deep.resolve(environ={"APP_A_B": "v"}, layers=[top, lower])
        assert [w.name for w in r.warnings] == ["zz"]  # the control: the only one reported

    def test_suggestion_present_and_absent_at_the_cutoff(self) -> None:
        settings = make(Setting("abcd", "", ""))
        # "abce" vs "abcd": ratio 0.75, the cutoff, so suggested; "abxy": 0.5, not.
        assert warnings_of(settings, layer({"abce": 1, "abxy": 2})) == [
            f"unknown key 'abce' in {ETC}: did you mean 'abcd'?",
            f"unknown key 'abxy' in {ETC}",
        ]
        # "abcdefgh" vs "abcdwxyz" is 0.5; "abcdefgh" vs "abcdefzz" is exactly 0.75.
        long = make(Setting("abcdefgh", "", ""))
        assert warnings_of(long, layer({"abcdefzz": 1, "abcdwxyz": 1})) == [
            f"unknown key 'abcdefzz' in {ETC}: did you mean 'abcdefgh'?",
            f"unknown key 'abcdwxyz' in {ETC}",
        ]

    def test_a_non_string_key_is_named_by_its_yaml_text(self, tmp_path: Path) -> None:
        text = "on: 1\nlisten:\n  1: x\n  null: y\n  port: 2\n"
        r = resolve_yaml(tmp_path, text, PORT, Setting("one", "", ""))
        path = tmp_path / "config.yaml"
        assert [(w.name, str(w)) for w in r.warnings] == [
            ("true", f"unknown key 'true' in {path}"),
            ("listen.1", f"unknown key 'listen.1' in {path}"),
            ("listen.null", f"unknown key 'listen.null' in {path}"),
        ]

    def test_a_flat_dotted_key_is_unknown(self, tmp_path: Path) -> None:
        # Resolution reads only nested mappings, so `listen.port:` at the top is ignored.
        r = resolve_yaml(tmp_path, "listen.port: 1\n", PORT)
        assert r.values["listen.port"] == 8080
        assert [w.name for w in r.warnings] == ["listen.port"]

    def test_a_key_yaml_cant_write_is_named_by_its_repr(self) -> None:
        class Odd:
            def __repr__(self) -> str:
                return "Odd()"

        data: dict[Any, Any] = {Odd(): 1}  # only a project-built Layer can hold such a key
        assert warnings_of(make(PORT), layer(data)) == [f"unknown key 'Odd()' in {ETC}"]

    def test_a_layer_warning_names_its_where_or_origin(self) -> None:
        payload = Layer(
            {"listen": {"prot": 1}, "x": 1},
            source="control plane",
            where="control plane (config v12)",
            origins={"listen.prot": "payload.listen.prot"},
        )
        local = layer({"y": 1})
        assert warnings_of(make(PORT), payload, local) == [
            "unknown key 'listen.prot' in payload.listen.prot: did you mean 'listen.port'?",
            "unknown key 'x' in control plane (config v12)",
            f"unknown key 'y' in {ETC}",
        ]

    def test_no_value_is_in_a_warning(self, tmp_path: Path) -> None:
        settings = make(PORT, AUTH)
        text = f"proxy:\n  auht: {VALUE}\n  extra: [{VALUE}]\n"
        environ = {"APP_PROXY_AHTU": VALUE, "APP_PROXY_AUTH": VALUE}
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        r = settings.resolve(path, environ)
        assert [w.name for w in r.warnings] == ["proxy.auht", "proxy.extra", "APP_PROXY_AHTU"]
        for w in r.warnings:
            assert VALUE not in f"{w} {w!r}"

    def test_order_is_deterministic(self) -> None:
        data = {"b": 1, "listen": {"z": 1, "a": 1}, "a": 1}
        environ = {"APP_Z": "", "APP_B": "", "APP_A": ""}
        names = [w.name for w in make(PORT).resolve(environ=environ, layers=[layer(data)]).warnings]
        # Layers first, in document order; then variables by name.
        assert names == ["b", "listen.z", "listen.a", "a", "APP_A", "APP_B", "APP_Z"]
        reordered = dict(reversed(environ.items()))
        again = make(PORT).resolve(environ=reordered, layers=[layer(data)]).warnings
        assert [w.name for w in again] == names

    def test_warning_is_frozen_and_its_str_is_the_message(self) -> None:
        w = ConfigWarning("unknown", "a", "unknown key 'a' in x")
        assert str(w) == "unknown key 'a' in x"
        with pytest.raises(AttributeError):
            w.name = "b"  # type: ignore[misc]  # asserting that frozen=True refuses this


class TestUnknownVariables:
    def test_unknown_prefixed_variable_with_a_suggestion(self) -> None:
        settings = Settings("SLUICE_", (PORT,), default_path=DEFAULT)
        r = settings.resolve(environ={"SLUICE_LISTEN_PROT": "1", "OTHER_X": "1"}, layers=[])
        assert r.values["listen.port"] == 8080
        assert r.warnings == (
            ConfigWarning(
                "unknown",
                "SLUICE_LISTEN_PROT",
                "unknown variable 'SLUICE_LISTEN_PROT' in the environment: "
                "did you mean 'SLUICE_LISTEN_PORT'?",
            ),
        )

    def test_without_a_close_match_no_suggestion(self) -> None:
        assert warnings_of(make(PORT), environ={"APP_QQQ": "1"}) == [
            "unknown variable 'APP_QQQ' in the environment"
        ]

    def test_variables_settings_read_are_not_reported(self, tmp_path: Path) -> None:
        (tmp_path / "port").write_text("1\n", encoding="utf-8")
        environ = {
            "APP_LISTEN_PORT": "2",
            "APP_LISTEN_PORT_FILE": str(tmp_path / "port"),
            "APP_CONFIG": str(tmp_path / "missing.yaml"),
            "APP_STRAY": "1",  # the control: the only one reported
        }
        assert [w.name for w in make(PORT).resolve(environ=environ).warnings] == ["APP_STRAY"]

    def test_a_file_variable_typo_suggests_the_file_variable(self) -> None:
        assert warnings_of(make(PORT), environ={"APP_LISTEN_PORT_FIEL": "x"}) == [
            "unknown variable 'APP_LISTEN_PORT_FIEL' in the environment: "
            "did you mean 'APP_LISTEN_PORT_FILE'?"
        ]

    def test_no_prefix_checks_no_variables(self) -> None:
        settings = Settings("", (PORT,), default_path=DEFAULT)
        assert settings.resolve(environ={"PATH": "/bin", "HOME": "/"}, layers=[]).warnings == ()

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_STRAY_FROM_OS", "1")
        names = [w.name for w in make(PORT).resolve(layers=[]).warnings]
        assert "APP_STRAY_FROM_OS" in names


class TestExtraEnv:
    def test_extra_env_suppresses_the_warning(self) -> None:
        environ = {"APP_X": "1", "APP_Y": "1"}
        plain = make(PORT)
        assert [w.name for w in plain.resolve(environ=environ, layers=[]).warnings] == [
            "APP_X",
            "APP_Y",
        ]
        settings = Settings("APP_", (PORT,), default_path=DEFAULT, extra_env=("APP_X",))
        assert settings.extra_env == ("APP_X",)
        assert [w.name for w in settings.resolve(environ=environ, layers=[]).warnings] == ["APP_Y"]
        assert make(PORT).extra_env == ()

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (("OTHER_X",), "extra_env 'OTHER_X': doesn't start with the prefix 'APP_'"),
            (("APP_X", "APP_X"), "extra_env 'APP_X': named twice"),
            (("APP_LISTEN_PORT",), "extra_env 'APP_LISTEN_PORT': already read by 'listen.port'"),
            (("APP_LISTEN_PORT_FILE",), "already read by 'listen.port'"),
            (("APP_CONFIG",), "extra_env 'APP_CONFIG': already the config file variable"),
            ("APP_X", "extra_env: a tuple of variable names, not one str"),
        ],
    )
    def test_extra_env_is_validated(self, extra: Any, message: str) -> None:
        with pytest.raises(ValueError, match=re.escape(message)):
            Settings("APP_", (PORT,), default_path=DEFAULT, extra_env=extra)


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
        where = re.escape(str(tmp_path / "config.yaml"))
        with pytest.raises(ConfigError, match=rf"^flag: expected bool, got .* \(from {where}\)$"):
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
        where = re.escape(str(tmp_path / "config.yaml"))
        with pytest.raises(ConfigError, match=rf"^count: expected int, got .* \(from {where}\)$"):
            yaml_value(tmp_path, COUNT, text)

    def test_message_shows_the_value(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as info:
            yaml_value(tmp_path, COUNT, "0.5")
        assert str(info.value) == f"count: expected int, got 0.5 (from {tmp_path / 'config.yaml'})"


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
        assert str(info.value) == f"proxy.auth: expected str (from {tmp_path / 'config.yaml'})"
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


# --- aliases (design 0009, step 3) ---------------------------------------------------------------

OLD_PORT = Setting("listen.port", 8080, "Port.", aliases=("port",))


def alias_warning(old: str, where: str, current: str) -> ConfigWarning:
    return ConfigWarning(
        "alias", old, f"{old!r} (in {where}) is the old name of {current!r}: rename it"
    )


class TestAliasLookup:
    def test_alias_in_the_config_file(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "port: 9000\n", OLD_PORT)
        assert r.values["listen.port"] == 9000
        assert r.sources["listen.port"] == "file"
        path = tmp_path / "config.yaml"
        assert r.warnings == (alias_warning("port", str(path), "listen.port"),)
        assert make(OLD_PORT)[0].aliases == ("port",)  # public, for a project's `config` command

    def test_dotted_alias_in_a_layer(self) -> None:
        moved = Setting("server.listen.port", 8080, "", aliases=("old.port", "port"))
        r = make(moved).resolve(environ={}, layers=[layer({"old": {"port": 1}})])
        assert (r.values["server.listen.port"], r.sources["server.listen.port"]) == (1, "file")
        assert r.warnings == (alias_warning("old.port", ETC, "server.listen.port"),)

    def test_alias_variable(self) -> None:
        r = make(OLD_PORT).resolve(environ={"APP_PORT": "9000"}, layers=[])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (9000, "APP_PORT")
        assert r.warnings == (alias_warning("APP_PORT", "the environment", "APP_LISTEN_PORT"),)

    def test_alias_file_variable(self, tmp_path: Path) -> None:
        (tmp_path / "port").write_text("9000\n", encoding="utf-8")
        environ = {"APP_PORT_FILE": str(tmp_path / "port")}
        r = make(OLD_PORT).resolve(environ=environ, layers=[])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (9000, "APP_PORT_FILE")
        assert r.warnings == (
            alias_warning("APP_PORT_FILE", "the environment", "APP_LISTEN_PORT_FILE"),
        )

    def test_an_unreadable_alias_file_variable_names_it(self, tmp_path: Path) -> None:
        environ = {"APP_PORT_FILE": str(tmp_path / "missing")}
        with pytest.raises(ConfigError, match=r"^cannot read APP_PORT_FILE \("):
            make(OLD_PORT).resolve(environ=environ, layers=[])

    def test_a_list_setting_alias(self) -> None:
        hosts = Setting("allow.hosts", "", "", is_list=True, aliases=("hosts",))
        r = make(hosts).resolve(environ={"APP_HOSTS": "a, b"}, layers=[layer({"hosts": ["c"]})])
        assert r.values["allow.hosts"] == ("a", "b")

    def test_a_null_alias_is_unset(self) -> None:
        r = make(OLD_PORT).resolve(
            environ={}, layers=[layer({"port": None}), layer({"port": 2}, where="/lower.yaml")]
        )
        assert r.values["listen.port"] == 2
        assert r.warnings == (alias_warning("port", "/lower.yaml", "listen.port"),)


class TestAliasSameLevel:
    def test_equal_values_in_a_layer_the_key_is_read(self) -> None:
        # Equal once typed: the int 9000 and the string "9000".
        data = {"listen": {"port": 9000}, "port": "9000"}
        r = make(OLD_PORT).resolve(environ={}, layers=[layer(data)])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (9000, "file")
        assert r.warnings == (alias_warning("port", ETC, "listen.port"),)

    def test_equal_values_in_variables_the_key_is_read(self, tmp_path: Path) -> None:
        r = make(OLD_PORT).resolve(environ={"APP_LISTEN_PORT": "9000", "APP_PORT": " 9000"})
        assert (r.values["listen.port"], r.sources["listen.port"]) == (9000, "APP_LISTEN_PORT")
        assert r.warnings == (alias_warning("APP_PORT", "the environment", "APP_LISTEN_PORT"),)
        (tmp_path / "a").write_text("9000\n", encoding="utf-8")
        (tmp_path / "b").write_text("9000", encoding="utf-8")
        environ = {
            "APP_LISTEN_PORT_FILE": str(tmp_path / "a"),
            "APP_PORT_FILE": str(tmp_path / "b"),
        }
        r = make(OLD_PORT).resolve(environ=environ, layers=[])
        assert r.sources["listen.port"] == "APP_LISTEN_PORT_FILE"
        assert [(w.kind, w.name) for w in r.warnings] == [("alias", "APP_PORT_FILE")]

    def test_different_values_in_a_layer_are_an_error(self) -> None:
        data = {"listen": {"port": 9000}, "port": 9001}
        with pytest.raises(ConfigError) as e:
            make(OLD_PORT).resolve(environ={}, layers=[layer(data)])
        assert str(e.value) == (
            f"listen.port: set as both 'listen.port' and its old name 'port' (from {ETC}), "
            "to different values"
        )
        assert "9000" not in str(e.value) and "9001" not in str(e.value)

    def test_the_conflict_names_the_aliass_origin(self) -> None:
        data = {"listen": {"port": 1}, "port": 2}
        payload = layer(data, where="control plane", origins={"port": "payload.port"})
        with pytest.raises(ConfigError, match=r"old name 'port' \(from payload\.port\), to diff"):
            make(OLD_PORT).resolve(environ={}, layers=[payload])

    def test_different_values_in_variables_are_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            make(OLD_PORT).resolve(environ={"APP_LISTEN_PORT": "1", "APP_PORT": "2"}, layers=[])
        assert str(e.value) == (
            "listen.port: set as both 'APP_LISTEN_PORT' and its old name 'APP_PORT' "
            "(from the environment), to different values"
        )
        (tmp_path / "a").write_text("1", encoding="utf-8")
        (tmp_path / "b").write_text("2", encoding="utf-8")
        environ = {
            "APP_LISTEN_PORT_FILE": str(tmp_path / "a"),
            "APP_PORT_FILE": str(tmp_path / "b"),
        }
        with pytest.raises(
            ConfigError, match="'APP_LISTEN_PORT_FILE' and its old name 'APP_PORT_FILE'"
        ):
            make(OLD_PORT).resolve(environ=environ, layers=[])

    def test_two_aliases_that_differ_name_each_other(self) -> None:
        twice = Setting("listen.port", 8080, "", aliases=("port", "bind_port"))
        with pytest.raises(ConfigError, match="both 'port' and its old name 'bind_port'"):
            make(twice).resolve(environ={}, layers=[layer({"port": 1, "bind_port": 2})])

    def test_an_aliass_coercion_error_names_its_source(self) -> None:
        with pytest.raises(
            ConfigError, match=r"^listen\.port: expected int, got 'x' \(from APP_PORT\)$"
        ):
            make(OLD_PORT).resolve(environ={"APP_PORT": "x"}, layers=[])
        with pytest.raises(ConfigError, match=rf"expected int, got 'x' \(from {ETC}\)$"):
            make(OLD_PORT).resolve(environ={}, layers=[layer({"listen": {"port": 1}, "port": "x"})])
        payload = layer({"port": "x"}, origins={"port": "payload.port"})
        with pytest.raises(ConfigError, match=r"got 'x' \(from payload\.port\)$"):
            make(OLD_PORT).resolve(environ={}, layers=[payload])

    def test_an_aliass_check_error_names_its_layer(self) -> None:
        checked = Setting("listen.port", 8080, "", check=port, aliases=("port",))
        with pytest.raises(
            ConfigError, match=rf"^listen\.port: 0 is not a TCP port \(from {ETC}\)$"
        ):
            make(checked).resolve(environ={}, layers=[layer({"port": 0})])


class TestAliasAcrossLevels:
    def test_alias_variable_beats_the_key_in_the_file(self) -> None:
        r = make(OLD_PORT).resolve(
            environ={"APP_PORT": "2"}, layers=[layer({"listen": {"port": 1}})]
        )
        assert (r.values["listen.port"], r.sources["listen.port"]) == (2, "APP_PORT")

    def test_alias_file_variable_beats_the_key_variable(self, tmp_path: Path) -> None:
        (tmp_path / "port").write_text("3", encoding="utf-8")
        environ = {"APP_PORT_FILE": str(tmp_path / "port"), "APP_LISTEN_PORT": "2"}
        r = make(OLD_PORT).resolve(environ=environ, layers=[])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (3, "APP_PORT_FILE")

    def test_an_alias_in_a_higher_layer_beats_the_key_in_a_lower_one(self) -> None:
        upper = layer({"port": 1}, source="control plane", where="control plane (config v12)")
        lower = layer({"listen": {"port": 2}})
        r = make(OLD_PORT).resolve(environ={}, layers=[upper, lower])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (1, "control plane")

    def test_the_key_variable_beats_an_alias_in_the_file_which_still_warns(self) -> None:
        # Shadowed, not used: different values at different levels are no conflict.
        r = make(OLD_PORT).resolve(environ={"APP_LISTEN_PORT": "2"}, layers=[layer({"port": 1})])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (2, "APP_LISTEN_PORT")
        assert r.warnings == (alias_warning("port", ETC, "listen.port"),)

    def test_every_alias_set_warns_used_or_shadowed(self) -> None:
        twice = Setting("listen.port", 8080, "", aliases=("port", "bind.port"))
        upper = layer({"listen": {"port": 1}, "bind": {"port": 1}}, where="/upper.yaml")
        lower = layer({"port": 5, "bind": {"port": 6}})
        environ = {"APP_PORT": "1", "APP_BIND_PORT_FILE": "/nonexistent/x"}
        with pytest.raises(ConfigError, match="cannot read APP_BIND_PORT_FILE"):
            make(twice).resolve(environ=environ, layers=[upper, lower])
        del environ["APP_BIND_PORT_FILE"]
        r = make(twice).resolve(environ=environ, layers=[upper, lower])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (1, "APP_PORT")
        assert r.warnings == (
            alias_warning("bind.port", "/upper.yaml", "listen.port"),
            alias_warning("port", ETC, "listen.port"),
            alias_warning("bind.port", ETC, "listen.port"),
            alias_warning("APP_PORT", "the environment", "APP_LISTEN_PORT"),
        )


class TestAliasWarnings:
    def test_aliases_are_known_names_never_unknown(self, tmp_path: Path) -> None:
        moved = Setting("listen.port", 8080, "", aliases=("old.port",))
        (tmp_path / "port").write_text("1", encoding="utf-8")
        data = {"old": {"port": 1, "prot": 1}, "listen": {"port": 1}}
        environ = {"APP_OLD_PORT": "1", "APP_OLD_PORT_FILE": str(tmp_path / "port")}
        r = make(moved).resolve(environ=environ, layers=[layer(data)])
        assert [(w.kind, w.name) for w in r.warnings] == [
            ("alias", "old.port"),
            ("unknown", "old.prot"),  # the control: below an alias's parent, still checked
            ("alias", "APP_OLD_PORT"),
            ("alias", "APP_OLD_PORT_FILE"),
        ]

    def test_an_alias_is_never_suggested(self) -> None:
        renamed = Setting("listen.port", 8080, "", aliases=("port", "old.port"))
        data = {"prot": 1, "old": {"prot": 1}}
        assert warnings_of(make(renamed), layer(data), environ={"APP_PROT": "1"}) == [
            f"unknown key 'prot' in {ETC}",
            f"unknown key 'old.prot' in {ETC}",
            "unknown variable 'APP_PROT' in the environment",
        ]
        # The control: with `port` the current name, each typo gets its suggestion.
        current = make(Setting("port", 8080, ""))
        assert warnings_of(current, layer({"prot": 1}), environ={"APP_PROT": "1"}) == [
            f"unknown key 'prot' in {ETC}: did you mean 'port'?",
            "unknown variable 'APP_PROT' in the environment: did you mean 'APP_PORT'?",
        ]

    def test_a_flat_dotted_alias_is_unknown_and_unread(self) -> None:
        moved = Setting("listen.port", 8080, "", aliases=("old.port",))
        r = make(moved).resolve(environ={}, layers=[layer({"old.port": 1})])
        assert r.values["listen.port"] == 8080
        assert [(w.kind, w.name) for w in r.warnings] == [("unknown", "old.port")]
        # The control: nested, as resolution reads it, the alias is read and warns as one.
        r = make(moved).resolve(environ={}, layers=[layer({"old": {"port": 1}})])
        assert r.values["listen.port"] == 1
        assert [(w.kind, w.name) for w in r.warnings] == [("alias", "old.port")]

    def test_with_no_prefix_alias_variables_still_warn(self) -> None:
        settings = Settings("", (OLD_PORT,), default_path=DEFAULT)
        r = settings.resolve(environ={"PORT": "1", "PATH": "/bin"}, layers=[])
        assert (r.values["listen.port"], r.sources["listen.port"]) == (1, "PORT")
        assert r.warnings == (alias_warning("PORT", "the environment", "LISTEN_PORT"),)

    def test_order_layers_in_document_order_then_variables_by_name(self) -> None:
        data = {"z": 1, "port": 1, "a": 1}
        environ = {"APP_Q": "", "APP_PORT": "1", "APP_B": ""}
        r = make(OLD_PORT).resolve(environ=environ, layers=[layer(data)])
        assert [(w.kind, w.name) for w in r.warnings] == [
            ("unknown", "z"),
            ("alias", "port"),
            ("unknown", "a"),
            ("unknown", "APP_B"),
            ("alias", "APP_PORT"),
            ("unknown", "APP_Q"),
        ]


class TestAliasDeclaration:
    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            ((Setting("a", "", "", aliases="b"),), "'a': aliases are a tuple of dotted keys"),  # type: ignore[arg-type]  # the wrong type, on purpose
            ((Setting("a", "", "", aliases=["b"]),), "'a': aliases are a tuple of dotted keys"),  # type: ignore[arg-type]  # the wrong type, on purpose
            ((Setting("a", "", "", aliases=("B",)),), "'a': alias 'B': a key is lower-case"),
            ((Setting("a", "", "", aliases=("b..c",)),), "'a': alias 'b..c': a key is lower"),
            ((Setting("a", "", "", aliases=(1,)),), "'a': alias 1: a key is lower-case"),  # type: ignore[arg-type]  # the wrong type, on purpose
            (
                (Setting("a", "", "", aliases=("x.on",)),),
                "'a': alias 'x.on': YAML doesn't read 'on'",
            ),
            ((Setting("a", "", "", aliases=("a",)),), "'a': alias 'a' is a setting's key"),
            (
                (Setting("a", "", "", aliases=("b",)), Setting("b", "", "")),
                "'a': alias 'b' is a setting's key",
            ),
            ((Setting("a", "", "", aliases=("b", "b")),), "'a': alias 'b' named twice"),
            (
                (Setting("a", "", "", aliases=("c",)), Setting("b", "", "", aliases=("c",))),
                "'b': alias 'c' is already an alias of 'a'",
            ),
            (
                (Setting("listen.port", 0, "", aliases=("listen",)),),
                "'listen.port': alias 'listen' and 'listen.port': one is a parent of the other",
            ),
            (
                (Setting("a", "", "", aliases=("a.old",)),),
                "'a': alias 'a.old' and 'a': one is a parent of the other",
            ),
            (
                (Setting("a", "", "", aliases=("x.y", "x")),),
                "'a': alias 'x' and 'x.y': one is a parent of the other",
            ),
            (
                (Setting("a", "", "", aliases=("x",)), Setting("b", "", "", aliases=("x.y",))),
                "'b': alias 'x.y' and 'x': one is a parent of the other",
            ),
            (
                (Setting("listen.port", 0, "", aliases=("listen_port",)),),
                "'listen.port': alias 'listen_port': APP_LISTEN_PORT is also read by 'listen.port'",
            ),
            ((Setting("a", "", "", aliases=("config",)),), "APP_CONFIG is also the config file"),
            (
                (Setting("x", "", ""), Setting("a", "", "", aliases=("x_file",))),
                "'a': alias 'x_file': APP_X_FILE is also read by 'x'",
            ),
            (
                (Setting("a", "", "", aliases=("p.q",)), Setting("b", "", "", aliases=("p_q",))),
                "'b': alias 'p_q': APP_P_Q is also read by 'a' (its alias 'p.q')",
            ),
            (
                (Setting("a", "", "", aliases=("p",)), Setting("b", "", "", aliases=("p_file",))),
                "'b': alias 'p_file': APP_P_FILE is also read by 'a' (its alias 'p')",
            ),
        ],
    )
    def test_rejects(self, settings: tuple[Setting, ...], message: str) -> None:
        with pytest.raises(ValueError, match=re.escape(message)):
            make(*settings)

    def test_extra_env_may_not_name_an_alias_variable(self) -> None:
        with pytest.raises(ValueError, match=re.escape("extra_env 'APP_PORT': already read by")):
            Settings("APP_", (OLD_PORT,), default_path=DEFAULT, extra_env=("APP_PORT",))

    def test_accepts_aliases_that_share_a_parent_with_keys(self) -> None:
        settings = make(
            Setting("listen.port", 0, "", aliases=("old.port", "port")),
            Setting("listen.host", "", "", aliases=("old.host",)),
        )
        assert [s.aliases for s in settings] == [("old.port", "port"), ("old.host",)]
        r = settings.resolve(environ={}, layers=[layer({"old": {"port": 1, "host": "h"}})])
        assert dict(r.values) == {"listen.port": 1, "listen.host": "h"}


class TestAliasSecrets:
    TOKEN = Setting("proxy.auth", "", "", secret=True, aliases=("auth",))

    def test_a_secret_alias_resolves_masked(self) -> None:
        r = make(self.TOKEN).resolve(environ={"APP_AUTH": VALUE}, layers=[])
        assert r.secret("proxy.auth").reveal() == VALUE
        assert r.sources["proxy.auth"] == "APP_AUTH"
        assert VALUE not in f"{r!r} {r.warnings}"

    def test_a_secret_aliass_value_is_in_no_message(self) -> None:
        cases: list[tuple[Mapping[str, str], list[Layer]]] = [
            ({"APP_PROXY_AUTH": VALUE, "APP_AUTH": VALUE + "x"}, []),
            ({}, [layer({"proxy": {"auth": VALUE}, "auth": VALUE + "x"})]),
            ({}, [layer({"auth": [VALUE]})]),
        ]
        for environ, layers in cases:
            with pytest.raises(ConfigError) as e:
                make(self.TOKEN).resolve(environ=environ, layers=layers)
            assert "proxy.auth" in str(e.value)
            assert VALUE not in str(e.value)


# --- ${VAR} expansion (design 0009, step 4) -----------------------------------------------------

URL = Setting("upstream.url", "", "Upstream.")
HOSTS = Setting("hosts", "", "Hosts.", is_list=True)


def expanding(*settings: Setting, **kwargs: Any) -> Settings:
    return Settings("APP_", settings, default_path=DEFAULT, expand=True, **kwargs)


def resolve_expanding(
    tmp_path: Path,
    yaml_text: str,
    *settings: Setting,
    environ: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Resolved:
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return expanding(*settings, **kwargs).resolve(path, environ or {})


def expanded_url(environ: Mapping[str, str] | None = None) -> Value:
    """`${HOST}` from a layer with `expand=True`: proves expansion is on where a test shows a
    value that must not expand."""
    data = {"upstream": {"url": "${HOST}"}}
    r = make(URL).resolve(environ=environ or {"HOST": "h"}, layers=[layer(data, expand=True)])
    return r.values["upstream.url"]


class TestExpansionOptIn:
    def test_off_by_default_a_var_stays_literal(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "upstream: {url: '${HOST}'}\n", URL, environ={"HOST": "h"})
        assert r.values["upstream.url"] == "${HOST}"
        assert make(URL).expand is False and make(URL).expand_unset == "error"
        r = resolve_expanding(tmp_path, "upstream: {url: '${HOST}'}\n", URL, environ={"HOST": "h"})
        assert r.values["upstream.url"] == "h"

    def test_read_file_sets_the_layers_expand(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("a: 1\n", encoding="utf-8")
        assert make(URL).read_file(path, {}).expand is False
        assert expanding(URL).read_file(path, {}).expand is True
        assert Layer({}, source="file", where=ETC).expand is False

    def test_a_layer_without_expand_doesnt_expand_even_when_the_project_does(self) -> None:
        r = expanding(URL).resolve(
            environ={"HOST": "h"}, layers=[layer({"upstream": {"url": "${HOST}"}})]
        )
        assert r.values["upstream.url"] == "${HOST}"
        assert expanded_url() == "h"

    def test_a_layer_with_expand_expands_even_when_the_project_doesnt(self) -> None:
        data = {"upstream": {"url": "${HOST}"}}
        r = make(URL).resolve(environ={"HOST": "h"}, layers=[layer(data, expand=True)])
        assert (r.values["upstream.url"], r.sources["upstream.url"]) == ("h", "file")

    def test_variables_never_expand(self) -> None:
        environ = {"APP_UPSTREAM_URL": "${HOST}", "HOST": "h"}
        r = expanding(URL).resolve(environ=environ, layers=[layer({}, expand=True)])
        assert (r.values["upstream.url"], r.sources["upstream.url"]) == (
            "${HOST}",
            "APP_UPSTREAM_URL",
        )
        assert expanded_url() == "h"

    def test_file_variable_contents_never_expand(self, tmp_path: Path) -> None:
        (tmp_path / "url").write_text("${HOST}\n", encoding="utf-8")
        environ = {"APP_UPSTREAM_URL_FILE": str(tmp_path / "url"), "HOST": "h"}
        r = expanding(URL).resolve(environ=environ, layers=[layer({}, expand=True)])
        assert r.values["upstream.url"] == "${HOST}"
        assert expanded_url() == "h"

    def test_the_default_never_expands(self) -> None:
        r = expanding(Setting("a", "${HOST}", "")).resolve(environ={"HOST": "h"}, layers=[])
        assert r.values["a"] == "${HOST}"
        assert expanded_url() == "h"

    @pytest.mark.parametrize("bad", ["", "Error", "unset", None])
    def test_expand_unset_is_validated(self, bad: Any) -> None:
        with pytest.raises(ValueError, match=r'^expand_unset: "error" or "empty", not '):
            Settings("APP_", (URL,), default_path=DEFAULT, expand_unset=bad)


class TestExpansionSyntax:
    @pytest.mark.parametrize(
        ("written", "environ", "expected"),
        [
            ("${HOST}", {"HOST": "h"}, "h"),
            ("https://${HOST}:${PORT}/x", {"HOST": "h", "PORT": "1"}, "https://h:1/x"),
            ("${HOST:-d}", {}, "d"),
            ("${HOST:-d}", {"HOST": ""}, "d"),
            ("${HOST:-d}", {"HOST": "h"}, "h"),
            ("${HOST:-}", {}, ""),
            ("${HOST}", {"HOST": ""}, ""),
            ("${_h1:-a b:c}", {}, "a b:c"),
            ("$${HOST}", {"HOST": "h"}, "${HOST}"),
            ("a$${HOST}${HOST}", {"HOST": "h"}, "a${HOST}h"),
            ("$HOST $ $x} ${HOST}", {"HOST": "h"}, "$HOST $ $x} h"),
            ("${HOST:-a}b}", {}, "ab}"),
            ("${HOST:-${X}}", {"X": "x"}, "${X}"),
        ],
    )
    def test_expands(
        self, tmp_path: Path, written: str, environ: Mapping[str, str], expected: str
    ) -> None:
        r = expanding(URL).resolve(
            environ=environ, layers=[layer({"upstream": {"url": written}}, expand=True)]
        )
        assert r.values["upstream.url"] == expected

    @pytest.mark.parametrize(
        ("written", "at"),
        [("${HOST", 1), ("x${}", 2), ("${vars.x}", 1), ("${1A}", 1), ("${A:x}", 1), ("ok ${", 4)],
    )
    def test_any_other_dollar_brace_is_an_error(self, written: str, at: int) -> None:
        with pytest.raises(ConfigError) as e:
            expanding(URL).resolve(
                environ={}, layers=[layer({"upstream": {"url": written}}, expand=True)]
            )
        assert str(e.value) == (
            f"upstream.url: a ${{ at character {at} that isn't ${{NAME}} or ${{NAME:-default}} "
            f"(from {ETC}): write $${{ for a literal ${{"
        )

    def test_unset_with_no_default_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("upstream: {url: 'https://${HOST}'}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as e:
            expanding(URL).resolve(path, {})
        assert (
            str(e.value) == f"upstream.url: ${{HOST}} is not set and has no default (from {path})"
        )

    def test_unset_names_the_finer_origin(self) -> None:
        payload = layer(
            {"upstream": {"url": "${HOST}"}}, origins={"upstream.url": "line 3"}, expand=True
        )
        with pytest.raises(ConfigError, match=r"has no default \(from line 3\)$"):
            expanding(URL).resolve(environ={}, layers=[payload])

    def test_unset_with_empty_expands_to_the_empty_string(self, tmp_path: Path) -> None:
        r = resolve_expanding(tmp_path, "upstream: {url: 'a${HOST}b'}\n", URL, expand_unset="empty")
        assert r.values["upstream.url"] == "ab"

    def test_the_environment_is_read_on_every_resolve(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("upstream: {url: '${HOST}'}\n", encoding="utf-8")
        settings = expanding(URL)
        assert settings.resolve(path, {"HOST": "a"}).values["upstream.url"] == "a"
        assert settings.resolve(path, {"HOST": "b"}).values["upstream.url"] == "b"


class TestExpansionScope:
    def test_list_items_expand_and_keys_dont(self, tmp_path: Path) -> None:
        text = "hosts: ['${A}', 'b', '${B:-c}', 7]\n"
        r = resolve_expanding(tmp_path, text, HOSTS, environ={"A": "a"})
        assert r.values["hosts"] == ("a", "b", "c", "7")
        r = resolve_expanding(tmp_path, "hosts: '${A},x'\n", HOSTS, environ={"A": "a"})
        assert r.values["hosts"] == ("a", "x")

    def test_typing_comes_after(self, tmp_path: Path) -> None:
        listen = "listen: {port: '${PORT:-5000}'}\n"
        assert resolve_expanding(tmp_path, listen, PORT).values["listen.port"] == 5000
        r = resolve_expanding(tmp_path, listen, PORT, environ={"PORT": ""})
        assert r.values["listen.port"] == 5000
        r = resolve_expanding(tmp_path, listen, PORT, environ={"PORT": "6000"})
        assert r.values["listen.port"] == 6000
        flag = Setting("debug", False, "")
        r = resolve_expanding(tmp_path, "debug: '${DEBUG}'\n", flag, environ={"DEBUG": "yes"})
        assert r.values["debug"] is True

    def test_without_expand_the_coercion_error_shows_the_text(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("listen: {port: '${PORT:-5000}'}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as e:
            make(PORT).resolve(path, {})
        assert str(e.value) == f"listen.port: expected int, got '${{PORT:-5000}}' (from {path})"
        assert expanding(PORT).resolve(path, {}).values["listen.port"] == 5000

    def test_non_strings_are_untouched(self, tmp_path: Path) -> None:
        text = "listen: {port: 5000}\nupstream: {url: '${HOST}'}\n"
        r = resolve_expanding(tmp_path, text, PORT, URL, environ={"HOST": "h"})
        assert dict(r.values) == {"listen.port": 5000, "upstream.url": "h"}

    def test_an_unknown_key_never_expands(self, tmp_path: Path) -> None:
        text = "upstream: {url: '${H}', urll: '${X'}\nother: '${NOPE}'\n"
        r = resolve_expanding(tmp_path, text, URL, environ={"H": "x"})
        assert r.values["upstream.url"] == "x"
        assert [w.name for w in r.warnings] == ["upstream.urll", "other"]

    def test_an_unread_setting_in_a_lower_layer_never_expands(self) -> None:
        top = layer({"upstream": {"url": "${H}"}}, expand=True)
        low = layer({"upstream": {"url": "${NOPE}"}}, expand=True)
        r = expanding(URL).resolve(environ={"H": "x"}, layers=[top, low])
        assert r.values["upstream.url"] == "x"

    def test_a_mapping_expands_its_values_and_still_fails_typing_as_written(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("upstream: {url: {'${K}': '${V}'}}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"\$\{V\} is not set"):
            expanding(URL).resolve(path, {"K": "k"})
        with pytest.raises(ConfigError) as e:
            expanding(URL).resolve(path, {"V": "v"})
        assert (
            str(e.value) == f"upstream.url: expected str, got {{'${{K}}': '${{V}}'}} (from {path})"
        )

    def test_an_alias_value_expands(self) -> None:
        data = {"port": "${PORT}"}
        r = expanding(OLD_PORT).resolve(environ={"PORT": "9000"}, layers=[layer(data, expand=True)])
        assert r.values["listen.port"] == 9000

    def test_an_alias_conflict_compares_expanded_typed_values(self) -> None:
        data = {"listen": {"port": 9000}, "port": "${PORT}"}
        r = expanding(OLD_PORT).resolve(environ={"PORT": "9000"}, layers=[layer(data, expand=True)])
        assert r.values["listen.port"] == 9000
        with pytest.raises(ConfigError) as e:
            expanding(OLD_PORT).resolve(environ={"PORT": "9001"}, layers=[layer(data, expand=True)])
        assert str(e.value) == (
            f"listen.port: set as both 'listen.port' and its old name 'port' (from {ETC}), "
            "to different values"
        )
        assert "9001" not in str(e.value)

    def test_an_aliass_coercion_error_shows_its_text(self) -> None:
        # The key's value expands (else it would be the error); the alias's fails as written.
        data = {"listen": {"port": "${P}"}, "port": "${PORT}x"}
        environ = {"P": "1", "PORT": "1"}
        with pytest.raises(
            ConfigError, match=r"^listen\.port: expected int, got '\$\{PORT\}x' \(from"
        ):
            expanding(OLD_PORT).resolve(environ=environ, layers=[layer(data, expand=True)])


class TestExpansionMessages:
    def test_a_coercion_error_shows_the_text_as_written(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("listen: {port: 'https://${HOST}:x'}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as e:
            expanding(PORT).resolve(path, {"HOST": "internal.example"})
        assert str(e.value) == f"listen.port: expected int, got 'https://${{HOST}}:x' (from {path})"
        assert "internal.example" not in str(e.value)
        path.write_text("listen: {port: '${PORT}'}\n", encoding="utf-8")
        assert expanding(PORT).resolve(path, {"PORT": "1"}).values["listen.port"] == 1

    def test_a_check_error_shows_the_text_as_written(self) -> None:
        mode = Setting("mode", "", "", check=one_of("fast", "slow"))
        data = {"mode": "${MODE}-ish"}
        with pytest.raises(ConfigError) as e:
            expanding(mode).resolve(environ={"MODE": VALUE}, layers=[layer(data, expand=True)])
        assert str(e.value) == f"mode: expected one of fast, slow, got '${{MODE}}-ish' (from {ETC})"
        # `one_of` replaces the value with its declared spelling, after expansion.
        r = expanding(mode).resolve(
            environ={"MODE": "FAST"}, layers=[layer({"mode": "${MODE}"}, expand=True)]
        )
        assert r.values["mode"] == "fast"

    def test_an_int_check_error_shows_the_text_as_written(self) -> None:
        listen = Setting("listen.port", 8080, "", check=port)
        data = {"listen": {"port": "${PORT:-70000}"}}
        with pytest.raises(ConfigError) as e:
            expanding(listen).resolve(environ={}, layers=[layer(data, expand=True)])
        assert str(e.value) == f"listen.port: ${{PORT:-70000}} is not a TCP port (from {ETC})"
        # A value that didn't change in expansion shows as it is.
        unchanged = {"listen": {"port": 70000}}
        with pytest.raises(ConfigError, match=r"^listen\.port: 70000 is not a TCP port"):
            expanding(listen).resolve(environ={}, layers=[layer(unchanged, expand=True)])

    def test_a_list_check_error_shows_each_item_as_written(self) -> None:
        def no_secret(value: Value) -> None:
            raise ValueError(f"bad hosts {value!r}")

        hosts = Setting("hosts", "", "", is_list=True, check=no_secret)
        data = {"hosts": ["a", "${H}", "${EMPTY}"]}
        with pytest.raises(ConfigError) as e:
            expanding(hosts).resolve(
                environ={"H": VALUE, "EMPTY": ""}, layers=[layer(data, expand=True)]
            )
        assert str(e.value) == f"hosts: bad hosts ('a', '${{H}}') (from {ETC})"
        with pytest.raises(ConfigError) as e:
            expanding(hosts).resolve(
                environ={"H": VALUE}, layers=[layer({"hosts": "a,${H}"}, expand=True)]
            )
        assert str(e.value) == f"hosts: bad hosts ('a', '${{H}}') (from {ETC})"

    def test_a_list_item_a_variable_split_is_masked(self) -> None:
        def echo(value: Value) -> None:
            raise ValueError(f"bad hosts {value!r}")

        hosts = Setting("hosts", "", "", is_list=True, check=echo)
        data = {"hosts": "a,${H}"}
        with pytest.raises(ConfigError) as e:
            expanding(hosts).resolve(environ={"H": "x,y-secret"}, layers=[layer(data, expand=True)])
        assert str(e.value) == f"hosts: bad hosts ('a', '{MASK}', '{MASK}') (from {ETC})"

    def test_no_variable_value_in_any_message(self, tmp_path: Path) -> None:
        token = Setting("proxy.auth", "", "", secret=True, check=one_of("nope"))
        cases: list[tuple[Setting, Any]] = [
            (URL, "${PASS"),
            (URL, "${NOPE}"),
            (PORT, "https://user:${PASS}@host"),  # a non-secret value embedding a secret variable
            (Setting("upstream.url", "", "", check=one_of("x")), "https://user:${PASS}@host"),
            (token, "${PASS}"),
            (token, "x${PASS}"),
            (Setting("proxy.auth", 0, "", secret=False), "${PASS}"),
        ]
        for setting, written in cases:
            data: dict[str, Any] = {}
            node = data
            *parents, leaf = setting.key.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = written
            with pytest.raises(ConfigError) as e:
                expanding(setting).resolve(
                    environ={"PASS": VALUE}, layers=[layer(data, expand=True)]
                )
            assert setting.key in str(e.value)
            assert VALUE not in str(e.value) and "hunter2" not in str(e.value)

    def test_a_secret_setting_written_as_a_variable(self) -> None:
        token = Setting("proxy.auth", "", "", secret=True)
        data = {"proxy": {"auth": "${API_TOKEN}"}}
        r = expanding(token).resolve(
            environ={"API_TOKEN": VALUE}, layers=[layer(data, expand=True)]
        )
        assert r.secret("proxy.auth").reveal() == VALUE
        assert r.sources["proxy.auth"] == "file"
        assert VALUE not in repr(r)

    def test_a_secret_coercion_error_shows_nothing(self) -> None:
        token = Setting("proxy.auth", "", "", secret=True)
        data = {"proxy": {"auth": ["${API_TOKEN}"]}}
        with pytest.raises(ConfigError) as e:
            expanding(token).resolve(
                environ={"API_TOKEN": VALUE}, layers=[layer(data, expand=True)]
            )
        assert str(e.value) == f"proxy.auth: expected str (from {ETC})"
        plain = {"proxy": {"auth": "${API_TOKEN}"}}
        r = expanding(token).resolve(
            environ={"API_TOKEN": VALUE}, layers=[layer(plain, expand=True)]
        )
        assert r.secret("proxy.auth").reveal() == VALUE


# --- sections (design 0009, step 5) --------------------------------------------------------------

MW = Setting("middleware.config", "", "Per-middleware options.", is_section=True)
REVERSE = Setting("reverse", "", "Reverse-proxy listeners.", is_section=True, is_list=True)
CONTROL = "control plane (config v12)"


def control(data: Mapping[str, Any], **kwargs: Any) -> Layer:
    """A layer as a managed-mode project builds one from a control-plane payload."""
    return Layer(data, source="control plane", where=CONTROL, **kwargs)


def secret_file(tmp_path: Path, text: str = VALUE + "\n", name: str = "token") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestSectionDeclaration:
    def test_is_section_is_public_and_off_by_default(self) -> None:
        assert make(MW)[0].is_section is True
        assert Setting("a", "", "").is_section is False
        assert make(MW).resolve(DEFAULT, {}).values["middleware.config"] == Section()

    @pytest.mark.parametrize("is_list", [False, True])
    def test_the_default_must_be_empty(self, is_list: bool) -> None:
        with pytest.raises(ValueError, match=r"'s': a section setting's default must be \"\"$"):
            make(Setting("s", "x", "", is_section=True, is_list=is_list))
        with pytest.raises(ValueError, match="default must be"):
            make(Setting("s", 0, "", is_section=True))

    def test_a_section_cant_be_secret(self) -> None:
        with pytest.raises(ValueError, match=r"'s': a section can't be secret: use secret_file"):
            make(Setting("s", "", "", is_section=True, secret=True))

    @pytest.mark.parametrize(
        "other",
        [
            Setting("middleware.config.x", "", ""),  # below the section
            Setting("middleware", "", ""),  # above it
        ],
    )
    @pytest.mark.parametrize("section_first", [True, False])
    def test_a_section_is_no_parent_or_child_of_a_key(
        self, other: Setting, section_first: bool
    ) -> None:
        settings = (MW, other) if section_first else (other, MW)
        with pytest.raises(ValueError) as info:
            make(*settings)
        assert str(info.value) == (
            f"'middleware.config': a section, and {other.key!r}: one is a parent of the other"
        )

    def test_only_a_parent_or_child_clashes_not_a_sibling(self) -> None:
        make(MW, Setting("middleware.order", "", ""), Setting("middleware.configs", "", ""))
        with pytest.raises(ValueError, match=r"a section, and 'middleware\.config\.x'"):
            make(MW, Setting("middleware.order", "", ""), Setting("middleware.config.x", "", ""))

    def test_aliases_clash_with_a_section_as_with_any_key(self) -> None:
        # A section's own subtree check first (new), then the alias rule applied to sections.
        with pytest.raises(ValueError, match=r"'old': a section, and 'old\.x'"):
            make(Setting("old", "", "", is_section=True), Setting("old.x", "", ""))
        moved = Setting("x", "", "", aliases=("middleware.config.x",))
        with pytest.raises(ValueError, match="one is a parent of the other"):
            make(MW, moved)
        moved = Setting("mw", "", "", is_section=True, aliases=("old",))
        with pytest.raises(ValueError, match="one is a parent of the other"):
            make(moved, Setting("old.x", "", ""))


class TestSectionValue:
    def test_freezes_nested_mappings_and_lists(self) -> None:
        day = datetime.date(2026, 11, 4)
        when = datetime.datetime(2026, 11, 4, 10, 0)
        s = Section({"a": {"b": [1, {"c": None}], "t": (True, 1.5)}, "d": day, "w": when})
        assert isinstance(s["a"], Section)
        a: Any = s["a"]  # a Node: Any to index it further
        assert a["b"] == (1, Section({"c": None}))
        assert isinstance(a["b"][1], Section)
        assert a["t"] == (True, 1.5)
        assert (s["d"], s["w"]) == (day, when)
        assert list(s) == ["a", "d", "w"]
        assert len(s) == 3
        assert "a" in s
        assert s.get("nope") is None
        assert Section() == Section({}) and len(Section()) == 0

    def test_a_section_value_is_kept_as_it_is(self) -> None:
        inner = Section({"x": 1})
        assert Section({"i": inner})["i"] is inner

    def test_a_non_str_key_is_a_type_error(self) -> None:
        with pytest.raises(TypeError, match="a Section's keys are str, not int"):
            Section({1: "x"})  # type: ignore[dict-item]  # the point: a non-str key
        with pytest.raises(TypeError, match="not bool"):
            Section({"a": {True: "x"}})

    @pytest.mark.parametrize("value", [b"x", {"a"}, object()])
    def test_another_type_is_a_type_error(self, value: object) -> None:
        with pytest.raises(TypeError, match="a Section holds mappings, lists and scalars, not"):
            Section({"a": [value]})

    def test_it_cant_be_changed_at_any_depth(self) -> None:
        s = Section({"a": {"b": [1, {"c": 2}]}, "l": [1]})
        with pytest.raises(TypeError):
            s["a"] = 1  # type: ignore[index]  # the point: a Section has no __setitem__
        with pytest.raises(TypeError):
            del s["a"]  # type: ignore[attr-defined]  # nor __delitem__
        with pytest.raises(TypeError):
            cast(Any, s)["a"]["b"][1]["c"] = 3  # nested too
        with pytest.raises(AttributeError):
            s["l"].append(2)  # type: ignore[union-attr]  # a list is a tuple
        with pytest.raises(AttributeError, match="read-only"):
            s._items = {}  # the slots are blocked
        with pytest.raises(AttributeError, match="read-only"):
            del s._items
        with pytest.raises(AttributeError, match="read-only"):
            s.extra = 1
        with pytest.raises(TypeError):
            s._items["a"] = 1  # type: ignore[index]  # a read-only view
        with pytest.raises(AttributeError, match="read-only"):
            s.__init__({"a": 2})  # type: ignore[misc]  # the point: init twice is refused
        assert s["l"] == (1,)
        assert not hasattr(s, "__dict__")

    def test_freezing_copies_so_the_source_cant_change_it(self) -> None:
        source: dict[str, Any] = {"a": [1], "b": {"c": 1}}
        s = Section(source)
        source["a"].append(2)
        source["b"]["c"] = 2
        source["d"] = 1
        assert s == Section({"a": [1], "b": {"c": 1}})

    def test_equal_content_is_equal_and_hashes_the_same(self) -> None:
        a = Section({"x": 1, "n": {"l": [1, "two"]}, "k": Secret(VALUE)})
        b = Section({"n": {"l": (1, "two")}, "k": Secret(VALUE), "x": 1})  # another order
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1
        assert a != Section({"x": 1, "n": {"l": [1, "two"]}, "k": Secret("other")})
        assert a != Section({"x": 2, "n": {"l": [1, "two"]}, "k": Secret(VALUE)})

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (1, True),
            (1, 1.0),
            (0, False),
            ("1", 1),
            (datetime.date(2026, 1, 1), datetime.datetime(2026, 1, 1)),
            ([1], [True]),
            ({"a": 1}, {"a": True}),
        ],
    )
    def test_values_of_another_type_differ(self, left: object, right: object) -> None:
        # Python says 1 == True == 1.0; a config that changes `1` to `true` has changed.
        assert Section({"v": left}) != Section({"v": right})

    def test_nan_equals_nan_and_hashes_the_same(self) -> None:
        # Issue #49: nan != nan, and two NaN objects hash apart, so equality maps every NaN to one.
        # PyYAML gives one shared object for every `.nan`, so these are built apart.
        yaml_nan = Section(yaml.safe_load("x: .nan"))
        for other in (float("nan"), math.nan, json.loads("NaN"), -float("nan")):
            assert other is not yaml_nan["x"]
            assert Section({"x": other}) == yaml_nan
            assert hash(Section({"x": other})) == hash(yaml_nan)
        assert len({Section({"x": float("nan")}), Section({"x": float("nan")})}) == 1

    def test_a_nan_with_another_payload_or_sign_is_still_nan(self) -> None:
        payload = struct.unpack("<d", struct.pack("<Q", 0x7FF8000000000001))[0]
        negative = struct.unpack("<d", struct.pack("<Q", 0xFFF8000000000000))[0]
        assert math.isnan(payload) and math.isnan(negative) and math.copysign(1, negative) < 0
        nan = Section(yaml.safe_load("x: .nan"))
        assert Section({"x": payload}) == nan and hash(Section({"x": payload})) == hash(nan)
        assert Section({"x": negative}) == nan and hash(Section({"x": negative})) == hash(nan)

    def test_a_nan_nested_in_a_list_or_a_mapping_equals_nan(self) -> None:
        a = Section({"l": [1, float("nan")], "m": {"n": float("nan"), "l": [{"x": float("nan")}]}})
        b = Section({"l": [1, float("nan")], "m": {"n": float("nan"), "l": [{"x": float("nan")}]}})
        assert a == b and hash(a) == hash(b)

    @pytest.mark.parametrize("other", [math.inf, -math.inf, 0.0, "nan", ".nan", "-.nan", None])
    def test_nan_still_differs_from_other_values(self, other: object) -> None:
        # Only a float NaN is NaN: inf, a string spelling it (YAML reads `-.nan` as a str) differ.
        assert Section({"x": float("nan")}) != Section({"x": other})

    def test_inf_still_equals_itself(self) -> None:
        assert Section({"x": float("inf")}) == Section({"x": math.inf})
        assert Section({"x": -math.inf}) != Section({"x": math.inf})

    def test_equal_only_to_a_section(self) -> None:
        assert Section({"a": 1}) != {"a": 1}
        assert dict(Section({"a": 1})) == {"a": 1}

    def test_repr_masks_every_secret(self) -> None:
        s = Section({"token": Secret(VALUE), "n": {"l": [Secret(VALUE)]}, "u": "user"})
        text = repr(s)
        assert VALUE not in text
        assert text == (
            "Section({'token': Secret('********'), 'n': Section({'l': (Secret('********'),)}), "
            "'u': 'user'})"
        )
        assert VALUE not in str(s)

    def test_copy_and_pickle_give_an_equal_section(self) -> None:
        s = Section({"a": {"b": [1, "x"]}})
        assert copy.copy(s) == s
        assert copy.deepcopy(s) == s
        assert pickle.loads(pickle.dumps(s)) == s  # noqa: S301  # our own bytes, just made


class TestSectionSources:
    def test_a_mapping_section_from_the_file(self, tmp_path: Path) -> None:
        text = (
            "middleware:\n  config:\n    auth: {realm: x, users: [a, b], retries: 3}\n"
            "    ratio: 0.5\n    on_: true\n    day: 2026-11-04\n    at: 2026-11-04 10:00:00\n"
            "    none: null\n"
        )
        r = resolve_yaml(tmp_path, text, MW)
        value = r.values["middleware.config"]
        assert value == Section(
            {
                "auth": {"realm": "x", "users": ["a", "b"], "retries": 3},
                "ratio": 0.5,
                "on_": True,
                "day": datetime.date(2026, 11, 4),
                "at": datetime.datetime(2026, 11, 4, 10, 0),
                "none": None,
            }
        )
        assert r.sources["middleware.config"] == "file"
        assert r.warnings == ()

    def test_a_list_section_from_the_file(self, tmp_path: Path) -> None:
        text = "reverse:\n  - {listen: ':80', upstream: a}\n  - {listen: ':81', upstream: b}\n"
        r = resolve_yaml(tmp_path, text, REVERSE)
        assert r.values["reverse"] == (
            Section({"listen": ":80", "upstream": "a"}),
            Section({"listen": ":81", "upstream": "b"}),
        )
        assert r.sources["reverse"] == "file"

    def test_an_empty_mapping_in_the_file_is_an_empty_section(self, tmp_path: Path) -> None:
        r = resolve_yaml(tmp_path, "middleware: {config: {}}\nreverse: []\n", MW, REVERSE)
        assert (r.values["middleware.config"], r.values["reverse"]) == (Section(), ())
        assert dict(r.sources) == {"middleware.config": "file", "reverse": "file"}

    def test_the_default_is_empty(self) -> None:
        r = make(MW, REVERSE).resolve(DEFAULT, {})
        assert r.values["middleware.config"] == Section()
        assert r.values["reverse"] == ()
        assert dict(r.sources) == {"middleware.config": "default", "reverse": "default"}

    def test_a_null_falls_through_to_the_next_layer(self) -> None:
        r = make(MW).resolve(
            environ={},
            layers=[
                control({"middleware": {"config": None}}),
                layer({"middleware": {"config": {"a": 1}}}),
            ],
        )
        assert (r.values["middleware.config"], r.sources["middleware.config"]) == (
            Section({"a": 1}),
            "file",
        )

    def test_the_first_layer_with_it_wins_whole(self) -> None:
        first = control({"middleware": {"config": {"a": 1}}})
        second = layer({"middleware": {"config": {"a": 2, "b": 2}}})
        r = make(MW).resolve(environ={}, layers=[first, second])
        assert r.values["middleware.config"] == Section({"a": 1})  # replaced, never merged
        assert r.sources["middleware.config"] == "control plane"

    def test_from_its_file_variable(self, tmp_path: Path) -> None:
        mounted = tmp_path / "mw.yaml"
        mounted.write_text("auth: {realm: x}\n", encoding="utf-8")
        items = tmp_path / "reverse.yaml"
        items.write_text("- {listen: ':80'}\n", encoding="utf-8")
        environ = {"APP_MIDDLEWARE_CONFIG_FILE": str(mounted), "APP_REVERSE_FILE": str(items)}
        r = resolve_yaml(
            tmp_path, "middleware: {config: {other: 1}}\n", MW, REVERSE, environ=environ
        )
        assert r.values["middleware.config"] == Section({"auth": {"realm": "x"}})
        assert r.values["reverse"] == (Section({"listen": ":80"}),)
        assert dict(r.sources) == {
            "middleware.config": "APP_MIDDLEWARE_CONFIG_FILE",
            "reverse": "APP_REVERSE_FILE",
        }
        assert r.warnings == ()  # the _FILE variables are known

    def test_an_empty_file_is_an_empty_section(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        environ = {"APP_MIDDLEWARE_CONFIG_FILE": str(empty), "APP_REVERSE_FILE": str(empty)}
        r = make(MW, REVERSE).resolve(DEFAULT, environ)
        assert (r.values["middleware.config"], r.values["reverse"]) == (Section(), ())
        assert r.sources["reverse"] == "APP_REVERSE_FILE"

    @pytest.mark.parametrize("expand", [True, False])
    def test_its_file_expands_with_the_projects_expand(self, tmp_path: Path, expand: bool) -> None:
        mounted = tmp_path / "mw.yaml"
        mounted.write_text("url: 'http://${HOST}:80'\n", encoding="utf-8")
        settings = Settings("APP_", (MW,), default_path=DEFAULT, expand=expand)
        environ = {"APP_MIDDLEWARE_CONFIG_FILE": str(mounted), "HOST": "h"}
        value = settings.resolve(DEFAULT, environ).section("middleware.config")
        assert value["url"] == ("http://h:80" if expand else "http://${HOST}:80")

    def test_an_unreadable_file_variable_is_an_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.yaml"
        missing.write_text("a: 1\n", encoding="utf-8")  # the control: readable, it is read
        r = make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(missing)})
        assert r.section("middleware.config") == Section({"a": 1})
        missing.unlink()
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(missing)})
        assert str(info.value) == (
            f"cannot read APP_MIDDLEWARE_CONFIG_FILE ({missing}): No such file or directory"
        )

    def test_a_file_variable_that_isnt_utf8_is_an_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_bytes(b"a: " + VALUE.encode() + b"\n")  # the control: UTF-8, it is read
        r = make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(bad)})
        assert r.section("middleware.config") == Section({"a": VALUE})
        bad.write_bytes(b"a: " + VALUE.encode() + b"\xff\n")
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(bad)})
        assert str(info.value) == f"cannot read APP_MIDDLEWARE_CONFIG_FILE ({bad}): not UTF-8"
        assert VALUE not in str(info.value)

    def test_a_file_variable_that_isnt_yaml_names_the_variable_never_the_text(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(f"a: [{VALUE}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(bad)})
        message = str(info.value)
        assert message.startswith(f"cannot parse APP_MIDDLEWARE_CONFIG_FILE ({bad}) at line ")
        assert VALUE not in message

    @pytest.mark.parametrize(
        ("setting", "text", "expected"),
        [
            (MW, "- a\n", "expected a section (a mapping), got a list"),
            (MW, "just text\n", "expected a section (a mapping), got str"),
            (REVERSE, "a: 1\n", "expected a list of sections (mappings), got a mapping"),
        ],
    )
    def test_a_file_variable_of_the_wrong_shape_is_an_error(
        self, tmp_path: Path, setting: Setting, text: str, expected: str
    ) -> None:
        mounted = tmp_path / "s.yaml"
        mounted.write_text(text, encoding="utf-8")
        var = f"APP_{setting.key.upper().replace('.', '_')}_FILE"
        with pytest.raises(ConfigError) as info:
            make(setting).resolve(DEFAULT, {var: str(mounted)})
        assert str(info.value) == f"{setting.key}: {expected} (from {var} ({mounted}))"

    @pytest.mark.parametrize("with_file", [False, True])
    def test_a_plain_variable_is_an_error_saying_to_use_file(
        self, tmp_path: Path, with_file: bool
    ) -> None:
        environ = {"APP_MIDDLEWARE_CONFIG": "{auth: {}}"}
        if with_file:  # set beside a _FILE, it is still a mistake, never silently ignored
            mounted = tmp_path / "mw.yaml"
            mounted.write_text("{}\n", encoding="utf-8")
            environ["APP_MIDDLEWARE_CONFIG_FILE"] = str(mounted)
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(DEFAULT, environ)
        assert str(info.value) == (
            "middleware.config: APP_MIDDLEWARE_CONFIG is set, but a section is read from a YAML "
            "file, never a variable: set APP_MIDDLEWARE_CONFIG_FILE to the file's path"
        )

    def test_a_plain_variable_for_a_list_section_is_an_error_too(self) -> None:
        with pytest.raises(ConfigError, match="APP_REVERSE is set, but a section is read"):
            make(REVERSE).resolve(DEFAULT, {"APP_REVERSE": "a,b"})


class TestSectionShape:
    @pytest.mark.parametrize(
        ("setting", "data", "expected"),
        [
            (
                MW,
                {"middleware": {"config": "x"}},
                "middleware.config: expected a section (a mapping), got str",
            ),
            (
                MW,
                {"middleware": {"config": [1]}},
                "middleware.config: expected a section (a mapping), got a list",
            ),
            (
                REVERSE,
                {"reverse": {"a": 1}},
                "reverse: expected a list of sections (mappings), got a mapping",
            ),
            (
                REVERSE,
                {"reverse": "a,b"},
                "reverse: expected a list of sections (mappings), got str",
            ),
            (
                REVERSE,
                {"reverse": [{"a": 1}, "x"]},
                "reverse[1]: expected a section (a mapping), got str",
            ),
            (REVERSE, {"reverse": [[1]]}, "reverse[0]: expected a section (a mapping), got a list"),
        ],
    )
    def test_the_wrong_shape_is_an_error(
        self, setting: Setting, data: Mapping[str, Any], expected: str
    ) -> None:
        with pytest.raises(ConfigError) as info:
            make(setting).resolve(environ={}, layers=[layer(data)])
        assert str(info.value) == f"{expected} (from {ETC})"

    @pytest.mark.parametrize(
        ("setting", "text", "path", "kind"),
        [
            (MW, "middleware: {config: {on: 1}}\n", "middleware.config.true", "bool"),
            (MW, "middleware: {config: {a: {1: x}}}\n", "middleware.config.a.1", "int"),
            (
                MW,
                "middleware: {config: {a: [{b: [{null: x}]}]}}\n",
                "middleware.config.a[0].b[0].null",
                "NoneType",
            ),
            (REVERSE, "reverse: [{a: 1}, {no: 1}]\n", "reverse[1].false", "bool"),
        ],
    )
    def test_a_non_string_key_is_an_error_naming_its_path(
        self, tmp_path: Path, setting: Setting, text: str, path: str, kind: str
    ) -> None:
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, text, setting)
        assert str(info.value) == (
            f"{path}: a section's keys are strings, and this one is {kind}: quote it "
            f"(from {tmp_path / 'config.yaml'})"
        )

    @pytest.mark.parametrize(
        ("text", "kind"),
        [("x: !!binary aGk=\n", "bytes"), ("x: !!set {a: null}\n", "set")],
    )
    def test_a_value_yaml_types_as_something_else_is_an_error(
        self, tmp_path: Path, text: str, kind: str
    ) -> None:
        config = "middleware:\n  config:\n    " + text.replace("\n", "\n    ").rstrip() + "\n"
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, config, MW)
        assert str(info.value) == (
            f"middleware.config.x: a section holds mappings, lists and scalars, not {kind} "
            f"(from {tmp_path / 'config.yaml'})"
        )

    def test_a_project_layers_tuples_and_sections_are_accepted(self) -> None:
        data = {"middleware": {"config": {"t": ("a", {"b": 1}), "s": Section({"c": 2})}}}
        r = make(MW).resolve(environ={}, layers=[layer(data)])
        assert r.section("middleware.config") == Section({"t": ["a", {"b": 1}], "s": {"c": 2}})

    def test_a_layer_is_never_changed_or_kept(self) -> None:
        data: dict[str, Any] = {"middleware": {"config": {"a": [1], "b": {"c": 1}}}}
        r = make(MW).resolve(environ={}, layers=[layer(data)])
        data["middleware"]["config"]["a"].append(2)
        data["middleware"]["config"]["b"]["c"] = 2
        assert r.section("middleware.config") == Section({"a": [1], "b": {"c": 1}})
        assert data == {"middleware": {"config": {"a": [1, 2], "b": {"c": 2}}}}

    def test_expansion_reaches_every_leaf_but_no_key(self, tmp_path: Path) -> None:
        text = "middleware: {config: {'${K}': {url: 'http://${HOST}', l: ['${HOST}', 1]}}}\n"
        r = resolve_expanding(tmp_path, text, MW, environ={"HOST": "h", "K": "k"})
        assert r.section("middleware.config") == Section(
            {"${K}": {"url": "http://h", "l": ["h", 1]}}
        )

    def test_expansion_in_a_project_layers_tuple(self) -> None:
        data = {"middleware": {"config": {"t": ("${HOST}",)}}}
        r = make(MW).resolve(environ={"HOST": "h"}, layers=[layer(data, expand=True)])
        assert r.section("middleware.config")["t"] == ("h",)


class TestSectionAccessors:
    def resolved(self) -> Resolved:
        data = {"middleware": {"config": {"a": 1}}, "reverse": [{"l": ":80"}]}
        return make(MW, REVERSE, *KINDS).resolve(
            environ={"APP_L": "a,b", "APP_K": VALUE}, layers=[layer(data)]
        )

    def test_section_and_sections_give_their_kind(self) -> None:
        r = self.resolved()
        assert r.section("middleware.config") == Section({"a": 1})
        assert r.sections("reverse") == (Section({"l": ":80"}),)

    def test_empty_values_keep_their_kind(self) -> None:
        r = make(MW, REVERSE, Setting("l", "", "", is_list=True)).resolve(DEFAULT, {})
        assert r.section("middleware.config") == Section()
        assert r.sections("reverse") == ()
        assert r.list("l") == ()
        # An empty list section's () is not an empty list setting's, nor the reverse.
        with pytest.raises(TypeError, match=r"^'reverse' is a list of sections setting, not list"):
            r.list("reverse")
        with pytest.raises(TypeError, match=r"^'l' is a list setting, not list of sections$"):
            r.sections("l")

    @pytest.mark.parametrize(
        ("accessor", "key", "message"),
        [
            ("section", "s", "'s' is a str setting, not section"),
            ("section", "l", "'l' is a list setting, not section"),
            (
                "section",
                "reverse",
                "'reverse' is a list of sections setting, not section: use sections()",
            ),
            ("section", "k", "'k' is a secret setting, not section: use secret() and reveal()"),
            ("sections", "l", "'l' is a list setting, not list of sections"),
            ("sections", "i", "'i' is a int setting, not list of sections"),
            (
                "sections",
                "middleware.config",
                "'middleware.config' is a section setting, not list of sections: use section()",
            ),
            (
                "str",
                "middleware.config",
                "'middleware.config' is a section setting, not str: use section()",
            ),
            (
                "list",
                "reverse",
                "'reverse' is a list of sections setting, not list: use sections()",
            ),
            (
                "list",
                "middleware.config",
                "'middleware.config' is a section setting, not list: use section()",
            ),
            (
                "secret",
                "middleware.config",
                "'middleware.config' is a section setting, not secret: use section()",
            ),
            ("int", "reverse", "'reverse' is a list of sections setting, not int: use sections()"),
            (
                "bool",
                "middleware.config",
                "'middleware.config' is a section setting, not bool: use section()",
            ),
        ],
    )
    def test_another_kind_is_a_type_error_naming_the_key(
        self, accessor: str, key: str, message: str
    ) -> None:
        with pytest.raises(TypeError) as info:
            getattr(self.resolved(), accessor)(key)
        assert str(info.value) == message

    def test_a_hand_built_resolved_knows_a_section_by_its_value(self) -> None:
        r = Resolved(values={"s": Section({"a": 1}), "l": (Section(),)}, sources={})
        assert r.section("s") == Section({"a": 1})
        assert r.sections("l") == (Section(),)

    @pytest.mark.parametrize("accessor", ["section", "sections"])
    def test_a_missing_key_is_a_key_error(self, accessor: str) -> None:
        with pytest.raises(KeyError):
            getattr(self.resolved(), accessor)("nope")


class TestSectionChecks:
    def test_the_check_receives_the_section(self) -> None:
        seen: list[Value] = []
        mw = Setting("mw", "", "", is_section=True, check=lambda v: seen.append(v))
        items = Setting(
            "items", "", "", is_section=True, is_list=True, check=lambda v: seen.append(v)
        )
        make(mw, items).resolve(environ={}, layers=[layer({"mw": {"a": 1}, "items": [{"b": 2}]})])
        assert seen == [Section({"a": 1}), (Section({"b": 2}),)]

    def test_the_check_runs_on_the_default_too(self) -> None:
        seen: list[Value] = []
        make(Setting("mw", "", "", is_section=True, check=seen.append)).resolve(DEFAULT, {})
        assert seen == [Section()]

    def test_the_check_may_replace_it(self) -> None:
        def defaults(value: Value) -> Value:
            assert isinstance(value, Section)
            return Section({"timeout": 5, **value})

        def first_only(value: Value) -> Value:
            assert isinstance(value, tuple)
            return value[:1]

        mw = Setting("mw", "", "", is_section=True, check=defaults)
        items = Setting("items", "", "", is_section=True, is_list=True, check=first_only)
        r = make(mw, items).resolve(
            environ={}, layers=[layer({"mw": {"a": 1}, "items": [{"b": 1}, {"b": 2}]})]
        )
        assert r.section("mw") == Section({"timeout": 5, "a": 1})
        assert r.sections("items") == (Section({"b": 1}),)

    @pytest.mark.parametrize(
        ("is_list", "returned", "message"),
        [
            (False, {"a": 1}, "mw: its check returned dict, expected Section"),
            (False, (Section(),), "mw: its check returned tuple, expected Section"),
            (True, [Section()], "mw: its check returned list, expected a tuple of Section"),
            (True, ({"a": 1},), "mw: its check returned tuple, expected a tuple of Section"),
            (True, Section(), "mw: its check returned Section, expected a tuple of Section"),
        ],
    )
    def test_a_replacement_of_another_kind_is_an_error(
        self, is_list: bool, returned: object, message: str
    ) -> None:
        mw = Setting("mw", "", "", is_section=True, is_list=is_list, check=lambda v: returned)  # type: ignore[arg-type,return-value]  # the point: a wrong type
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(DEFAULT, {})
        assert str(info.value) == message

    def test_a_check_error_names_the_key_and_where(self) -> None:
        def needs_auth(value: Value) -> None:
            raise ValueError("needs an auth entry")

        mw = Setting("mw", "", "", is_section=True, check=needs_auth)
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(environ={}, layers=[layer({"mw": {"a": 1}})])
        assert str(info.value) == f"mw: needs an auth entry (from {ETC})"
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(DEFAULT, {})
        assert str(info.value) == "mw: needs an auth entry"

    def test_a_check_error_masks_every_secret_leaf(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        other = secret_file(tmp_path, 'it\'s\\ "quoted"\n', name="other")

        def leaky(value: Value) -> None:
            assert isinstance(value, Section)
            deep: Any = value
            revealed = [s.reveal() for s in (deep["a"]["t"], deep["l"][0])]
            raise ValueError(f"bad {revealed[0]} and {revealed!r} and {value!r}")

        mw = Setting("mw", "", "", is_section=True, check=leaky)
        text = f"mw: {{a: {{t: {{secret_file: {token}}}}}, l: [{{secret_file: {other}}}, 1]}}\n"
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, text, mw)
        message = str(info.value)
        assert VALUE not in message
        assert "quoted" not in message
        assert message.startswith(f"mw: bad {MASK} and ['{MASK}', ")

    def test_a_check_error_shows_expanded_leaves_as_written(self, tmp_path: Path) -> None:
        def bad_url(value: Value) -> None:
            assert isinstance(value, Section)
            raise ValueError(f"bad url {value['url']!r} in {value['l']!r}")

        mw = Setting("mw", "", "", is_section=True, check=bad_url)
        text = "mw: {url: 'https://u:${PASS}@h', l: ['${PASS}'], n: 1, s: plain}\n"
        with pytest.raises(ConfigError) as info:
            resolve_expanding(tmp_path, text, mw, environ={"PASS": VALUE})
        message = str(info.value)
        assert VALUE not in message
        assert message == (
            "mw: bad url 'https://u:${PASS}@h' in ('${PASS}',) "
            f"(from {tmp_path / 'config.yaml'})"
        )


class TestSectionUnknownKeys:
    def test_nothing_below_a_section_is_checked_but_a_typo_beside_it_is(self) -> None:
        data = {"middleware": {"config": {"anything": {"at": ["all"]}}, "confg": {"x": 1}}}
        assert warnings_of(make(MW), layer(data)) == [
            f"unknown key 'middleware.confg' in {ETC}: did you mean 'middleware.config'?"
        ]

    def test_nor_below_a_list_section_or_a_sections_alias(self) -> None:
        moved = Setting("mw", "", "", is_section=True, aliases=("old.mw",))
        data = {"reverse": [{"x": 1}], "old": {"mw": {"y": 1}}}
        r = make(REVERSE, moved).resolve(environ={}, layers=[layer(data)])
        assert [(w.kind, w.name) for w in r.warnings] == [("alias", "old.mw")]

    def test_its_variables_are_known(self, tmp_path: Path) -> None:
        mounted = tmp_path / "mw.yaml"
        mounted.write_text("{}\n", encoding="utf-8")
        r = make(MW).resolve(
            DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(mounted), "APP_MIDDLEWARE_CONFG_FILE": ""}
        )
        assert [w.name for w in r.warnings] == ["APP_MIDDLEWARE_CONFG_FILE"]
        assert (r.section("middleware.config"), r.sources["middleware.config"]) == (
            Section(),
            "APP_MIDDLEWARE_CONFIG_FILE",
        )


class TestSectionAliases:
    MOVED = Setting("mw", "", "", is_section=True, aliases=("middleware",))

    def test_an_alias_is_read_with_a_warning(self) -> None:
        r = make(self.MOVED).resolve(environ={}, layers=[layer({"middleware": {"a": 1}})])
        assert r.section("mw") == Section({"a": 1})
        assert r.warnings == (alias_warning("middleware", ETC, "mw"),)

    def test_an_aliass_file_variable_is_read(self, tmp_path: Path) -> None:
        mounted = tmp_path / "mw.yaml"
        mounted.write_text("a: 1\n", encoding="utf-8")
        r = make(self.MOVED).resolve(DEFAULT, {"APP_MIDDLEWARE_FILE": str(mounted)})
        assert (r.section("mw"), r.sources["mw"]) == (Section({"a": 1}), "APP_MIDDLEWARE_FILE")

    def test_an_aliass_plain_variable_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="mw: APP_MIDDLEWARE is set, but a section"):
            make(self.MOVED).resolve(DEFAULT, {"APP_MIDDLEWARE": "x"})

    def test_equal_sections_at_one_level_are_fine(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        again = secret_file(tmp_path, VALUE, name="again")  # the same secret, another file
        data = {
            "mw": {"a": [1, {"t": {"secret_file": str(token)}}]},
            "middleware": {"a": [1, {"t": {"secret_file": str(again)}}]},
        }
        r = make(self.MOVED).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert r.section("mw") == Section({"a": [1, {"t": Secret(VALUE)}]})

    def test_nan_as_both_the_key_and_an_old_name_is_no_conflict(self) -> None:
        # Issue #49: the key and an old name both NaN, each its own object, aren't a conflict.
        data = {
            "mw": {"a": float("nan"), "l": [float("nan")]},
            "middleware": {"a": float("nan"), "l": [float("nan")]},
        }
        assert data["mw"]["a"] is not data["middleware"]["a"]
        r = make(self.MOVED).resolve(environ={}, layers=[control(data)])
        value: Any = r.section("mw")
        assert math.isnan(value["a"]) and math.isnan(value["l"][0])

    @pytest.mark.parametrize("old", [{"a": 2}, {"a": True}, {"a": 1, "b": 1}, {"a": 1.0}])
    def test_different_sections_at_one_level_are_an_error(self, old: dict[str, Any]) -> None:
        data = {"mw": {"a": 1}, "middleware": old}
        with pytest.raises(ConfigError) as info:
            make(self.MOVED).resolve(environ={}, layers=[layer(data)])
        assert str(info.value) == (
            f"mw: set as both 'mw' and its old name 'middleware' (from {ETC}), to different values"
        )

    def test_different_secret_leaves_at_one_level_are_an_error_never_shown(
        self, tmp_path: Path
    ) -> None:
        token = secret_file(tmp_path)
        other = secret_file(tmp_path, "other", name="other")
        data = {
            "mw": {"t": {"secret_file": str(token)}},
            "middleware": {"t": {"secret_file": str(other)}},
        }
        with pytest.raises(ConfigError, match=r"to different values$") as info:
            make(self.MOVED).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert VALUE not in str(info.value)

    def test_an_aliass_secret_leaf_in_a_remote_layer_is_refused(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        data = {"mw": {"a": 1}, "middleware": {"t": {"secret_file": str(token)}}}
        with pytest.raises(ConfigError, match="read only from local configuration"):
            make(self.MOVED).resolve(environ={}, layers=[control(data)])


class TestSecretLeaves:
    def test_a_leaf_is_read_as_a_secret_newline_stripped(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        text = (
            f"middleware:\n  config:\n    auth: {{token: {{secret_file: {token}}}}}\n"
            f"    keys: [{{secret_file: {token}}}, plain]\n"
        )
        value = resolve_yaml(tmp_path, text, MW).section("middleware.config")
        leaf = cast(Any, value)["auth"]["token"]
        assert isinstance(leaf, Secret)
        assert leaf.reveal() == VALUE
        assert value["keys"] == (Secret(VALUE), "plain")
        assert VALUE not in repr(value)

    def test_a_leaf_in_a_list_sections_item(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        text = f"reverse: [{{listen: ':80', auth: {{secret_file: {token}}}}}]\n"
        items = resolve_yaml(tmp_path, text, REVERSE).sections("reverse")
        assert items == (Section({"listen": ":80", "auth": Secret(VALUE)}),)

    def test_a_leaf_in_a_sections_file_variable(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        mounted = tmp_path / "mw.yaml"
        mounted.write_text(f"auth: {{secret_file: {token}}}\n", encoding="utf-8")
        r = make(MW).resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(mounted)})
        assert r.section("middleware.config") == Section({"auth": Secret(VALUE)})

    def test_a_project_layer_with_secret_files_reads_leaves(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        data = {"middleware": {"config": {"t": {"secret_file": str(token)}}}}
        r = make(MW).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert r.section("middleware.config") == Section({"t": Secret(VALUE)})

    def test_only_an_exact_leaf_is_one(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        data = {"middleware": {"config": {"t": {"secret_file": str(token), "mode": "x"}}}}
        r = make(MW).resolve(environ={}, layers=[control(data)])  # not a leaf: never refused
        assert r.section("middleware.config") == Section(
            {"t": {"secret_file": str(token), "mode": "x"}}
        )

    def test_a_trailing_whitespace_is_stripped_as_for_file(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path, f"  {VALUE} \r\n\n")
        data = {"mw": {"t": {"secret_file": str(token)}}}
        mw = Setting("mw", "", "", is_section=True)
        r = make(mw).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert r.section("mw")["t"] == Secret(f"  {VALUE}")

    def test_an_unreadable_file_names_the_path_and_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing"
        text = f"middleware: {{config: {{a: [{{t: {{secret_file: {missing}}}}}]}}}}\n"
        with pytest.raises(ConfigError) as info:
            resolve_yaml(tmp_path, text, MW)
        assert str(info.value) == (
            f"middleware.config.a[0].t: cannot read secret_file {missing}: No such file or "
            f"directory (from {tmp_path / 'config.yaml'})"
        )

    def test_a_file_that_isnt_utf8_never_shows_its_contents(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad"
        bad.write_bytes(VALUE.encode() + b"\xff")
        data = {"mw": {"t": {"secret_file": str(bad)}}}
        mw = Setting("mw", "", "", is_section=True)
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert str(info.value) == f"mw.t: cannot read secret_file {bad}: not UTF-8 (from {ETC})"
        assert VALUE not in str(info.value)

    @pytest.mark.parametrize("path", [1, "", None, ["a"]])
    def test_a_path_that_isnt_one_is_an_error(self, path: object) -> None:
        data = {"mw": {"t": {"secret_file": path}}}
        mw = Setting("mw", "", "", is_section=True)
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert str(info.value) == f"mw.t.secret_file: expected a file's path (from {ETC})"

    @pytest.mark.parametrize("is_list", [False, True])
    def test_a_section_or_item_cant_itself_be_a_leaf(self, tmp_path: Path, is_list: bool) -> None:
        token = secret_file(tmp_path)
        leaf = {"secret_file": str(token)}
        mw = Setting("mw", "", "", is_section=True, is_list=is_list)
        data = {"mw": [leaf] if is_list else leaf}
        path = "mw[0]" if is_list else "mw"
        with pytest.raises(ConfigError) as info:
            make(mw).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert str(info.value) == (
            f"{path}: a section is a mapping, so a secret_file leaf goes under a key in it "
            f"(from {ETC})"
        )

    @pytest.mark.parametrize("expand", [True, False])
    def test_a_path_expands_only_where_the_layer_does(self, tmp_path: Path, expand: bool) -> None:
        secret_file(tmp_path)
        text = "middleware: {config: {t: {secret_file: '${DIR}/token'}}}\n"
        environ = {"DIR": str(tmp_path)}
        if expand:
            r = resolve_expanding(tmp_path, text, MW, environ=environ)
            assert r.section("middleware.config")["t"] == Secret(VALUE)
        else:
            with pytest.raises(ConfigError, match=r"cannot read secret_file \$\{DIR\}/token: No"):
                resolve_yaml(tmp_path, text, MW, environ=environ)


class TestSecretLeavesFromRemoteLayers:
    """Design 0009, decision E: a control-plane payload never makes this process read a file."""

    @pytest.fixture
    def opened(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Every path opened for reading while the test runs, by any of the usual routes."""
        paths: list[str] = []
        real_read_text, real_open, real_builtin_open = Path.read_text, Path.open, open

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            paths.append(str(self))
            return real_read_text(self, *args, **kwargs)

        def path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            paths.append(str(self))
            return real_open(self, *args, **kwargs)

        def builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            paths.append(str(file))
            return real_builtin_open(file, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(Path, "open", path_open)
        monkeypatch.setattr("builtins.open", builtin_open)
        return paths

    @staticmethod
    def token(tmp_path: Path, opened: list[str]) -> Path:
        """An existing, readable secret file; writing it isn't reading it."""
        token = secret_file(tmp_path)
        opened.clear()
        return token

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            (
                {"middleware": {"config": {"auth": {"secret_file": "TOKEN"}}}},
                "middleware.config.auth",
            ),
            (
                {"middleware": {"config": {"a": [{"b": {"secret_file": "TOKEN"}}]}}},
                "middleware.config.a[0].b",
            ),
            ({"middleware": {"config": {"secret_file": "TOKEN"}}}, "middleware.config"),
        ],
    )
    def test_a_leaf_in_a_control_plane_layer_is_refused_and_never_read(
        self, tmp_path: Path, opened: list[str], data: dict[str, Any], path: str
    ) -> None:
        token = self.token(tmp_path, opened)
        payload = _replace_leaf(data, str(token))
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(environ={}, layers=[control(payload)])
        assert str(info.value) == (
            f"{path}: secret files are read only from local configuration (the config file or a "
            f"section's _FILE), not from {CONTROL}"
        )
        assert str(token) not in opened
        assert VALUE not in str(info.value)

    def test_in_a_list_sections_item_too(self, tmp_path: Path, opened: list[str]) -> None:
        token = self.token(tmp_path, opened)
        payload = {"reverse": [{"auth": {"secret_file": str(token)}}]}
        with pytest.raises(ConfigError, match=r"^reverse\[0\]\.auth: secret files are read only"):
            make(REVERSE).resolve(environ={}, layers=[control(payload)])
        assert str(token) not in opened

    def test_even_in_a_layer_that_expands(self, tmp_path: Path, opened: list[str]) -> None:
        token = self.token(tmp_path, opened)
        payload = {"middleware": {"config": {"t": {"secret_file": "${DIR}/token"}}}}
        with pytest.raises(ConfigError, match="read only from local configuration"):
            make(MW).resolve(environ={"DIR": str(tmp_path)}, layers=[control(payload, expand=True)])
        assert str(token) not in opened

    def test_above_the_local_file_it_is_still_refused(
        self, tmp_path: Path, opened: list[str]
    ) -> None:
        # mitm-proxy's managed mode: the payload first, then the local file.
        token = self.token(tmp_path, opened)
        config = tmp_path / "config.yaml"
        config.write_text(
            f"middleware: {{config: {{t: {{secret_file: {token}}}}}}}\n", encoding="utf-8"
        )
        settings = make(MW)
        payload = {"middleware": {"config": {"t": {"secret_file": str(token)}}}}
        with pytest.raises(ConfigError, match="read only from local configuration"):
            settings.resolve(environ={}, layers=[control(payload), settings.read_file(config, {})])
        assert str(token) not in opened
        # The control: from the local file alone, the same leaf is read.
        r = settings.resolve(environ={}, layers=[control({}), settings.read_file(config, {})])
        assert r.section("middleware.config") == Section({"t": Secret(VALUE)})
        assert str(token) in opened


def _replace_leaf(node: Any, path: str) -> Any:
    """`node` with each `"TOKEN"` replaced by `path`."""
    if isinstance(node, dict):
        return {k: _replace_leaf(v, path) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_leaf(v, path) for v in node]
    return path if node == "TOKEN" else node


class TestSectionReload:
    def test_each_resolve_reads_leaves_afresh(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        settings = make(MW)
        config = tmp_path / "config.yaml"
        config.write_text(
            f"middleware: {{config: {{t: {{secret_file: {token}}}, n: [1]}}}}\n", encoding="utf-8"
        )
        first = settings.resolve(config, {}).section("middleware.config")
        again = settings.resolve(config, {}).section("middleware.config")
        assert first == again and hash(first) == hash(again)
        assert first is not again
        token.write_text("rotated\n", encoding="utf-8")
        rotated = settings.resolve(config, {}).section("middleware.config")
        assert rotated["t"] == Secret("rotated")
        assert rotated != first

    def test_an_unchanged_nan_is_no_change(self, tmp_path: Path) -> None:
        # Issue #49: a reload of the same payload (each NaN its own object, as a project's
        # parser may build them) or of the same file sees no change.
        def payload() -> dict[str, Any]:
            nan = float("nan")
            return {"middleware": {"config": {"r": nan, "l": [-nan, {"x": float("nan")}]}}}

        settings = make(MW)
        first = settings.resolve(environ={}, layers=[control(payload())])
        again = settings.resolve(environ={}, layers=[control(payload())])
        old, new = first.section("middleware.config"), again.section("middleware.config")
        assert old["r"] is not new["r"]
        assert old == new and hash(old) == hash(new)
        config = tmp_path / "config.yaml"
        text = "middleware: {config: {r: .nan, l: [.nan, {x: .nan}]}}\n"
        config.write_text(text, encoding="utf-8")
        from_file = settings.resolve(config, {}).section("middleware.config")
        assert from_file == settings.resolve(config, {}).section("middleware.config")
        assert from_file == new and hash(from_file) == hash(new)


# --- typed list items and indexed variables (design 0009 step 6) ----------------------------------

CRON = Setting("cron", "", "A 5-field cron expression.", required=True)
CONTEXT = Setting("context", "", "The context to enqueue.", required=True)
SCHEDULE = Setting(
    "schedule", "", "Cron schedule.", is_section=True, is_list=True, fields=(CRON, CONTEXT)
)
ROUTE = Setting(
    "route",
    "",
    "Routes.",
    is_section=True,
    is_list=True,
    fields=(
        Setting("listen", "", "Listen address.", required=True),
        Setting("port", 80, "Port.", check=port),
        Setting("tls", False, "TLS."),
        Setting("hosts", "", "Hosts.", is_list=True),
        Setting("token", "", "Token.", secret=True),
        Setting("opts", "", "Options.", is_section=True),
    ),
)
ROUTE_DEFAULTS: dict[str, Any] = {
    "port": 80,
    "tls": False,
    "hosts": (),
    "token": Secret(""),
    "opts": Section(),
}


def route(**fields: Any) -> Section:
    return Section({"listen": ":80", **ROUTE_DEFAULTS, **fields})


def typed(key: str = "schedule", *fields: Setting, **kwargs: Any) -> Setting:
    return Setting(key, "", "", is_section=True, is_list=True, fields=fields or (CRON,), **kwargs)


class TestFieldDeclaration:
    def test_the_records_example_is_accepted_and_public(self) -> None:
        bound = make(SCHEDULE)[0]
        assert [(f.key, f.required) for f in bound.fields] == [("cron", True), ("context", True)]
        assert (Setting("a", "", "").fields, Setting("a", "", "").required) == ((), False)

    @pytest.mark.parametrize(
        "setting",
        [
            Setting("a", "", "", fields=(CRON,)),
            Setting("a", "", "", is_list=True, fields=(CRON,)),
            Setting("a", "", "", is_section=True, fields=(CRON,)),
        ],
    )
    def test_fields_only_on_a_list_section(self, setting: Setting) -> None:
        with pytest.raises(ValueError, match=r"^'a': only a list section \(is_section=True"):
            make(setting)

    @pytest.mark.parametrize(
        "setting",
        [Setting("a", "", "", required=True), typed("a", required=True)],
    )
    def test_required_only_on_a_field(self, setting: Setting) -> None:
        with pytest.raises(ValueError, match=r"^'a': required is for a list section's field"):
            make(setting)

    def test_fields_are_a_tuple_of_settings(self) -> None:
        with pytest.raises(ValueError, match="fields are a tuple of Setting"):
            make(Setting("a", "", "", is_section=True, is_list=True, fields=[CRON]))  # type: ignore[arg-type]  # the wrong type on purpose
        with pytest.raises(ValueError, match="a field is a Setting, not str"):
            make(typed("a", "cron"))  # type: ignore[arg-type]  # the wrong type on purpose

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            (Setting("a.b", "", ""), "a field's key is one lower-case word of [a-z0-9_]"),
            (Setting("Cron", "", ""), "a field's key is one lower-case word of [a-z0-9_]"),
            (Setting("on", "", ""), "YAML doesn't read 'on' as a string"),
            (typed("inner"), "a field can't have fields of its own"),
            (Setting("x", "", "", aliases=("y",)), "a field has no aliases"),
            (Setting("x", 1, "", is_section=True), 'a section setting\'s default must be ""'),
            (Setting("x", 1, "", secret=True), "a secret setting must be a str setting"),
        ],
    )
    def test_a_bad_field_is_rejected(self, field: Setting, expected: str) -> None:
        with pytest.raises(ValueError) as info:
            make(typed("s", field))
        assert str(info.value) == f"'s': field {field.key!r}: {expected}"

    def test_a_field_declared_twice(self) -> None:
        with pytest.raises(ValueError, match=r"^'s': field 'cron': declared twice$"):
            make(typed("s", CRON, Setting("cron", 1, "")))

    def test_two_fields_cant_read_one_variable(self) -> None:
        with pytest.raises(ValueError) as info:
            make(typed("s", Setting("x", "", ""), Setting("x_file", "", "")))
        assert str(info.value) == (
            "'s': field 'x_file': its variable <KEY>_<N>_X_FILE is also field 'x''s"
        )

    @pytest.mark.parametrize(
        ("other", "extra", "expected"),
        [
            (Setting("schedule_0_cron", "", ""), (), "APP_SCHEDULE_0_CRON is read by"),
            (Setting("schedule_1", "", ""), (), "APP_SCHEDULE_1_FILE is read by"),
            (Setting("x", "", "", aliases=("schedule_2_y",)), (), "APP_SCHEDULE_2_Y is read by"),
            (None, ("APP_SCHEDULE_0_ANY",), "APP_SCHEDULE_0_ANY is named in extra_env"),
        ],
    )
    def test_no_other_variable_may_have_the_indexed_form(
        self, other: Setting | None, extra: tuple[str, ...], expected: str
    ) -> None:
        declared = (SCHEDULE,) if other is None else (SCHEDULE, other)
        with pytest.raises(ValueError, match=f"^'schedule': {expected}") as info:
            Settings("APP_", declared, default_path=DEFAULT, extra_env=extra)
        assert str(info.value).endswith(
            "but has the form of APP_SCHEDULE_<N>_<FIELD>, the list's indexed variables"
        )

    def test_nor_the_form_of_an_aliass_indexed_variables(self) -> None:
        with pytest.raises(ValueError, match=r"APP_OLD_0_CRON is read by 'old_0_cron'.*APP_OLD_"):
            make(typed("schedule", aliases=("old",)), Setting("old_0_cron", "", ""))

    def test_names_that_only_look_alike_are_fine(self) -> None:
        make(SCHEDULE, Setting("schedule_extra", "", ""), Setting("schedules_0_x", "", ""))


class TestTypedItemsFromLayers:
    def test_each_item_holds_every_field_typed(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        text = (
            "route:\n"
            "  - {listen: ':80'}\n"
            "  - listen: ':443'\n"
            "    port: '8443'\n"
            "    tls: yes\n"
            "    hosts: [a, b]\n"
            f"    token: {{secret_file: {token}}}\n"
            "    opts: {retries: 3, auth: {secret_file: " + str(token) + "}}\n"
        )
        r = resolve_yaml(tmp_path, text, ROUTE)
        assert r.sections("route") == (
            route(),
            route(
                listen=":443",
                port=8443,
                tls=True,
                hosts=("a", "b"),
                token=Secret(VALUE),
                opts=Section({"retries": 3, "auth": Secret(VALUE)}),
            ),
        )
        assert list(r.sections("route")[0]) == ["listen", "port", "tls", "hosts", "token", "opts"]
        assert (r.sources["route"], r.warnings) == ("file", ())

    def test_a_secret_field_written_inline_is_a_secret(self) -> None:
        r = make(ROUTE).resolve(
            environ={}, layers=[layer({"route": [{"listen": "x", "token": "t"}]})]
        )
        assert r.sections("route")[0]["token"] == Secret("t")

    def test_an_empty_list_keeps_its_kind(self) -> None:
        r = make(SCHEDULE).resolve(environ={}, layers=[layer({"schedule": []})])
        assert r.sections("schedule") == ()
        assert make(SCHEDULE).resolve(DEFAULT, {}).sections("schedule") == ()

    @pytest.mark.parametrize("item", [{"cron": "* * * * *"}, {"cron": "x", "context": None}])
    def test_a_missing_required_field_names_the_item_and_where(self, item: dict[str, Any]) -> None:
        data = {"schedule": [{"cron": "a", "context": "b"}, item]}
        with pytest.raises(ConfigError) as info:
            make(SCHEDULE).resolve(environ={}, layers=[layer(data, origins={"schedule": "x.yaml"})])
        assert str(info.value) == "schedule[1].context: a required field, not set (from x.yaml)"

    def test_a_coercion_error_names_the_item_never_a_secret(self) -> None:
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(
                environ={}, layers=[layer({"route": [{"listen": "x", "port": "y"}]})]
            )
        assert str(info.value) == f"route[0].port: expected int, got 'y' (from {ETC})"
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(
                environ={}, layers=[layer({"route": [{"listen": "x", "token": [VALUE]}]})]
            )
        assert str(info.value) == f"route[0].token: expected str (from {ETC})"

    def test_a_fields_check_runs_and_names_the_item(self) -> None:
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(environ={}, layers=[layer({"route": [{"listen": "x", "port": 0}]})])
        assert str(info.value) == f"route[0].port: 0 is not a TCP port (from {ETC})"

    def test_a_fields_check_may_replace_its_value(self) -> None:
        level = Setting("level", "info", "", check=one_of("INFO", "DEBUG"))
        r = make(typed("logs", level)).resolve(
            environ={}, layers=[layer({"logs": [{}, {"level": "debug"}]})]
        )
        assert [item["level"] for item in r.sections("logs")] == ["INFO", "DEBUG"]

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (
                {"schedule": {"cron": "x"}},
                "schedule: expected a list of sections (mappings), got a mapping",
            ),
            ({"schedule": ["x"]}, "schedule[0]: expected a section (a mapping), got str"),
        ],
    )
    def test_the_wrong_shape_is_an_error(self, data: dict[str, Any], expected: str) -> None:
        with pytest.raises(ConfigError) as info:
            make(SCHEDULE).resolve(environ={}, layers=[layer(data)])
        assert str(info.value) == f"{expected} (from {ETC})"

    def test_the_lists_check_gets_the_typed_items(self) -> None:
        seen: list[Value] = []
        setting = typed("schedule", CRON, check=lambda v: seen.append(v))
        make(setting).resolve(environ={}, layers=[layer({"schedule": [{"cron": "a"}]})])
        assert seen == [(Section({"cron": "a"}),)]

    def test_the_lists_check_error_shows_fields_as_written(self, tmp_path: Path) -> None:
        def reject(value: Value) -> None:
            raise ValueError(f"bad {cast(tuple[Section, ...], value)[0]['cron']}")

        settings = Settings(
            "APP_", (typed("schedule", CRON, check=reject),), default_path=DEFAULT, expand=True
        )
        path = tmp_path / "config.yaml"
        path.write_text("schedule:\n  - {cron: '${C}', other: '${UNSET}'}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            settings.resolve(path, {"C": "hunter2"})
        assert str(info.value) == f"schedule: bad ${{C}} (from {path})"

    def test_fields_expand_but_unknown_fields_never_do(self, tmp_path: Path) -> None:
        settings = Settings("APP_", (SCHEDULE,), default_path=DEFAULT, expand=True)
        path = tmp_path / "config.yaml"
        path.write_text(
            "schedule:\n  - {cron: '${C:-* * * * *}', context: '${X}', typo: '${UNSET}'}\n",
            encoding="utf-8",
        )
        r = settings.resolve(path, {"X": "nightly"})
        assert r.sections("schedule") == (Section({"cron": "* * * * *", "context": "nightly"}),)
        with pytest.raises(ConfigError, match=r"^schedule\[0\]\.context: \$\{X\} is not set"):
            settings.resolve(path, {})

    def test_the_first_layer_with_the_list_wins_whole(self) -> None:
        first = control({"schedule": [{"cron": "a", "context": "b"}]})
        second = layer({"schedule": [{"cron": "c", "context": "d"}, {"cron": "e", "context": "f"}]})
        r = make(SCHEDULE).resolve(environ={}, layers=[first, second])
        assert r.sections("schedule") == (Section({"cron": "a", "context": "b"}),)

    def test_an_alias_with_equal_or_different_items(self) -> None:
        moved = typed("schedule", CRON, aliases=("jobs",))
        same = {"schedule": [{"cron": "a"}], "jobs": [{"cron": "a", "typo": 1}]}
        r = make(moved).resolve(environ={}, layers=[layer(same)])
        assert r.sections("schedule") == (Section({"cron": "a"}),)
        assert [(w.kind, w.name) for w in r.warnings] == [
            ("alias", "jobs"),
            ("unknown", "jobs[0].typo"),
        ]
        with pytest.raises(ConfigError, match="set as both 'schedule' and its old name 'jobs'"):
            make(moved).resolve(
                environ={}, layers=[layer({"schedule": [{"cron": "a"}], "jobs": [{"cron": "b"}]})]
            )


class TestTypedItemSecretLeaves:
    """Decision E holds for fields: a leaf from a remote layer is refused and never read."""

    opened = TestSecretLeavesFromRemoteLayers.opened

    @pytest.mark.parametrize(
        ("item", "path"),
        [
            ({"listen": "x", "token": {"secret_file": "TOKEN"}}, "route[0].token"),
            ({"listen": "x", "port": {"secret_file": "TOKEN"}}, "route[0].port"),
            ({"listen": "x", "opts": {"a": {"secret_file": "TOKEN"}}}, "route[0].opts.a"),
        ],
    )
    def test_a_leaf_in_a_control_plane_item_is_refused_and_never_read(
        self, tmp_path: Path, opened: list[str], item: dict[str, Any], path: str
    ) -> None:
        token = TestSecretLeavesFromRemoteLayers.token(tmp_path, opened)
        payload = {"route": [_replace_leaf(item, str(token))]}
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(environ={}, layers=[control(payload)])
        assert str(info.value) == (
            f"{path}: secret files are read only from local configuration (the config file or a "
            f"section's _FILE), not from {CONTROL}"
        )
        assert str(token) not in opened

    def test_a_leaf_is_only_for_a_secret_field(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        data = {"route": [{"listen": {"secret_file": str(token)}}]}
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(environ={}, layers=[layer(data, secret_files=True)])
        assert str(info.value) == (
            f"route[0].listen: a secret_file leaf is read only for a secret field (from {ETC})"
        )

    def test_a_leafs_path_expands_but_its_contents_never_do(self, tmp_path: Path) -> None:
        secret_file(tmp_path, "${HOME}\n")
        data = {"route": [{"listen": "x", "token": {"secret_file": "${DIR}/token"}}]}
        r = make(ROUTE).resolve(
            environ={"DIR": str(tmp_path)}, layers=[layer(data, secret_files=True, expand=True)]
        )
        assert r.sections("route")[0]["token"] == Secret("${HOME}")


class TestTypedItemsFromFileVariable:
    def test_items_and_unknown_fields_from_the_lists_file(self, tmp_path: Path) -> None:
        mounted = tmp_path / "schedule.yaml"
        mounted.write_text("- {cron: a, context: b, contxt: c, 1: d}\n", encoding="utf-8")
        environ = {"APP_SCHEDULE_FILE": str(mounted), "APP_SCHEDULE_0_CRON": "ignored"}
        r = resolve_yaml(
            tmp_path, "schedule: [{cron: x, context: y, extra: z}]\n", SCHEDULE, environ=environ
        )
        assert r.sections("schedule") == (Section({"cron": "a", "context": "b"}),)
        assert r.sources["schedule"] == "APP_SCHEDULE_FILE"
        label = f"APP_SCHEDULE_FILE ({mounted})"
        assert [str(w) for w in r.warnings] == [
            f"unknown field 'schedule[0].extra' in {tmp_path / 'config.yaml'}",
            f"unknown field 'schedule[0].contxt' in {label}: did you mean 'schedule[0].context'?",
            f"unknown field 'schedule[0].1' in {label}",
        ]

    def test_a_missing_field_in_it_names_the_file(self, tmp_path: Path) -> None:
        mounted = tmp_path / "schedule.yaml"
        mounted.write_text("- {cron: a}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            make(SCHEDULE).resolve(DEFAULT, {"APP_SCHEDULE_FILE": str(mounted)})
        assert str(info.value) == (
            f"schedule[0].context: a required field, not set (from APP_SCHEDULE_FILE ({mounted}))"
        )


class TestTypedItemUnknownFields:
    def test_an_unknown_field_warns_with_a_suggestion(self) -> None:
        # Warned, never an error (design 0009, question 2), and ignored for resolution.
        optional = typed("schedule", Setting("cron", "", ""), Setting("context", "", ""))
        items = [{"cron": "a"}, {"cronn": "a", "zzz": 1}, {True: 1}]
        r = make(optional).resolve(environ={}, layers=[layer({"schedule": items})])
        assert [str(w) for w in r.warnings] == [
            f"unknown field 'schedule[1].cronn' in {ETC}: did you mean 'schedule[1].cron'?",
            f"unknown field 'schedule[1].zzz' in {ETC}",
            f"unknown field 'schedule[2].true' in {ETC}",
        ]
        assert {w.kind for w in r.warnings} == {"unknown"}
        assert [dict(item) for item in r.sections("schedule")] == [
            {"cron": "a", "context": ""},
            {"cron": "", "context": ""},
            {"cron": "", "context": ""},
        ]

    def test_only_items_are_walked_and_a_misshapen_list_is_resolutions_to_report(self) -> None:
        good = layer({"schedule": [{"cron": "a", "context": "b"}]})
        odd = layer({"schedule": {"cronn": 1}}, where="odd.yaml")
        mixed = layer({"schedule": ["x", {"x": 1}]}, where="mixed.yaml")
        r = make(SCHEDULE).resolve(environ={}, layers=[good, odd, mixed])
        assert [w.name for w in r.warnings] == ["schedule[1].x"]


Q = "QBT_RULES_"


def qbt(*settings: Setting) -> Settings:
    return Settings(Q, settings or (SCHEDULE,), default_path=DEFAULT)


class TestIndexedVariables:
    def test_items_from_the_environment(self) -> None:
        environ = {
            "QBT_RULES_SCHEDULE_0_CRON": "0 * * * *",
            "QBT_RULES_SCHEDULE_0_CONTEXT": "hourly",
            "QBT_RULES_SCHEDULE_1_CRON": "0 0 * * *",
            "QBT_RULES_SCHEDULE_1_CONTEXT": "nightly",
        }
        r = qbt().resolve(DEFAULT, environ)
        assert r.sections("schedule") == (
            Section({"cron": "0 * * * *", "context": "hourly"}),
            Section({"cron": "0 0 * * *", "context": "nightly"}),
        )
        assert r.sources["schedule"] == "QBT_RULES_SCHEDULE_<N>_*"
        assert r.warnings == ()

    def test_they_replace_the_files_list_never_merge(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("schedule:\n  - {cron: a, context: b}\n  - {cron: c, context: d}\n")
        environ = {"QBT_RULES_SCHEDULE_0_CRON": "x", "QBT_RULES_SCHEDULE_0_CONTEXT": "y"}
        r = qbt().resolve(config, environ)
        assert r.sections("schedule") == (Section({"cron": "x", "context": "y"}),)

    def test_the_lists_file_variable_beats_them(self, tmp_path: Path) -> None:
        mounted = tmp_path / "s.yaml"
        mounted.write_text("[]\n", encoding="utf-8")
        environ = {"QBT_RULES_SCHEDULE_FILE": str(mounted), "QBT_RULES_SCHEDULE_0_CRON": "x"}
        r = qbt().resolve(DEFAULT, environ)
        assert (r.sections("schedule"), r.sources["schedule"]) == ((), "QBT_RULES_SCHEDULE_FILE")

    def test_each_field_typed_from_its_variable_or_file(self, tmp_path: Path) -> None:
        token = secret_file(tmp_path)
        opts = tmp_path / "opts.yaml"
        opts.write_text(f"a: ${{X}}\nt: {{secret_file: {token}}}\n", encoding="utf-8")
        settings = Settings("APP_", (ROUTE,), default_path=DEFAULT, expand=True)
        environ = {
            "APP_ROUTE_0_LISTEN": ":443",
            "APP_ROUTE_0_PORT": "8443",
            "APP_ROUTE_0_TLS": "on",
            "APP_ROUTE_0_HOSTS": "a, b",
            "APP_ROUTE_0_TOKEN": "plain is shadowed",
            "APP_ROUTE_0_TOKEN_FILE": str(token),
            "APP_ROUTE_0_OPTS_FILE": str(opts),
            "APP_ROUTE_1_LISTEN": "${X}",
            "X": "x",
        }
        r = settings.resolve(DEFAULT, environ)
        assert r.sections("route") == (
            route(
                listen=":443",
                port=8443,
                tls=True,
                hosts=("a", "b"),
                token=Secret(VALUE),
                opts=Section({"a": "x", "t": Secret(VALUE)}),
            ),
            route(listen="${X}"),  # a variable never expands
        )
        assert r.warnings == ()

    def test_an_error_names_the_exact_variable(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(DEFAULT, {"APP_ROUTE_0_LISTEN": "x", "APP_ROUTE_0_PORT": "y"})
        assert str(info.value) == "route[0].port: expected int, got 'y' (from APP_ROUTE_0_PORT)"
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(DEFAULT, {"APP_ROUTE_0_LISTEN": "x", "APP_ROUTE_0_PORT": "0"})
        assert str(info.value) == "route[0].port: 0 is not a TCP port (from APP_ROUTE_0_PORT)"
        missing = tmp_path / "missing"
        with pytest.raises(
            ConfigError, match=f"^cannot read APP_ROUTE_0_TOKEN_FILE \\({missing}\\)"
        ):
            make(ROUTE).resolve(DEFAULT, {"APP_ROUTE_0_TOKEN_FILE": str(missing)})

    def test_a_section_field_from_a_plain_variable_is_an_error(self) -> None:
        with pytest.raises(ConfigError) as info:
            make(ROUTE).resolve(DEFAULT, {"APP_ROUTE_0_LISTEN": "x", "APP_ROUTE_0_OPTS": "{}"})
        assert str(info.value) == (
            "route[0].opts: APP_ROUTE_0_OPTS is set, but a section is read from a YAML file, "
            "never a variable: set APP_ROUTE_0_OPTS_FILE to the file's path"
        )

    def test_the_lists_plain_variable_is_an_error_naming_both_forms(self) -> None:
        with pytest.raises(ConfigError) as info:
            qbt().resolve(DEFAULT, {"QBT_RULES_SCHEDULE": "x"})
        assert str(info.value) == (
            "schedule: QBT_RULES_SCHEDULE is set, but a list section is read from a YAML file or "
            "indexed variables: set QBT_RULES_SCHEDULE_FILE to the file's path, or "
            "QBT_RULES_SCHEDULE_<N>_<FIELD> for each item's fields"
        )

    def test_a_straggler_past_the_end_warns(self) -> None:
        # qbt-rules ignored it silently; here it is an `ignored` warning (design 0009).
        environ = {
            "QBT_RULES_SCHEDULE_0_CRON": "a",
            "QBT_RULES_SCHEDULE_0_CONTEXT": "b",
            "QBT_RULES_SCHEDULE_2_CRON": "c",
            "QBT_RULES_SCHEDULE_3_CONTEXT_FILE": "/nonexistent",
        }
        r = qbt().resolve(DEFAULT, environ)
        assert r.sections("schedule") == (Section({"cron": "a", "context": "b"}),)
        assert [(w.kind, str(w)) for w in r.warnings] == [
            (
                "ignored",
                f"ignored variable 'QBT_RULES_SCHEDULE_{n}' in the environment: the list "
                "'schedule' ends before it, as no QBT_RULES_SCHEDULE_1_* is set",
            )
            for n in ("2_CRON", "3_CONTEXT_FILE")
        ]

    def test_only_stragglers_set_leave_the_files_list(self, tmp_path: Path) -> None:
        # No item 0, so the indexed list is empty and every variable is past its end: ignored,
        # with a warning, and the file's list stands.
        config = tmp_path / "config.yaml"
        config.write_text("schedule: [{cron: a, context: b}]\n", encoding="utf-8")
        r = qbt().resolve(config, {"QBT_RULES_SCHEDULE_1_CRON": "x"})
        assert (r.sections("schedule"), r.sources["schedule"]) == (
            (Section({"cron": "a", "context": "b"}),),
            "file",
        )
        assert [(w.kind, w.name) for w in r.warnings] == [("ignored", "QBT_RULES_SCHEDULE_1_CRON")]
        assert "no QBT_RULES_SCHEDULE_0_* is set" in str(r.warnings[0])

    def test_a_lone_context_is_an_error_naming_both_variables(self) -> None:
        # qbt-rules ended the list at the first missing _CRON; interpose can't know which field
        # marks an item, so item 1 exists and its cron is missing (design 0009).
        environ = {
            "QBT_RULES_SCHEDULE_0_CRON": "a",
            "QBT_RULES_SCHEDULE_0_CONTEXT": "b",
            "QBT_RULES_SCHEDULE_1_CONTEXT": "c",
        }
        with pytest.raises(ConfigError) as info:
            qbt().resolve(DEFAULT, environ)
        assert str(info.value) == (
            "schedule[1].cron: QBT_RULES_SCHEDULE_1_CRON is not set, but "
            "QBT_RULES_SCHEDULE_1_CONTEXT is"
        )

    def test_the_variable_named_as_set_may_be_a_file(self, tmp_path: Path) -> None:
        cron = tmp_path / "cron"
        cron.write_text("a\n", encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            qbt().resolve(DEFAULT, {"QBT_RULES_SCHEDULE_0_CRON_FILE": str(cron)})
        assert str(info.value) == (
            "schedule[0].context: QBT_RULES_SCHEDULE_0_CONTEXT is not set, but "
            "QBT_RULES_SCHEDULE_0_CRON_FILE is"
        )

    def test_an_optional_field_takes_its_default(self) -> None:
        r = make(ROUTE).resolve(DEFAULT, {"APP_ROUTE_0_LISTEN": ":80"})
        assert r.sections("route") == (route(),)

    @pytest.mark.parametrize(
        ("name", "suggestion"),
        [
            ("QBT_RULES_SCHEDULE_0_CRONN", "QBT_RULES_SCHEDULE_0_CRON"),
            ("QBT_RULES_SCHEDULE_01_CRON", "QBT_RULES_SCHEDULE_1_CRON"),
            ("QBT_RULES_SCHEDULE_0_ZZZZZZZZZZZZ", None),
        ],
    )
    def test_an_unknown_indexed_variable_warns_with_a_suggestion(
        self, name: str, suggestion: str | None
    ) -> None:
        environ = {"QBT_RULES_SCHEDULE_0_CRON": "a", "QBT_RULES_SCHEDULE_0_CONTEXT": "b", name: ""}
        r = qbt().resolve(DEFAULT, environ)
        hint = "" if suggestion is None else f": did you mean {suggestion!r}?"
        assert [(w.kind, str(w)) for w in r.warnings] == [
            ("unknown", f"unknown variable {name!r} in the environment{hint}")
        ]

    def test_an_index_of_any_length_is_compared_never_converted(self) -> None:
        huge = "9" * 5000  # more digits than int() converts
        environ = {f"QBT_RULES_SCHEDULE_{huge}_CRON": "a", f"QBT_RULES_SCHEDULE_0{huge}_CRON": "a"}
        r = qbt().resolve(DEFAULT, environ)
        assert [w.kind for w in r.warnings] == ["unknown", "ignored"]
        assert r.warnings[0].message.endswith(f"did you mean 'QBT_RULES_SCHEDULE_{huge}_CRON'?")

    @pytest.mark.parametrize(("items", "index"), [(2, "10"), (10, "11"), (10, "100")])
    def test_past_the_end_compares_as_numbers(self, items: int, index: str) -> None:
        # "10" sorts before "2" as text, but item 10 is past a 2-item list's end.
        environ = {f"QBT_RULES_SCHEDULE_{n}_CRON": "a" for n in range(items)}
        environ |= {f"QBT_RULES_SCHEDULE_{n}_CONTEXT": "b" for n in range(items)}
        environ[f"QBT_RULES_SCHEDULE_{index}_CRON"] = "c"
        r = qbt().resolve(DEFAULT, environ)
        assert len(r.sections("schedule")) == items
        assert [(w.kind, w.name) for w in r.warnings] == [
            ("ignored", f"QBT_RULES_SCHEDULE_{index}_CRON")
        ]

    def test_with_no_prefix_stragglers_still_warn(self) -> None:
        settings = Settings("", (SCHEDULE,), default_path=DEFAULT)
        environ = {"SCHEDULE_1_CRON": "a", "SCHEDULE_0_CRONN": "a", "OTHER": "x"}
        r = settings.resolve(DEFAULT, environ)
        assert [(w.kind, w.name) for w in r.warnings] == [("ignored", "SCHEDULE_1_CRON")]


class TestIndexedAliases:
    MOVED = Setting(
        "schedule", "", "", is_section=True, is_list=True, fields=(CRON, CONTEXT), aliases=("jobs",)
    )

    def test_an_aliass_indexed_variables_are_read_with_a_warning(self) -> None:
        environ = {"QBT_RULES_JOBS_0_CRON": "a", "QBT_RULES_JOBS_0_CONTEXT": "b"}
        r = qbt(self.MOVED).resolve(DEFAULT, environ)
        assert r.sections("schedule") == (Section({"cron": "a", "context": "b"}),)
        assert r.sources["schedule"] == "QBT_RULES_JOBS_<N>_*"
        assert r.warnings == tuple(
            alias_warning(f"QBT_RULES_JOBS_0_{f}", "the environment", f"QBT_RULES_SCHEDULE_0_{f}")
            for f in ("CONTEXT", "CRON")
        )

    def test_an_aliass_straggler_warns_twice(self) -> None:
        r = qbt(self.MOVED).resolve(DEFAULT, {"QBT_RULES_JOBS_1_CRON": "a"})
        assert [(w.kind, w.name) for w in r.warnings] == [
            ("alias", "QBT_RULES_JOBS_1_CRON"),
            ("ignored", "QBT_RULES_JOBS_1_CRON"),
        ]

    def test_an_unknown_alias_variable_suggests_the_current_name(self) -> None:
        r = qbt(self.MOVED).resolve(DEFAULT, {"QBT_RULES_JOBS_0_CRONN": "a"})
        assert [str(w) for w in r.warnings] == [
            "unknown variable 'QBT_RULES_JOBS_0_CRONN' in the environment: "
            "did you mean 'QBT_RULES_SCHEDULE_0_CRON'?"
        ]

    def test_key_and_alias_set_alike_are_fine_and_differently_an_error(self) -> None:
        both = {
            "QBT_RULES_SCHEDULE_0_CRON": "a",
            "QBT_RULES_SCHEDULE_0_CONTEXT": "b",
            "QBT_RULES_JOBS_0_CRON": "a",
            "QBT_RULES_JOBS_0_CONTEXT": "b",
        }
        r = qbt(self.MOVED).resolve(DEFAULT, both)
        assert r.sources["schedule"] == "QBT_RULES_SCHEDULE_<N>_*"
        with pytest.raises(ConfigError) as info:
            qbt(self.MOVED).resolve(DEFAULT, {**both, "QBT_RULES_JOBS_0_CONTEXT": "c"})
        assert str(info.value) == (
            "schedule: set as both 'QBT_RULES_SCHEDULE_<N>_*' and its old name "
            "'QBT_RULES_JOBS_<N>_*' (from the environment), to different values"
        )


# --- a value that holds itself (issue #48) -------------------------------------------------------


def cycle_error(path: str, earlier: str, origin: str) -> str:
    return (
        f"{path}: refers back to {earlier}, which holds it, so it never ends: a YAML alias "
        f"can't be inside its own anchor (from {origin})"
    )


class TestSectionCycles:
    """A YAML alias inside its own anchor is a `ConfigError` naming the path, never the value,
    found by identity rather than Python's recursion limit; one shared alias is no cycle."""

    @pytest.mark.parametrize("expand", [False, True])
    @pytest.mark.parametrize(
        ("yaml_text", "path", "earlier"),
        [
            # A mapping inside itself, and the section itself inside itself.
            (
                "{a: &x {b: *x}}",
                "middleware.config.a.b",
                "middleware.config.a",
            ),
            ("&x {a: *x}", "middleware.config.a", "middleware.config"),
            # A list inside itself: the issue's own case.
            ("{a: &x [*x]}", "middleware.config.a[0]", "middleware.config.a"),
            # Two levels down, through a list and a mapping.
            (
                "{a: {b: &x {c: [1, *x]}}}",
                "middleware.config.a.b.c[1]",
                "middleware.config.a.b",
            ),
        ],
    )
    def test_a_cycle_in_the_file_names_its_path(
        self, tmp_path: Path, yaml_text: str, path: str, earlier: str, expand: bool
    ) -> None:
        text = f"middleware:\n  config: {yaml_text}\n"
        config = tmp_path / "config.yaml"
        with pytest.raises(ConfigError) as info:
            (resolve_expanding if expand else resolve_yaml)(tmp_path, text, MW)
        assert str(info.value) == cycle_error(path, earlier, str(config))

    @pytest.mark.parametrize("expand", [False, True])
    def test_a_cycle_in_a_sections_file_variable(self, tmp_path: Path, expand: bool) -> None:
        mounted = tmp_path / "mw.yaml"
        mounted.write_text("auth: &x {users: [a, *x]}\n", encoding="utf-8")
        settings = expanding(MW) if expand else make(MW)
        with pytest.raises(ConfigError) as info:
            settings.resolve(DEFAULT, {"APP_MIDDLEWARE_CONFIG_FILE": str(mounted)})
        assert str(info.value) == cycle_error(
            "middleware.config.auth.users[1]",
            "middleware.config.auth",
            f"APP_MIDDLEWARE_CONFIG_FILE ({mounted})",
        )

    def test_a_cycle_in_a_project_layer(self) -> None:
        looped: dict[str, Any] = {"k": 1}
        looped["self"] = [looped]
        data = {"middleware": {"config": {"a": looped}}}
        with pytest.raises(ConfigError) as info:
            make(MW).resolve(environ={}, layers=[control(data)])
        assert str(info.value) == cycle_error(
            "middleware.config.a.self[0]", "middleware.config.a", CONTROL
        )

    @pytest.mark.parametrize("expand", [False, True])
    def test_a_cycle_in_a_list_sections_item(self, tmp_path: Path, expand: bool) -> None:
        text = "reverse:\n  - &x {up: [*x]}\n"
        with pytest.raises(ConfigError) as info:
            (resolve_expanding if expand else resolve_yaml)(tmp_path, text, REVERSE)
        assert str(info.value) == cycle_error(
            "reverse[0].up[0]", "reverse[0]", str(tmp_path / "config.yaml")
        )

    @pytest.mark.parametrize("expand", [False, True])
    def test_a_cycle_in_a_typed_items_section_field(self, tmp_path: Path, expand: bool) -> None:
        text = "route:\n  - {listen: ':80', opts: &x {more: *x}}\n"
        with pytest.raises(ConfigError) as info:
            (resolve_expanding if expand else resolve_yaml)(tmp_path, text, ROUTE)
        assert str(info.value) == cycle_error(
            "route[0].opts.more", "route[0].opts", str(tmp_path / "config.yaml")
        )

    def test_a_cycle_in_an_expanding_list_setting(self, tmp_path: Path) -> None:
        # Expansion walks a plain setting's list too; without it the list is just not a list of
        # scalars.
        with pytest.raises(ConfigError) as info:
            resolve_expanding(tmp_path, "hosts: &x [a, *x]\n", HOSTS)
        assert str(info.value) == cycle_error("hosts[1]", "hosts", str(tmp_path / "config.yaml"))

    @pytest.mark.parametrize("expand", [False, True])
    def test_a_shared_alias_is_no_cycle(self, tmp_path: Path, expand: bool) -> None:
        text = (
            "middleware:\n  config:\n"
            "    a: &x {k: [1, {n: 2}]}\n    b: *x\n    c: [*x, *x]\n    d: {e: *x}\n"
            "reverse:\n  - &y {up: [h]}\n  - *y\n"
        )
        r = (resolve_expanding if expand else resolve_yaml)(tmp_path, text, MW, REVERSE)
        shared = Section({"k": [1, {"n": 2}]})
        assert r.section("middleware.config") == Section(
            {"a": shared, "b": shared, "c": [shared, shared], "d": {"e": shared}}
        )
        assert r.sections("reverse") == (Section({"up": ["h"]}),) * 2


class TestSectionFreezeCycles:
    def test_a_mapping_that_holds_itself_is_a_value_error(self) -> None:
        looped: dict[str, Any] = {}
        looped["a"] = {"b": looped}
        with pytest.raises(ValueError, match="a Section can't hold itself"):
            Section(looped)

    def test_a_list_that_holds_itself_is_a_value_error(self) -> None:
        looped: list[Any] = [1]
        looped.append({"l": looped})
        with pytest.raises(ValueError, match="a Section can't hold itself"):
            Section({"a": looped})

    def test_a_shared_value_freezes_each_time(self) -> None:
        shared: dict[str, Any] = {"k": [1]}
        items = [shared, shared]
        s = Section({"a": shared, "b": shared, "c": items, "d": items})
        assert s["a"] == s["b"] == Section({"k": [1]})
        assert s["c"] == s["d"] == (Section({"k": [1]}),) * 2
