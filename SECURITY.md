# Security

Report a vulnerability to ndf@mail.grokbot.com. Please do not open a public issue for an unpublished security bug.

## Secrets

Never commit `.env`, `~/.skwoop/env`, or any file that holds a key. `.gitignore` ignores `.env`.

The bridge reads secrets only from the environment. The list and the placeholder values are in [.env.example](.env.example). Real values stay on the machine that runs the bridge (`chmod 600` the file).

Firmware does not compile API keys into the image. Menuconfig stores key-slot names; the key bytes go in encrypted NVS on the device. Do not put a key in `sdkconfig` or in a default.

The ElevenLabs voice is an environment variable listed in `.env.example`. Leave it unset in git. The placeholder in that file is not a real voice.
