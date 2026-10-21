/**
 * Live AI Concierge - Feed and Card Management
 * Handles message bubbles, transcript streaming accumulation, tool progress, and responsive card placement.
 */

import { STRINGS } from "./i18n.js";
import { renderWeatherCard } from "./cards/weather.js";

function escapeHtml(str) {
  if (typeof str !== "string") return String(str ?? "");
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export class FeedManager {
  constructor({ feedElement, cardsElement, language = "en" }) {
    this.feedEl = feedElement || document.getElementById("feed");
    this.cardsEl = cardsElement || document.getElementById("cards-pane");
    this.language = language;
    this.currentAssistantBubble = null;
    this.currentTurnCard = null;
    this.progressEl = null;
  }

  setLanguage(lang) {
    this.language = lang;
  }

  isNarrowViewport() {
    return window.innerWidth < 720;
  }

  scrollToBottom() {
    if (this.feedEl) {
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
    }
  }

  addUserMessage(text) {
    this.removeProgress();
    this.currentAssistantBubble = null;
    this.currentTurnCard = null;

    const bubble = document.createElement("div");
    bubble.className = "msg msg-user";
    bubble.setAttribute("data-testid", "msg-user");
    bubble.innerHTML = text;

    this.feedEl.appendChild(bubble);
    this.scrollToBottom();
    return bubble;
  }

  appendTranscript(text) {
    if (!this.currentAssistantBubble) {
      const bubble = document.createElement("div");
      bubble.className = "msg msg-assistant";
      bubble.setAttribute("data-testid", "msg-assistant");

      if (this.currentTurnCard && this.currentTurnCard.parentNode === this.feedEl) {
        this.feedEl.insertBefore(bubble, this.currentTurnCard);
      } else if (this.progressEl && this.progressEl.parentNode === this.feedEl) {
        this.feedEl.insertBefore(bubble, this.progressEl);
      } else {
        this.feedEl.appendChild(bubble);
      }
      this.currentAssistantBubble = bubble;
    }

    this.currentAssistantBubble.textContent += text;
    this.scrollToBottom();
  }

  showToolProgress(toolName, args = {}) {
    const lang = args?.lang || this.language;
    const isJa = typeof lang === "string" && lang.startsWith("ja");
    const langStrings = isJa ? STRINGS.ja : (STRINGS[this.language] || STRINGS.en);
    const progressMap = langStrings.progress || {};
    const text = progressMap[toolName] || `Checking ${toolName}…`;

    if (this.progressEl && this.progressEl.parentNode === this.feedEl) {
      const textEl = this.progressEl.querySelector(".progress-text");
      if (textEl) {
        textEl.textContent = text;
      } else {
        this.progressEl.textContent = text;
      }
    } else {
      const el = document.createElement("div");
      el.className = "progress";
      el.setAttribute("data-testid", "progress");

      const spinner = document.createElement("span");
      spinner.className = "progress-spinner";
      el.appendChild(spinner);

      const textNode = document.createElement("span");
      textNode.className = "progress-text";
      textNode.textContent = text;
      el.appendChild(textNode);

      this.progressEl = el;
      this.feedEl.appendChild(el);
    }

    this.scrollToBottom();
  }

  removeProgress() {
    if (this.progressEl) {
      if (this.progressEl.parentNode) {
        this.progressEl.parentNode.removeChild(this.progressEl);
      }
      this.progressEl = null;
    }
  }

  createCardElement(toolName, result) {
    const card = document.createElement("div");
    card.className = `card card-${toolName}`;
    card.setAttribute("data-testid", "card");
    card.setAttribute("data-card-type", toolName);

    if (toolName === "weather" && typeof renderWeatherCard === "function") {
      const custom = renderWeatherCard(result, card);
      if (custom instanceof HTMLElement) {
        if (!custom.getAttribute("data-testid")) {
          custom.setAttribute("data-testid", "card");
        }
        if (!custom.getAttribute("data-card-type")) {
          custom.setAttribute("data-card-type", toolName);
        }
        return custom;
      }
    }

    if (!card.children.length && !card.textContent.trim()) {
      const title = result?.location || result?.query || toolName;
      card.innerHTML = `
        <div class="card-header">
          <span class="card-title">${escapeHtml(title)}</span>
        </div>
        <div class="card-body">
          <pre>${escapeHtml(JSON.stringify(result, null, 2))}</pre>
        </div>
      `;
    }

    return card;
  }

  showToolResult(toolName, result) {
    this.removeProgress();

    const card = this.createCardElement(toolName, result);

    if (this.isNarrowViewport()) {
      this.currentTurnCard = card;
      // Inline placement in feed immediately following the assistant bubble
      if (this.currentAssistantBubble && this.currentAssistantBubble.parentNode === this.feedEl) {
        const next = this.currentAssistantBubble.nextSibling;
        if (next) {
          this.feedEl.insertBefore(card, next);
        } else {
          this.feedEl.appendChild(card);
        }
      } else {
        this.feedEl.appendChild(card);
      }
      this.scrollToBottom();
    } else {
      // Desktop: place in right pane
      if (this.cardsEl) {
        const starters = this.cardsEl.querySelector("#starters");
        if (starters) {
          starters.style.display = "none";
        }
        this.cardsEl.appendChild(card);
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  }

  completeTurn() {
    this.removeProgress();
    this.currentAssistantBubble = null;
    this.currentTurnCard = null;
  }

  clear() {
    this.removeProgress();
    this.currentAssistantBubble = null;
    this.currentTurnCard = null;
    if (this.feedEl) {
      this.feedEl.innerHTML = "";
    }
    if (this.cardsEl) {
      const cards = this.cardsEl.querySelectorAll('[data-testid="card"]');
      cards.forEach((c) => c.remove());
      const starters = this.cardsEl.querySelector("#starters");
      if (starters) {
        starters.style.display = "";
      }
    }
  }
}
