Read all notes in `notes/` and create `catalog.csv` in the workspace root.

The CSV must have this header:

`id,title,priority`

Include one row per note, sorted by `id` ascending. Also create a marker file under `processed/` for each note named `<id>.done` containing `processed`.
