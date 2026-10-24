import re

import frappe
from frappe import _

FIELDS = ("endpoint", "region", "bucket", "access_key", "secret_key")
CREDENTIALS = ("access_key", "secret_key")
FILES = "files"
TARGET_NAME = re.compile(r"^[a-z0-9_]+$")


class NotConfigured(frappe.ValidationError):
	pass


_clients = {}


def config_keys(target=FILES):
	if not TARGET_NAME.match(target or ""):
		raise ValueError(f"Invalid S3 target name: {target!r}")
	return {field: f"s3_{target}_{field}" for field in FIELDS}


def target_config(target=FILES):
	values = {field: frappe.conf.get(key) for field, key in config_keys(target).items()}
	if target != FILES and not any(values[field] for field in CREDENTIALS):
		files_keys = config_keys(FILES)
		values |= {field: frappe.conf.get(files_keys[field]) for field in CREDENTIALS}
	return frappe._dict(values) if all(values.values()) else None


def get_client(target=FILES, config=None):
	settings = frappe._dict(config) if config is not None else target_config(target)
	missing = [field for field in FIELDS if not (settings or {}).get(field)]
	if missing:
		name = "explicit config" if config is not None else target
		raise NotConfigured(_("S3 target {0} is missing: {1}").format(name, ", ".join(missing)))
	return client_for(settings), settings


def client_for(settings):
	cache_key = tuple(settings[field] for field in FIELDS)
	if cache_key not in _clients:
		_clients[cache_key] = make_client(settings)
	return _clients[cache_key]


def make_client(settings):
	import boto3
	from botocore.config import Config

	return boto3.client(
		"s3",
		endpoint_url=settings.endpoint,
		region_name=settings.region,
		aws_access_key_id=settings.access_key,
		aws_secret_access_key=settings.secret_key,
		config=Config(
			s3={"addressing_style": "path"},
			retries={"max_attempts": 3, "mode": "standard"},
			connect_timeout=5,
			read_timeout=30,
			request_checksum_calculation="when_required",
			response_checksum_validation="when_required",
		),
	)
