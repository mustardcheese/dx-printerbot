# Fraternity Print Server — Plan

## 1. Goal

Let each member send a PDF from a phone or a laptop. The PDF prints on the Brother HL-L2460DW.
The member does not install software, does not change networks, and does not use a USB cable.
Members already use Slack. Thus each member sends the job in a private Slack DM to a bot.

## 2. Key Constraint: Why Peer-to-Peer From Users Is Not Possible

Eduroam usually uses **client isolation**. Client isolation is a network setting that blocks one device on the network from connecting to another device on the same network.
Thus a phone on eduroam cannot connect directly to the TV computer.

Result:
- A job must go from the user to an **internet service** (Slack). The bridge computer then gets the job from Slack.
- The only direct device-to-device link is **bridge computer → printer**, through Wi-Fi Direct. This is the stable link.

```
 User (Slack app on phone/laptop)
        │  DMs the PDF to PrintBot (private)
        ▼
 Slack
        │  event pushed at once over an open WebSocket (Socket Mode)
        ▼
 Bridge computer (Windows TV computer) — printbot.py
   ├─ Link 1: internet (Ethernet or eduroam)
   └─ Link 2: printer Wi-Fi Direct network "DIRECT-xx-HL-L2460DW"
        │  IPP (driverless)
        ▼
 Brother HL-L2460DW
```

## 3. Bridge Hardware (Do This First)

The bridge needs **two network links at the same time**:
1. Internet, to get the jobs.
2. The printer's Wi-Fi Direct network, to print.

One Wi-Fi adapter cannot connect to two networks at the same time. Use one of these branches:

| Branch | Internet link | Printer link | Cost |
|---|---|---|---|
| A | Ethernet port on the TV computer | Built-in Wi-Fi → DIRECT-xx network | $0 |
| B | Built-in Wi-Fi → eduroam | USB Wi-Fi adapter → DIRECT-xx network | ~$15 |
| C (fallback) | Raspberry Pi: Ethernet or Wi-Fi | Pi Wi-Fi or USB adapter | ~$50 |

Use Branch C only if the TV computer cannot stay on all the time.

### Routing Step (All Branches)

The printer network has no internet. Windows must not send internet traffic to it.
- Set a **high interface metric** on the printer adapter. The interface metric is the number that Windows uses to select a route. A lower number has priority.
  ```powershell
  Set-NetIPInterface -InterfaceAlias "Wi-Fi 2" -InterfaceMetric 500
  ```
- On the printer Wi-Fi profile, set "Connect automatically" to on.

## 4. Print Path (Driverless)

No Brother driver is necessary on user devices or on the bridge.
1. Connect the bridge to the printer DIRECT-xx network.
2. On the bridge, add the printer as an IPP printer: `http://<printer-ip>:631/ipp/print`. IPP (Internet Printing Protocol) is a standard protocol. Windows has a built-in driverless IPP class driver for it.
3. Print a PDF without a dialog box. Use the SumatraPDF command line:
   ```
   SumatraPDF.exe -print-to "Brother-WiFiDirect" -silent job.pdf
   ```

## 5. Intake: Private DM to a Slack Bot (Event-Driven)

### Why a DM
- Members already use Slack. They do not need a new account or new software. It works on mobile and on laptops.
- A DM is private between the member and the bot. On the free plan, workspace admins cannot export or read DMs.
- A bot can delete only its own messages, and everyone in a channel can see each message in it. Thus a channel cannot give private jobs. There is **no print channel and no public queue**.
- Slack confirms who sent each message. There is no sender spoofing. Only workspace members can DM the bot.
- The workspace is on the free plan, it has fewer than 10 apps, and the admin can approve the app (confirmed by the user).

### Why There Is No Polling
The bot uses **Socket Mode**. In Socket Mode, the bot opens one outgoing WebSocket to Slack and keeps it open. A WebSocket is a connection that stays open so that each side can send data at any time.
- Slack **pushes** each new DM event to the bot at once.
- When there are no jobs, the connection is idle (keep-alive pings only).
- Slack does not need a public URL or an incoming connection to the bridge. Thus eduroam client isolation and NAT do not stop it.

### Slack App Setup
Fast path: at api.slack.com/apps, select **Create New App → From a manifest**, and paste `manifest.json`. It does steps 1–5 below, except the app-level token in step 2.

1. At api.slack.com, create an app "PrintBot" from scratch in the workspace.
2. Turn on **Socket Mode**. Make an **app-level token** (`xapp-…`) with scope `connections:write`.
3. Bot token scopes: `files:read`, `chat:write`, `im:history`, `im:read`, `im:write`, `reactions:read`, `reactions:write`.
4. Event Subscriptions: subscribe to the bot event `message.im`.
5. App Home: turn on the Messages tab, so members can DM the bot.
6. Install the app to the workspace (the admin approves it). Copy the **bot token** (`xoxb-…`).

### Configuration (Environment Variables on the Bridge)
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`: the two tokens. Do not put them in the script or in a repository.
- `ALERT_USER_IDS`: the Slack member IDs of the user and the housing manager, separated by a comma. These two people get a DM when a job fails.
- `PRINTER_NAME`: the Windows printer name (see `Get-Printer`). Default: `Brother-WiFiDirect`.
- `PRINTER_IP` (optional): the printer IP on the Wi-Fi Direct network. If you set it, the bot pings the printer before each job. Do not set it if Windows opens the Wi-Fi Direct link only while it prints.
- `WIFI_DIRECT_PROFILE` (optional): the Windows Wi-Fi profile of the printer network. The bot uses it to connect again when the ping fails.
- `SUMATRA_PATH` (optional): the path to `SumatraPDF.exe`. Default: `C:\Program Files\SumatraPDF\SumatraPDF.exe`.

### Dependency
- `slack_bolt` (the official Slack Python library). It includes the Socket Mode client. The Python standard library has no WebSocket client.

## 6. Bot Design (`printbot.py`)

One Python script on the bridge. It uses `slack_bolt` and the standard library.

### Queue
- The bot keeps one in-memory queue (Python `queue.Queue`). One worker thread sends jobs to the printer, one at a time.

### On Each DM With a File (Pushed by Slack)
1. Check the file:
   - Accept only `filetype == "pdf"`.
   - Download it from `url_private` with header `Authorization: Bearer <bot token>`.
   - Make sure that the file starts with `%PDF`. Maximum size: 20 MB.
   - Maximum 30 jobs for each user each day (you can tune this value). Keep the count in memory. It resets at restart, and that is acceptable.
   - If a check fails, reply in the DM with the reason. Do not alert the admins, because this is a user error.
2. Put the job in the queue. Post a status reply in the DM: **"You are #N in the queue."** N = the number of jobs that wait, plus 1 if a job is printing now.
3. When the place of a waiting job changes, edit its status message to show the new place.
4. When the worker starts the job, edit the status message to **"Printing…"**.
5. **Success:** delete the status message. Add ✅ to the user's file message. The ✅ gives the user a record and lets the bot detect duplicates.
6. **Failure:**
   - Edit the status message to **"Could not print: <reason>. The house admins were told."** Add ❌ to the user's file message.
   - DM each ID in `ALERT_USER_IDS` (`conversations.open`, then `chat.postMessage`). The DM gives the sender, the file name, the reason, and the time.
7. Write each action to `printbot.log`.

### What "Success" Means
A job is successful only when all three conditions are true:
1. SumatraPDF returns exit code 0.
2. The Windows print queue for the printer becomes empty within 3 minutes (`Get-PrintJob -PrinterName …`).
3. `Get-Printer` shows no error status.

If one condition is false, the job failed. Examples: printer offline, no Wi-Fi Direct link, paper jam, no paper.

Before each job, the bot makes sure that the printer IP replies. If it does not reply, the bot runs `netsh wlan connect name="DIRECT-xx-HL-L2460DW"` and tries one more time.

**Known limit:** the printer can jam *after* it accepts the full job. Windows may not see this jam. If this occurs in use, add a check of the IPP `printer-state-reasons` attribute after each job.

### Duplicates and Restart
- The ✅/❌ reaction on the user's file message is the only saved state. There is no database.
- Slack sends an event again if the bot does not confirm it. The bot skips each message that already has its ✅ or ❌.
- Slack does not keep events for a bot that is offline. Example: the PC is off, and a member sends a PDF. Thus, when the bot starts:
  1. Wait until Slack replies (`auth.test`). If the network is not ready yet after a boot, try again every 10 seconds.
  2. Open the Socket Mode connection **first**. Then new DMs that arrive during the scan are not lost.
  3. Delete the old status messages from the bot.
  4. List the bot's DMs (`users.conversations`, `types=im`).
  5. Read the last 24 hours of each DM (`conversations.history`).
  6. Queue each PDF message that does not have the bot's ✅ or ❌, in time order. Each sender gets a new "You are #N in the queue." message.
- The bot keeps an in-memory set of the message IDs (`ts`) that it has queued. A file can come from the scan and from a live event at the same time. The set makes sure that the file is queued only one time.
- **Known limit:** the scan reads only the last 24 hours. If the PC is off for longer, older files do not print. The sender gets no queue message and can send the file again. Change the window if this is a problem.
- **Known limit:** this uses one history call for each DM at startup. For a house-sized workspace, this is acceptable.

### Self-Test (`test_printbot.py`)
Use fake event payloads and a fake print function. Use no Slack calls. Test these cases:
- A valid PDF gets the correct queue place.
- A non-PDF file is rejected with a reply and no admin alert.
- A message that already has ✅ is skipped.
- A successful print deletes the status message and adds ✅.
- A failed print adds ❌ and sends a DM to each alert ID.

### Optional Features (Do Not Build Now)
- Print options in the message text (for example `duplex`, `copies=2`). Add these only when members ask.
- Image or DOCX files. Add this only when members ask.
- A "Retry" button in the admin alert. Add this only if failures occur often.

## 7. Operation

- Run the bot from **Task Scheduler**. The bot then starts when Windows starts, before anyone logs in.
  - Trigger: "At startup", with a delay of 30 seconds, to let the network connect.
  - General: "Run whether user is logged on or not".
  - Settings: "If the task fails, restart every 1 minute", up to 999 times. Turn off "Stop the task if it runs longer than 3 days".
  - Set the printer Wi-Fi Direct profile and the internet profile to connect automatically for all users. If not, Windows connects to them only after someone logs in.
- Set Windows Power to "Sleep: Never" (screen sleep is acceptable).
- `slack_bolt` connects again automatically after an internet drop. The catch-up step runs only at startup. Thus a DM sent during a long internet drop can be missed. In that case, the user gets no queue message and can send the file again.
- The TV services can continue to run. The bot uses almost no CPU when idle.

## 8. Build Steps

1. Do the bridge hardware branch (Section 3). Test: `ping <printer-ip>` and `ping 8.8.8.8` both succeed at the same time.
2. Add the IPP printer (Section 4). Test: print one PDF manually with SumatraPDF.
3. Create the Slack app (Section 5). Get the admin to approve it.
4. Get the two Slack member IDs for `ALERT_USER_IDS` (in Slack: Profile → ⋯ → Copy member ID).
5. Install Python and `pip install slack_bolt` on the bridge. Set the environment variables.
6. Write `printbot.py` and `test_printbot.py`. Run the test.
7. Run the bot manually. Test these cases:
   1. Send one PDF. You see "You are #1 in the queue.", then "Printing…". Then the message is deleted and ✅ shows.
   2. Send one non-PDF file. You see a rejection reply.
   3. From two accounts, send 3 PDFs quickly. The places are #1, #2, #3. Each place moves up as jobs finish. Each status message is deleted after its job prints.
   4. Turn the printer off. Send a PDF. The sender sees ❌ and the reason. Both alert IDs get a DM.
8. Stop the bot. Send a PDF. Start the bot. Make sure that the catch-up step prints it.
9. Install the Task Scheduler task. Shut down the computer. Send a PDF to the bot. Turn on the computer and do not log in. Make sure that the PDF prints and ✅ shows.
10. Tell members: "DM your PDF to @PrintBot. It tells you your place in the queue. ✅ = printed."

## 9. Fallback Intake (If Slack Is Not Available)

- **Email to a dedicated Gmail account:** an IMAP poller with a whitelist. It must print only when Gmail's `Authentication-Results` header shows `dmarc=pass`, because the `From:` header is easy to fake.
- **Shared Google Drive folder:** Drive for Desktop syncs the folder, and a script watches it. This needs the least code.
Both options use the same print path (Section 4).

## 10. Facts Not Verified

I did not check these facts. Check each one before you build:
- The HL-L2460DW supports IPP / AirPrint / Mopria (driverless printing). Check the Brother specification page.
- The printer's Wi-Fi Direct IP address. Brother often uses `192.168.118.1`. Read it on the printer's network configuration report.
- Whether "Group Owner" mode must be on, to keep the DIRECT-xx network stable.
- The maximum number of Wi-Fi Direct clients on the printer. The bridge uses one connection.
- SumatraPDF silent print works with the Windows IPP class driver.
- `Get-PrintJob` shows an empty queue only after the printer accepts the full job.
- On the free plan, workspace admins cannot export or read DMs.
- The exact Slack scope list in Section 5. The Slack app setup page shows an error for each missing scope.
- Slack's 2025 rate limits on `conversations.history` do not block the startup catch-up for an internal app.

## 11. Open Items (Only You Can Answer)

1. Does the TV computer have an Ethernet port that has internet? (This selects Branch A or B.)
2. Is the TV computer on all the time? (If not, use Branch C.)
3. Which Windows version does the TV computer use?
4. What are the Slack member IDs of you and the housing manager? (They go in `ALERT_USER_IDS`.)
