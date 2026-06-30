"""
Azure Blob Storage wrapper for photo storage.
Replaces local file system storage (/data/photos/).

Environment variables:
    AZURE_BLOB_CONNECTION_STRING  — Blob Storage connection string
    PHOTO_DIR                     — Local fallback dir (when Blob not configured)

Usage:
    from blob_storage import upload_photo, get_photo_url

    url = upload_photo(file_bytes, "AGENT_8580506857_1234_a1b2.jpg")
    # url = "https://mystorage.blob.core.windows.net/photos/AGENT_8580506857_1234_a1b2.jpg"
"""

import os
import logging

logger = logging.getLogger(__name__)

BLOB_CONNECTION_STRING = os.environ.get("AZURE_BLOB_CONNECTION_STRING", "")
BLOB_CONTAINER = "photos"
PHOTO_DIR = os.environ.get("PHOTO_DIR", "/data/photos")

_available = None  # cached availability check


def _is_blob_available():
    """Check if Azure Blob Storage is configured and accessible."""
    global _available
    if _available is not None:
        return _available

    if not BLOB_CONNECTION_STRING:
        _available = False
        return False

    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        client.get_container_client(BLOB_CONTAINER)
        _available = True
    except Exception:
        _available = False

    return _available


def _get_container_client():
    """Get or create the photos container client."""
    from azure.storage.blob import BlobServiceClient
    client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
    container = client.get_container_client(BLOB_CONTAINER)
    if not container.exists():
        container.create_container()
    return container


def upload_photo(file_bytes, blob_name):
    """
    Upload photo bytes to Blob Storage (or local fallback).
    Returns the URL or local file path.
    """
    if _is_blob_available():
        try:
            container = _get_container_client()
            blob = container.get_blob_client(blob_name)
            blob.upload_blob(file_bytes, overwrite=True)
            url = blob.url
            logger.info(f"Photo uploaded to blob: {blob_name}")
            return url
        except Exception as e:
            logger.error(f"Blob upload failed, falling back to local: {e}")

    # Local fallback
    os.makedirs(PHOTO_DIR, exist_ok=True)
    local_path = os.path.join(PHOTO_DIR, blob_name)
    with open(local_path, "wb") as f:
        f.write(file_bytes)
    logger.info(f"Photo saved locally: {local_path}")
    return local_path


def delete_photo(path_or_url):
    """
    Delete a photo from Blob Storage or local filesystem.
    Call with the same value that was returned by upload_photo().
    """
    if not path_or_url:
        return

    if path_or_url.startswith("http"):
        # Blob URL — extract blob name
        try:
            container = _get_container_client()
            blob_name = path_or_url.split(f"/{BLOB_CONTAINER}/")[-1]
            blob = container.get_blob_client(blob_name)
            blob.delete_blob()
            logger.info(f"Photo deleted from blob: {blob_name}")
        except Exception as e:
            logger.warning(f"Failed to delete blob photo: {e}")
    else:
        # Local file
        try:
            if os.path.exists(path_or_url):
                os.remove(path_or_url)
                logger.info(f"Photo deleted locally: {path_or_url}")
        except Exception as e:
            logger.warning(f"Failed to delete local photo: {e}")


def get_photo_bytes(path_or_url):
    """
    Download photo bytes from Blob Storage or local filesystem.
    Used for resending photos as attachments.
    """
    if not path_or_url:
        return None

    if path_or_url.startswith("http"):
        try:
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
            blob_name = path_or_url.split(f"/{BLOB_CONTAINER}/")[-1]
            blob = client.get_blob_client(BLOB_CONTAINER, blob_name)
            return blob.download_blob().readall()
        except Exception as e:
            logger.warning(f"Failed to download blob photo: {e}")
            return None
    else:
        try:
            with open(path_or_url, "rb") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read local photo: {e}")
            return None
