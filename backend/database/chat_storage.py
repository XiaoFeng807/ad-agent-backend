"""Ordinary chat messages and the MySQL read view fixed at admission."""

from contextlib import closing

from backend.database import database, mysql_diagnosis


def connect(*, read_only=False):
    if not database.settings.DIAGNOSIS_MYSQL_ENABLED:
        return database.get_db()
    if not mysql_diagnosis.chat_enabled():
        raise mysql_diagnosis.MySQLStorageError("MySQL chat is disabled")
    return mysql_diagnosis.connect(read_only=read_only)


def upgrade_sqlite(conn):
    # Never infer pairs from adjacency, timestamps or view IDs in old history.
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_turns (
        user_message_id INTEGER PRIMARY KEY REFERENCES conversations(id),
        assistant_message_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id),
        user_id INTEGER NOT NULL,
        CHECK (user_message_id < assistant_message_id)
    )""")


def committed_turn(conn, user_id, user_message_id, assistant_message_id):
    row = conn.execute(
        """SELECT q.content AS question, a.content AS answer FROM conversation_turns t
           JOIN conversations q ON q.id=t.user_message_id AND q.user_id=t.user_id AND q.role='user'
           JOIN conversations a ON a.id=t.assistant_message_id AND a.user_id=t.user_id AND a.role='assistant'
           WHERE t.user_id=? AND t.user_message_id=? AND t.assistant_message_id=?
             AND q.id<a.id""",
        (user_id, user_message_id, assistant_message_id),
    ).fetchone()
    return (row["question"], row["answer"]) if row else None


def save_message(user_id, role, content, priority, chat_view_id, *, user_message_id=None, validate=None):
    if not isinstance(chat_view_id, str) or not 0 < len(chat_view_id) <= 64:
        chat_view_id = None
    with closing(connect()) as conn, conn:
        if validate is not None:
            conn.execute("BEGIN IMMEDIATE")
            validate(conn)
        if user_message_id is not None:
            if role != "assistant" or type(user_message_id) is not int or user_message_id <= 0:
                raise ValueError("conversation_turn_invalid_question")
            if conn.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=? AND role='user'",
                            (user_message_id, user_id)).fetchone() is None:
                raise ValueError("conversation_turn_invalid_question")
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, role, content, priority, chat_view_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, role, content, priority, chat_view_id),
        )
        message_id = cursor.lastrowid
        if user_message_id is not None:
            conn.execute("""INSERT INTO conversation_turns (user_message_id, assistant_message_id, user_id)
                            VALUES (?, ?, ?)""", (user_message_id, message_id, user_id))
        return message_id


def read_messages(conn, user_id, *, before=None, through=None, exclude_chat_view_id=None, limit=50):
    sql = "SELECT role, content, priority FROM conversations WHERE user_id=?"
    params = [user_id]
    if before is not None:
        sql += " AND id<?"
        params.append(before)
    if through is not None:
        sql += " AND id<=?"
        params.append(through)
    if exclude_chat_view_id:
        # NULL view IDs from older records are still visible.
        sql += " AND (chat_view_id IS NULL OR chat_view_id<>?)"
        params.append(exclude_chat_view_id)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]


class MySQLChatSnapshot:
    def __init__(self):
        self.connection = mysql_diagnosis.connect(read_only=True)
        try:
            self.connection.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            self.memory_root = mysql_diagnosis.chat_memory_root(self.connection)
        except BaseException:
            self.close()
            raise

    def accept(self, user_id, message, priority, chat_view_id):
        message_id = save_message(user_id, "user", message, priority, chat_view_id)
        history = read_messages(self.connection, user_id, before=message_id, limit=49)
        history.append({"role": "user", "content": message, "priority": priority})
        return message_id, history

    def close(self):
        self.connection.close()
