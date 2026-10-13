# Clip index (clips.idx)

Byte layout for the on-flash clip index. Little-endian. Every offset and record is 4-byte aligned. A C reader can walk it from a flash mapping with no heap at open.

## Header

| Field | Type | Value |
| --- | --- | --- |
| magic | 8 bytes | `SKIDX\0\0\0` |
| version | u32 | 1 |
| flags | u32 | 0 |
| manifest_sha256 | 32 bytes | sha256 of manifest.json |
| section_count | u32 | number of section-table entries that follow |

## Section table

Each entry: u32 kind, u32 offset, u32 count, u32 record_size.

## 1. Lookups

Built by `SkwoopLibrary.load` from `manifest.json` (`parse_manifest` / `load_manifest`). Keys that are spoken text use `norm` (same key `find` probes with). `norm` maps curly apostrophes to `'`, rewrites `N%`, temperatures, and bare integers through `stitch.pct_words` / `stitch.temp_words` / `stitch.num_words`, then `stitch._words_key` (lowercase, `-` to space, drop characters outside `[a-z0-9' ]`, collapse whitespace).

Mood strings in the index are the manifest's, not aliases. `speak_mood` runs at lookup time (`sleep`/`sleepy` → `calm`, `neutral`/`thinking`/`listening`/`speaking`/empty → `idle`). `_pick_mood` then tries that mood, then `MOOD_FALLBACK` (`idle`, `happy`, `calm`, `alert`, `sad`), then the first stored pair.

`find` returns `None` before any of these tables when the library is off, `self.m` is missing, `text` is empty, `NONVERBAL` matches, or `norm(text)` is empty.

### whole

Key: `norm(sentence)` (UTF-8). Value: `(mood, clip id)` pairs in precedence order (the tuple `load` / `_put_whole` keeps).

How `load` fills it, in order:

1. Each `manifest.whole` entry, object order. Raw key is `tid|mood|{json values}` (split on `|` twice; the rest is a JSON object). Clip id is the value. Skip the entry when `tid` is not in `templates` or the clip id is not in `clips`. Sentence key is `norm(template.sentence.format(**values))`. `_put_whole(..., overwrite=True)`: a new mood is appended; the same mood replaces the clip id in place and keeps its position.
2. Each `manifest.lines` entry. Raw key is `kind|mood`. For each `{text, id}`, if `id` is in `clips` and `NONVERBAL` does not match `text`, `_put_whole(norm(text), mood, id, overwrite=False)`. An existing mood is left alone, so a whole-sentence clip beats a line for that mood. A new mood is appended after the whole-sentence moods.

Two sentences that `norm` to the same string share one key; their moods merge under those overwrite rules.

Consumer: `SkwoopLibrary.find`. `self.whole.get(norm(text))`, then `_pick_mood` over the pair moods, then the clip id of the chosen mood. That is the `Found(how="whole", clip_id=...)` path. `render` plays it with `clip_pcm`.

### slot_values

Key: slot kind, then `norm(value)`. Value: the value string as stored on the slot key (library casing, hyphens, the words `norm` undoes).

`load` walks `manifest.slots`. Each key splits on `|` into five fields (`split("|", 4)`): `kind|left word|right word|mood|value`. `value` may itself contain `|`. `slot_values[kind][norm(value)] = value`. The same `(kind, norm(value))` keeps the last write.

Consumer: `SkwoopLibrary._find_b2`, called from `find` for each b2 pattern. The regex capture is already in `norm` space (`hit.group(slot)`); this map turns it back into the value `_b2_pieces` puts on the slot key. A miss rejects the pattern.

### titles

Key: `norm(tv title)`. Value: `values["title"]` from that whole entry (the library's casing).

`load` writes this only while scanning `manifest.whole`, and only when `tid == "tv_title"`. Later entries with the same `norm` replace earlier ones.

Consumer: `SkwoopLibrary.display_title` (not `find` / `render`). Hit returns the stored casing. Miss returns `title` unchanged when it already has an uppercase letter, otherwise `title.title()`.

### slots

Key: the manifest key `kind|left word|right word|mood|value`. Value: clip id.

`left` / `right` are the neighbour words `_b2_pieces` computes, not `norm`'d further. A missing neighbour is Python `None`, which the f-string stores as the four characters `None` (fixture: `wx|and|None|happy|sunny`). `value` is the `slot_values` original, not the `norm` form. One record per manifest key.

Consumer: `SkwoopLibrary._b2_pieces` (from `_find_b2`). For a `{slot}` piece at index `i` of `re.split(r"(\{\w+\})", sentence)`:

`m.slots[f"{k}|{left[-1] if left else None}|{right[0] if right else None}|{mood}|{values[k]}"]`

`left` / `right` come from `_words` on the previous and next pieces (`[A-Za-z0-9']+`). A missing clip id, or an id not in `clips`, fails the whole piece list.

### carriers

Key: `tid|part index|mood`. Value: clip id. `part index` is `i // 2` for that same `re.split` index `i` (fixture: `weather_now|0|idle` is the leading "Right now it's " carrier). One record per manifest key.

Consumer: `SkwoopLibrary._b2_pieces`. A literal piece is a carrier only when it has words and it is not a one-word link strictly between the ends (`0 < i < len(parts) - 1` and one word). Those links ride on the neighbour overlap and have no carrier record. Same miss rule as slots.

### lines

Key: `kind|mood` (the manifest key, e.g. `ack|idle`, `tv_ok|happy`). Value: the list of `{text, id}` in manifest order. Non-verbal lines stay in this table. `load` only drops them when copying into `whole`.

Consumers:

- `SkwoopLibrary.has_lines(kind)`: true if any key's kind (text before the first `|`) equals `kind`.
- `SkwoopLibrary.pick_line(kind, mood)`: group by mood, `_pick_mood`, drop `NONVERBAL`, `random.choice` avoiding the last text stored in `_last[(kind, mood)]`, then `clip_pcm(id)`.
- `load`, indirectly: the non-verbal filter above when folding lines into `whole`. `find` does not read this table; it sees those lines only through `whole`.

### templates and b2 patterns

Templates, manifest object order. Each `Template` is `id`, `sentence`, `moods` (tuple, manifest order), `mode` (`"b2"` only when the JSON mode is exactly `"b2"`, otherwise `"whole"`). `tier` is not kept. Small table: parsed once at open, not binary-searched.

Consumers: `load` (format the whole-sentence key, and build b2 patterns), `_find_b2` (`templates[id].moods` is the mood list `_pick_mood` and the retry order use), `_b2_pieces` (the sentence split), `describe` (counts and the b2 id list only).

b2 patterns: one per template with `mode == "b2"`, in that same template order (`weather_now` in the fixture; the module also names `room_pct` and `room_color` when they are present). `SkwoopLibrary._b2_pattern` splits the sentence on `(\{\w+\})`. A `{name}` piece appends `name` to `slots` and `(?P<name>.+?)` to the regex. Any other piece with a non-empty `norm` appends `re.escape(norm(piece))`. Pieces that `norm` to empty (punctuation) are omitted. The compiled pattern is `"^" + " ".join(parts) + "$"`.

Consumer: `find`, which tries `self.b2` in list order and stops at the first `_find_b2` hit. First match wins over a later template.

### clip id → file

Key: clip id (manifest `clips` key, e.g. `44f0e87875b0`). Value: `Clip.file` (a path relative to the library root, e.g. `clips/44f0e87875b0.wav`) and the file's length in bytes. `Clip.mood` is checked by `parse_manifest` and then dropped; mood lives on the lookup keys. `Clip.text` is the transcript `render` passes to `stitch.join` for a b2 piece; it is not a column of the clips table below.

Consumers:

- `clip_pcm`: `_path` is `root / clips[cid].file`. Bytes are `stitch._fade(stitch._trim(stitch._load(path)))` then `from_floats_trunc(..., 32767.0)` (s16le mono at 24 kHz). An LRU of `PCM_CACHE` (48) decoded buffers is the only per-clip memory, and it is filled on use, not at open. `render` calls this for a whole hit and for `pick_line`.
- `render`, b2 arm: `stitch.join([(clips[c].text, root / clips[c].file, {}) for c in found.pieces], profile="overlap")`. `v3_fallback` is true when a piece's join report contains `"fallback"` (no align sidecar). `OSError`, `ValueError`, or `KeyError` become a live-TTS miss, not an index error.
- `load` / `_b2_pieces`: reject clip ids that are not in this table.

## 2. Byte layout

Little-endian. Multi-byte integers are `u32` unless noted. Every section offset and every fixed record starts on a 4-byte boundary. Absolute offsets are from the first byte of the file. The reader uses the section table; it does not infer a section's offset from its kind.

A **string ref** is two `u32`s, `(offset, length)`, into the string pool: `offset` is the first UTF-8 byte, `length` is the byte count (not code points, not including a NUL, not including pad). `offset` itself is 4-byte aligned. Comparison and sort are bytewise on those `length` bytes.

A **value range** is two `u32`s, `(start, count)`: records `[start, start+count)` of another section. `start` is a record index, not a byte offset.

### Header (52 bytes)

| Offset | Field | Type | Value |
| --- | --- | --- | --- |
| 0 | magic | 8 bytes | `SKIDX\0\0\0` (`53 4b 49 44 58 00 00 00`) |
| 8 | version | u32 | 1 |
| 12 | flags | u32 | 0 |
| 16 | manifest_sha256 | 32 bytes | raw SHA-256 of the bytes of `manifest.json` (not hex, not of the parsed object) |
| 48 | section_count | u32 | 13 |

### Section table

Starts at byte 52. `section_count` entries of 16 bytes. This file has 13 entries (208 bytes), so the table occupies `[52, 260)`.

| Offset in entry | Field | Type |
| --- | --- | --- |
| 0 | kind | u32 |
| 4 | offset | u32, 4-byte aligned, absolute |
| 8 | count | u32 |
| 12 | record_size | u32 |

Span of a section is `[offset, offset + count * record_size)`. The end may be unaligned only for kind 1, 12, and 13 (`record_size` 1). The next section starts at the next 4-byte boundary at or after that end. Padding bytes in the gap are `0x00` and are part of no section. `count * record_size` must not wrap.

Writer emits the thirteen kinds below once each, in this order, with ascending offsets and no overlap. Empty tables are present with `count` 0 and a 4-byte-aligned `offset`.

| Kind | Name | record_size | count | Sorted by |
| --- | --- | --- | --- | --- |
| 1 | string pool | 1 | pool bytes, including 0–3 trailing zeros so the pool's padded end is 4-aligned | not sorted; not searched |
| 2 | whole | 16 | sentences | key, bytewise UTF-8 |
| 3 | whole_pairs | 16 | pairs | not sorted; precedence order inside each whole range |
| 4 | slot_values | 16 | slot kinds | kind, bytewise UTF-8 |
| 5 | slot_value_ents | 16 | values | grouped by kind; each kind's range is sorted by `norm(value)` |
| 6 | titles | 16 | titles | key, bytewise UTF-8 |
| 7 | slots | 16 | slot keys | key, bytewise UTF-8 |
| 8 | carriers | 16 | carrier keys | key, bytewise UTF-8 |
| 9 | lines | 16 | `kind\|mood` keys | key, bytewise UTF-8 |
| 10 | line_items | 16 | line clips | not sorted; manifest order inside each lines range |
| 11 | clips | 20 | clips | clip id, bytewise UTF-8 |
| 12 | templates_json | 1 | JSON byte length | not sorted; parsed once at open |
| 13 | b2_json | 1 | JSON byte length | not sorted; parsed once at open |

`u32` file length tops out at 4 GiB − 1. These clips are short wavs. A builder that sees a longer file fails the build; it does not truncate the length.

### String pool (kind 1)

UTF-8 bytes. Identical strings may share one span. Each string is followed by 0–3 `0x00` bytes so the next string's `offset` is 4-byte aligned. `length` does not include that pad. The pool's own `count` includes its final pad, so `count` is a multiple of 4 and `record_size * count` stays 4-aligned.

No string is NUL-terminated for the reader. A `length` of 0 is the empty string; its `offset` may be 0.

### Fixed records

**whole (kind 2), 16 bytes.** One record per `norm(sentence)` key.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `norm(sentence)` |
| 8 | pairs | value range into kind 3 |

**whole_pairs (kind 3), 16 bytes.** Precedence is the `_put_whole` tuple order: manifest `whole` moods first (same mood overwritten in place), then line moods that were not already present (`overwrite=False`).

| Offset | Field | Type |
| --- | --- | --- |
| 0 | mood | string ref |
| 8 | clip_id | string ref |

**slot_values (kind 4), 16 bytes.** One record per slot kind.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, kind (`temp`, `wx`, …) |
| 8 | values | value range into kind 5 |

**slot_value_ents (kind 5), 16 bytes.** One record per `(kind, norm(value))`. Last manifest write wins.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `norm(value)` |
| 8 | value | string ref, library casing |

**titles (kind 6), 16 bytes.** One record per `norm(title)`. Last `tv_title` whole entry wins.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `norm(title)` |
| 8 | value | string ref, `values["title"]` |

**slots (kind 7), 16 bytes.** Key text is the manifest key, including a literal `None` neighbour.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `kind\|left\|right\|mood\|value` |
| 8 | clip_id | string ref |

**carriers (kind 8), 16 bytes.**

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `tid\|part index\|mood` |
| 8 | clip_id | string ref |

**lines (kind 9), 16 bytes.** One record per manifest `kind|mood` key. Items include non-verbal lines.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | key | string ref, `kind\|mood` |
| 8 | items | value range into kind 10 |

**line_items (kind 10), 16 bytes.** Manifest order. `pick_line` still filters `NONVERBAL` and then chooses at random.

| Offset | Field | Type |
| --- | --- | --- |
| 0 | text | string ref |
| 8 | id | string ref |

**clips (kind 11), 20 bytes.** Sorted by clip id. `C` layout: `uint32_t id_off, id_len, file_off, file_len, nbytes` (20 bytes, 4-aligned).

| Offset | Field | Type |
| --- | --- | --- |
| 0 | id | string ref |
| 8 | file | string ref, path relative to the library root |
| 16 | nbytes | u32, size of that file in bytes |

`Clip.text` is not in this record. `render` still needs it for `stitch.join` on a b2 hit; that transcript stays in `manifest.json` (`clips[id].text`) until a later revision adds a string ref. Opening the index does not allocate a clip object per row.

### JSON blobs (kinds 12 and 13)

`record_size` is 1, `count` is the UTF-8 byte length, no trailing NUL required. Parsed once at open into the small template list and the b2 list. Not one object per clip. UTF-8 JSON, no comments.

Kind 12, templates, manifest object order. Only the fields `Template` keeps (`tier` is dropped):

```json
[
  {"id": "weather_now", "sentence": "Right now it's {temp} and {wx}.", "moods": ["happy", "idle"], "mode": "b2"}
]
```

`mode` is `"b2"` or `"whole"` using the same rule as `parse_manifest`.

Kind 13, b2 patterns, only `mode == "b2"`, same relative order `load` appends to `self.b2`. `pattern` is the source `_b2_pattern` compiles (`^` + `re.escape(norm(literal))` and `(?P<name>.+?)` joined by single spaces + `$`). `slots` is those names in sentence order:

```json
[
  {"template": "weather_now", "slots": ["temp", "wx"], "pattern": "^right\\ now\\ it's (?P<temp>.+?) and (?P<wx>.+?)$"}
]
```

That `pattern` is what `_b2_pattern` builds for the fixture sentence `Right now it's {temp} and {wx}.`: `norm` of the leading literal is `right now it's`, `re.escape` escapes the spaces and leaves the apostrophe, the one-word `and` is its own piece, and the trailing `.` is dropped because `norm` is empty. The builder stores that source string, the one `re.compile` receives. `find` tries the array in order; the first regex hit whose slots all resolve wins. Moods for that hit come from the kind-12 object with the same `id`, not from this blob.

## 3. Reader rules

The index is a read-only `mmap` of `clips.idx`. Lookups are `struct.unpack_from` on that mapping plus a binary search. Open keeps the mapping and thirteen `(offset, count, record_size)` descriptors, and it parses kinds 12 and 13 once. It does not build a `Clip`, a path object, or a decoded PCM buffer per clip. `clip_pcm`'s LRU (48 entries) still fills only when a clip is played.

Binary search is over a fixed-record section, or over a `(start, count)` subrange of one. Probe keys are the same bytes the runtime uses (`norm(text)`, a slot kind, a composed `kind|left|right|mood|value` key, a clip id). Compare those bytes to the string-ref slice `mmap[pool + off : pool + off + length]`. Ordering is bytewise unsigned, not Unicode casefold and not code-point order.

```text
lo, hi = start, start + count
while lo < hi:
    mid = (lo + hi) // 2
    rec = section_offset + mid * record_size
    koff, klen = struct.unpack_from("<II", mm, rec)
    key = mm[pool + koff : pool + koff + klen]
    if key < probe: lo = mid + 1
    elif key > probe: hi = mid
    else: hit mid
```

Who searches what:

| Caller | Section | Probe |
| --- | --- | --- |
| `find` | kind 2 whole | `norm(text)`. On a hit, walk the kind-3 range in order only to apply `_pick_mood` (requested mood, then `MOOD_FALLBACK`, then pair 0). Do not binary-search pairs. |
| `find` → `_find_b2` | kind 4, then the kind-5 subrange | slot kind, then `norm` of the regex capture. Patterns themselves come from the kind-13 list, tried in order, not searched. |
| `_b2_pieces` | kind 7 slots, kind 8 carriers | the f-string keys in section 1. Clip ids are then searched in kind 11. |
| `display_title` | kind 6 titles | `norm(title)` |
| `has_lines` | kind 9 lines | lower-bound the first key ≥ `kind + "\|"` and scan while the key starts with that prefix. `\|` is `0x7C`, so a longer kind such as `ack2` sorts before `ack\|` and is not part of the scan. |
| `pick_line` | kind 9, then kind 10 | exact key `kind\|mood` for each candidate mood from `_pick_mood`. Items stay in range order; the random choice and the `NONVERBAL` drop happen after the search. |
| `clip_pcm` / `render` | kind 11 clips | clip id. The file string is `root /` that relative path. `nbytes` is the expected wav size for the read. PCM decode stays on the existing `stitch._load` / `_trim` / `_fade` path. |

`render` of a b2 hit still reads each piece's transcript from `manifest.json` (`Clip.text`) because kind 11 has no text column. That read is per played sentence, not per clip at open.

### Validation

Check in this order. The first failure wins. Do not hash the manifest for a file that is already `IndexMissing` or `IndexCorrupt`.

**`IndexMissing`.** The `clips.idx` path does not exist (`ENOENT`). No mapping. A short or unreadable file that does exist is not this error.

**`IndexCorrupt`.** Anything that means the bytes are not this layout:

- Mapping shorter than 52 bytes, or shorter than `52 + section_count * 16`.
- Magic is not `SKIDX\0\0\0`.
- `version` is not 1.
- `flags` is not 0.
- `section_count` is not 13, a kind is missing, duplicated, or outside 1..13.
- A section `offset` is not 4-byte aligned, or `offset` is past the mapping.
- `record_size` is not the size in the kind table above.
- `offset + count * record_size` wraps or lands past the mapping.
- Two section spans overlap. The 0-pad between a blob's unaligned end and the next aligned offset is not overlap. A `count` of 0 is an empty span and does not overlap.
- A value range's `start + count` is past the target section's `count` (kind 2 → 3, kind 4 → 5, kind 9 → 10).
- A string ref's `offset` is not 4-byte aligned, or `offset + length` is past the kind-1 pool.

Do this bounds walk at open. It touches each record and allocates no clip object. Invalid UTF-8 is not `IndexCorrupt`; searches compare bytes.

**`IndexStale`.** The header and the section table are well-formed, and the stored `manifest_sha256` does not match `manifest.json`. Stream the file. Do not `read()` it into one `bytes` and do not `json.loads` it for this check:

```text
h = hashlib.sha256()
with manifest.json opened "rb" as f:
    for chunk in iter(lambda: f.read(65536), b""):
        h.update(chunk)
```

Compare `h.digest()` (32 raw bytes) to header bytes `[16, 48)`. Unequal → `IndexStale`. If `manifest.json` cannot be opened, there is nothing to match the stored digest → `IndexStale` as well. Chunk size may be any fixed size; 65536 is the one this note specifies. The full manifest is loaded later, only if the digest matches, by the existing `load_manifest` path.
