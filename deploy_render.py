"""
Deploy to Render via API
Usage: python deploy_render.py <RENDER_API_KEY>
"""
import sys
import requests
import time

API_BASE = "https://api.render.com/v1"
REPO_URL = "https://github.com/garmentssecurity-ai/garments-app"
SERVICE_NAME = "garments-app"
BRANCH = "master"
ROOT_DIR = "outfithub"
BUILD_COMMAND = "pip install -r requirements.txt"
START_COMMAND = "gunicorn app:app"
ENVIRONMENT = "python3"
REGION = "oregon"
PLAN = "free"

def create_service(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check if service already exists
    print("🔍 Checking existing services...")
    resp = requests.get(f"{API_BASE}/services", headers=headers)
    if resp.status_code == 200:
        for svc in resp.json():
            if svc.get("name") == SERVICE_NAME:
                print(f"📌 Service '{SERVICE_NAME}' already exists (ID: {svc['id']})")
                return svc["id"]

    # Create the web service
    print(f"🏗️  Creating web service '{SERVICE_NAME}'...")
    
    payload = {
        "type": "web_service",
        "name": SERVICE_NAME,
        "repo": REPO_URL,
        "branch": BRANCH,
        "rootDir": ROOT_DIR,
        "buildCommand": BUILD_COMMAND,
        "startCommand": START_COMMAND,
        "environment": ENVIRONMENT,
        "region": REGION,
        "plan": PLAN,
        "autoDeploy": True,
        "healthCheckPath": "/",
    }

    resp = requests.post(
        f"{API_BASE}/services",
        headers=headers,
        json=payload,
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        service_id = data["id"]
        print(f"✅ Service created! ID: {service_id}")
        print(f"🌐 Your site will be at: https://{SERVICE_NAME}.onrender.com")
        print("⏳ Waiting for initial deploy to start...")
        return service_id
    else:
        print(f"❌ Failed to create service: {resp.status_code}")
        print(resp.text)
        return None


def wait_for_deploy(token, service_id):
    """Wait for the first deploy to finish."""
    headers = {"Authorization": f"Bearer {token}"}
    print("⏳ Waiting for deploy to finish (this may take 2-5 minutes)...")
    
    for i in range(60):
        time.sleep(10)
        resp = requests.get(
            f"{API_BASE}/services/{service_id}/deploys?limit=1",
            headers=headers,
        )
        if resp.status_code == 200:
            deploys = resp.json()
            if deploys:
                status = deploys[0].get("status", "")
                print(f"   Deploy status: {status}")
                if status == "live":
                    print(f"\n✅ Deploy successful!")
                    print(f"🌐 https://{SERVICE_NAME}.onrender.com")
                    return True
                elif status in ("failed", "canceled"):
                    print(f"\n❌ Deploy {status}")
                    return False
        else:
            print(f"   Checking deploys... ({resp.status_code})")
    
    print("\n⏰ Timed out waiting. Check your Render dashboard.")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deploy_render.py <RENDER_API_KEY>")
        print("")
        print("Get your API key at: https://dashboard.render.com/u/settings/tokens")
        sys.exit(1)

    token = sys.argv[1]
    
    service_id = create_service(token)
    if not service_id:
        sys.exit(1)
    
    print(f"\n📋 Service ID: {service_id}")
    print(f"📊 Dashboard: https://dashboard.render.com/web/{service_id}")
    
    # Don't wait by default
    deploy_now = input("\nWait for deploy to finish? (y/N): ").strip().lower()
    if deploy_now == 'y':
        wait_for_deploy(token, service_id)
    else:
        print(f"\n✅ Service is being created!")
        print(f"🌐 Check progress: https://dashboard.render.com/web/{service_id}")
        print(f"🌐 Your URL: https://{SERVICE_NAME}.onrender.com")
