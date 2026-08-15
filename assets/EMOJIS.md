# BigV application emojis

BigV looks for these application emoji names once during startup:

- `bigv_verify`
- `bigv_success`
- `bigv_error`
- `bigv_warning`
- `bigv_shield`
- `bigv_lock`
- `bigv_code`
- `bigv_help`
- `bigv_role`
- `bigv_channel`
- `bigv_repair`

Upload the PNG files from `assets/emojis/` in the Discord Developer Portal under
your BigV application. Use each PNG filename without `.png` as its application
emoji name. Restart BigV after uploading so its startup cache sees the new
emojis.

All names are optional. BigV uses Unicode fallbacks whenever an expected
application emoji is unavailable, so verification never depends on emoji
upload or loading.

`bigv_shield.png` comes from the canonical logo provided by the BigV project
owner. The other files are branded derivatives of Google Material Icons Round;
see `THIRD_PARTY.md` and `licenses/material-icons-APACHE-2.0.txt`.
