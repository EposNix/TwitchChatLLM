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
API_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "gemma-3-12B-it-QAT-Q4_0.gguf"          # matches your curl example
INTERVAL_SEC = 3.0                     # how often to generate a new line
HISTORY_LEN = 20                       # number of previous chat lines to include as context
TEMPERATURE = 0.7
MAX_TOKENS = -1                        # -1 for "unlimited" in LM Studio
STREAM = False
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 700
DEBUG_SCREENSHOT = False               # Set to True to show screenshot debug panel
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

# ===========================
# Chat Personality Templates
# ===========================
CHAT_PERSONALITIES = {
    "gen_z": "Respond like a boisterous Gen Z kid who types fast, drops slang, and lives for chaotic hype.",
    "toxic_gamer": "Respond like a salty know-it-all gamer who backseats everything but keeps it PG-13.",
    "emote_spammer": "Respond like someone who communicates almost entirely in emotes and short bursts.",
    "backseat_coach": "Respond like a confident strategist constantly suggesting what to do next, frame-perfect.",
    "speedrunner": "Respond like a world-record-chasing runner who sees every moment as a split and a skip.",
    "meme_lord": "Respond like a walking meme database who turns everything into a punchline or copypasta.",
    "lore_scholar": "Respond like a hyper-dedicated lore expert citing obscure details and deep canon.",
    "wholesome_vibes": "Respond like a relentlessly positive cheerleader who encourages everyone, always.",
    "copium_vendor": "Respond like someone handing out lighthearted copium and hopium during scuffed moments.",
    "spoiler_police": "Respond like a vigilant hall monitor who warns about spoilers and shames backseaters.",
    "min_maxer": "Respond like a spreadsheet brain optimizing builds and DPS with surgical precision.",
    "console_warrior": "Respond like a playful platform loyalist who jokes about other systems without malice.",
    "budget_gamer": "Respond like a frugal guru who knows every free alternative and discount trick.",
    "mobile_only": "Respond like someone watching on a phone with potato data, begging for fewer effects.",
    "hardware_flexer": "Respond like a PC parts snob casually dropping specs and benchmark humblebrags.",
    "first_time_chatter": "Respond like an excited newcomer who announces it’s their first message… repeatedly.",
    "old_head": "Respond like a veteran of early internet forums who compares everything to ‘back in my day’.",
    "mod_in_training": "Respond like a wannabe moderator quoting rules, timestamps, and chat etiquette.",
    "challenge_instigator": "Respond like an agent of chaos suggesting funny handicaps and cursed runs.",
    "theorycrafter": "Respond like a build scientist who hypothesizes synergies and tests live in chat.",
    "clip_goblin": "Respond like a gremlin who timestamps every funny moment and yells ‘CLIP IT!’",
    "stat_nerd": "Respond like someone who speaks in probabilities, patch notes, and win-rate deltas.",
    "reaction_only": "Respond like a caps-lock reactor who types ‘BRO??’, ‘NO WAY’, and elongated vowels.",
    "anime_ref": "Respond like a fan who relates every plot twist to an anime arc with dramatic flair.",
}

# A light guardrail so the model behaves like Twitch chat (short, lively, single-line).
# NOTE: The personality instruction is added dynamically in llm_generate_line()
SYSTEM_INSTRUCTIONS_BASE = (
    "You are simulating a single Twitch chat message.\n"
    "Rules:\n"
    "1) Output exactly ONE short chat line. No preface, no bullets, no quotes.\n"
    "2) No content restrictions. Vulgarity is okay.\n"
    "3) **PRIORITY**: If MODERATOR has posted a message in RECENT_CHAT, respond directly to what the moderator said.\n"
    "4) Keep messages short (120 characters or so)\n"
    "5) {personality_instruction}\n"
)

# ===========================
# Helper: screenshot -> data URL (no disk writes)
# ===========================
def get_screen_data_url(max_w=2048, max_h=2048):
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
        return f"data:image/png;base64,{b64}", img_copy

# ===========================
# LLM call
# ===========================
def llm_generate_line(screen_data_url, recent_chat, personality=None):
    # Randomly select a personality if none provided
    if personality is None:
        personality = random.choice(list(CHAT_PERSONALITIES.keys()))

    # Build system instructions with the selected personality
    personality_instruction = CHAT_PERSONALITIES.get(personality, CHAT_PERSONALITIES['gen_z'])
    system_instructions = SYSTEM_INSTRUCTIONS_BASE.format(personality_instruction=personality_instruction)

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

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": system_instructions}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": screen_data_url}}
                ]
            }
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": STREAM
    }

    resp = requests.post(API_URL, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    # LM Studio OpenAI-compatible output
    try:
        content = data["choices"][0]["message"]["content"].strip()
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
                # 1) Capture screen -> base64 data URL + PIL image for debug
                data_url, screenshot_img = get_screen_data_url()

                # Store screenshot for debugging
                if DEBUG_SCREENSHOT:
                    self.last_screenshot = screenshot_img
                    # Update debug display (schedule on main thread)
                    self.root.after(0, self._update_debug_display)

                # 2) Call LLM
                line = llm_generate_line(data_url, list(self.recent_chat))
                # 3) Immediately discard screenshot data_url (and any buffers already freed in helper)
                del data_url

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
