import os

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")

import app  # noqa: E402

assert app.application is not None
print("app-import-ok")
