# Skwoop pod firmware

ESP-IDF firmware for the pod (target `esp32s3`). The pin map is written for a
Waveshare ESP32-S3-Touch-AMOLED-1.75C. This tree compiles and the host tests
pass. It has never run on hardware.

`libopus` under `components/opus/libopus` is a git submodule. Clone with
`--recurse-submodules`.

## Build

From this directory, with ESP-IDF exported (the component manifest asks for
IDF 5.3.2 or newer):

```sh
idf.py set-target esp32s3 && idf.py build
```

`sdkconfig.defaults` selects the target, a 32 MB partition table, and the
options the project is developed with. Host, port, and the provisioning SSID
prefix are in the menuconfig menu named **Skwoop** (`bridge.example`, port
`8003`, prefix `skwoop`). API keys are not compiled in; they are key-slot
names for encrypted NVS.

## What is not in the tree

Mood weights (`components/mood/mood_v11.bin`) are generated and are not
shipped. Without that file the build embeds nothing, mood stays disabled, and
the firmware logs that once. Generate the file with `tools/export_mood.py`
when you have the mood-model repository (needs numpy).

WakeNet and MultiNet directories under `models/` are optional. An empty
`models/wakenet` or `models/multinet` does not fail `idf.py build`; the image
still packs the models selected in sdkconfig. See `models/README.md`.

`skwoop_roots.pem` is a bundle of public CA certificates (GlobalSign Root CA,
ISRG Root X1, ISRG Root X2, GTS Root R1, GTS Root R4).

## Host tests

From the repository root:

```sh
cd tests/firmware && IDF_PATH=/path/to/esp-idf sh run.sh
```

The mood suite skips, with a message, when `mood_v11.bin` is absent. The
other suites do not need a device or generated weights.

## Components

- `main` — wires the board, face, Wi-Fi, bridge client, and (when
  `CONFIG_SKWOOP_STANDALONE` is on) the on-device voice loop.
- `board` — pins, AXP2101, button, and I2S for the board above.
- `face` — robot face on the round display.
- `net` — Wi-Fi station plus a provisioning softAP. The SSID prefix is
  `CONFIG_SKWOOP_AP_SSID_PREFIX`.
- `bridge` — HTTP check-in and websocket client for the bridge host
  (`CONFIG_SKWOOP_BRIDGE_HOST` and `CONFIG_SKWOOP_BRIDGE_PORT`).
- `audio` — resample, end-point, WAV, Opus frames, the voice path, and a
  WakeNet loader. A custom model under `models/wakenet/` is optional.
- `opus` — libopus.
- `cloud` — OpenAI-compatible STT and chat, and a pluggable TTS provider
  (OpenAI-compatible or ElevenLabs). The ElevenLabs voice default is the
  placeholder `your-voice-id`.
- `mood` — sentence mood from an optional int8 table. Disabled when the
  weights file is missing.
- `cmds` — phrase table the app can hand to MultiNet when that model is packed.
- `timers` — alarms and a countdown. The POSIX TZ is `CONFIG_SKWOOP_TZ`.
- `mqtt_lite` — small MQTT client. Empty broker URL leaves it off.
- `clipcache` — clip files on the storage partition.
- `storage` — encrypted NVS and the FAT storage partition.
- `skwoop` — config store, fact memory, clip index fetch, and the on-pod
  harness used by the standalone loop.

## Status

`idf.py build` for `esp32s3` completes, and `tests/firmware/run.sh` passes
(mood skips until weights are supplied). Nothing in this tree has been run
on a device.
