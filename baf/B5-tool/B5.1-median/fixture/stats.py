import csv
import statistics
import sys

with open(sys.argv[1], newline="") as source:
    values = [int(row["value"]) for row in csv.DictReader(source)]
print(statistics.median(values))
