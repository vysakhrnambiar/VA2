# db_setup.py
import sqlite3
import os

DATABASE_NAME = "scheduled_calls.db"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE_NAME)

def log_message(message, level="INFO"):
    print(f"[{level}] [DB_SETUP] {message}")

def execute_sql_statements(conn, statements):
    try:
        c = conn.cursor()
        for statement in statements:
            log_message(f"Executing: {statement[:70]}...", level="DEBUG")
            c.execute(statement)
        conn.commit()
        log_message(f"Successfully executed {len(statements)} statements.", level="DEBUG")
    except sqlite3.Error as e:
        log_message(f"Error executing SQL batch. Last statement attempted: {statement[:70]}... Error: {e}", level="ERROR")
        raise

def main():
    log_message(f"Database setup script started for {DB_PATH}.")
    
    if os.path.exists(DB_PATH):
        log_message(f"Deleting existing database file: {DB_PATH} to ensure clean schema application.")
        try:
            os.remove(DB_PATH)
            log_message("Old database file deleted.")
        except OSError as e:
            log_message(f"Error deleting old database file: {e}. Please delete it manually and retry.", level="CRITICAL")
            return

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        log_message(f"Successfully connected to database at {DB_PATH}")
        
        log_message("Ensuring foreign key support is enabled.")
        execute_sql_statements(conn, ["PRAGMA foreign_keys = ON;"])

        sql_create_scheduled_calls_table = """
        CREATE TABLE scheduled_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            contact_name TEXT,
            initial_call_objective_description TEXT NOT NULL,
            current_call_objective_description TEXT NOT NULL,
            overall_status TEXT NOT NULL DEFAULT 'PENDING',
            retries_attempted INTEGER DEFAULT 0 NOT NULL,
            max_retries INTEGER DEFAULT 3 NOT NULL,
            final_summary_for_main_agent TEXT,
            main_agent_informed_user BOOLEAN DEFAULT FALSE NOT NULL,
            next_retry_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
        """
        
        # === MODIFIED HERE ===
        sql_create_call_attempts_table = """
        CREATE TABLE call_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            attempt_number INTEGER NOT NULL,
            objective_for_this_attempt TEXT NOT NULL,
            ultravox_call_id TEXT,
            twilio_call_sid TEXT,
            attempt_started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            attempt_ended_at TIMESTAMP,
            end_reason TEXT,
            transcript TEXT,
            strategist_summary_of_attempt TEXT,
            strategist_objective_met_status_for_attempt TEXT,
            strategist_reasoning_for_attempt TEXT,
            attempt_status TEXT,                       -- << ADDED
            attempt_error_details TEXT,                -- << ADDED
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            FOREIGN KEY (job_id) REFERENCES scheduled_calls (id) ON DELETE CASCADE
        );
        """
        # === END OF MODIFICATION ===
        
        log_message("Creating tables...")
        execute_sql_statements(conn, [
            sql_create_scheduled_calls_table, 
            sql_create_call_attempts_table
        ])
        log_message("Tables created successfully.")

        sql_indexes = [
            "CREATE INDEX idx_scheduled_calls_status_updated ON scheduled_calls (overall_status, updated_at);",
            "CREATE INDEX idx_scheduled_calls_next_retry ON scheduled_calls (next_retry_at);",
            "CREATE INDEX idx_call_attempts_job_id ON call_attempts (job_id);"
        ]
        
        log_message("Creating indexes...")
        execute_sql_statements(conn, sql_indexes)
        log_message("Indexes created successfully.")

        log_message("Database schema setup complete.")
            
    except sqlite3.Error as e:
        log_message(f"A critical error occurred during database setup: {e}", level="CRITICAL")
    finally:
        if conn:
            conn.close()
            log_message("Database connection closed.")

if __name__ == '__main__':
    main()