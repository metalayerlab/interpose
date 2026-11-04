"""interpose.settings: declaration, lookup order, coercion, provenance, checks and secrets."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from interpose.secret import MASK, Secret
from interpose.settings import (
    ConfigError,
    ConfigWarning,
    Layer,
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
        assert file_layer == Layer({"listen": {"port": 1}}, source="file", where=str(config))
        assert settings.resolve(environ=environ, layers=[file_layer]) == settings.resolve(
            environ=environ
        )
        assert settings.read_file(config, {}) == file_layer

    def test_read_file_of_a_missing_file_is_an_empty_layer(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.yaml"
        assert make(PORT).read_file(missing, {}) == Layer({}, source="file", where=str(missing))
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
