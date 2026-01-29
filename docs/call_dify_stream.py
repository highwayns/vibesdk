import json
import httpx
from typing import Dict, Iterator, Optional, Tuple

def iter_sse_events(resp: httpx.Response) -> Iterator[Dict]:
    """
    Dify SSEを「chunk境界に依存せず」復元してJSONイベントをyieldする。
    SSEは event/data 行で来る場合と、data行のJSON内に event が入る場合の両方を吸収。
    """
    buf = ""

    for chunk in resp.iter_text():
        buf += chunk
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            raw = raw.strip()
            if not raw:
                continue

            event_name: Optional[str] = None
            data_lines = []

            for line in raw.splitlines():
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())

            if not data_lines:
                continue

            data_str = "\n".join(data_lines)

            # OpenAI風に data: [DONE] が来る実装もあるので念のため
            if data_str.strip() == "[DONE]":
                return

            payload = json.loads(data_str)
            if event_name and "event" not in payload:
                payload["event"] = event_name
            yield payload

def call_dify_stream(
    base_url: str,
    api_key: str,
    query: str,
    user: str,
    conversation_id: Optional[str] = None,
    inputs: Optional[dict] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    戻り値: (full_answer, conversation_id, task_id)
    """
    url = f"{base_url.rstrip('/')}/v1/chat-messages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "user": user,
        "inputs": inputs or {},
        "response_mode": "streaming",
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    full = []
    last_conversation_id = conversation_id
    task_id = None

    # read timeout を長め/無制限寄りに
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()

            for ev in iter_sse_events(resp):
                etype = ev.get("event")

                # 代表: message / message_end / message_file など
                if etype == "message":
                    # Dify は message のたびに answer を“チャンク”で返す
                    text = ev.get("answer", "")
                    if text:
                        full.append(text)
                        print(text, end="", flush=True)  # 逐次表示したい場合

                    task_id = task_id or ev.get("task_id")
                    last_conversation_id = ev.get("conversation_id") or last_conversation_id

                elif etype == "message_end":
                    # ここでストリーム完了
                    last_conversation_id = ev.get("conversation_id") or last_conversation_id
                    break

                else:
                    # Chatflow/Workflowだと node_started 等も飛ぶことがある
                    # 必要ならログに出す
                    # print("event:", etype, ev)
                    task_id = task_id or ev.get("task_id")
                    last_conversation_id = ev.get("conversation_id") or last_conversation_id

    return ("".join(full), last_conversation_id, task_id)
