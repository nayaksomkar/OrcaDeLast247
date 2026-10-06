package main

// Import a pure-Go SQLite driver so the libsql client can open local
// file-based databases (TURSO_DATABASE_URL=file:./news.db).
import _ "modernc.org/sqlite"
