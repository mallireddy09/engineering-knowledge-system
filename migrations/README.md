# Schema migration policy

Version 1 is the first published schema. `ekm.models.migrate_record` validates v1
records and rejects unknown future versions. No fictional version-0 conversion
is provided. A future migration must preserve UUIDs and relationships, run on an
exported backup first, validate all records before writing, and use reviewed
ETag-aware updates. Provider IDs and tokens are never migrated between profiles.

SQLite is a rebuildable cache, not the migration authority. Back up pending
operations before replacing a cache; export records from remote storage or refresh
and use `ekm backup`. Keep profile-local tombstones when rebuilding this machine's
data root so that deleted records are not inadvertently restored.
