import os
import sys

# Add project root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.google_auth import get_drive_service

drive = get_drive_service()

print("Google Drive Connected Successfully!")

about = drive.about().get(fields="user").execute()

print(about)