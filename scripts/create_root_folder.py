import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.google_drive_service import GoogleDriveService

drive = GoogleDriveService()

folder = drive.create_folder("Business Card Scanner")

print(folder)