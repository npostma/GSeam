# Tool-library conversion examples

```bash
# sync a tool.tbl from the Fusion 360 export; if ./tool.tbl already exists
# its measured Z offsets are PRESERVED (default --z-source preserve) and a
# diff + .bak backup are produced
python3 ../../f360_toollib_convert.py Library.json -o tool.tbl

# see what would change without writing
python3 ../../f360_toollib_convert.py Library.json -o tool.tbl --dry-run

# force a fixed Z length for all tools instead
python3 ../../f360_toollib_convert.py Library.json -o tool.tbl --z-value -142.357
```
