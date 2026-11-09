"""PrintBot: members DM a PDF to a Slack bot; this computer prints it over Wi-Fi Direct.

Runs on the Windows bridge computer. See PLAN.md for the design.

Environment variables:
  SLACK_BOT_TOKEN      xoxb-... bot token (required)
  SLACK_APP_TOKEN      xapp-... app-level token for Socket Mode (required)
  ALERT_USER_IDS       Slack member IDs to DM when a job fails, comma-separated (required)
  PRINTER_NAME         Windows printer name (default: Brother-WiFiDirect)
  PRINTER_IP           printer IP on the Wi-Fi Direct network; if set, ping it before each job (optional)
  WIFI_DIRECT_PROFILE  Windows Wi-Fi profile of the printer network, used to reconnect
                       (default: DIRECT-92HL-L2460DW_BRddaf)
  SUMATRA_PATH         path to SumatraPDF.exe (default: C:\\Program Files\\SumatraPDF\\SumatraPDF.exe)
"""
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from datetime import date, datetime

PRINTER_NAME = os.environ.get("PRINTER_NAME", "Brother-WiFiDirect")
PRINTER_IP = os.environ.get("PRINTER_IP", "")
WIFI_DIRECT_PROFILE = os.environ.get("WIFI_DIRECT_PROFILE", "DIRECT-92HL-L2460DW_BRddaf")
SUMATRA_PATH = os.environ.get("SUMATRA_PATH", r"C:\Program Files\SumatraPDF\SumatraPDF.exe")

MAX_BYTES = 20 * 1024 * 1024
DAILY_LIMIT = 30
CATCH_UP_SECONDS = 24 * 3600
SPOOL_TIMEOUT = 180
DONE, FAILED = "white_check_mark", "x"
# Get-Printer PrinterStatus values that mean the job did not print. "Offline" is not here:
# a Wi-Fi Direct printer can show it when idle. An offline printer never drains the queue,
# so the spool timeout catches it.
ERROR_STATES = {"Error", "PaperJam", "PaperOut", "PaperProblem", "NoToner", "DoorOpen",
                "OutputBinFull", "UserIntervention", "OutOfMemory"}

log = logging.getLogger("printbot")


class PrintError(Exception):
    """A failure with a reason that is safe to show to the member."""


# ---------- Windows print path ----------

def _powershell(command):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def _printer_reachable():
    return subprocess.run(["ping", "-n", "1", "-w", "1000", PRINTER_IP],
                          capture_output=True).returncode == 0


def print_pdf(path):
    """Print a PDF and wait until Windows has handed the job to the printer."""
    if PRINTER_IP and not _printer_reachable():
        if WIFI_DIRECT_PROFILE:
            subprocess.run(["netsh", "wlan", "connect", f"name={WIFI_DIRECT_PROFILE}"],
                           capture_output=True)
            time.sleep(10)
        if not _printer_reachable():
            raise PrintError("the printer is not reachable over Wi-Fi Direct")

    r = subprocess.run([SUMATRA_PATH, "-print-to", PRINTER_NAME, "-silent", path], timeout=120)
    if r.returncode != 0:
        raise PrintError(f"SumatraPDF failed (exit code {r.returncode})")

    # ponytail: waits for the whole Windows queue, not only this job; fine for one house printer.
    deadline = time.monotonic() + SPOOL_TIMEOUT
    while True:
        count = _powershell(f"@(Get-PrintJob -PrinterName '{PRINTER_NAME}').Count")
        if not count.isdigit():
            raise PrintError(f"cannot read the Windows print queue for '{PRINTER_NAME}'")
        if count == "0":
            break
        if time.monotonic() > deadline:
            raise PrintError("the job was still in the Windows print queue after 3 minutes")
        time.sleep(3)

    status = _powershell(f"(Get-Printer -Name '{PRINTER_NAME}').PrinterStatus")
    if ERROR_STATES & {s.strip() for s in status.split(",")}:
        raise PrintError(f"the printer reports status '{status}'")


# ---------- Slack bot ----------

class PrintBot:
    def __init__(self, client, token, bot_user_id, alert_ids, print_fn=print_pdf):
        self.client = client
        self.token = token
        self.bot_user_id = bot_user_id
        self.alert_ids = alert_ids
        self.print_fn = print_fn
        self.jobs = queue.Queue()
        self.pending = []      # jobs that wait or print now, in order
        self.seen = set()      # (channel, ts) already handled: live event and catch-up can overlap
        self.daily = Counter()  # (user, date) -> accepted jobs; resets at restart
        self.lock = threading.Lock()

    def call(self, method, **kwargs):
        """Call a Slack method. Log a failure; never raise."""
        try:
            return getattr(self.client, method)(**kwargs)
        except Exception:
            log.exception("Slack call %s failed", method)
            return None

    # --- intake ---

    def handle_message(self, msg):
        if msg.get("bot_id") or msg.get("user") in (None, self.bot_user_id):
            return
        if msg.get("subtype") not in (None, "file_share") or not msg.get("files"):
            return
        if any(r.get("name") in (DONE, FAILED) and self.bot_user_id in r.get("users", [])
               for r in msg.get("reactions", [])):
            return
        key = (msg["channel"], msg["ts"])
        with self.lock:
            if key in self.seen:
                return
            self.seen.add(key)
        for f in msg["files"]:
            self.accept_file(msg, f)

    def accept_file(self, msg, f):
        channel, ts, user = msg["channel"], msg["ts"], msg["user"]
        name = f.get("name") or "file"
        try:
            if "url_private_download" not in f:  # event gave a stub, e.g. file_access=check_file_info
                f = self.client.files_info(file=f["id"])["file"]
                name = f.get("name") or name
            if f.get("filetype") != "pdf":
                raise PrintError("only PDF files can be printed")
            if f.get("size", 0) > MAX_BYTES:
                raise PrintError("the file is larger than 20 MB")
            if self.daily[(user, date.today())] >= DAILY_LIMIT:
                raise PrintError(f"you reached the limit of {DAILY_LIMIT} jobs today")
            data = self.download(f)
            if not data.startswith(b"%PDF"):
                raise PrintError("the file is not a valid PDF")
        except PrintError as e:
            self.reject(channel, ts, name, str(e))
            return
        except Exception:
            log.exception("download of %s failed", name)
            self.reject(channel, ts, name, "the file could not be downloaded from Slack. Send it again")
            return

        job = {"channel": channel, "ts": ts, "user": user, "name": name, "data": data}
        with self.lock:
            self.daily[(user, date.today())] += 1
            self.pending.append(job)
            r = self.call("chat_postMessage", channel=channel,
                          text=f"{name}: you are #{len(self.pending)} in the queue.")
            job["status_ts"] = r["ts"] if r else None
            self.jobs.put(job)
        log.info("queued %s from %s", name, user)

    def download(self, f):
        req = urllib.request.Request(f["url_private_download"],
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise PrintError("the file is larger than 20 MB")
        return data

    def reject(self, channel, ts, name, reason):
        log.info("rejected %s: %s", name, reason)
        self.call("chat_postMessage", channel=channel, text=f"Cannot print {name}: {reason}.")
        self.call("reactions_add", channel=channel, timestamp=ts, name=FAILED)

    # --- printing ---

    def worker(self):
        while True:
            self.process(self.jobs.get())

    def process(self, job):
        self.set_status(job, f"{job['name']}: printing…")
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                path = os.path.join(d, "job.pdf")
                with open(path, "wb") as fh:
                    fh.write(job["data"])
                self.print_fn(path)
        except Exception as e:
            reason = str(e) if isinstance(e, PrintError) else f"unexpected error ({e})"
            log.error("failed %s from %s: %s", job["name"], job["user"], reason)
            self.set_status(job, f"Could not print {job['name']}: {reason}. The house admins were told.")
            self.call("reactions_add", channel=job["channel"], timestamp=job["ts"], name=FAILED)
            self.alert(job, reason)
        else:
            log.info("printed %s from %s", job["name"], job["user"])
            if job["status_ts"]:
                self.call("chat_delete", channel=job["channel"], ts=job["status_ts"])
            self.call("reactions_add", channel=job["channel"], timestamp=job["ts"], name=DONE)
        finally:
            job["data"] = None
            with self.lock:
                self.pending.remove(job)
                waiting = list(self.pending)
            for i, j in enumerate(waiting, 1):
                self.set_status(j, f"{j['name']}: you are #{i} in the queue.")

    def set_status(self, job, text):
        if job["status_ts"]:
            self.call("chat_update", channel=job["channel"], ts=job["status_ts"], text=text)

    def alert(self, job, reason):
        text = (f":warning: PrintBot could not print a job.\n"
                f"From: <@{job['user']}>\nFile: {job['name']}\nReason: {reason}\n"
                f"Time: {datetime.now():%Y-%m-%d %H:%M}")
        for uid in self.alert_ids:
            r = self.call("conversations_open", users=uid)
            if r:
                self.call("chat_postMessage", channel=r["channel"]["id"], text=text)

    # --- startup ---

    def catch_up(self):
        """Queue PDFs that members sent while the bot was off (last 24 hours)."""
        oldest = str(time.time() - CATCH_UP_SECONDS)
        channels, cursor = [], None
        while True:
            r = self.client.users_conversations(types="im", limit=200, cursor=cursor)
            channels += [c["id"] for c in r["channels"]]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

        messages = []
        for channel in channels:
            # ponytail: one page (200 messages) per DM; enough for 24 hours in a house.
            try:
                r = self.client.conversations_history(channel=channel, oldest=oldest, limit=200)
            except Exception:
                log.exception("catch-up: cannot read DM %s", channel)
                continue
            for m in r["messages"]:
                m["channel"] = channel
                text = m.get("text", "")
                if m.get("user") == self.bot_user_id or m.get("bot_id"):
                    if text.endswith(("in the queue.", "printing…")):  # stale status message
                        self.call("chat_delete", channel=channel, ts=m["ts"])
                else:
                    messages.append(m)

        messages.sort(key=lambda m: float(m["ts"]))
        log.info("catch-up: %d DMs, %d member messages in the last 24 hours",
                 len(channels), len(messages))
        for m in messages:
            self.handle_message(m)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(filename=os.path.join(here, "printbot.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.environ["SLACK_BOT_TOKEN"]
    alert_ids = [u.strip() for u in os.environ["ALERT_USER_IDS"].split(",") if u.strip()]

    while True:  # after a boot, the network may not be ready yet
        try:
            app = App(token=bot_token)
            break
        except Exception as e:
            log.warning("Slack is not reachable yet (%s). Trying again in 10 seconds.", e)
            time.sleep(10)

    try:  # retry Slack 429 (rate limited) answers instead of failing
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        app.client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    except ImportError:
        log.warning("RateLimitErrorRetryHandler not found; rate-limited calls will not retry")

    bot = PrintBot(app.client, bot_token, app.client.auth_test()["user_id"], alert_ids)

    @app.event("message")
    def on_message(event):
        bot.handle_message(event)

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.connect()  # connect first: no DM is lost during catch-up
    threading.Thread(target=bot.worker, daemon=True).start()
    try:
        bot.catch_up()
    except Exception:
        log.exception("catch-up failed")
    log.info("PrintBot is running")
    threading.Event().wait()


if __name__ == "__main__":
    main()
