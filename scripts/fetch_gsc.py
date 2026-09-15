#!/usr/bin/env python3
"""OAuth connector for Google Search Console — authenticate once, then query
search analytics data.

Setup (one-time, requires browser access) — see SETUP.md in this skill for
the full walkthrough:
  1. console.cloud.google.com -> select/create a project
  2. APIs & Services > Library -> enable "Google Search Console API"
  3. APIs & Services > OAuth consent screen -> External -> add the Google
     account that has access to the GSC property as a test user
  4. APIs & Services > Credentials -> Create Credentials -> OAuth client ID
     -> Application type: Desktop app -> Download JSON
  5. Save the downloaded JSON to the path set in GSC_CLIENT_SECRET_PATH
     (see .env.example)

First run opens a browser: log in with the Google account that has access
to the GSC property you want to query. The token is cached at
GSC_TOKEN_PATH and reused/refreshed automatically after that.
"""

import argparse
import csv
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_SECRET_PATH = Path(os.environ.get("GSC_CLIENT_SECRET_PATH", SCRIPT_DIR / "secrets" / "gsc_client_secret.json"))
TOKEN_PATH = Path(os.environ.get("GSC_TOKEN_PATH", SCRIPT_DIR / "secrets" / "gsc_token.json"))


def get_credentials_from_values(client_id, client_secret, refresh_token):
    """Build credentials directly from stored values (Streamlit Secrets) —
    no local file, no browser. Used by the hosted app; the interactive
    browser login only ever happens once, locally, to obtain the
    refresh_token in the first place."""
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def get_credentials():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_PATH.exists():
                sys.exit(
                    f"Missing {CLIENT_SECRET_PATH}\n"
                    "Download the OAuth client JSON (Desktop app type) from Google Cloud "
                    "Console and save it there, or point GSC_CLIENT_SECRET_PATH at it — "
                    "see SETUP.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def list_sites(service):
    sites = service.sites().list().execute()
    for entry in sites.get("siteEntry", []):
        print(f"{entry['siteUrl']}\t{entry['permissionLevel']}")


def run_query(service, site_url, start_date, end_date, dimensions, row_limit, page_filter=None,
              page_operator="equals"):
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": row_limit,
    }
    if page_filter:
        body["dimensionFilterGroups"] = [
            {"filters": [{"dimension": "page", "operator": page_operator, "expression": page_filter}]}
        ]
    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    return response.get("rows", [])


def main():
    parser = argparse.ArgumentParser(description="Query Google Search Console via OAuth")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-sites", help="List GSC properties accessible to the authenticated account")

    q = sub.add_parser("query", help="Query search analytics data")
    q.add_argument("--site", required=True, help="Property URL, e.g. https://www.papernest.com/ or sc-domain:papernest.com")
    q.add_argument("--start", required=True, help="YYYY-MM-DD")
    q.add_argument("--end", required=True, help="YYYY-MM-DD")
    q.add_argument("--dimensions", default="query", help="Comma-separated: query,page,date,country,device")
    q.add_argument("--row-limit", type=int, default=1000)
    q.add_argument("--page", help="Filter to an exact page URL (dimensionFilterGroups, operator=equals)")
    q.add_argument("--out", help="CSV output path (default: stdout)")

    args = parser.parse_args()
    creds = get_credentials()
    service = build("searchconsole", "v1", credentials=creds)

    if args.command == "list-sites":
        list_sites(service)
        return

    dims = args.dimensions.split(",")
    rows = run_query(service, args.site, args.start, args.end, dims, args.row_limit, args.page)
    if not rows:
        print("No data.", file=sys.stderr)
        return

    header = dims + ["clicks", "impressions", "ctr", "position"]
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row["keys"] + [row["clicks"], row["impressions"], row["ctr"], row["position"]])
    if args.out:
        out.close()
        print(f"Written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
