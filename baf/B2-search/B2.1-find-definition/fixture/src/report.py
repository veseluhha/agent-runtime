from lib.dates import parse_date


def report_day(record):
    return parse_date(record["day"])
