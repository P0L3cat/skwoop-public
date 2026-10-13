# Skwoop

![Skwoop face](face/assets/skwoop-face.gif)

Skwoop is an open-source desk companion: a small round-display ESP32-S3 pod that listens, thinks with an LLM, talks back, and shows a face.

The pod firmware in this repository compiles and its host tests pass. It has never run on hardware. What runs today is the bridge, with a browser "web pod" standing in for the device. The static face above is the same drawing the web pod uses.

GitHub Pages can serve the `face/` directory as a live demo of the drawing. It does not listen or speak.

## Repository map

| Path | What it is |
| --- | --- |
| `firmware/` | ESP-IDF firmware for the pod (target `esp32s3`) |
| `bridge/` | Host service: speech, routing, tools, and the web pod |
| `face/` | Static web face (also what Pages would publish) |
| `tools/` | Host utilities, including the clip-index builder |
| `docs/` | Architecture and the other public docs |
| `tests/` | Tests only. Nothing test-related lives in the product trees |

## Quickstart

### Face

From `face/`:

```sh
python3 -m http.server
```

Open <http://localhost:8000/>. `?face=dot` draws the sample face. `?pad=1` cycles named expressions. See [face/README.md](face/README.md) and [face/MAKE-YOUR-OWN-FACE.md](face/MAKE-YOUR-OWN-FACE.md).

### Bridge

The bridge is a Python process. It can run on any computer. Secrets stay in the environment, never in the repo.

```sh
python3 -m venv bridge/.venv
bridge/.venv/bin/pip install -r bridge/requirements.txt
# libopus from the OS (Debian: libopus0; the headers package is libopus-dev)
bridge/.venv/bin/python bridge/dsp.py build
mkdir -p ~/.skwoop
cp .env.example ~/.skwoop/env
chmod 600 ~/.skwoop/env
# fill in keys in ~/.skwoop/env, then:
bridge/run.sh
```

`bridge/run.sh` loads `~/.skwoop/env` (`SKWOOP_ENV_FILE` overrides that path) and starts `bridge/skwoop_bridge.py` on port 8003. The variable list is [.env.example](.env.example).

The web pod is the browser stand-in. It needs a local TLS certificate before a browser will share the microphone:

```sh
bridge/webpod/make_cert.sh
bridge/webpod/run.sh
```

That serves the page from `bridge/webpod/server.py` and speaks the same websocket protocol as the pod firmware.

### Firmware

Clone with submodules (`firmware/components/opus/libopus` is upstream libopus). ESP-IDF 5.3.2 or newer (`firmware/main/idf_component.yml` asks for `>=5.3.2`). From `firmware/`, with IDF exported:

```sh
idf.py set-target esp32s3 && idf.py build
```

Host tests, from the repository root (no device):

```sh
IDF_PATH=/path/to/esp-idf sh tests/firmware/run.sh
```

Details: [firmware/README.md](firmware/README.md).

### Tools

`tools/build_clip_index.py` writes a `clips.idx` for a clip library root. It does not read environment variables.

```sh
python3 tools/build_clip_index.py /path/to/library
```

## Status

Honest scope of this tree:

- Firmware compiles for `esp32s3` and `tests/firmware/run.sh` passes on a host. **It has never run on hardware.** No battery, speed, or offline behavior has been measured, and none is claimed.
- The bridge runs today. A browser web pod stands in for the device. There is no claim that a wake word works. The firmware can load a WakeNet model, but the preferred "heyskwoop" model is not in the tree, and that path has not been tried on a device.
- No latency numbers are published here.
- Mood-model weights are **CC-BY-SA** and are distributed separately. They are not in this repository. Without `firmware/components/mood/mood_v11.bin` the firmware leaves mood disabled.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — what runs where, and how a turn moves
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, tests, coding standard
- [SECURITY.md](SECURITY.md) — how to report a problem

Commits are authored by **Grok Bot** (AI-assisted, human-reviewed).

## License

Skwoop is MIT. See [LICENSE](LICENSE). Copyright (c) 2026 The Skwoop Authors.

`firmware/components/opus/libopus` is a git submodule of upstream [xiph/opus](https://github.com/xiph/opus) and stays under its BSD license (`COPYING` in that directory). It is not part of the MIT grant.
