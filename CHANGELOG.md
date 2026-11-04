# Changelog

## [0.4.0](https://github.com/metalayerlab/interpose/compare/v0.3.3...v0.4.0) (2026-11-04)


### ⚠ BREAKING CHANGES

* **settings:** Value gains Section and tuple[Section, ...], so an exhaustive match on Value needs two more cases.

### ✨ Features

* **settings:** section settings with secret leaves (design 0009 step 5) ([#46](https://github.com/metalayerlab/interpose/issues/46)) ([1279ede](https://github.com/metalayerlab/interpose/commit/1279edebe843bea2741e26c37ae7bf3360cd8bfe)), closes [#35](https://github.com/metalayerlab/interpose/issues/35)

## [0.3.3](https://github.com/metalayerlab/interpose/compare/v0.3.2...v0.3.3) (2026-11-04)


### ✨ Features

* **settings:** opt-in ${VAR} expansion in config files (design 0009 step 4) ([#44](https://github.com/metalayerlab/interpose/issues/44)) ([01456a4](https://github.com/metalayerlab/interpose/commit/01456a4b300f415ac48cffa818209d130ec6af0d)), closes [#32](https://github.com/metalayerlab/interpose/issues/32)

## [0.3.2](https://github.com/metalayerlab/interpose/compare/v0.3.1...v0.3.2) (2026-11-04)


### ✨ Features

* **settings:** aliases for renamed settings (design 0009 step 3) ([#42](https://github.com/metalayerlab/interpose/issues/42)) ([a4857b6](https://github.com/metalayerlab/interpose/commit/a4857b6597e858d03335165d4cefcfc4e7c70dd6)), closes [#33](https://github.com/metalayerlab/interpose/issues/33)

## [0.3.1](https://github.com/metalayerlab/interpose/compare/v0.3.0...v0.3.1) (2026-11-04)


### ✨ Features

* **settings:** warn on unknown keys and variables, with did-you-mean (design 0009 step 2) ([#40](https://github.com/metalayerlab/interpose/issues/40)) ([5d7b0db](https://github.com/metalayerlab/interpose/commit/5d7b0db1524299639b92d006d85371eeb200a5d0)), closes [#34](https://github.com/metalayerlab/interpose/issues/34)

## [0.3.0](https://github.com/metalayerlab/interpose/compare/v0.2.2...v0.3.0) (2026-11-04)


### ⚠ BREAKING CHANGES

* **settings:** an error about a value from the config file names the file's path instead of "file", e.g. "listen.port: expected int, got 'x' (from /etc/sluice/config.yaml)", and a check error for such a value now ends with the same "(from <path>)". Values and sources are unchanged.

### ✨ Features

* **settings:** resolve from layers with provenance (design 0009 step 1) ([#39](https://github.com/metalayerlab/interpose/issues/39)) ([2a5fbcd](https://github.com/metalayerlab/interpose/commit/2a5fbcdfbac41ac8b6af813fbce64f576d6aaae8)), closes [#36](https://github.com/metalayerlab/interpose/issues/36)


### 📚 Documentation

* propose settings extensions (design 0009) ([#37](https://github.com/metalayerlab/interpose/issues/37)) ([380fcf8](https://github.com/metalayerlab/interpose/commit/380fcf88f866a97046a781d112a4b47bf19a7989))

## [0.2.2](https://github.com/metalayerlab/interpose/compare/v0.2.1...v0.2.2) (2026-11-02)


### ⚡ Performance

* resolve skip_inherited overrides once per entry, not per call ([#28](https://github.com/metalayerlab/interpose/issues/28)) ([57a69fc](https://github.com/metalayerlab/interpose/commit/57a69fc7807daf1d530397e43bbd006919973f7e)), closes [#25](https://github.com/metalayerlab/interpose/issues/25)

## [0.2.1](https://github.com/metalayerlab/interpose/compare/v0.2.0...v0.2.1) (2026-11-02)


### 📚 Documentation

* present the components as pipeline, settings, metrics ([#26](https://github.com/metalayerlab/interpose/issues/26)) ([f287221](https://github.com/metalayerlab/interpose/commit/f287221e7d61a6074b769369373e19e8f53cb059))

## [0.2.0](https://github.com/metalayerlab/interpose/compare/v0.1.5...v0.2.0) (2026-11-02)


### ⚠ BREAKING CHANGES

* one_of no longer keeps the case a value was given in (LOG_LEVEL=info resolves to "INFO" when "INFO" is declared), and a check's non-None return value is no longer ignored.

### ✨ Features

* let checks replace values, one_of gives the declared spelling, typed Resolved accessors ([#22](https://github.com/metalayerlab/interpose/issues/22)) ([40aa8d3](https://github.com/metalayerlab/interpose/commit/40aa8d302cf0e65e02dab2dc9278feaed59fe384))

## [0.1.5](https://github.com/metalayerlab/interpose/compare/v0.1.4...v0.1.5) (2026-11-02)


### ⚡ Performance

* cut the guard's per-call cost ([#20](https://github.com/metalayerlab/interpose/issues/20)) ([a2d9362](https://github.com/metalayerlab/interpose/commit/a2d9362c369e169507b0eaaf6b40bc1e5f09a0b2))

## [0.1.4](https://github.com/metalayerlab/interpose/compare/v0.1.3...v0.1.4) (2026-11-02)


### ✨ Features

* treat hook results that come back after their limit as timeouts ([#18](https://github.com/metalayerlab/interpose/issues/18)) ([3d037a9](https://github.com/metalayerlab/interpose/commit/3d037a97ad997e24be5fc9c90709fbd10d954d4f))

## [0.1.3](https://github.com/metalayerlab/interpose/compare/v0.1.2...v0.1.3) (2026-11-02)


### 🐛 Bug Fixes

* skip_inherited counts instance hooks as overrides and still asks until ([#16](https://github.com/metalayerlab/interpose/issues/16)) ([9bbef78](https://github.com/metalayerlab/interpose/commit/9bbef78a186be0e7ed58aa4eceec4911dc096cd4))

## [0.1.2](https://github.com/metalayerlab/interpose/compare/v0.1.1...v0.1.2) (2026-11-02)


### ✨ Features

* pipeline core with per-project and per-middleware failure policy (design 0003) ([4692653](https://github.com/metalayerlab/interpose/commit/4692653217ecc743e9bd66121ea4a951c62351ce))

## [0.1.1](https://github.com/metalayerlab/interpose/compare/v0.1.0...v0.1.1) (2026-11-02)


### ✨ Features

* metrics with declared label sets (design 0002) ([44e4196](https://github.com/metalayerlab/interpose/commit/44e4196fae838803f676a1e696790eecad5f32da))

## 0.1.0 (2026-11-02)


### ✨ Features

* settings and secrets (design 0001) ([a67c229](https://github.com/metalayerlab/interpose/commit/a67c22925665b14c690e24ba9a967ed862e91a6e))
* settings and secrets (design 0001) ([19a70bd](https://github.com/metalayerlab/interpose/commit/19a70bde857079e5bbf0ea5152e3402c49ab634b))


### 🐛 Bug Fixes

* reject settings that share a variable or a non-string YAML key, and mask escaped secrets in check reasons ([69fc327](https://github.com/metalayerlab/interpose/commit/69fc3270c881cb52e31cac656db6d25ca3e3d660))


### 📚 Documentation

* design 0001, settings and secrets for v0.1 ([740989c](https://github.com/metalayerlab/interpose/commit/740989cd1e35c58685ae35afa7863e107e05385a))
* design 0001, settings and secrets for v0.1 ([e5787b6](https://github.com/metalayerlab/interpose/commit/e5787b6aea7d90d8ea5d5b42416b2073ea0bb00e))

## Changelog

Generated by [release-please](https://github.com/googleapis/release-please) from
Conventional Commit messages on `main`. Do not edit by hand: change the commit
or PR title instead. See the wiki's Release-Process page.
