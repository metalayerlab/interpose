## interpose

[GitHub](https://github.com/metalayerlab/interpose) • [Releases](https://github.com/metalayerlab/interpose/releases)

---

### 📚 Getting Started

- [Home](Home)
- [Introduction](Introduction)
- [Quick Start](Quick-Start)

### 📖 Core Concepts

<!-- Project pages go here, e.g. - [How Matching Works](How-Matching-Works) -->
- [API Reference](API-Reference)

### 🔒 Security

- [Security](Security)

### 🆘 Help & Support

- [FAQ](Frequently-Asked-Questions)
- [Troubleshooting](Troubleshooting)

### 🧑‍💻 For Developers

- [Contributing](Contributing)
- [Architecture Internals](Architecture-Internals)
- [Testing Guide](Testing-Guide)
- [Release Process](Release-Process)

---

### 🔑 Key Features

- 🧅 One middleware loop; fail open or closed is policy, per project and per middleware
- 🥞 One lookup order: `_FILE` > environment > YAML > default
- 🧱 Strict types: no silent `0` for a YAML `0.5`
- 🧭 Where each value came from, for every setting
- 🔒 `Secret`: masked in logs, reprs and errors
- 📈 Prometheus text with labels from declared value sets
