import os
import time

import httpx


class EmailDelivery:
    def __init__(self, store):
        self.store = store

    def deliver_once(self, now=None):
        now = time.time() if now is None else now
        key = os.environ.get("LOOP_RESEND_API_KEY")
        sender = os.environ.get("LOOP_EMAIL_FROM")
        recipient = os.environ.get("LOOP_EMAIL_TO")
        if not all((key, sender, recipient)):
            return "unconfigured"
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM alerts WHERE status IN ('pending','retry') "
                "AND next_attempt<=? ORDER BY id LIMIT 10",
                (now,),
            ).fetchall()
        for row in rows:
            status, error = "sent", None
            try:
                response = httpx.post(
                    "https://api.resend.com/emails",
                    timeout=10,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Idempotency-Key": f"loop-alert-{row['id']}-{int(row['timestamp'])}",
                    },
                    json={
                        "from": sender,
                        "to": [recipient],
                        "subject": row["subject"],
                        "text": row["body"],
                    },
                )
                response.raise_for_status()
            except Exception as exc:
                # Persist only exception class; response bodies may contain account information.
                status, error = "retry", type(exc).__name__
            attempts = row["attempts"] + 1
            if attempts >= 20 and status == "retry":
                status = "failed"
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE alerts SET status=?,attempts=?,next_attempt=?,error=? WHERE id=?",
                    (
                        status,
                        attempts,
                        now + min(3600, 15 * 2 ** min(attempts, 8)),
                        error,
                        row["id"],
                    ),
                )
        return "configured"
