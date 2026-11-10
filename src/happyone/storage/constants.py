"""存储模块共享常量（避免 service 与 deps 相互 import）。"""

CHUNK_SIZE = 8 * 1024 * 1024  # happy-hub 分片会话固定 8 MiB
MULTIPART_THRESHOLD = CHUNK_SIZE
SIGN_TTL_DEFAULT = 300
SIGN_TTL_MIN = 60
SIGN_TTL_MAX = 900
READ_BLOCK_SIZE = 1024 * 1024
