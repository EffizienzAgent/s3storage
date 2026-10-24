import json
import sys

import click
from frappe.commands import get_site, pass_context


@click.command("s3storage-migrate")
@click.option("--dry-run", is_flag=True, help="Only count what would be copied")
@click.option("--to-local", is_flag=True, help="Download objects to the local files folders instead")
@pass_context
def migrate_files(context, dry_run=False, to_local=False):
	"""Copy files between local disk and the S3 bucket. Nothing is ever deleted."""
	import frappe

	from s3storage import migrate

	frappe.init(get_site(context))
	frappe.connect()
	try:
		click.echo(json.dumps(migrate.run(dry_run=dry_run, to_local=to_local), indent=1))
	finally:
		frappe.destroy()


@click.command("s3storage-verify")
@click.option("--sample", type=click.IntRange(min=0), help="Hash only N random files (default: all)")
@pass_context
def verify_files(context, sample=None):
	"""Check that every File in S3 exists with the right size and hash. Read-only."""
	import frappe

	from s3storage import verify

	frappe.init(get_site(context))
	frappe.connect()
	try:
		result = verify.run(sample=sample)
	finally:
		frappe.destroy()
	click.echo(json.dumps(result, indent=1))
	if result.get("problems"):
		sys.exit(1)


commands = [migrate_files, verify_files]
