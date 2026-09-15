import { Injectable, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import type { DurableNotification, NotificationListResponse } from '../models/types';

export type NotificationPermissionState = 'default' | 'granted' | 'denied' | 'unsupported';

const PREF_NATIVE_NOTIFICATIONS = 'job_applier_native_notifications_enabled';
const PREF_SOUND_ENABLED = 'job_applier_notification_sound_enabled';
const STORAGE_LAST_NOTIFIED_TAG = 'job_applier_last_notified_tag';
const STORAGE_LAST_SEEN_EVENT_ID = 'job_applier_last_seen_event_id';

@Injectable({
  providedIn: 'root',
})
export class NotificationService {
  private api = inject(ApiService);

  // Notifications State Signals
  notifications = signal<DurableNotification[]>([]);
  unreadCount = signal<number>(0);
  latestNotificationId = signal<number>(0);
  isPaused = signal<boolean>(false);
  isStopped = signal<boolean>(false);
  activeJob = signal<Record<string, unknown> | null>(null);
  sessionExpired = signal<boolean>(false);

  // Opt-in & Permission Signals (Per-Browser Preferences)
  permissionState = signal<NotificationPermissionState>('default');
  nativeEnabled = signal<boolean>(false);
  soundEnabled = signal<boolean>(false); // Default OFF

  // Event stream tracking
  private eventSource: EventSource | null = null;
  private lastSeenEventId = 0;
  private broadcastChannel: BroadcastChannel | null = null;
  private audioCtx: AudioContext | null = null;

  constructor() {
    this.initEnvironmentAndPrefs();
    this.initBroadcastChannel();
  }

  private initEnvironmentAndPrefs(): void {
    if (typeof window === 'undefined') return;

    // Detect browser Notification support
    if ('Notification' in window) {
      this.permissionState.set(Notification.permission as NotificationPermissionState);
    } else {
      this.permissionState.set('unsupported');
    }

    // Load saved preferences
    try {
      const savedNative = localStorage.getItem(PREF_NATIVE_NOTIFICATIONS) === 'true';
      const savedSound = localStorage.getItem(PREF_SOUND_ENABLED) === 'true';
      const savedLastEvent = localStorage.getItem(STORAGE_LAST_SEEN_EVENT_ID);
      if (savedLastEvent && !isNaN(Number(savedLastEvent))) {
        this.lastSeenEventId = Number(savedLastEvent);
      }

      this.nativeEnabled.set(
        savedNative && 'Notification' in window && Notification.permission === 'granted',
      );
      this.soundEnabled.set(savedSound); // Default false if not set
    } catch {
      // Ignore localStorage access restrictions
    }
  }

  private initBroadcastChannel(): void {
    if (typeof window !== 'undefined' && 'BroadcastChannel' in window) {
      try {
        this.broadcastChannel = new BroadcastChannel('job_applier_notifications');
        this.broadcastChannel.onmessage = (event) => {
          if (event.data?.type === 'ack') {
            this.handleRemoteAck(event.data.id);
          } else if (event.data?.type === 'ack_all') {
            this.handleRemoteAckAll();
          } else if (event.data?.type === 'dismiss') {
            this.handleRemoteDismiss(event.data.id);
          } else if (event.data?.type === 'clear_all') {
            this.handleRemoteClearAll();
          }
        };
      } catch {
        this.broadcastChannel = null;
      }
    }
  }

  /**
   * Checks if current runtime is running over secure HTTPS / localhost context.
   */
  isSecureOrigin(): boolean {
    if (typeof window === 'undefined') return false;
    return (
      window.isSecureContext ||
      window.location.protocol === 'https:' ||
      window.location.hostname === 'localhost' ||
      window.location.hostname === '127.0.0.1'
    );
  }

  /**
   * Explicit user opt-in request for the Web Notifications API.
   * MUST only be called from an explicit user click handler.
   */
  async requestNotificationPermission(): Promise<boolean> {
    if (typeof window === 'undefined' || !('Notification' in window)) {
      this.permissionState.set('unsupported');
      return false;
    }

    if (!this.isSecureOrigin()) {
      return false;
    }

    try {
      const perm = await Notification.requestPermission();
      this.permissionState.set(perm as NotificationPermissionState);
      if (perm === 'granted') {
        this.nativeEnabled.set(true);
        localStorage.setItem(PREF_NATIVE_NOTIFICATIONS, 'true');
        return true;
      } else {
        this.nativeEnabled.set(false);
        localStorage.setItem(PREF_NATIVE_NOTIFICATIONS, 'false');
        return false;
      }
    } catch {
      this.permissionState.set('denied');
      return false;
    }
  }

  /**
   * Toggles native browser notifications setting.
   */
  setNativeNotificationsEnabled(enabled: boolean): void {
    if (enabled && this.permissionState() !== 'granted') {
      this.requestNotificationPermission();
      return;
    }
    this.nativeEnabled.set(enabled);
    try {
      localStorage.setItem(PREF_NATIVE_NOTIFICATIONS, enabled ? 'true' : 'false');
    } catch {}
  }

  /**
   * Toggles sound alerts. OFF by default. Unlocks AudioContext on user action.
   */
  setSoundEnabled(enabled: boolean): void {
    this.soundEnabled.set(enabled);
    try {
      localStorage.setItem(PREF_SOUND_ENABLED, enabled ? 'true' : 'false');
    } catch {}

    if (enabled) {
      this.ensureAudioContext();
      this.playNotificationSound();
    }
  }

  private ensureAudioContext(): void {
    if (typeof window === 'undefined') return;
    try {
      const AudioCtxClass =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (AudioCtxClass && !this.audioCtx) {
        this.audioCtx = new AudioCtxClass();
      }
      if (this.audioCtx && this.audioCtx.state === 'suspended') {
        this.audioCtx.resume();
      }
    } catch {
      this.audioCtx = null;
    }
  }

  /**
   * Plays a synthesized gentle double-chime beep using Web Audio API.
   * Safe and self-contained; requires zero external sound asset downloads.
   */
  playNotificationSound(): void {
    if (!this.soundEnabled()) return;
    this.ensureAudioContext();
    if (!this.audioCtx) return;

    try {
      const ctx = this.audioCtx;
      const now = ctx.currentTime;

      // Note 1: D5 (587.33 Hz)
      const osc1 = ctx.createOscillator();
      const gain1 = ctx.createGain();
      osc1.type = 'sine';
      osc1.frequency.setValueAtTime(587.33, now);
      gain1.gain.setValueAtTime(0.08, now);
      gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.15);
      osc1.connect(gain1);
      gain1.connect(ctx.destination);
      osc1.start(now);
      osc1.stop(now + 0.16);

      // Note 2: A5 (880 Hz)
      const osc2 = ctx.createOscillator();
      const gain2 = ctx.createGain();
      osc2.type = 'sine';
      osc2.frequency.setValueAtTime(880.0, now + 0.12);
      gain2.gain.setValueAtTime(0.08, now + 0.12);
      gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.32);
      osc2.connect(gain2);
      gain2.connect(ctx.destination);
      osc2.start(now + 0.12);
      osc2.stop(now + 0.33);
    } catch {
      // Audio playback failed or blocked by autoplay policy
    }
  }

  /**
   * Connects to the authenticated Server-Sent Events stream (/api/automation/events).
   * Reconciles durable alerts, pause banners, and unread badges in real-time.
   */
  connectEventStream(
    onToastMessage?: (msg: string, type: 'info' | 'warning' | 'error' | 'success') => void,
  ): void {
    if (typeof window === 'undefined' || this.eventSource) return;

    let sseUrl = this.api.resolveUrl('/api/automation/events');
    if (this.lastSeenEventId > 0) {
      sseUrl += `?after=${this.lastSeenEventId}`;
    }

    const es = new EventSource(sseUrl, { withCredentials: true });
    this.eventSource = es;

    es.addEventListener('snapshot', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        if (typeof data.latest_event_id === 'number') {
          this.updateLastSeenEventId(data.latest_event_id);
        }
        if (typeof data.unread_notifications === 'number') {
          this.unreadCount.set(data.unread_notifications);
        }
        // Load fresh notification list
        this.fetchNotifications();
      } catch {}
    });

    es.addEventListener('notification', (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data);
        if (typeof ev.id === 'number') {
          this.updateLastSeenEventId(ev.id);
        }

        const details = ev.details || {};
        const notif: DurableNotification = {
          id: details.id || ev.id,
          notification_id: details.notification_id || `notif_${ev.id}`,
          job_id: ev.job_id,
          app_id: ev.app_id,
          event_id: ev.id,
          category: details.category || 'general',
          severity: details.severity || 'warning',
          title: details.title || ev.step || 'Action Required',
          message: ev.message || 'Job automation paused.',
          acknowledged: false,
          created_at: ev.created_at || new Date().toISOString(),
        };

        this.handleIncomingNotification(notif, onToastMessage);
      } catch {}
    });

    es.addEventListener('notification_ack', (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data);
        const details = ev.details || {};
        const nid = details.notification_id || details.id;
        if (nid) {
          this.handleRemoteAck(nid);
        }
      } catch {}
    });

    es.addEventListener('notification_ack_all', () => {
      this.handleRemoteAckAll();
    });

    es.addEventListener('notification_clear_all', () => {
      this.handleRemoteClearAll();
    });

    es.addEventListener('expired', () => {
      this.sessionExpired.set(true);
      if (onToastMessage) {
        onToastMessage('Session expired. Reauthentication required.', 'warning');
      }
      this.disconnectEventStream();
    });

    es.onerror = () => {
      // EventSource automatically retries with Last-Event-ID
    };
  }

  disconnectEventStream(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  private updateLastSeenEventId(id: number): void {
    if (id > this.lastSeenEventId) {
      this.lastSeenEventId = id;
      try {
        localStorage.setItem(STORAGE_LAST_SEEN_EVENT_ID, id.toString());
      } catch {}
    }
  }

  /**
   * Loads notifications list from server.
   */
  fetchNotifications(limit = 50): void {
    this.api.getNotifications(undefined, false, limit).subscribe({
      next: (res: NotificationListResponse) => {
        this.notifications.set(res.notifications || []);
        this.unreadCount.set(res.unread_count || 0);
        this.latestNotificationId.set(res.latest_id || 0);
      },
      error: () => {},
    });
  }

  /**
   * Handles an incoming notification event.
   * Enforces focus-aware native suppression, generic private preview,
   * cross-tab Web Locks deduplication, and sound opt-in.
   */
  private handleIncomingNotification(
    notif: DurableNotification,
    onToastMessage?: (msg: string, type: 'info' | 'warning' | 'error' | 'success') => void,
  ): void {
    // 1. Update in-memory state
    this.notifications.update((list) => {
      const exists = list.some((n) => n.notification_id === notif.notification_id);
      if (exists) return list;
      return [notif, ...list];
    });
    this.unreadCount.update((c) => c + 1);

    // 2. In-App Toast Alert (Always shown inside app shell)
    if (onToastMessage) {
      const toastType =
        notif.severity === 'critical' || notif.severity === 'error'
          ? 'error'
          : notif.severity === 'warning'
            ? 'warning'
            : 'info';
      onToastMessage(`${notif.title}: ${notif.message}`, toastType);
    }

    // 3. Focus-Aware Native Suppression:
    // If the dashboard tab is visible and focused, suppress native notification!
    const isTabFocused =
      typeof document !== 'undefined' &&
      document.visibilityState === 'visible' &&
      document.hasFocus();

    if (isTabFocused) {
      // Suppress native OS alert when actively looking at the dashboard
      return;
    }

    // 4. Check user opt-in and permission
    if (!this.nativeEnabled() || this.permissionState() !== 'granted') {
      return;
    }

    // 5. Cross-Tab Deduplication with Web Locks or storage fallback
    const dedupTag = `job_applier_${notif.notification_id || notif.id}`;
    this.dispatchDeduplicatedAlert(notif, dedupTag);
  }

  /**
   * Dispatches native OS notification and sound via Web Locks API (or BroadcastChannel fallback).
   * Only the elected notifier tab triggers the alert.
   */
  private dispatchDeduplicatedAlert(notif: DurableNotification, tag: string): void {
    if (typeof window === 'undefined') return;

    const spawnAlert = () => {
      this.playNotificationSound();

      const isActionRequired =
        notif.severity === 'critical' || notif.severity === 'error' || notif.severity === 'warning';
      const categoryLabel = notif.category
        ? notif.category.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
        : isActionRequired
          ? 'Action Required'
          : 'Notification';
      const safeTitle = `JobApplier: ${categoryLabel}`;
      const safeBody = isActionRequired
        ? 'Action required in the dashboard. Click to open.'
        : 'A new notification is available. Click to open dashboard.';

      try {
        const nativeNotif = new Notification(safeTitle, {
          body: safeBody,
          tag,
          icon: '/favicon.ico',
        });

        // Safe Canonical Click Navigation: Focuses existing tab, does NOT submit or acknowledge!
        nativeNotif.onclick = () => {
          window.focus();
          nativeNotif.close();
        };
      } catch {
        // Notification constructor error or restricted context
      }
    };

    const isAlreadyDispatched = (): boolean => {
      try {
        const lastDispatched = localStorage.getItem(STORAGE_LAST_NOTIFIED_TAG);
        if (lastDispatched) {
          const [prevTag, prevTime] = lastDispatched.split('|');
          if (prevTag === tag && Date.now() - Number(prevTime) < 5000) {
            return true;
          }
        }
      } catch {
        // Storage access error
      }
      return false;
    };

    const recordDispatched = (): void => {
      try {
        localStorage.setItem(STORAGE_LAST_NOTIFIED_TAG, `${tag}|${Date.now()}`);
      } catch {
        // Storage access error
      }
    };

    if (isAlreadyDispatched()) {
      return;
    }

    // Use Web Locks API if available
    if ('locks' in navigator && typeof navigator.locks?.request === 'function') {
      navigator.locks.request('job_applier_notifier_lock', { ifAvailable: true }, async (lock) => {
        if (!lock) {
          return;
        }
        if (isAlreadyDispatched()) {
          return;
        }
        recordDispatched();
        spawnAlert();
        await new Promise((r) => setTimeout(r, 400));
      });
    } else {
      if (isAlreadyDispatched()) {
        return;
      }
      recordDispatched();
      spawnAlert();
    }
  }

  /**
   * Acknowledges a notification idempotently.
   * Distinct from job resolution/resumption: does not resume automation.
   */
  ackNotification(id: number | string): void {
    this.api.ackNotification(id).subscribe({
      next: () => {
        this.handleRemoteAck(id);
        if (this.broadcastChannel) {
          this.broadcastChannel.postMessage({ type: 'ack', id });
        }
      },
      error: () => {},
    });
  }

  /**
   * Acknowledges all unacknowledged notifications.
   */
  ackAll(): void {
    this.api.ackAllNotifications().subscribe({
      next: () => {
        this.handleRemoteAckAll();
        if (this.broadcastChannel) {
          this.broadcastChannel.postMessage({ type: 'ack_all' });
        }
      },
      error: () => {},
    });
  }

  private handleRemoteAck(id: number | string): void {
    const idStr = id.toString();
    this.notifications.update((list) =>
      list.map((n) => {
        if (n.id.toString() === idStr || n.notification_id === idStr) {
          return { ...n, acknowledged: true, acknowledged_at: new Date().toISOString() };
        }
        return n;
      }),
    );
    this.unreadCount.update((c) => Math.max(0, c - 1));
  }

  private handleRemoteAckAll(): void {
    this.notifications.update((list) =>
      list.map((n) => ({
        ...n,
        acknowledged: true,
        acknowledged_at: new Date().toISOString(),
      })),
    );
    this.unreadCount.set(0);
  }

  /**
   * Dismisses and deletes a single notification.
   */
  dismissNotification(id: number | string): void {
    this.handleRemoteDismiss(id);
    if (this.broadcastChannel) {
      this.broadcastChannel.postMessage({ type: 'dismiss', id });
    }
    this.api.deleteNotification(id).subscribe({
      next: () => {},
      error: () => {
        this.fetchNotifications();
      },
    });
  }

  /**
   * Clears all notifications.
   */
  clearAll(): void {
    this.handleRemoteClearAll();
    if (this.broadcastChannel) {
      this.broadcastChannel.postMessage({ type: 'clear_all' });
    }
    this.api.clearAllNotifications().subscribe({
      next: () => {},
      error: () => {
        this.fetchNotifications();
      },
    });
  }

  handleRemoteDismiss(id: number | string): void {
    const idStr = id.toString();
    const target = this.notifications().find(
      (n) => n.id.toString() === idStr || n.notification_id === idStr,
    );
    this.notifications.update((list) =>
      list.filter((n) => n.id.toString() !== idStr && n.notification_id !== idStr),
    );
    if (target && !target.acknowledged) {
      this.unreadCount.update((c) => Math.max(0, c - 1));
    }
  }

  handleRemoteClearAll(): void {
    this.notifications.set([]);
    this.unreadCount.set(0);
  }
}
