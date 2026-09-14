User data-only PostgreSQL backup
================================

Created: 2026-08-27 16:19:35 Asia/Shanghai
Source database: hr_agent
Source PostgreSQL: 16.13
Archive format: PostgreSQL custom format, data-only

Scope
-----
- Only the 14 tables listed in USER_DATA_TABLES.txt.
- Only the 3 sequence values listed in USER_DATA_SEQUENCES.txt.
- Includes accounts/authentication state, employees, sessions and conversations,
  summaries, parsed user documents, guidance/coach reports and history, coach
  task checkpoints, and derived personality profiles.

Explicit exclusions
-------------------
- All knowledge-base and retrieval tables (kb_*).
- All embeddings, vector indexes, tokenizer/BM25 catalogs and build metadata.
- Application metadata, schema migration records and image-analysis caches.
- Redis data, model caches, TTS voices, the repository data/ tree and upload files.

Restore prerequisites
---------------------
1. Use the same or a compatible application version to create/migrate an empty
   target database schema first. This archive intentionally contains no DDL.
2. Ensure all 14 target tables are empty; do not merge this archive directly into
   a database containing existing user rows.
3. Restore as a database superuser with pg_restore --data-only,
   --disable-triggers, --single-transaction and --exit-on-error.
4. Compare restored row counts with source_counts.txt and verify the three
   restored sequence values before accepting writes.

Notes
-----
- documents records include parsed database content, but raw_path may refer to
  an uploaded source file that is deliberately not included in this DB-only backup.
- The archive contains sensitive user and authentication data. Keep directory and
  files private and do not commit them to Git.
