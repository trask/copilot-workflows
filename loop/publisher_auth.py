"""Select a publisher secret by the actual head repository owner."""

import os
import re

from loop.policy import ACCOUNT, CENTRAL, REPO, require
from loop.publication import SECRET
from loop.verify import parse_json

SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*_PUBLISH_TOKEN\Z")


def publisher_secret(head_repo):
    require(isinstance(head_repo, str) and REPO.fullmatch(head_repo),
            "Invalid publisher head repository")
    configuration = os.environ.get("PUBLISHER_SECRET_MAP", "")
    require(len(configuration) <= 16384, "Publisher secret mapping exceeds limits")
    mapping = (parse_json(configuration) if configuration
               else {CENTRAL.split("/")[0]: SECRET})
    require(isinstance(mapping, dict) and len(mapping) <= 100,
            "Publisher secret mapping must be an owner-to-secret JSON object")
    require(all(isinstance(owner, str) and ACCOUNT.fullmatch(owner)
                and isinstance(secret, str) and len(secret) <= 100
                and SECRET_NAME.fullmatch(secret)
                for owner, secret in mapping.items()),
            "Publisher mapping requires GitHub owners and *_PUBLISH_TOKEN secret names")
    normalized = {owner.casefold(): secret for owner, secret in mapping.items()}
    require(len(normalized) == len(mapping), "Duplicate case-insensitive publisher owner")
    return normalized.get(head_repo.split("/")[0].casefold(), "")
