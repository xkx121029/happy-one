"""happy-one 存储接入层：happy-hub 的客户端封装与业务映射。

上层只需要：

    from happyone.storage import StorageService, HubClient
    from happyone.storage.router import router as storage_router
"""

from .client import HubClient
from .errors import StorageError
from .keys import (
    BUCKET_BACKUP,
    BUCKET_LICENSE_ARTIFACTS,
    BUCKET_RELAY,
    NAMESPACE_BUCKETS,
    bucket_for,
    content_addressed_key,
    entity_key,
)
from .models import SignedLink, StoredObject, UploadOutcome, UploadSession
from .service import StorageService

__all__ = [
    "BUCKET_BACKUP",
    "BUCKET_LICENSE_ARTIFACTS",
    "BUCKET_RELAY",
    "HubClient",
    "NAMESPACE_BUCKETS",
    "SignedLink",
    "StorageError",
    "StorageService",
    "StoredObject",
    "UploadOutcome",
    "UploadSession",
    "bucket_for",
    "content_addressed_key",
    "entity_key",
]
