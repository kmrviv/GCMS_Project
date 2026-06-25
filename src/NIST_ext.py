"""
NIST MS Search 2.3 — Batch export library to MSP files
========================================================
Automates the exact manual workflow:
  1. Search menu
  2. ID_Number submenu
  3. Type start_id, Tab, type end_id
  4. Enter (run search)
  5. Ctrl+A (select all results)
  6. Shift+F10 (right-click context menu)
  7. Down arrow x6 (navigate to "Export selected")
  8. Enter (open save dialog)
  9. Type filename, Enter

Then loop for the next chunk.

USAGE
-----
    pip install pywinauto
    # Open NIST MS Search manually, then:
    python nist_batch_export.py                # full run

The script is RESUMABLE, will start from where previously stopped
TIMING
------
If any failiures, increase delay times
"""

import sys
import time
import argparse
from pathlib import Path

from pywinauto import Application, findwindows
from pywinauto.keyboard import send_keys


# ============================== CONFIG ==============================
OUTPUT_DIR    = Path(r"C:\NIST17\MSSEARCH\MSP_Export")
CHUNK_SIZE    = 1000               # check max data that can be selected at a time
TOTAL_SPECTRA = 267380             # adjust to your library (Options → Libraries)
LIBRARY_NAME  = "mainlib"          # used in output filenames, otherwise no need to specify(will manually select the library before running program to ensure data from mainlib only)

WINDOW_TITLE  = "NIST MS Search"   # Give the name of the window (top left name), will match via subtring matching, so give general name

# Delays (seconds)
STEP_DELAY    = 0.6                # between consecutive UI actions
SEARCH_WAIT   = 4.0                # after Enter on search button
EXPORT_WAIT   = 12.0                # for the save-to-disk to finish
KEY_DELAY     = 0.08               # tiny delay between successive keystrokes
# ====================================================================


def connect_to_nist():
    #finds and attaches the name of the window to be used
    handles = findwindows.find_windows(title_re=f".*{WINDOW_TITLE}.*")
    #if window not open, then the following error will be thrown:
    if not handles:
        sys.exit("ERROR: NIST MS Search is not running. Open it first.")
    
    app = Application(backend="win32").connect(handle=handles[0])
    return app.window(handle=handles[0])


def export_one_chunk(win, start_id: int, end_id: int, output_file: Path):
    #one chunk processing:
    win.set_focus()
    time.sleep(STEP_DELAY)

    # Search menu -> ID_Number submenu
    win.menu_select("Search->ID_Number")

    # Step 3 — type start_id, Tab, type end_id.
    # Clear each field first (Ctrl+A then Delete) in case a previous value lingers.
    send_keys("^a", pause=KEY_DELAY)
    send_keys("{DELETE}")
    send_keys(str(start_id), pause=KEY_DELAY)
    send_keys("-")
    send_keys(str(end_id), pause=KEY_DELAY)

    # Step 4 — Enter to run search.
    send_keys("{ENTER}")
    time.sleep(SEARCH_WAIT)

    # Step 5 — Ctrl+A to select all in the result list.
    send_keys("^a")
    time.sleep(STEP_DELAY)

    # Step 6 — Shift+F10 = keyboard equivalent of right-click. Opens context menu.
    send_keys("+{F10}")
    time.sleep(STEP_DELAY)

    # Step 7 — Down arrow 6 times to reach "Export selected".
    for _ in range(6):
        send_keys("{DOWN}")
        time.sleep(KEY_DELAY * 2)

    # Step 8 — Enter on "Export selected" — opens the Save dialog.
    send_keys("{ENTER}")
    time.sleep(STEP_DELAY * 2)

    # Step 9 — type the filename (full path so it lands in OUTPUT_DIR
    # regardless of the dialog's "remembered" folder).
    send_keys("^a", pause=KEY_DELAY)
    send_keys("{DELETE}")
    send_keys(str(output_file), with_spaces=True, pause=KEY_DELAY)
    send_keys("{ENTER}")
    time.sleep(EXPORT_WAIT)

    # If a "File exists, overwrite?" prompt appears, answer Yes.
    # Harmless if no such prompt — the keypress just goes to the main window.
    send_keys("y")
    time.sleep(STEP_DELAY)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-id", type=int, default=1,
                    help="Resume from this ID (default: 1)")
    ap.add_argument("--end-id", type=int, default=TOTAL_SPECTRA,
                    help=f"Last ID to export (default: {TOTAL_SPECTRA})")
    ap.add_argument("--chunk", type=int, default=CHUNK_SIZE,
                    help=f"Chunk size (default: {CHUNK_SIZE})")
    ap.add_argument("--test", action="store_true",
                    help="Export only the first chunk, then stop "
                         "(useful for verifying the workflow once)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned chunks without exporting")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build the chunk list.
    chunks = []
    for start in range(args.start_id, args.end_id + 1, args.chunk):
        end = min(start + args.chunk - 1, args.end_id)
        chunks.append((start, end))
    if args.test:
        chunks = chunks[:1]

    print(f"Library      : {LIBRARY_NAME}")
    print(f"ID range     : {args.start_id} – {args.end_id}")
    print(f"Chunk size   : {args.chunk}")
    print(f"Total chunks : {len(chunks)}")
    print(f"Output dir   : {OUTPUT_DIR}")
    print()

    if args.dry_run:
        for s, e in chunks:
            f = OUTPUT_DIR / f"{LIBRARY_NAME}_{s}_{e}.msp"
            mark = "skip" if f.exists() and f.stat().st_size > 0 else "would export"
            print(f"  {mark}: {f.name}")
        return

    win = connect_to_nist()

    print("Starting in 5 seconds. Bring NIST MS Search to the foreground.")
    print("DO NOT touch the mouse or keyboard during the run.")
    for i in range(5, 0, -1):
        print(f"  {i}…", end=" ", flush=True)
        time.sleep(1)
    print()

    for idx, (start, end) in enumerate(chunks, 1):
        output_file = OUTPUT_DIR / f"{LIBRARY_NAME}_{start}_{end}.msp"

        if output_file.exists() and output_file.stat().st_size > 0:
            print(f"[{idx:>3}/{len(chunks)}] {start:>7}–{end:>7}   skip (exists)")
            continue

        print(f"[{idx:>3}/{len(chunks)}] {start:>7}–{end:>7}   exporting…",
              end=" ", flush=True)
        try:
            export_one_chunk(win, start, end, output_file)
            if output_file.exists() and output_file.stat().st_size > 0:
                kb = output_file.stat().st_size / 1024
                print(f"done ({kb:,.0f} KB)")
            else:
                print("FAILED — no file written")
                ans = input("  [r]etry / [s]kip / [q]uit? ").strip().lower()
                if ans.startswith("q"):
                    return
                if ans.startswith("r"):
                    export_one_chunk(win, start, end, output_file)
        except KeyboardInterrupt:
            print("\n\nInterrupted. Re-run to resume — already-exported chunks "
                  "will be skipped.")
            return
        except Exception as e:
            print(f"\n  ERROR: {e}")
            ans = input("  [r]etry / [s]kip / [q]uit? ").strip().lower()
            if ans.startswith("q"):
                return
            if ans.startswith("r"):
                try:
                    export_one_chunk(win, start, end, output_file)
                except Exception as e2:
                    print(f"  Retry also failed: {e2}. Skipping.")

    print("\nAll chunks complete.")
    print(f"\nNext: concatenate the chunks with:")
    print(f"  Get-Content {OUTPUT_DIR}\\{LIBRARY_NAME}_*.msp | "
          f"Set-Content {OUTPUT_DIR}\\full_{LIBRARY_NAME}.msp")


if __name__ == "__main__":
    main()

