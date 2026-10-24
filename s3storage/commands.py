import json

import click
from frappe.commands import get_site, pass_context


@click.command("s3storage-migrate")
@click.option("--dry-run", is_flag=True, help="Only count what would be uploaded")
@pass_context
def migrate_files(context, dry_run=False):
	"""Upload existing local files to the S3 bucket. Local files are never deleted."""
	import frappe

	from s3storage import migrate

	frappe.init(get_site(context))
	frappe.connect()
	try:
		click.echo(json.dumps(migrate.run(dry_run=dry_run), indent=1))
	finally:
		frappe.destroy()


commands = [migrate_files]
