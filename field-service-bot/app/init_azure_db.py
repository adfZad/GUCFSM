import os
import sqlite3
import csv
import pyodbc

# Path to the existing SQLite DB (for migration)
SQLITE_DB_PATH = os.environ.get("DB_PATH", "/data/field_service.db")

# Azure SQL connection string
AZURE_SQL_CONN_STR = os.environ.get("AZURE_SQL_CONNECTION_STRING")

# T-SQL Schema Definitions
TSQL_SCHEMA = {
    "master_units": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "phone_number": "NVARCHAR(255) DEFAULT ''",
        "phone_display": "NVARCHAR(255) DEFAULT ''",
        "telegram_user_id": "NVARCHAR(255) DEFAULT ''",
        "owner_name": "NVARCHAR(255)",
        "units": "NVARCHAR(MAX) NOT NULL",
    },
    "submissions": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "telegram_user_id": "NVARCHAR(255)",
        "phone_number": "NVARCHAR(255) NOT NULL",
        "unit": "NVARCHAR(255) NOT NULL",
        "compound": "NVARCHAR(255)",
        "request_type": "NVARCHAR(255) NOT NULL",
        "category": "NVARCHAR(255)",
        "service": "NVARCHAR(255)",
        "sub_service": "NVARCHAR(255)",
        "issue_description": "NVARCHAR(MAX)",
        "photo_path": "NVARCHAR(MAX)",
        "photo_file_id": "NVARCHAR(MAX)",
        "status": "NVARCHAR(50) DEFAULT 'submitted'",
        "priority": "NVARCHAR(50) DEFAULT 'normal'",
        "submitted_at": "DATETIME2 DEFAULT SYSUTCDATETIME()",
        # Workflow columns
        "required_approvals": "INT DEFAULT 2",
        "work_done_by": "NVARCHAR(255)",
        "work_done_at": "DATETIME2",
        "work_done_note": "NVARCHAR(MAX)",
        "actual_cost": "NVARCHAR(255)",
        "completion_photo_path": "NVARCHAR(MAX)",
        "completion_photo_file_id": "NVARCHAR(MAX)",
        "closed_by": "NVARCHAR(255)",
        "closed_at": "DATETIME2",
        "close_note": "NVARCHAR(MAX)",
    },
    "master_units_hierarchy": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "compound": "NVARCHAR(255) NOT NULL",
        "unit_type": "NVARCHAR(255) NOT NULL",
        "villa_number": "NVARCHAR(50)",
        "building_number": "NVARCHAR(50)",
        "flat_number": "NVARCHAR(50)",
        "full_label": "NVARCHAR(255) NOT NULL",
        "assigned_to": "NVARCHAR(255)",
    },
    "unit_agents": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "full_label": "NVARCHAR(255) NOT NULL",
        "telegram_user_id": "NVARCHAR(255) NOT NULL",
    },
    "services": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "main_category": "NVARCHAR(255) NOT NULL",
        "category": "NVARCHAR(255) NOT NULL",
        "sub_category": "NVARCHAR(255) NOT NULL",
    },
    "form_state": {
        "telegram_user_id": "NVARCHAR(255) PRIMARY KEY",
        "current_step": "NVARCHAR(255) NOT NULL",
        "data": "NVARCHAR(MAX) NOT NULL DEFAULT '{}'",
        "updated_at": "DATETIME2 DEFAULT SYSUTCDATETIME()",
    },
    "agents": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "telegram_user_id": "NVARCHAR(255) NOT NULL",
        "name": "NVARCHAR(255) NOT NULL",
        "role": "NVARCHAR(50) NOT NULL DEFAULT 'field_agent'",
        "compound": "NVARCHAR(255)",
        "active": "INT DEFAULT 1",
    },
    "approvals": {
        "id": "INT IDENTITY(1,1) PRIMARY KEY",
        "submission_id": "INT NOT NULL",
        "level": "INT NOT NULL",
        "action": "NVARCHAR(50) NOT NULL",
        "actor_id": "NVARCHAR(255) NOT NULL",
        "actor_note": "NVARCHAR(MAX)",
        "acted_at": "DATETIME2 DEFAULT SYSUTCDATETIME()",
    },
}

def create_azure_tables(azure_conn):
    print("Creating tables in Azure SQL if they do not exist...")
    cursor = azure_conn.cursor()
    for table, columns in TSQL_SCHEMA.items():
        # Check if table exists
        cursor.execute(f"SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{table}'")
        exists = cursor.fetchone()[0]
        
        if not exists:
            col_defs = ", ".join(f"{name} {defn}" for name, defn in columns.items())
            cursor.execute(f"CREATE TABLE {table} ({col_defs})")
            print(f"  ✓ Created table {table}")
        else:
            print(f"  ℹ️ Table {table} already exists. Skipping creation.")
    azure_conn.commit()
    cursor.close()

def migrate_data_from_sqlite(sqlite_conn, azure_conn):
    print("Migrating data from SQLite to Azure SQL...")
    sqlite_cursor = sqlite_conn.cursor()
    azure_cursor = azure_conn.cursor()

    for table in TSQL_SCHEMA.keys():
        try:
            # Get data from SQLite
            sqlite_cursor.execute(f"SELECT * FROM {table}")
            rows = sqlite_cursor.fetchall()
            
            if not rows:
                print(f"  ℹ️ Table {table} has no data to migrate.")
                continue

            # Check if Azure table already has data
            azure_cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = azure_cursor.fetchone()[0]
            if count > 0:
                print(f"  ⚠ Azure table {table} already has {count} rows. Skipping migration to prevent duplicates.")
                continue
            
            # Prepare insert statement based on SQLite columns
            col_names = [description[0] for description in sqlite_cursor.description]
            
            # If the table has an IDENTITY insert, we need to turn it on to preserve IDs from SQLite
            identity_insert = False
            if "id" in col_names and "id" in TSQL_SCHEMA[table] and "IDENTITY" in TSQL_SCHEMA[table]["id"]:
                identity_insert = True
            
            if identity_insert:
                azure_cursor.execute(f"SET IDENTITY_INSERT {table} ON")
            
            placeholders = ", ".join(["?"] * len(col_names))
            cols_str = ", ".join(col_names)
            insert_query = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"
            
            azure_cursor.executemany(insert_query, rows)
            
            if identity_insert:
                azure_cursor.execute(f"SET IDENTITY_INSERT {table} OFF")
                
            azure_conn.commit()
            print(f"  ✓ Migrated {len(rows)} rows to {table}")
            
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                print(f"  ℹ️ SQLite table {table} does not exist, skipping.")
            else:
                raise e
        except Exception as e:
            print(f"  ❌ Error migrating {table}: {e}")
            azure_conn.rollback()

    azure_cursor.close()

if __name__ == "__main__":
    if not AZURE_SQL_CONN_STR:
        print("❌ AZURE_SQL_CONNECTION_STRING environment variable not set.")
        print("Please set it and run the script again. Data will be migrated from SQLite.")
        exit(1)
        
    print("Connecting to Azure SQL...")
    azure_conn = pyodbc.connect(AZURE_SQL_CONN_STR)
    
    create_azure_tables(azure_conn)
    
    # Try to migrate from SQLite
    if os.path.exists(SQLITE_DB_PATH):
        print(f"Connecting to SQLite database at {SQLITE_DB_PATH}...")
        sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
        sqlite_conn.row_factory = sqlite3.Row  # to get column names easily if needed, though fetchall returns tuples
        
        # We actually need tuples for executemany, so we will reset row_factory or handle it.
        sqlite_conn.row_factory = None
        
        migrate_data_from_sqlite(sqlite_conn, azure_conn)
        sqlite_conn.close()
    else:
        print(f"⚠ SQLite database not found at {SQLITE_DB_PATH}. No data to migrate.")
    
    azure_conn.close()
    print("✓ Azure Database initialization and migration complete.")
