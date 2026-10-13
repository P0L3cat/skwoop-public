# Contributing

Skwoop is the desk companion in this repository: pod firmware, the bridge host service, and the web face. Commits are authored by Grok Bot (AI-assisted, human-reviewed).

## Setup

Python 3.12 is what CI uses. A newer 3.x is fine for local work if the tests pass.

```sh
python3 -m venv bridge/.venv
bridge/.venv/bin/pip install -r bridge/requirements.txt ruff
```

Install libopus from the OS (Debian/Ubuntu: `libopus-dev`, which also pulls in the runtime library the bridge loads). Build the small C extension the bridge imports:

```sh
bridge/.venv/bin/python bridge/dsp.py build
```

Copy [.env.example](.env.example) to `~/.skwoop/env` and `chmod 600` it if you run the bridge. Tests do not need real keys.

Firmware host tests need ESP-IDF 5.3.2 or newer (the version this tree is developed against is 5.3.2). Export it so `IDF_PATH` is set. A full `idf.py build` also needs the usual IDF tools and a submodule checkout of libopus.

The face check needs Node and a Chrome (or Chromium) binary. `CHROME_PATH` points at one if it is not on the default path.

## Gates

From the repository root, with the virtualenv active:

```sh
ruff check bridge
python -m pytest -q
python3 tests/face/check_face.py
IDF_PATH=/path/to/esp-idf sh tests/firmware/run.sh
```

`ruff check bridge` uses `bridge/ruff.toml`. Pytest uses `pytest.ini` (`pythonpath = bridge`, tests under `tests/`).

Firmware image, from `firmware/` with IDF exported:

```sh
idf.py set-target esp32s3 && idf.py build
```

CI (`.github/workflows/ci.yml`) runs the same four jobs: Python (ruff and pytest), the face check, firmware host tests, and `idf.py build`.

## Coding standard

Apply this to code you touch. Do not reformat unrelated files.

- Parse untrusted input once at the boundary into a typed model. Do not parse it again later.
- No chained casts. If a cast or a `# type: ignore` stays, put a `# SAFETY:` comment on the line above it that says why the value is actually that type.
- Do not use `Any` or `dict[str, Any]` as a contract. A parsing boundary may hold raw JSON until it is validated.
- Do not `mock.patch` a module path. Pass a fake into the function under test.
- Blank line between top-level declarations.
- Names say what the thing is.
- Tests live only under `tests/`. No fakes, fixtures, benches, or test hooks in `bridge/`, `face/`, `firmware/`, or `tools/`.

## What not to claim

Firmware has never run on hardware. Do not add claims about speed, battery, offline use, or a working wake word, and do not add latency numbers.
