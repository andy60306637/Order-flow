# Wick Reversal v4 Band Files

Each JSON file stores one editable parameter set for one price band.

Current generated layout:

- Symbol: `BTCUSDT`
- Range: `0` to `15000`
- Band size: `1000`

File naming:

- `00000_01000.json` means `[0, 1000)`
- `10000_11000.json` means `[10000, 11000)`

Regenerate defaults from the original `v4` values with:

```powershell
python utils\generate_wick_reversal_v4_band_params.py --symbol BTCUSDT --start 0 --end 15000 --step 1000
```
