import csv
import io
import json


def parse_json(text):
    return json.loads(text)


def parse_csv(text):
    return list(csv.DictReader(io.StringIO(text)))
