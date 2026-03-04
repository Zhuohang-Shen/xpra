## Fix Window Encoders section in Statistics tab

The "Window Encoders" section in the Statistics tab of the session info dialog
has two bugs that make it invisible and non-functional:

### Bug 1: Server doesn't expose window encoder info at the top level

`get_sources_info` places all source info under `info["client"][i]`, but the
session info dialog calls `server_last_info.get("window")` expecting it at the
top level. This always returns `None`, so no encoder stats are ever displayed.

**Fix:** After building `cinfo`, collect `sinfo["window"]` from each source and
expose the merged result as `info["window"]`.

### Bug 2: Title widget removed on every Statistics refresh

`populate_statistics` called `get_children()` and removed *all* children of
`encoder_info_box` including the "Window Encoders" title widget added in
`__init__`, then never re-added it. The section became invisible after the first
refresh cycle.

**Fix:** Only remove and replace encoder labels (children at index 1+), leaving
the title widget at index 0 untouched. Labels are only replaced when fresh data
is available.

### Additional improvements

- **Layout:** Changed `encoder_info_box` from `HBox` to `VBox` so multiple
  windows stack vertically rather than overflowing horizontally.
- **Spacing:** Added `margin_top=20` to match the padding used by the stats
  grid above.
- **Non-video encodings:** When no video encoder is active, falls back to
  `last_used` (e.g. `webp`, `png`) so the section always shows the current
  encoding rather than going blank.
- **Window titles:** Each entry now shows the window title (truncated to 40
  characters) alongside the window ID and encoder, making it easier to identify
  which window is using which encoder.
- **Tooltip:** The encoder detail tooltip now excludes the internal `"title"`
  key in addition to the encoder type name already shown in the label.
