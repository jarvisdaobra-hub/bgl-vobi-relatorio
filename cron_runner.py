import os
import sys

import requests


def main():
    url = os.environ["REPORT_RUN_URL"]
    token = os.environ["JOB_TOKEN"]
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    print(f"report_run status={response.status_code}")
    if response.status_code >= 400:
        print(response.text[:500])
        sys.exit(1)


if __name__ == "__main__":
    main()
