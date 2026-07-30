from services.google_auth import get_drive_service


class GoogleDriveService:

    def __init__(self):
        self.drive = get_drive_service()

    def create_folder(self, folder_name, parent_id=None):
        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }

        if parent_id:
            metadata["parents"] = [parent_id]

        folder = (
            self.drive.files()
            .create(body=metadata, fields="id,name")
            .execute()
        )

        return folder

    def find_folder(self, folder_name, parent_id=None):
        query = (
            f"name='{folder_name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        )

        if parent_id:
            query += f" and '{parent_id}' in parents"

        result = (
            self.drive.files()
            .list(q=query, fields="files(id,name)")
            .execute()
        )

        files = result.get("files", [])

        if files:
            return files[0]

        return None

    def get_or_create_folder(self, folder_name, parent_id=None):
        folder = self.find_folder(folder_name, parent_id)

        if folder:
            return folder

        return self.create_folder(folder_name, parent_id)