"""Keep this process running so due follow-up drafts are prepared even if the browser is closed.

Follow-ups are never auto-sent. The dashboard still requires Approve & Send.

    python worker.py
"""

from scheduler import loop

if __name__ == "__main__":
    print("AI Outbound follow-up worker running. Ctrl+C to stop.")
    loop(poll_seconds=45)
