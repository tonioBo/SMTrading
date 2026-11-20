"""Automatic backups: the nightly `pg_dump` with its sha256 sidecar and the restore
config bundle, copied to every BACKUP_DIRS destination with a 30 daily + 12 monthly
retention, and the weekly restore test. Run by the compose `backup` service
(`python -m smtrading.backup.main`); the state is in `app_settings` (`backup.state`)."""
