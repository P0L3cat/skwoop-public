# Extra esp-sr models

Every directory here is packed into the `model` partition image next to the
models chosen in sdkconfig (see `tools/pack_srmodels.py`, run by the build).
Empty `wakenet/` and `multinet/` directories are fine: `idf.py build` still
packs the sdkconfig-selected models.

- `wakenet/wn9_<keyword>/` holds `_MODEL_INFO_`, `wn9_data`, `wn9_index` for a
  WakeNet9 model. Set `CONFIG_SKWOOP_WAKE_KEYWORD` to `<keyword>`. The loader
  also looks for `wn9_hiesp` when that directory is not packed.
- `multinet/` is reserved for MultiNet command models not shipped by esp-sr.

A Hey Skwoop model (`wakenet/wn9_heyskwoop`) is not in this tree.
