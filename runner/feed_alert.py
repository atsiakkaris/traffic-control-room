"""Email an alert about the Cyprus live-traffic feed.

Run by .github/workflows/feed_alert.yml, which the cyprus-live-traffic Worker
triggers (workflow_dispatch) when the upstream feed has been frozen for 30+
minutes ("frozen") and again once it has been moving for another 30 minutes
("recovered"). Uses the same Gmail secrets as the digest; standard library only.

Environment:
    KIND          "frozen" or "recovered"
    DETAIL        one line of context from the Worker (e.g. the newest
                  measurement time); shown in the body as plain text
    GMAIL_USER    Gmail address the mail is sent from
    GMAIL_APP_PW  Gmail App Password
    NOTIFICATIONS_LIST  recipients, separated by commas, semicolons or new lines.
                  No fallback: if it is empty nothing is sent (the digest's
                  NOTIFY_EMAIL is deliberately not used)
"""

import os
import re
import smtplib
import sys
from email.mime.text import MIMEText

# Subject and body per kind. The Worker's one-line DETAIL (the newest
# measurement's date and time in Cyprus time, and how long ago) is added below
# the body.
MESSAGES = {
    "frozen": (
        "Cyprus live traffic: feed frozen for 30+ minutes",
        "The upstream traffic feed (Bluetooth Paths Live) is stale and it has "
        "stopped producing new measurements for at least 30 minutes.\n"
        "The Worker keeps publishing the last readings, the app shows a "
        "\"feed stuck\" note, and it has paused learning until the feed moves "
        "again.\n"
        "When it is restored for at least 30 minutes, you will be notified.",
    ),
    "recovered": (
        "Cyprus live traffic: feed back, fresh for 30 minutes",
        "The upstream traffic feed (Bluetooth Paths Live) is producing new "
        "measurements again and has been for at least 30 minutes.\n"
        "The Worker has resumed learning.",
    ),
}


def parse_recipients(raw):
    """Addresses from a list separated by commas, semicolons or white space."""
    return [p for p in re.split(r"[,;\s]+", raw) if "@" in p]


def main():
    kind = os.environ.get("KIND", "")
    if kind not in MESSAGES:
        print(f"Unknown KIND {kind!r}; expected one of {sorted(MESSAGES)}", file=sys.stderr)
        return 1

    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pw = os.environ.get("GMAIL_APP_PW", "")
    recipients = parse_recipients(os.environ.get("NOTIFICATIONS_LIST", ""))
    if not gmail_user or not gmail_pw:
        print("GMAIL_USER / GMAIL_APP_PW not set - cannot send the alert.", file=sys.stderr)
        return 1
    if not recipients:
        print("NOTIFICATIONS_LIST is empty or not set - nobody to send the alert to.", file=sys.stderr)
        return 1

    subject, body = MESSAGES[kind]
    detail = " ".join(os.environ.get("DETAIL", "").split())[:300]
    text = f"{body}\n\n{detail}\n" if detail else f"{body}\n"

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pw)
        server.sendmail(gmail_user, recipients, msg.as_string())
    print(f"Sent {kind!r} alert to {len(recipients)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
