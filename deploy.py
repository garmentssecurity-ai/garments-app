"""
Netlify Deploy Script — Deploys the outfithub Flask app with a consistent site name.
Usage: python deploy.py <NETLIFY_TOKEN>
"""
import os
import sys
import json
import uuid
import zipfile
import tempfile
import requests

API_BASE = "https://api.netlify.com/api/v1"
CANDIDATE_NAMES = [
    "garments-app",
    "garments-fashion",
    "garments-social",
    "the-garments",
    "garments-daily",
    "garments-looks",
    "outfit-garments",
    "garments-club",
]


def get_existing_sites(token: str) -> dict:
    """Get all sites in the account. Returns dict of name->id."""
    resp = requests.get(
        f"{API_BASE}/sites?filter=all",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return {}
    return {s.get("name"): s["id"] for s in resp.json() if s.get("name")}


def find_available_name(token: str) -> tuple:
    """
    Try candidate names. Returns (site_name, site_id or None).
    If a name exists in the user's account, returns it with its id.
    If a name is free, returns it with site_id=None.
    """
    existing = get_existing_sites(token)

    # First, check if any of our candidates exist in the user's account
    for name in CANDIDATE_NAMES:
        if name in existing:
            print(f"📌 Found your existing site: {name}")
            return name, existing[name]

    # Then, try to find a name that's available globally
    for name in CANDIDATE_NAMES:
        # Check if name is available
        check = requests.get(
            f"{API_BASE}/sites/{name}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if check.status_code == 404:
            print(f"✨ Name available: {name}")
            return name, None
        print(f"⏳ {name} is taken, trying next...")

    return None, None


def create_deploy(token: str, source_dir: str, site_name: str, site_id: str = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/zip",
    }

    # Create a zip of the source directory
    zip_path = os.path.join(tempfile.gettempdir(), f"deploy-{uuid.uuid4().hex}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if not d.startswith((".", "__")) and d != ".venv" and d != "node_modules"]
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, source_dir)
                zf.write(file_path, arcname)

    try:
        if site_id:
            print(f"📦 Deploying to existing site: {site_name}...")
        else:
            print(f"🏗️  Creating new site: {site_name}...")
            resp = requests.post(
                f"{API_BASE}/sites",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "name": site_name,
                    "force_ssl": True,
                },
            )
            if resp.status_code == 201:
                site_data = resp.json()
                site_id = site_data["id"]
                print(f"✅ Site created!")
            else:
                print(f"❌ Failed to create site: {resp.status_code}")
                print(resp.text)
                return False

        # Step 2: Create a deploy upload
        print("📤 Uploading files to Netlify...")
        with open(zip_path, "rb") as f:
            deploy_resp = requests.post(
                f"{API_BASE}/sites/{site_id}/deploys",
                headers=headers,
                data=f,
            )

        if deploy_resp.status_code in (200, 201):
            deploy_data = deploy_resp.json()
            deploy_url = deploy_data.get("ssl_url") or deploy_data.get("url")
            print(f"\n✅ Deploy successful!")
            print(f"🌐 Your site URL: {deploy_url}")
            print(f"⚙️  Admin panel: https://app.netlify.com/sites/{site_name}/general")
            print(f"📝 To change the name, go to Site Settings > General > Change site name")
            return True
        else:
            print(f"❌ Deploy failed: {deploy_resp.status_code}")
            print(deploy_resp.text)
            return False

    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deploy.py <NETLIFY_TOKEN>")
        print("")
        print("Get your token at: https://app.netlify.com/user/applications/personal")
        sys.exit(1)

    token = sys.argv[1]
    source = os.path.join(os.path.dirname(__file__), "outfithub")

    print("🔍 Looking for an available site name...")
    site_name, site_id = find_available_name(token)

    if not site_name:
        print("❌ Could not find an available name. Please set a custom name in deploy.py")
        sys.exit(1)

    success = create_deploy(token, source, site_name, site_id)
    if success:
        print(f"\n🎉 Done! Your site URL will stay the same forever: https://{site_name}.netlify.app")
    else:
        print("\n❌ Deployment failed. Check the errors above.")
        sys.exit(1)
