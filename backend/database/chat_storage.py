"""Ordinary chat messages and the MySQL read view fixed at admission."""

from contextlib import closing, nullcontext
import json

from backend.database import database, mysql_diagnosis, product_receipt_history, review_receipt_history


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
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_review_receipts (
        assistant_message_id INTEGER PRIMARY KEY REFERENCES conversations(id),
        receipt TEXT NOT NULL
    )""")


def review_receipt_columns(conn, alias):
    if mysql_diagnosis.is_mysql(conn) and not conn.execute(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='conversation_review_receipts'"
    ).fetchone():
        return "NULL AS review_receipt", ""
    return "p.receipt AS review_receipt", (
        f"LEFT JOIN conversation_review_receipts p ON p.assistant_message_id={alias}.id"
    )


def filter_review_receipts(conn, user_id, messages, *, role_key="role"):
    for message in messages:
        receipt = message.pop("review_receipt")
        if message[role_key] == "assistant" and not review_receipt_history.ordinary_visible(
            conn, user_id, message["content"], receipt,
        ):
            message["content"] = review_receipt_history.UNAVAILABLE


def committed_turn(conn, user_id, user_message_id, assistant_message_id, *, visible_only=False):
    columns, join = review_receipt_columns(conn, "a") if visible_only else ("NULL AS review_receipt", "")
    row = conn.execute(
        f"""SELECT q.content AS question, a.content AS answer, {columns} FROM conversation_turns t
           JOIN conversations q ON q.id=t.user_message_id AND q.user_id=t.user_id AND q.role='user'
           JOIN conversations a ON a.id=t.assistant_message_id AND a.user_id=t.user_id AND a.role='assistant'
           {join}
           WHERE t.user_id=? AND t.user_message_id=? AND t.assistant_message_id=?
             AND q.id<a.id""",
        (user_id, user_message_id, assistant_message_id),
    ).fetchone()
    if row and visible_only and not review_receipt_history.ordinary_visible(
        conn, user_id, row["answer"], row["review_receipt"],
    ):
        return None
    return (row["question"], row["answer"]) if row else None


def save_message(user_id, role, content, priority, chat_view_id, *, user_message_id=None, validate=None,
                 review_receipt=None):
    if not isinstance(chat_view_id, str) or not 0 < len(chat_view_id) <= 64:
        chat_view_id = None
    if review_receipt is not None and (role != "assistant" or user_message_id is None):
        raise ValueError("conversation_turn_invalid_review_receipt")
    with closing(connect()) as conn, conn:
        if validate is not None:
            if mysql_diagnosis.is_mysql(conn):
                conn.begin()
            else:
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
        if review_receipt is not None:
            conn.execute("INSERT INTO conversation_review_receipts (assistant_message_id, receipt) VALUES (?, ?)",
                         (message_id, json.dumps(review_receipt, ensure_ascii=False)))
        return message_id


def read_messages(conn, user_id, *, before=None, through=None, exclude_chat_view_id=None, limit=50):
    columns, join = review_receipt_columns(conn, "c")
    sql = f"SELECT c.role, c.content, c.priority, {columns} FROM conversations c {join} WHERE c.user_id=?"
    params = [user_id]
    if before is not None:
        sql += " AND c.id<?"
        params.append(before)
    if through is not None:
        sql += " AND c.id<=?"
        params.append(through)
    if exclude_chat_view_id:
        # NULL view IDs from older records are still visible.
        sql += " AND (chat_view_id IS NULL OR chat_view_id<>?)"
        params.append(exclude_chat_view_id)
    needs_snapshot = not mysql_diagnosis.is_mysql(conn) and not conn.in_transaction
    with conn if needs_snapshot else nullcontext():
        if needs_snapshot:
            conn.execute("BEGIN")
        rows = conn.execute(sql + " ORDER BY c.id DESC LIMIT ?", (*params, limit)).fetchall()
        messages = [dict(row) for row in reversed(rows)]
        filter_review_receipts(conn, user_id, messages)
        product_receipt_history.filter_receipts(conn, user_id, messages)
    return messages


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
