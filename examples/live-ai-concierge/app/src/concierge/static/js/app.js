/**
 * Live AI Concierge - Application Controller
 * Bootstraps WebSocket connection, live clock, status pill, feed, and composer.
 */

import { STRINGS } from "./i18n.js";
import { FeedManager } from "./feed.js";
import { Composer } from "./composer.js";
import { AudioPlayer } from "./audio.js";

export function formatClock(date = new Date()) {
  const weekday = date.toLocaleDateString("en-US", { weekday: "short" });
  const month = date.toLocaleDateString("en-US", { month: "short" });
  const day = date.getDate();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${weekday}, ${month} ${day} ${hours}:${minutes}`;
}

export class StatusManager {
  constructor({ statusElement, language = "en" }) {
    this.statusEl = statusElement || document.getElementById("status");
    this.language = language;
    this.state = "connecting";
  }

  setLanguage(lang) {
    this.language = lang;
    this.setState(this.state);
  }

  setState(state) {
    this.state = state;
    if (this.statusEl) {
      this.statusEl.setAttribute("data-state", state);
      const textEl = this.statusEl.querySelector(".status-text");
      if (textEl) {
        const langDict = STRINGS[this.language] || STRINGS.en;
        const localized = langDict.status?.[state] || state;
        textEl.textContent = localized;
      }
    }
  }

  getState() {
    return this.state;
  }
}

export class App {
  constructor() {
    this.language = "en";
    this.clockEl = document.getElementById("clock");
    this.statusManager = new StatusManager({ language: this.language });
    this.feedManager = new FeedManager({ language: this.language });
    this.audioPlayer = new AudioPlayer();
    this.ws = null;
    this.reconnectAttempts = 0;
    this.reconnectTimer = null;

    this.composer = new Composer({
      onSendMessage: (text) => this.sendMessage(text),
      onMicToggle: () => this.toggleMicrophone(),
    });

    this.initClock();
    this.connectWebSocket();
  }

  initClock() {
    const update = () => {
      if (this.clockEl) {
        this.clockEl.textContent = formatClock();
      }
    };
    update();
    setTimeout(update, 60000);
  }

  connectWebSocket() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${location.host}/ws`;

    this.statusManager.setState("connecting");

    try {
      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = "arraybuffer";
    } catch (e) {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
    };

    this.ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const msg = JSON.parse(event.data);
          this.handleServerMessage(msg);
        } catch (e) {
          console.error("Failed to parse WebSocket JSON message:", e);
        }
      } else if (event.data instanceof ArrayBuffer) {
        if (this.audioPlayer && typeof this.audioPlayer.playChunk === "function") {
          this.audioPlayer.playChunk(event.data);
        }
      }
    };

    this.ws.onclose = () => {
      this.statusManager.setState("error");
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      this.statusManager.setState("error");
    };
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    if (this.reconnectAttempts >= 3) return;
    const delay = Math.min(1000 * Math.pow(1.5, this.reconnectAttempts), 10000);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connectWebSocket();
    }, delay);
  }

  handleServerMessage(msg) {
    switch (msg.type) {
      case "status":
        this.statusManager.setState(msg.state);
        break;
      case "transcript":
        this.feedManager.appendTranscript(msg.text);
        break;
      case "tool_start":
        this.feedManager.showToolProgress(msg.name, msg.args);
        break;
      case "tool_result":
        this.feedManager.showToolResult(msg.name, msg.result);
        break;
      case "audio":
        // Server handles status transitions for audio or audio player handles playback
        break;
      case "turn_complete":
        this.feedManager.completeTurn();
        break;
      default:
        break;
    }
  }

  sendMessage(text) {
    this.feedManager.addUserMessage(text);
    this.statusManager.setState("thinking");

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "text", text }));
    } else {
      console.warn("WebSocket not open. ReadyState:", this.ws ? this.ws.readyState : "null");
    }
  }

  toggleMicrophone() {
    const currentState = this.statusManager.getState();
    if (currentState === "listening") {
      this.statusManager.setState("ready");
    } else {
      this.statusManager.setState("listening");
    }
  }
}

// Bootstrap on DOM ready
document.addEventListener("DOMContentLoaded", () => {
  window.app = new App();
});
