import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.google_drive_service import GoogleDriveService

drive = GoogleDriveService()

# Root Folder
root = drive.get_or_create_folder("Business Card Scanner")

# Subfolders
master = drive.get_or_create_folder("Master", root["id"])
companies = drive.get_or_create_folder("Companies", root["id"])
templates = drive.get_or_create_folder("Templates", root["id"])

print("Root:", root)
print("Master:", master)
print("Companies:", companies)
print("Templates:", templates)