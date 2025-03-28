import sqlite3
import os
import json

from typing import Optional, Dict
from .discord import DEFAULT_LIMIT
from modal import asgi_app
from openai import OpenAI
from datetime import datetime
from .discord import scrape_discord_server
import sqlite_vec
from sqlite_vec import serialize_float32
from fastapi import Request

from .common import DB_PATH, VOLUME_DIR, app, fastapi_app, get_db_conn, serialize, volume, TOOLS


@app.function(
    volumes={VOLUME_DIR: volume},
)
def init_db():
    """Initialize the SQLite database with a simple table."""
    volume.reload()
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    cursor = conn.cursor()

    # Create a simple table
    cursor.execute("""
            CREATE TABLE IF NOT EXISTS channel_summaries (
                channel_id TEXT PRIMARY KEY,
                summary_text TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                unique_authors INTEGER NOT NULL,
                most_active_hour TEXT NOT NULL,
                peak_hour_messages INTEGER NOT NULL,
                summary_start_date TIMESTAMP NOT NULL,
                summary_end_date TIMESTAMP NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_messages (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                );
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_metadata (
                    channel_id TEXT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
        """
    )
    cursor.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_discord_messages USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[1536]
        );
        """
    )

    conn.commit()
    conn.close()
    volume.commit()


@app.function(
    volumes={VOLUME_DIR: volume},
    timeout=900 # 15 min timeout
)
@asgi_app()
def fastapi_entrypoint():
    # Initialize database on startup
    init_db.remote()
    return fastapi_app

@fastapi_app.get("/channel-summaries")
async def get_summaries(force_refresh: bool = False):
    """
    Get channel summaries, using cached data unless force_refresh is True.
    """
    volume.reload()
    return await get_channel_summaries(force_refresh=force_refresh)
    volume.commit()

# async def get_channel_summaries():
#     """Get summaries of all channels for the past week."""
#     client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
#     conn = get_db_conn(DB_PATH)
#     cursor = conn.cursor()

#     excluded_channels = ["customer-whitelabel", "dots-admin"]

#     # Get all unique channels with messages in the last week
#     channels = cursor.execute(f"""
#             SELECT DISTINCT dm.channel_id
#             FROM discord_messages dm
#             JOIN channel_metadata cm ON dm.channel_id = cm.channel_id
#             WHERE dm.created_at >= datetime('now', '-7 days')
#               AND cm.channel_name NOT IN ({','.join(['?'] * len(excluded_channels))})
#             ORDER BY dm.channel_id
#         """, excluded_channels).fetchall()
#     summaries = []
#     for (channel_id,) in channels:
#         # Get messages from the last week for this channel
#         messages = cursor.execute("""
#             SELECT content, created_at
#             FROM discord_messages
#             WHERE channel_id = ?
#               AND created_at >= datetime('now', '-7 days')
#             ORDER BY created_at DESC
#         """, (channel_id,)).fetchall()

#         if not messages:
#             continue

#         # Prepare messages for summarization
#         messages_text = "\n".join([f"{msg[0]} ({msg[1]})" for msg in messages])

#         # Generate summary using OpenAI
#         summary_prompt = f"""Summarize the key discussions and topics from these Discord messages:

#             {messages_text}

#             Provide a concise summary highlighting:
#             1. Main topics discussed
#             2. Key decisions or conclusions (if any)
#             3. Notable activity or events mentioned
#         """

#         summary_response = client.chat.completions.create(
#             model="gpt-4o",
#             messages=[{
#                 "role": "user",
#                 "content": summary_prompt
#             }],
#             temperature=0.7,
#             max_tokens=250
#         )

#         # Get message count and active hours
#         stats = cursor.execute("""
#             SELECT
#                 COUNT(*) as msg_count,
#                 COUNT(DISTINCT author_id) as unique_authors,
#                 strftime('%H', created_at) as hour,
#                 COUNT(*) as hour_count
#             FROM discord_messages
#             WHERE channel_id = ?
#               AND created_at >= datetime('now', '-7 days')
#             GROUP BY strftime('%H', created_at)
#             ORDER BY hour_count DESC
#             LIMIT 1
#         """, (channel_id,)).fetchone()

#         # Get channel name
#         channel_name = cursor.execute("""
#             SELECT DISTINCT channel_name FROM channel_metadata WHERE channel_id = ?
#         """, (channel_id,)).fetchone()

#         summaries.append({
#             "channel_id": channel_id,
#             "channel_name": channel_name[0] if channel_name else "Unknown Channel",
#             "summary": summary_response.choices[0].message.content,
#             "message_count": stats[0],
#             "unique_authors": stats[1],
#             "most_active_hour": f"{stats[2]}:00",
#             "peak_hour_messages": stats[3]
#         })

#     conn.close()
#     print(summaries)
#     return {"summaries": summaries}

@fastapi_app.post("/ask")
async def ask_discord(request: Request):
    """
    This endpoint uses OpenAI function calling to decide if we should:
    1) Do RAG (similarity search)
    2) Generate & execute SQL
    to answer the user’s question.
    """
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    body = await request.json()
    user_query = body.get("query", "")

    if not user_query:
        return {"error": "No query provided."}

    # The system message can instruct the model how to decide.
    system_message = {
        "role": "system",
        "content":
            """
            You are a helpful assistant. You can answer user questions using either:\n\n
            1) RAG-based similarity search (when the user wants summarized info from the actual conversation content), OR\n
            2) Generating a SQL query if the user wants structured data queries.\n\n
            Please do not mix them. Decide which approach is best for the user's question.\n
            If you choose SQL, provide a valid SQL SELECT statement that references the 'discord_messages' table.\n
            here is the schema for the `discord_messages` table that we have:\n
            discord_messages (
                        id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        author_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL
                    )
            """
    }

    user_message = {
        "role": "user",
        "content": user_query
    }
    messages = [system_message, user_message]

    # 1) Ask the model to call our function
    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "decide_approach"}}
    )
    completion_message = completion.choices[0].message
    messages.append(completion_message)
    tool_calls = completion_message.tool_calls
    # 2) Parse the function call
    if not tool_calls:
        return {
            "answer": "No function call was produced by the LLM. Could not proceed."
        }
    for tool_call in tool_calls:
        fn_name = tool_call.function.name
        fn_args = json.loads(tool_call.function.arguments)
        approach = fn_args.get("approach", "rag")
        print(f"approach: {approach}")

        # 3) If approach == 'rag', do the existing similarity_search
        if approach == "rag":
            rag_data = similarity_search(user_query)
            messages.append(
                {
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": fn_name,
                    "content": str(rag_data),
                }
            )
            final_response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
            )
            messages.append(final_response.choices[0].message)
            return {
                "answer": final_response.choices[0].message.content,
                "chat_history": messages
            }

        # 4) If approach == 'sql', let's run the sql_query
        elif approach == "sql":
            sql_query = fn_args.get("sql_query", "")
            if not sql_query.strip():
                return {"answer": "No SQL query provided by LLM."}

            # Attempt to run it
            sql_data = do_sql_query(sql_query)
            messages.append(
                {
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": fn_name,
                    "content": str(sql_data),
                }
            )
            final_response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
            )
            messages.append(final_response.choices[0].message)
            return {
                "answer": final_response.choices[0].message.content,
                "chat_history": messages,
            }



        else:
            return {"answer": "Unknown approach returned by LLM."}


@fastapi_app.post("/discord/{guild_id}")
async def scrape_server(guild_id: str, limit: int = DEFAULT_LIMIT):
    discord_token = os.environ["DISCORD_TOKEN"]
    headers = {
        "Authorization": discord_token,
        "Content-Type": "application/json"
    }
    volume.reload()
    scrape_discord_server(guild_id, headers, limit)
    volume.commit()
    return {"status": "ok", "message": f"Scraped guild_id={guild_id}, limit={limit}"}

# @fastapi_app.get("/query/{message}")
def similarity_search(message: str, top_k: int = 15):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    conn = get_db_conn(DB_PATH)
    cursor = conn.cursor()
    query_vec = client.embeddings.create(model="text-embedding-ada-002", input=message).data[0].embedding
    query_bytes = serialize(query_vec)

    results = cursor.execute(
            """
            SELECT
                vec_discord_messages.id,
                distance,
                discord_messages.channel_id,
                discord_messages.author_id,
                discord_messages.content,
                discord_messages.created_at
            FROM vec_discord_messages
            LEFT JOIN discord_messages USING (id)
            WHERE embedding MATCH ?
              AND k = ?
            ORDER BY distance
            """,
            [query_bytes, top_k],
        ).fetchall()
    conn.close()

    return results


def do_sql_query(sql_query: str):
    """
    Executes the generated SQL and returns the rows.
    """
    print(f"sql query generated: {sql_query}")
    from .common import get_db_conn
    conn = get_db_conn(DB_PATH)
    cursor = conn.cursor()

    try:
        rows = cursor.execute(sql_query).fetchall()
        conn.close()
        return {
            "answer": f"SQL Query Results: {rows}",
            "approach": "sql",
            "sql_query": sql_query,
        }
    except Exception as e:
        return {
            "error": str(e),
            "approach": "sql",
            "sql_query": sql_query
        }

@fastapi_app.get("/")
def read_root():
    return {"message": "Hello World"}

async def get_channel_summaries(force_refresh: bool = False) -> Dict:
    """
    Get summaries for all channels, using cache when available unless force_refresh is True.
    """
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    conn = get_db_conn(DB_PATH)
    cursor = conn.cursor()

    # Get all channels with recent activity
    channels = cursor.execute("""
        SELECT DISTINCT
            m.channel_id,
            cm.channel_name
        FROM discord_messages m
        JOIN channel_metadata cm ON m.channel_id = cm.channel_id
        WHERE m.created_at >= datetime('now', '-7 days')
        ORDER BY m.channel_id
    """).fetchall()

    summaries = []
    for channel_id, channel_name in channels:
        # Check cache first if we're not forcing a refresh
        if not force_refresh:
            cached_summary = get_cached_channel_summary(conn, channel_id)
            if cached_summary:
                summaries.append(cached_summary)
                continue

        # Generate new summary if needed
        messages = cursor.execute("""
            SELECT content, created_at
            FROM discord_messages
            WHERE channel_id = ?
              AND created_at >= datetime('now', '-7 days')
            ORDER BY created_at DESC
        """, (channel_id,)).fetchall()

        if not messages:
            continue

        # Get channel statistics
        stats = cursor.execute("""
            SELECT
                COUNT(*) as msg_count,
                COUNT(DISTINCT author_id) as unique_authors,
                strftime('%H', created_at) as hour,
                COUNT(*) as hour_count
            FROM discord_messages
            WHERE channel_id = ?
              AND created_at >= datetime('now', '-7 days')
            GROUP BY strftime('%H', created_at)
            ORDER BY hour_count DESC
            LIMIT 1
        """, (channel_id,)).fetchone()

        # Generate summary using OpenAI
        messages_text = "\n".join([f"{msg[0]} ({msg[1]})" for msg in messages])
        summary_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": f"""Analyze the following messages from Discord channel '{channel_name}'
                    and provide a concise summary of the key discussions from the past week. Focus on:

                    1. Main conversation topics and themes
                    2. Important questions or issues raised
                    3. Any significant announcements or decisions
                    4. Notable community interactions or discussions

                    Channel: #{channel_name}
                    Messages: {messages_text}
                    """
            }],
        )

        summary_data = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "summary": summary_response.choices[0].message.content,
            "message_count": stats[0],
            "unique_authors": stats[1],
            "most_active_hour": f"{stats[2]}:00",
            "peak_hour_messages": stats[3],
            "summary_start_date": messages[-1][1],  # First message in time range
            "summary_end_date": messages[0][1],    # Last message in time range
        }

        # Store in cache
        store_channel_summary(conn, summary_data)
        summaries.append(summary_data)

    conn.close()
    return {
        "summaries": summaries,
        "generated_at": datetime.now().isoformat(),
        "cache_status": "fresh" if force_refresh else "mixed"
    }

def store_channel_summary(conn: sqlite3.Connection, summary_data: Dict) -> None:
    """
    Store a newly generated channel summary in the cache.
    """
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO channel_summaries (
            channel_id,
            summary_text,
            message_count,
            unique_authors,
            most_active_hour,
            peak_hour_messages,
            summary_start_date,
            summary_end_date,
            generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        summary_data["channel_id"],
        summary_data["summary"],
        summary_data["message_count"],
        summary_data["unique_authors"],
        summary_data["most_active_hour"],
        summary_data["peak_hour_messages"],
        summary_data["summary_start_date"],
        summary_data["summary_end_date"]
    ))
    conn.commit()

def get_cached_channel_summary(conn: sqlite3.Connection, channel_id: str) -> Optional[Dict]:
    """
    Retrieve a cached summary if it exists and is still valid (less than 24 hours old).
    Returns None if no valid cache exists.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            cs.*,
            cm.channel_name,
            strftime('%s', 'now') - strftime('%s', cs.generated_at) as age_seconds
        FROM channel_summaries cs
        JOIN channel_metadata cm ON cs.channel_id = cm.channel_id
        WHERE cs.channel_id = ?
        AND cs.generated_at >= datetime('now', '-1 day')
    """, (channel_id,))

    row = cursor.fetchone()
    if row:
        return {
            "channel_id": row[0],
            "summary": row[1],
            "message_count": row[2],
            "unique_authors": row[3],
            "most_active_hour": row[4],
            "peak_hour_messages": row[5],
            "summary_start_date": row[6],
            "summary_end_date": row[7],
            "generated_at": row[8],
            "channel_name": row[9],
            "cache_age_seconds": row[10]
        }
    return None
