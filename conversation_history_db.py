# conversation_history_db.py
import sqlite3
import os
from datetime import datetime
import logging

# --- Logging Setup ---
# Using a simple print-based log for this module, or can integrate with a more robust logger.
def _ch_log(message, level="INFO"):
    print(f"[{level}] [CONV_HISTORY_DB] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}")

# --- Database Configuration ---
CONVERSATION_DB_NAME = "conversation_history.db"
# Place the DB in the same directory as this script (which should be the project root)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONVERSATION_DB_NAME)

# --- Database Initialization ---
def init_db():
    """Initializes the conversation history database and creates tables if they don't exist."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Enable Foreign Keys for future use if needed, though not strictly used in this simple schema
        cursor.execute("PRAGMA foreign_keys = ON;")

        # Create conversation_turns table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool_call', 'tool_result', 'system_event')),
                content TEXT NOT NULL
            );
        """)
        
        # Create an index for faster querying by session_id and timestamp
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_timestamp ON conversation_turns (session_id, timestamp);
        """)
        
        conn.commit()
        _ch_log("Database initialized successfully and 'conversation_turns' table is ready.", "INFO")
    except sqlite3.Error as e:
        _ch_log(f"Error initializing database: {e}", "ERROR")
    finally:
        if conn:
            conn.close()

# --- Database Operations ---

# conversation_history_db.py
import sqlite3
import os
from datetime import datetime, date, timedelta, time # Ensure all are imported
from typing import List, Dict, Optional # For type hints
from dateutil import parser as dateutil_parser # For flexible date parsing

# ... (existing init_db, add_turn, _ch_log) ...

def get_filtered_turns(
    session_id: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    keywords: Optional[str] = None,
    limit: int = 50  # Default limit for broader searches
    ) -> List[Dict]:
    """
    Retrieves conversation turns based on various filters.
    All timestamps in DB are assumed to be UTC.
    """
    turns = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query_parts = []
        params = []

        base_query = "SELECT turn_id, session_id, timestamp, role, content FROM conversation_turns"

        if session_id:
            query_parts.append("session_id = ?")
            params.append(session_id)

        if start_datetime:
            query_parts.append("timestamp >= ?")
            # Ensure datetime is in UTC string format for SQLite
            params.append(start_datetime.strftime('%Y-%m-%d %H:%M:%S'))
        if end_datetime:
            query_parts.append("timestamp <= ?")
            params.append(end_datetime.strftime('%Y-%m-%d %H:%M:%S'))

        if keywords:
            # Simple keyword search, can be made more advanced (e.g., FTS5)
            keyword_parts = []
            for kw in keywords.split(): # Split by space for multiple keywords
                if kw.strip():
                    keyword_parts.append("content LIKE ?")
                    params.append(f"%{kw.strip()}%")
            if keyword_parts:
                query_parts.append(f"({' OR '.join(keyword_parts)})") # Find turns containing ANY of the keywords

        where_clause = ""
        if query_parts:
            where_clause = "WHERE " + " AND ".join(query_parts)

        # Always order by timestamp to get a chronological sequence for summarization
        # Fetch more than limit initially if keywords are involved, then limit after fetching,
        # or rely on SQL limit if broad. For simplicity now, direct SQL limit.
        order_by_limit_sql = f"ORDER BY timestamp DESC LIMIT ?" # Get most recent matching criteria
        params.append(limit)

        full_query = f"{base_query} {where_clause} {order_by_limit_sql}"
        
        _ch_log(f"Executing filtered turn query: {full_query} with params: {params}", "DEBUG")
        cursor.execute(full_query, tuple(params))
            
        rows = cursor.fetchall()
        for row in rows:
            turns.append(dict(row))
        
        turns.reverse() # Reverse to get oldest-first for summarizer
        _ch_log(f"Retrieved {len(turns)} filtered turns (Limit: {limit}).", "DEBUG")

    except sqlite3.Error as e:
        _ch_log(f"Error retrieving filtered turns: {e}", "ERROR")
    except Exception as e_gen: # Catch other potential errors
        _ch_log(f"Unexpected error in get_filtered_turns: {e_gen}", "ERROR")
    finally:
        if conn:
            conn.close()
    return turns

# Keep existing get_recent_turns, or adapt it to use get_filtered_turns
def get_recent_turns(session_id: str = None, limit: int = 20) -> list[dict]:
    # This can now be a simplified call to get_filtered_turns
    # if session_id is None, it fetches global recent turns.
    return get_filtered_turns(session_id=session_id, limit=limit)

def add_turn(session_id: str, role: str, content: str):
    """Adds a new conversation turn to the database.

    Args:
        session_id: The ID of the current OpenAI session.
        role: The role of the entity in the turn (e.g., 'user', 'assistant', 'tool_call', 'tool_result').
        content: The textual content of the turn. Can be JSON string for tool calls/results.
    """
    if not session_id:
        _ch_log("Attempted to add turn with no session_id. Skipping.", "WARN")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO conversation_turns (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
        """, (session_id, role, content, datetime.utcnow())) # Storing as UTC
        conn.commit()
        _ch_log(f"Added turn for session '{session_id}'. Role: {role}, Content snippet: '{content[:70]}...'", "DEBUG")
    except sqlite3.Error as e:
        _ch_log(f"Error adding turn for session '{session_id}': {e}", "ERROR")
    finally:
        if conn:
            conn.close()

def get_recent_turns(session_id: str = None, limit: int = 20) -> list[dict]:
    """Retrieves the most recent conversation turns.

    Args:
        session_id: Optional. If provided, retrieves turns only for this session.
        limit: The maximum number of recent turns to retrieve.

    Returns:
        A list of dictionaries, where each dictionary represents a conversation turn.
        Returns an empty list if an error occurs or no turns are found.
    """
    turns = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row # Access columns by name
        cursor = conn.cursor()

        if session_id:
            query = """
                SELECT turn_id, session_id, timestamp, role, content 
                FROM conversation_turns 
                WHERE session_id = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            """
            cursor.execute(query, (session_id, limit))
        else:
            query = """
                SELECT turn_id, session_id, timestamp, role, content 
                FROM conversation_turns 
                ORDER BY timestamp DESC 
                LIMIT ?
            """
            cursor.execute(query, (limit,))
            
        rows = cursor.fetchall()
        for row in rows:
            turns.append(dict(row))
        
        # Reverse the order so the oldest of the recent turns is first (more natural for history summary)
        turns.reverse() 
        _ch_log(f"Retrieved {len(turns)} recent turns (Session: {session_id if session_id else 'Any'}, Limit: {limit}).", "DEBUG")

    except sqlite3.Error as e:
        _ch_log(f"Error retrieving recent turns: {e}", "ERROR")
    finally:
        if conn:
            conn.close()
    return turns

# --- Example Usage (for direct testing of this module) ---
if __name__ == '__main__':
    _ch_log("Running conversation_history_db.py directly for testing...", "INFO")
    
    # Initialize DB (creates if not exists)
    init_db()

    # Test adding turns
    test_session_id_1 = "test_session_alpha"
    test_session_id_2 = "test_session_beta"

    add_turn(test_session_id_1, "user", "Hello, Jarvis.")
    add_turn(test_session_id_1, "assistant", "Hello! How can I help you today?")
    add_turn(test_session_id_1, "tool_call", '{"name": "get_weather", "arguments": {"location": "Dubai"}}')
    add_turn(test_session_id_1, "tool_result", '{"temperature": "35C", "condition": "Sunny"}')
    add_turn(test_session_id_1, "assistant", "The weather in Dubai is 35°C and sunny.")

    add_turn(test_session_id_2, "user", "Schedule a meeting.")
    add_turn(test_session_id_2, "assistant", "Okay, with whom and when?")

    # Test retrieving turns
    _ch_log("\n--- Retrieving last 5 turns (any session) ---", "INFO")
    recent_overall_turns = get_recent_turns(limit=5)
    if recent_overall_turns:
        for turn in recent_overall_turns:
            print(f"  ID: {turn['turn_id']}, Session: {turn['session_id']}, Time: {turn['timestamp']}, Role: {turn['role']}, Content: {turn['content'][:50]}...")
    else:
        print("  No turns found.")

    _ch_log(f"\n--- Retrieving last 3 turns for session '{test_session_id_1}' ---", "INFO")
    session_1_turns = get_recent_turns(session_id=test_session_id_1, limit=3)
    if session_1_turns:
        for turn in session_1_turns:
            print(f"  ID: {turn['turn_id']}, Time: {turn['timestamp']}, Role: {turn['role']}, Content: {turn['content'][:50]}...")
    else:
        print(f"  No turns found for session {test_session_id_1}.")
        
    _ch_log(f"\n--- Retrieving last 3 turns for session 'non_existent_session' ---", "INFO")
    non_existent_turns = get_recent_turns(session_id="non_existent_session", limit=3)
    if not non_existent_turns:
        print("  Correctly found no turns for non_existent_session.")
    else:
        print(f"  ERROR: Found turns for non_existent_session: {non_existent_turns}")

    _ch_log("\n--- Test Complete ---", "INFO")