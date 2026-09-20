"""Synthetic settings for transport unit tests; no real external credentials needed."""

import os

for key, value in {
    "WHATSAPP_API_TOKEN": "test-token",
    "WHATSAPP_PHONE_NUMBER_ID": "test-phone",
    "SERVICES_API_BASE_URL": "https://services.test",
    "MONGODB_URL": "mongodb://127.0.0.1:27017/test",
}.items():
    os.environ.setdefault(key, value)
