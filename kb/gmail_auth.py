"""One-time Gmail sign-in on a server with no browser.

1. Put the OAuth client file from Google Cloud at data/secrets/gmail_client.json
2. Run this script, open the link it prints on any computer, and approve read-only access.
3. Your browser then lands on a localhost page that fails to load. That is expected.
   Copy the full address from the address bar and paste it here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectors import GMAIL_SCOPES, SECRETS  # noqa: E402

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # only for the localhost redirect, nothing is served
from google_auth_oauthlib.flow import Flow  # noqa: E402

client = os.path.join(SECRETS, "gmail_client.json")
if not os.path.exists(client):
    sys.exit(f"Missing {client}. Download the Desktop OAuth client JSON from Google Cloud and save it there.")

flow = Flow.from_client_secrets_file(client, scopes=GMAIL_SCOPES, redirect_uri="http://localhost:8080/")
url, _ = flow.authorization_url(access_type="offline", prompt="consent")
print("\nOpen this link, sign in, and approve read-only Gmail access:\n\n" + url + "\n")
resp = input("Paste the full localhost address your browser ended on: ").strip()
flow.fetch_token(authorization_response=resp.replace("http://localhost", "http://localhost", 1))
tok = os.path.join(SECRETS, "gmail_token.json")
with open(tok, "w") as f:
    f.write(flow.credentials.to_json())
os.chmod(tok, 0o600)
print(f"\nSaved {tok}. Gmail is connected (read-only).")
