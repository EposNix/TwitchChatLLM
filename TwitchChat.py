import base64
import io
import json
import queue
import random
import threading
import time
from collections import deque
from datetime import datetime

import mss
import requests
from PIL import Image, ImageOps, ImageTk
import tkinter as tk
from tkinter import ttk

# ===========================
# Config (tweak as you like)
# ===========================
#API_URL = "http://127.0.0.1:1234/v1/chat/completions"
API_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:12b"          # matches your curl example
INTERVAL_SEC = 3.0                     # how often to generate a new line
HISTORY_LEN = 20                       # number of previous chat lines to include as context
TEMPERATURE = 0.7
MAX_TOKENS = -1                        # -1 for "unlimited" in LM Studio
STREAM = False
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 700
DEBUG_SCREENSHOT = True                # Set to True to show screenshot debug panel
USERNAME_POOL = [
    "SneakyPanda", "LagLord", "FrameDropper", "GGWP_123",
    "NoScopeNana", "PixelPirate", "EmoteMachine", "GachiMain",
    "BackseatBaron", "CopiumDealer", "ClutchGoblin", "PatchNotesPls"
]
USERNAME_COLORS = [
    "#1E90FF", "#32CD32", "#FF4500", "#8A2BE2",
    "#DAA520", "#FF69B4", "#00CED1", "#DC143C",
    "#2E8B57", "#FF8C00", "#20B2AA", "#BA55D3"
]

# A light guardrail so the model behaves like Twitch chat (short, lively, single-line).
SYSTEM_INSTRUCTIONS = (
    "You are simulating a single Twitch chat message.\n"
    "Rules:\n"
    "1) Output exactly ONE short chat line. No preface, no bullets, no quotes.\n"
    "2) React like Twitch chat would to the SCREENSHOT + RECENT_CHAT provided.\n"
    "3) **PRIORITY**: If MODERATOR has posted a message in RECENT_CHAT, respond directly to what the moderator said.\n"
    "4) Give reactions, advice, or recommendations on what to do next.\n"
    "5) Respond like a gen z teenager.\n"
)

# ===========================
# Helper: screenshot -> data URL (no disk writes)
# ===========================
def get_screen_data_url(max_w=1024, max_h=1024):
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor only (monitors[0] = all monitors combined)
        raw = sct.grab(monitor)
        # Convert BGRA to RGB PIL Image
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        # Downscale to reduce payload (keeps aspect)
        img = ImageOps.contain(img, (max_w, max_h))
        # Encode to PNG in-memory
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        # BEST PRACTICE: immediately discard buffers
        buf.close()
        # Return both the data URL and a copy of the image for debugging
        img_copy = img.copy()
        del img
        return b64, img_copy

# ===========================
# LLM call
# ===========================
def llm_generate_line(screen_b64, recent_chat):
    # Build a compact "recent chat" text block
    if recent_chat:
        recent = "\n".join(recent_chat)
    else:
        recent = "(none yet)"

    user_text = (
        "SCREENSHOT: (see image)\n"
        f"RECENT_CHAT:\n{recent}\n\n"
        "Now produce exactly ONE new Twitch-style chat line reacting to the screenshot."
    )

    prompt = SYSTEM_INSTRUCTIONS + "\n\n" + user_text

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": [screen_b64],
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": MAX_TOKENS
        },
        "stream": STREAM
    }

    resp = requests.post(API_URL, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    # Ollama generate output
    try:
        content = data["response"].strip()
    except Exception as e:
        content = f"(error parsing response: {e})"
    return content

# ===========================
# UI: Twitch-like scroller
# ===========================
class TwitchChatUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Local Twitch Chat (LLM)")
        self.root.attributes("-topmost", True)  # keep on top
        # Right-side docking
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - WINDOW_WIDTH - 10
        y = int((sh - WINDOW_HEIGHT) / 2)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")

        # Debug frame for screenshot display (shown if DEBUG_SCREENSHOT is True)
        if DEBUG_SCREENSHOT:
            self.debug_frame = ttk.Frame(root)
            self.debug_label = tk.Label(self.debug_frame, bg="#1A1A1D", text="No screenshot yet", fg="#FFFFFF")
            self.debug_label.pack(fill="both", expand=True, padx=5, pady=5)
            self.debug_frame.pack(side="top", fill="both", expand=False)

        # Moderator input frame at the bottom (pack BEFORE chat_box so it gets space)
        self.input_frame = tk.Frame(root, bg="#18181B", height=40)
        self.input_frame.pack(side="bottom", fill="x")
        self.input_frame.pack_propagate(False)

        self.input_entry = tk.Entry(
            self.input_frame, bg="#1F1F23", fg="#EFEFF1",
            font=("Segoe UI", 10), insertbackground="#FFFFFF",
            relief="flat", borderwidth=2, highlightthickness=1,
            highlightbackground="#3A3A3D", highlightcolor="#9147FF"
        )
        self.input_entry.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        self.input_entry.bind("<Return>", self._send_moderator_message)

        self.send_button = tk.Button(
            self.input_frame, text="Send", bg="#9147FF", fg="#FFFFFF",
            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
            activebackground="#772CE8", command=self._send_moderator_message
        )
        self.send_button.pack(side="right", padx=5, pady=5)

        # Chat box and scrollbar (pack AFTER input frame)
        self.chat_box = tk.Text(
            root, wrap="word", state="disabled", bg="#0E0E10", fg="#EDEEEE",
            font=("Segoe UI", 10), insertbackground="#FFFFFF"
        )
        self.scrollbar = ttk.Scrollbar(root, command=self.chat_box.yview)
        self.chat_box.configure(yscrollcommand=self.scrollbar.set)

        self.chat_box.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Threading & state
        self.running = False
        self.msg_queue = queue.Queue()
        self.recent_chat = deque(maxlen=HISTORY_LEN)
        self.last_screenshot = None
        self.last_photo_image = None  # Keep reference to prevent garbage collection

        # Periodic UI updater for queued messages
        self.root.after(100, self._drain_queue)

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _drain_queue(self):
        try:
            while True:
                username, color, text = self.msg_queue.get_nowait()
                self._append_line(username, color, text)
        except queue.Empty:
            pass
        # schedule again
        self.root.after(100, self._drain_queue)

    def _append_line(self, username, color, text):
        self.chat_box.configure(state="normal")
        # Username in color, message in default
        self.chat_box.insert("end", time.strftime("[%H:%M] ") )
        self.chat_box.insert("end", username, (username,))
        self.chat_box.insert("end", f": {text}\n")
        # tag config per user color (created once)
        if not self.chat_box.tag_cget(username, "foreground"):
            self.chat_box.tag_config(username, foreground=color, font=("Segoe UI Semibold", 10))
        # autoscroll
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def _send_moderator_message(self, event=None):
        """Send a moderator message to the chat and add it to context"""
        text = self.input_entry.get().strip()
        if not text:
            return

        # Clear the input field
        self.input_entry.delete(0, tk.END)

        # Display in chat with special MODERATOR styling
        username = "MODERATOR"
        color = "#00FF00"  # Green color for moderator

        # Add to chat display
        self.msg_queue.put((username, color, text))

        # Add to recent chat context with MODERATOR prefix so the LLM sees it clearly
        self.recent_chat.append(f"[MODERATOR]: {text}")

    def start(self):
        if self.running:
            return
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _update_debug_display(self):
        """Update the debug label with the latest screenshot"""
        if DEBUG_SCREENSHOT and self.last_screenshot:
            # Resize to fit the debug area (max 400x300)
            display_img = self.last_screenshot.copy()
            display_img.thumbnail((400, 300), Image.Resampling.LANCZOS)
            self.last_photo_image = ImageTk.PhotoImage(display_img)
            self.debug_label.config(image=self.last_photo_image, text="")

    def _loop(self):
        while self.running:
            try:
                # 1) Capture screen -> base64 + PIL image for debug
                b64, screenshot_img = get_screen_data_url()

                # Store screenshot for debugging
                if DEBUG_SCREENSHOT:
                    self.last_screenshot = screenshot_img
                    # Update debug display (schedule on main thread)
                    self.root.after(0, self._update_debug_display)

                # 2) Call LLM
                line = llm_generate_line(b64, list(self.recent_chat))
                # 3) Immediately discard b64 (and any buffers already freed in helper)
                del b64

                # 4) Compose a Twitchy line (add random username/color)
                username = random.choice(USERNAME_POOL) + str(random.randint(1, 999))
                color = random.choice(USERNAME_COLORS)
                text = sanitize_line(line)

                # Update state & UI (queue to main thread)
                self.recent_chat.append(text)
                self.msg_queue.put((username, color, text))
            except Exception as e:
                # Show one-off error line (doesn't stop the loop)
                self.msg_queue.put(("System", "#FF5555", f"(error: {e})"))
            # 5) Wait until next tick
            time.sleep(INTERVAL_SEC)

    def _on_close(self):
        self.stop()
        # tiny delay to let thread exit cleanly
        self.root.after(150, self.root.destroy)

def sanitize_line(s: str) -> str:
    # Keep it single-line, trim & lightly sanitize just in case
    s = s.replace("\r", " ").replace("\n", " ").strip()
    # Hard cap
    if len(s) > 140:
        s = s[:140] + "…"
    return s

def main():
    root = tk.Tk()
    # Native dark-ish ttk styling
    try:
        from tkinter import ttk
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = TwitchChatUI(root)
    app.start()  # Autostart the chat generation
    root.mainloop()

if __name__ == "__main__":
    main()
