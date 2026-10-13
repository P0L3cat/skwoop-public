#!/bin/sh
# Host tests without make: tests/firmware/run.sh [suite...]. IDF_PATH locates cJSON.
# Component sources live in the firmware tree, not next to this script.
set -e
cd "$(dirname "$0")"
IDF=${IDF_PATH:-$HOME/esp/esp-idf}
CJ="$IDF/components/json/cJSON"
CC="cc -O2 -Wall -Wextra -Werror"
C=../../firmware/components
mkdir -p build
run() { echo "== $1"; shift; $CC "$@" -o build/t -lm && ./build/t; }
for s in ${*:-net bridge audio face cloud clipcache wake cmds timers mood mqtt}; do
  case $s in
    net)    run net -I$C/net $C/net/prov_form.c net/test_prov_form.c ;;
    bridge) run bridge -I$C/bridge -I$CJ $C/bridge/bridge_msg.c $CJ/cJSON.c bridge/test_bridge_msg.c ;;
    audio)  run audio -I$C/audio $C/audio/resample.c audio/test_resample.c
            run endpoint -I$C/audio $C/audio/endpoint.c audio/test_endpoint.c
            run wav -I$C/audio $C/audio/wav.c audio/test_wav.c ;;
    face)   run face -I$C/face/include -I$C/face $C/face/face_geom.c face/test_face_mood.c ;;
    clipcache) run clipcache -I$C/clipcache $C/clipcache/clipcache.c clipcache/test_clipcache.c ;;
    wake)   run wake_pick -I$C/audio $C/audio/wake_pick.c wake/test_wake_pick.c
            SR=${ESP_SR:-../../firmware/managed_components/espressif__esp-sr}
            if [ -f "$SR/model/pack_model.py" ]; then python3 wake/test_pack.py "$SR"
            else echo "skip test_pack: no esp-sr at $SR (idf.py build fetches it, or set ESP_SR)"; fi ;;
    cmds)   run cmds -I$C/cmds $C/cmds/cmds.c cmds/test_cmds.c ;;
    timers) run timers -I$C/timers $C/timers/timers.c timers/test_timers.c ;;
    mood)
      BIN="$C/mood/mood_v11.bin"
      if [ ! -f "$BIN" ]; then
        echo "skip mood: $BIN absent (weights are not shipped). Generate it with firmware/tools/export_mood.py"
      else
        echo "== mood"
        $CC -I$C/mood $C/mood/mood.c mood/test_mood.c -o build/t -lm && ./build/t "$BIN" mood/golden.tsv
      fi
      ;;
    mqtt)   run mqtt -I$C/mqtt_lite $C/mqtt_lite/mqtt_codec.c mqtt/test_mqtt_codec.c
            run mqtt_url -I$C/mqtt_lite $C/mqtt_lite/mqtt_url.c mqtt/test_mqtt_url.c ;;
    cloud)  run cloud -I$C/cloud -I$CJ $C/cloud/cloud_proto.c $CJ/cJSON.c cloud/test_cloud_proto.c ;;
    *) echo "unknown suite $s"; exit 2 ;;
  esac
done
echo ALL OK
