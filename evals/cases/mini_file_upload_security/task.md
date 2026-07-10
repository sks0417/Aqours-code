Fix the file upload path traversal vulnerability in `src/uploader.py`.

Uploads must stay inside the selected user's upload directory. Attempts such as `../../secret.txt` must be rejected. Normal filenames, including filenames with dots like `report.v1.txt`, must still work.

Do not modify anything under `tests/`. Run the tests before you finish.
