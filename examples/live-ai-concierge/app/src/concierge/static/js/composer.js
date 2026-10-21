/**
 * Live AI Concierge - Composer & Starter Chips
 * Handles text input, send button state, enter-to-send, and starter chip dispatching.
 */

export class Composer {
  constructor({
    inputElement,
    sendButton,
    micButton,
    startersContainer,
    onSendMessage,
    onMicToggle,
  }) {
    this.input = inputElement || document.getElementById("composer-input");
    this.sendBtn = sendButton || document.getElementById("composer-send");
    this.micBtn = micButton || document.getElementById("composer-mic");
    this.starters = startersContainer || document.getElementById("starters");
    this.onSendMessage = onSendMessage || (() => {});
    this.onMicToggle = onMicToggle || (() => {});

    this.init();
  }

  init() {
    if (this.input && this.sendBtn) {
      // Toggle send button based on trimmed input value
      this.input.addEventListener("input", () => {
        this.updateSendButtonState();
      });

      // Enter key submits if non-empty and not composing (IME)
      this.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          this.submit();
        }
      });

      this.sendBtn.addEventListener("click", () => {
        this.submit();
      });
    }

    if (this.micBtn) {
      this.micBtn.addEventListener("click", () => {
        this.onMicToggle();
      });
    }

    if (this.starters) {
      this.starters.addEventListener("click", (e) => {
        const chip = e.target.closest('[data-testid="starter-chip"]');
        if (chip) {
          const prompt = chip.getAttribute("data-prompt");
          if (prompt && prompt.trim()) {
            this.dispatchPrompt(prompt.trim());
          }
        }
      });
    }
  }

  updateSendButtonState() {
    if (this.sendBtn && this.input) {
      const hasContent = this.input.value.trim().length > 0;
      this.sendBtn.disabled = !hasContent;
    }
  }

  submit() {
    if (!this.input) return;
    const text = this.input.value;
    if (!text) return;

    this.input.value = "";
    this.updateSendButtonState();

    this.onSendMessage(text);
  }

  dispatchPrompt(prompt) {
    this.onSendMessage(prompt);
  }

  focus() {
    if (this.input) {
      this.input.focus();
    }
  }
}
