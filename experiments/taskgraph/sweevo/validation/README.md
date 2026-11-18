# Gold-patch validation

Made by `validate_gold.py`; one JSON file per instance has the details. Counts are after the excluded tests are left out. Base: the base commit with the release's tests (F2P should all fail, P2P pass). Gold: the base commit plus the gold patch (all should pass). Grading: one `sweevo-eval` of a run, from copying `final/` to parsing the log; test run: the test command alone. Times were measured with up to four instances graded at once.

| Instance | F2P | P2P | Excluded | Base F2P passing | Base P2P failing | Gold F2P | Gold P2P | Gold resolved | Grading (s) | Test run (s) | Instance image (GB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| iterative__dvc_1.1.7_1.1.8 | 8 | 82 | 0 | 0/8 | 0 | 8/8 | 82/82 | yes | 17 | 13 | 4.77 |
| dask__dask_2023.6.1_2023.7.0 | 5 | 707 | 0 | 0/5 | 0 | 5/5 | 707/707 | yes | 81 | 75 | 8.61 |
| iterative__dvc_1.6.3_1.6.4 | 9 | 2 | 0 | 0/9 | 0 | 9/9 | 2/2 | yes | 24 | 20 | 4.87 |
| dask__dask_2023.8.0_2023.8.1 (pilot) | 11 | 2796 | 1 | 0/11 | 0 | 11/11 | 2795/2795 | yes | 244 | 237 | 8.61 |
| iterative__dvc_0.89.0_0.90.0 (pilot) | 27 | 282 | 1 | 0/27 | 0 | 27/27 | 281/281 | yes | 69 | 65 | 4.79 |
| iterative__dvc_2.19.0_2.20.0 | 14 | 66 | 0 | 0/14 | 0 | 14/14 | 66/66 | yes | 26 | 21 | 4.85 |
| pydantic__pydantic_v2.7.0_v2.7.1 | 234 | 1477 | 0 | 0/234 | 0 | 234/234 | 1477/1477 | yes | 24 | 18 | 4.90 |
| dask__dask_2023.6.0_2023.6.1 | 105 | 3415 | 0 | 0/105 | 0 | 105/105 | 3415/3415 | yes | 928 | 921 | 8.61 |

## Dropped

With the gold patch and no exclusions (these are not used):

- `iterative__dvc_3.43.1_3.44.0`: F2P 2/22 and P2P 104/104 pass. 20 of its 22 F2P tests fail with the gold patch: the release needs scmrepo 3, which our offline image cannot install
- `iterative__dvc_3.4.0_3.5.0`: F2P 1/2 and P2P 21/26 pass. 1 of its 2 F2P tests and 5 of its 26 P2P tests fail with the gold patch: the release needs dvc-data 2.5, which our offline image cannot install
