/**
 * AudioPlayer for 24 kHz 16-bit mono PCM playback via Web Audio API.
 * Supports smooth streaming chunk scheduling, Float32 normalization,
 * volume/mute control, and speaking lifecycle hooks.
 */

import { STRINGS } from "./i18n.js";

export class AudioPlayer {
  constructor(options = {}) {
    this.sampleRate = options.sampleRate || 24000;
    this.audioContext = null;
    this.gainNode = null;
    this.isPlaying = false;
    this.nextStartTime = 0;
    this.activeSources = [];
    this.speakingStartCallbacks = [];
    this.speakingEndCallbacks = [];
    this.volume = options.volume !== undefined ? options.volume : 1.0;
    this.muted = !!options.muted;

    this.initAudioContext();
  }

  /**
   * Initializes the Web Audio AudioContext configured for the target sample rate.
   */
  initAudioContext() {
    if (this.audioContext) return;
    if (typeof window === "undefined") return;

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;

    try {
      this.audioContext = new AudioContextClass({ sampleRate: this.sampleRate });
    } catch {
      // Fallback for browsers that don't support custom sample rates in constructor
      this.audioContext = new AudioContextClass();
    }

    if (this.audioContext) {
      this.gainNode = this.audioContext.createGain();
      this.gainNode.connect(this.audioContext.destination);
      this._updateGain();
    }
  }

  /**
   * Resumes AudioContext if suspended due to browser autoplay policies.
   */
  async ensureContext() {
    if (!this.audioContext) {
      this.initAudioContext();
    }
    if (this.audioContext && this.audioContext.state === "suspended") {
      try {
        await this.audioContext.resume();
      } catch {
        // Silently ignore if not allowed yet without user gesture
      }
    }
  }

  /**
   * Converts base64 encoded string to Uint8Array.
   */
  static base64ToUint8Array(base64) {
    const binStr = atob(base64);
    const len = binStr.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binStr.charCodeAt(i);
    }
    return bytes;
  }

  /**
   * Converts 16-bit signed integer linear PCM samples to Float32 [-1.0, 1.0].
   */
  static pcm16ToFloat32(int16Array) {
    const len = int16Array.length;
    const float32 = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const sample = int16Array[i];
      float32[i] = sample < 0 ? sample / 32768.0 : sample / 32767.0;
    }
    return float32;
  }

  /**
   * Converts Float32 [-1.0, 1.0] samples back to 16-bit signed integer linear PCM.
   */
  static float32ToPCM16(float32Array) {
    const len = float32Array.length;
    const int16 = new Int16Array(len);
    for (let i = 0; i < len; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      int16[i] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
    }
    return int16;
  }

  /**
   * Calculates sample count for given milliseconds at specified sample rate.
   */
  static sampleCount(ms, sampleRate = 24000) {
    return Math.floor((sampleRate * ms) / 1000);
  }

  /**
   * Calculates duration in ms from byte length for 16-bit mono PCM.
   */
  static durationMs(byteCount, sampleRate = 24000, bytesPerSample = 2) {
    return (byteCount / (sampleRate * bytesPerSample)) * 1000;
  }

  /**
   * Normalizes incoming PCM data (ArrayBuffer, TypedArray, string, or Blob) to Int16Array.
   */
  _toInt16Array(pcmData) {
    if (!pcmData) return null;

    if (typeof pcmData === "string") {
      const bytes = AudioPlayer.base64ToUint8Array(pcmData);
      return new Int16Array(bytes.buffer, 0, Math.floor(bytes.byteLength / 2));
    }

    if (pcmData instanceof Int16Array) {
      return pcmData;
    }

    if (pcmData instanceof ArrayBuffer) {
      return new Int16Array(pcmData, 0, Math.floor(pcmData.byteLength / 2));
    }

    if (ArrayBuffer.isView(pcmData)) {
      if (pcmData.byteOffset % 2 === 0) {
        return new Int16Array(
          pcmData.buffer,
          pcmData.byteOffset,
          Math.floor(pcmData.byteLength / 2)
        );
      } else {
        const copy = new Uint8Array(pcmData.byteLength);
        copy.set(new Uint8Array(pcmData.buffer, pcmData.byteOffset, pcmData.byteLength));
        return new Int16Array(copy.buffer, 0, Math.floor(copy.byteLength / 2));
      }
    }

    return null;
  }

  /**
   * Converts incoming data to Float32Array.
   */
  _toFloats(pcmData) {
    if (!pcmData) return null;
    if (pcmData instanceof Float32Array) {
      return pcmData;
    }
    const int16 = this._toInt16Array(pcmData);
    if (!int16 || int16.length === 0) return null;
    return AudioPlayer.pcm16ToFloat32(int16);
  }

  /**
   * Schedules an audio buffer chunk for playback.
   */
  _enqueueBuffer(pcmData) {
    const floats = this._toFloats(pcmData);
    if (!floats || floats.length === 0) return;

    if (!this.audioContext) {
      this.initAudioContext();
    }
    if (!this.audioContext) return;

    if (this.audioContext.state === "suspended") {
      this.audioContext.resume().catch(() => {});
    }

    const audioBuffer = this.audioContext.createBuffer(1, floats.length, this.sampleRate);
    audioBuffer.getChannelData(0).set(floats);

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;

    if (this.gainNode) {
      source.connect(this.gainNode);
    } else {
      source.connect(this.audioContext.destination);
    }

    const currentTime = this.audioContext.currentTime;
    const leadTime = 0.02; // 20ms lead time when starting from idle to avoid underruns
    const startTime =
      this.nextStartTime > currentTime ? this.nextStartTime : currentTime + leadTime;

    source.start(startTime);
    this.nextStartTime = startTime + audioBuffer.duration;
    this.activeSources.push(source);

    if (!this.isPlaying) {
      this.isPlaying = true;
      this._triggerSpeakingStart();
    }

    source.onended = () => {
      const idx = this.activeSources.indexOf(source);
      if (idx !== -1) {
        this.activeSources.splice(idx, 1);
      }
      if (this.activeSources.length === 0) {
        this.isPlaying = false;
        this.nextStartTime = 0;
        this._triggerSpeakingEnd();
      }
    };
  }

  /**
   * Plays a streamed 24 kHz 16-bit linear PCM audio buffer chunk.
   * Accepts ArrayBuffer, TypedArray, base64 string, or Blob.
   */
  async playChunk(pcmData) {
    if (!pcmData) return;
    if (typeof Blob !== "undefined" && pcmData instanceof Blob) {
      const arrayBuffer = await pcmData.arrayBuffer();
      this._enqueueBuffer(arrayBuffer);
      return;
    }
    this._enqueueBuffer(pcmData);
  }

  /**
   * Alias for playChunk.
   */
  enqueuePCM(pcmData) {
    return this.playChunk(pcmData);
  }

  /**
   * Immediately stops playback, cancels all scheduled audio nodes,
   * resets the queue, and invokes the speaking end hook.
   */
  stop() {
    for (const source of this.activeSources) {
      try {
        source.onended = null;
        source.stop(0);
      } catch {
        // Ignore if node is already stopped
      }
    }
    this.activeSources = [];
    this.nextStartTime = 0;
    if (this.isPlaying) {
      this.isPlaying = false;
      this._triggerSpeakingEnd();
    }
  }

  /**
   * Registers callback invoked when audio playback begins.
   */
  onSpeakingStart(callback) {
    if (typeof callback === "function") {
      this.speakingStartCallbacks.push(callback);
    }
  }

  /**
   * Registers callback invoked when all scheduled audio playback finishes.
   */
  onSpeakingEnd(callback) {
    if (typeof callback === "function") {
      this.speakingEndCallbacks.push(callback);
    }
  }

  _triggerSpeakingStart() {
    this._updateStatus("speaking");
    for (const cb of this.speakingStartCallbacks) {
      try {
        cb();
      } catch (e) {
        console.error("Error in onSpeakingStart callback:", e);
      }
    }
  }

  _triggerSpeakingEnd() {
    this._updateStatus("ready");
    for (const cb of this.speakingEndCallbacks) {
      try {
        cb();
      } catch (e) {
        console.error("Error in onSpeakingEnd callback:", e);
      }
    }
  }

  /**
   * Updates status element in DOM if present.
   */
  _updateStatus(state) {
    if (typeof document !== "undefined") {
      const statusEl = document.getElementById("status");
      if (statusEl) {
        statusEl.setAttribute("data-state", state);
        const textEl = statusEl.querySelector(".status-text");
        if (textEl) {
          const lang = document.documentElement?.lang === "ja" ? "ja" : "en";
          textEl.textContent =
            STRINGS?.[lang]?.status?.[state] ||
            state.charAt(0).toUpperCase() + state.slice(1);
        }
      }
    }
  }

  /**
   * Sets volume level (0.0 to 1.0 or 0 to 100).
   */
  setVolume(volume) {
    let vol = volume;
    if (vol > 1) {
      vol = vol / 100.0;
    }
    this.volume = Math.max(0, Math.min(1, vol));
    this._updateGain();
  }

  /**
   * Sets mute state.
   */
  setMuted(muted) {
    this.muted = !!muted;
    this._updateGain();
  }

  _updateGain() {
    if (this.gainNode && this.audioContext) {
      const targetGain = this.muted ? 0.0 : this.volume;
      this.gainNode.gain.setValueAtTime(targetGain, this.audioContext.currentTime);
    }
  }
}

export default AudioPlayer;
