"""Constants for the Road Trip integration."""

DOMAIN = "roadtrip"
NAME = "Road Trip"

CONF_WEBHOOK_ID = "webhook_id"
CONF_ALLOW_REMOTE = "allow_remote"

STORAGE_DIR = "roadtrip"  # under /config; keeps the last uploaded file per vehicle
INBOX_DIR = "inbox"  # /config/roadtrip/inbox — drop a backup here and call roadtrip.import_file

SIGNAL_UPDATED = "roadtrip_updated_{entry_id}"
EVENT_IMPORTED = "roadtrip_imported"

SERVICE_IMPORT_FILE = "import_file"
ATTR_PATH = "path"

API_URL = "/api/roadtrip/data"
